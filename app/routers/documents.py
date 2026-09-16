"""Endpoints que consume el portal (Bandeja SII).

Los nombres y la forma de estas rutas siguen el flujo ya definido en el prototipo:
listar documentos con filtros, sincronizar bajo demanda, y enviar (individual o en
lote, seleccionado por el usuario — el envío nunca es automático, ver requisitos).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.finnegans.client import FinnegansClient, FinnegansConfigError
from app.models import (
    Documento,
    Empresa,
    EmpresaOut,
    EmpresaUpdate,
    DocumentoOut,
    EnviarResult,
    EstadoDocumento,
    SyncResult,
)
from app.sii.client import (
    SIIAuthenticationError,
    SIIBloqueadoError,
    SIIClient,
    SIIPortalFEError,
    SIIRCVError,
)
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["documentos"])


@router.get("/documents", response_model=list[DocumentoOut])
def listar_documentos(
    empresa: str | None = None,
    estado: EstadoDocumento | None = None,
    tipo: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Documento)
    if empresa:
        stmt = stmt.where(Documento.empresa_rut == empresa)
    if estado:
        stmt = stmt.where(Documento.estado == estado)
    if tipo:
        stmt = stmt.where(Documento.tipo == tipo)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Documento.proveedor_nombre.ilike(like))
            | (Documento.proveedor_rut.ilike(like))
        )
    return db.execute(stmt.order_by(Documento.fecha.desc())).scalars().all()


@router.get("/empresas", response_model=list[EmpresaOut])
def listar_empresas(db: Session = Depends(get_db)):
    """Empresas representadas, con cuántos documentos tiene cada una en la bandeja.

    Se ordenan poniendo primero las que ya tienen nombre, para que las empresas con las
    que se trabaja de verdad queden arriba y no perdidas entre 55 RUT sueltos.
    """
    conteos = dict(
        db.execute(
            select(Documento.empresa_rut, func.count()).group_by(Documento.empresa_rut)
        ).all()
    )
    pendientes = dict(
        db.execute(
            select(Documento.empresa_rut, func.count())
            .where(Documento.estado == EstadoDocumento.PENDIENTE)
            .group_by(Documento.empresa_rut)
        ).all()
    )
    salida = [
        EmpresaOut(
            rut=e.rut,
            nombre=e.nombre,
            nombre_mostrado=e.nombre_mostrado,
            autorizada=e.autorizada,
            ultima_sincronizacion=e.ultima_sincronizacion,
            documentos=conteos.get(e.rut, 0),
            pendientes=pendientes.get(e.rut, 0),
        )
        for e in db.execute(select(Empresa)).scalars().all()
    ]
    salida.sort(key=lambda e: (e.nombre is None, (e.nombre or e.rut).lower()))
    return salida


@router.patch("/empresas/{rut}", response_model=EmpresaOut)
def nombrar_empresa(rut: str, datos: EmpresaUpdate, db: Session = Depends(get_db)):
    """Pone el nombre con el que el usuario conoce a la empresa.

    Hace falta porque el SII lista las empresas representadas solo por RUT: su campo de
    razón social viene vacío para todas. Sin esto, elegir entre 55 RUT es impracticable.
    """
    empresa = db.get(Empresa, rut)
    if not empresa:
        raise HTTPException(status_code=404, detail=f"No hay una empresa con RUT {rut}.")
    empresa.nombre = datos.nombre.strip() or None
    db.commit()
    # Se devuelve la fila tal como la arma el listado, para no tener dos formas de
    # construir un EmpresaOut que puedan divergir (ya pasó: faltaba `pendientes`).
    return next(e for e in listar_empresas(db) if e.rut == rut)


@router.post("/empresas/refrescar", response_model=list[EmpresaOut])
def refrescar_empresas(db: Session = Depends(get_db)):
    """Trae del SII la lista de empresas que este certificado puede consultar.

    Las que el SII ya no lista se marcan `autorizada=False` en vez de borrarse, para no
    perder los documentos que se les sincronizaron antes. Los nombres puestos a mano se
    conservan siempre.
    """
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    client = SIIClient(rut, cert_path, password)
    try:
        session = client.abrir_sesion(headless=True)
        try:
            ruts = client.get_empresas(session=session)
            nombradas = client.get_empresas_con_nombre(session=session)
        finally:
            session[1].close()
            session[2].close()
            session[3].stop()
    except SIIBloqueadoError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SIIAuthenticationError as exc:
        raise HTTPException(
            status_code=502, detail=f"No se pudo iniciar sesión en el SII: {exc}"
        ) from exc
    finally:
        client.close()

    conocidas = {e.rut: e for e in db.execute(select(Empresa)).scalars().all()}
    for r in ruts:
        if r not in conocidas:
            nueva = Empresa(rut=r, autorizada=True)
            db.add(nueva)
            conocidas[r] = nueva
    for r, empresa in conocidas.items():
        empresa.autorizada = r in ruts

    # El RCV no da razones sociales, pero el Portal de Facturación Electrónica sí.
    # Es una lista más corta (solo las empresas registradas en ese portal), así que
    # completa los nombres que puede y el resto queda para ponerlos a mano.
    for e in nombradas:
        empresa = conocidas.get(e["rut"])
        if empresa is None:
            empresa = Empresa(rut=e["rut"], autorizada=True)
            db.add(empresa)
            conocidas[e["rut"]] = empresa
        if e["nombre"] and not empresa.nombre:
            empresa.nombre = e["nombre"]
    db.commit()
    return listar_empresas(db)


@router.post("/sync", response_model=SyncResult)
def sincronizar(
    empresa: str | None = None,
    periodo: str | None = None,
    meses: int = 3,
    con_items: bool = True,
    db: Session = Depends(get_db),
):
    """Trae del SII los documentos de compra del RCV y los guarda.

    Sin parámetros sincroniza los últimos `meses` períodos terminando en el actual —
    el caso normal al apretar "Sincronizar" en el portal. Con `periodo` ("AAAA-MM")
    sincroniza solo ese.

    La sincronización es idempotente: la clave natural es (tipo, folio, proveedor_rut),
    así que volver a correrla actualiza los montos de un documento ya conocido en vez de
    duplicarlo, y nunca pisa su estado de envío a Finnegans.

    Trae cabeceras, no el detalle de ítems — el portal del SII no lo expone
    (ver `SIIClient.get_dte_xml()`).
    """
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    periodos = [periodo] if periodo else _ultimos_periodos(meses)
    objetivos = _empresas_a_sincronizar(db, empresa, rut)

    nuevos = actualizados = 0
    client = SIIClient(rut, cert_path, password)
    try:
        # Un solo login para todas las empresas y períodos: el SII limita la frecuencia
        # de autenticaciones (ver AGENTS.md), así que la sesión se reusa a propósito.
        session = client.abrir_sesion(headless=True)
        page, context, browser, playwright = session
        try:
            for rut_objetivo in objetivos:
                client.nombre_empresa = None
                for p in periodos:
                    for fila in client.get_rcv(p, rut_empresa=rut_objetivo, session=session):
                        if _guardar_documento(db, fila, rut_objetivo):
                            nuevos += 1
                        else:
                            actualizados += 1
                _registrar_empresa(db, rut_objetivo, client.nombre_empresa)
                if con_items:
                    _completar_items(db, client, session, rut_objetivo, periodos)
            db.commit()
        finally:
            context.close()
            browser.close()
            playwright.stop()
    except SIIBloqueadoError as exc:
        db.rollback()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SIIAuthenticationError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"No se pudo iniciar sesión en el SII: {exc}") from exc
    except SIIRCVError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"El SII rechazó la consulta: {exc}") from exc
    except NotImplementedError as exc:
        db.rollback()
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    finally:
        client.close()

    filtro = select(func.count()).select_from(Documento)
    if len(objetivos) == 1:
        filtro = filtro.where(Documento.empresa_rut == objetivos[0])
    total = db.scalar(filtro) or 0

    rango = periodos[0] if len(periodos) == 1 else f"{periodos[-1]} a {periodos[0]}"
    quien = f"{len(objetivos)} empresas" if len(objetivos) > 1 else _nombre_de(db, objetivos[0])
    return SyncResult(
        documentos_nuevos=nuevos,
        documentos_totales=total,
        mensaje=(
            f"{quien}, período {rango}: {nuevos} documentos nuevos, "
            f"{actualizados} ya conocidos. {total} en la bandeja."
        ),
    )


def _empresas_a_sincronizar(db: Session, empresa: str | None, rut_certificado: str) -> list[str]:
    """Resuelve qué empresas sincronizar: una, todas las autorizadas, o la del certificado."""
    if empresa == "todas":
        ruts = (
            db.execute(
                select(Empresa.rut).where(Empresa.autorizada.is_(True)).order_by(Empresa.rut)
            )
            .scalars()
            .all()
        )
        if not ruts:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No hay empresas registradas todavía. Usá POST /api/empresas/refrescar "
                    "para traer del SII la lista de empresas representadas."
                ),
            )
        return list(ruts)
    return [empresa or rut_certificado.replace(".", "")]


def _nombre_de(db: Session, rut: str) -> str:
    empresa = db.get(Empresa, rut)
    return empresa.nombre_mostrado if empresa else rut


def _completar_items(db: Session, client, session, rut_empresa: str, periodos: list[str]) -> None:
    """Completa el detalle de ítems leyendo el PDF de cada documento en el Portal FE.

    El RCV no publica los ítems, pero el Portal de Facturación Electrónica sí ofrece la
    representación impresa de cada documento recibido, y ese PDF lo genera el SII a
    partir del XML que guarda. Acá se cruzan las dos fuentes: el RCV da las cabeceras y
    el portal, el desglose.

    Es best-effort a propósito: una empresa puede no estar habilitada en ese portal, un
    documento puede no estar en la grilla, o un PDF puede no parsearse. Nada de eso debe
    tumbar la sincronización, que ya trajo las cabeceras — se deja el documento sin
    ítems y se sigue.
    """
    # La sesión es autoflush=False, así que los documentos recién agregados por la
    # sincronización todavía no son visibles para un SELECT. Sin este flush solo se
    # enriquecen los que ya estaban en la base de una corrida anterior, que es
    # exactamente el síntoma que apareció: una empresa nueva quedaba sin ningún ítem.
    db.flush()
    pendientes = db.execute(
        select(Documento).where(
            Documento.empresa_rut == rut_empresa,
            Documento.items.is_(None),
        )
    ).scalars().all()
    if not pendientes:
        return

    # Un rango que cubra los períodos sincronizados, con margen: el período tributario
    # del RCV agrupa por recepción y la grilla del portal filtra por emisión, así que
    # las fechas no calzan exactamente.
    mas_viejo = min(periodos)
    anho, mes = int(mas_viejo[:4]), int(mas_viejo[5:7])
    mes -= 1
    if mes == 0:
        mes, anho = 12, anho - 1
    desde = f"{anho:04d}-{mes:02d}-01"
    hasta = date.today().isoformat()

    try:
        recibidos = client.listar_recibidos_portal(
            rut_empresa, desde=desde, hasta=hasta, session=session
        )
    except SIIPortalFEError as exc:
        # La empresa no está en el Portal de Facturación: no hay PDF que leer.
        log.info("Sin detalle de ítems para %s: %s", rut_empresa, exc)
        return
    except Exception:
        # Best-effort: la sincronización ya trajo las cabeceras y no debe caerse por
        # esto, pero el motivo tiene que quedar en el log y no desaparecer.
        log.exception("Falló el listado del Portal FE para %s", rut_empresa)
        return

    indice = {(_norm_rut(r["rut_emisor"]), str(r["folio"]).strip()): r for r in recibidos}

    for documento in pendientes:
        fila = indice.get((_norm_rut(documento.proveedor_rut), str(documento.folio)))
        if not fila:
            continue
        try:
            detalle = client.get_items_documento(fila["codigo"], session)
        except Exception:
            log.warning(
                "No se pudo leer el PDF del documento %s (folio %s de %s)",
                fila["codigo"], documento.folio, documento.proveedor_rut, exc_info=True,
            )
            continue
        if not detalle.items:
            continue
        documento.items = [i.como_dict() for i in detalle.items]
        documento.pdf_codigo = fila["codigo"]
        documento.items_observacion = None if detalle.cuadra else detalle.observacion


def _norm_rut(rut: str) -> str:
    return (rut or "").replace(".", "").replace(" ", "").upper()


def _registrar_empresa(db: Session, rut: str, nombre: str | None) -> None:
    """Deja constancia de la empresa sincronizada y de cuándo fue.

    El nombre solo se escribe si todavía no había uno: un nombre puesto a mano por el
    usuario manda sobre la razón social que se lea del SII.
    """
    empresa = db.get(Empresa, rut)
    if not empresa:
        empresa = Empresa(rut=rut, autorizada=True)
        db.add(empresa)
    if nombre and not empresa.nombre:
        empresa.nombre = nombre
    empresa.ultima_sincronizacion = datetime.now(timezone.utc)


def _ultimos_periodos(cantidad: int) -> list[str]:
    """['2026-09', '2026-08', '2026-07'] para cantidad=3, del más reciente al más viejo."""
    hoy = date.today()
    periodos = []
    anho, mes = hoy.year, hoy.month
    for _ in range(max(1, cantidad)):
        periodos.append(f"{anho:04d}-{mes:02d}")
        mes -= 1
        if mes == 0:
            mes, anho = 12, anho - 1
    return periodos


def _guardar_documento(db: Session, fila: dict, empresa_rut: str) -> bool:
    """Inserta o actualiza un documento del RCV. Devuelve True si era nuevo.

    Sobre un documento ya existente solo refresca los datos que vienen del SII; no toca
    estado, fecha_envio, finnegans_id ni error_detalle — esos son del ciclo de envío y
    resincronizar no debe perderlos.
    """
    existente = db.execute(
        select(Documento).where(
            Documento.empresa_rut == empresa_rut,
            Documento.tipo == fila["tipo"],
            Documento.folio == fila["folio"],
            Documento.proveedor_rut == fila["proveedor_rut"],
        )
    ).scalar_one_or_none()

    campos = {
        "proveedor_nombre": fila["proveedor_nombre"],
        "fecha": fila["fecha"],
        "neto": fila.get("neto"),
        "iva": fila.get("iva"),
        "exento": fila.get("exento"),
        "total": fila.get("total") or 0,
        "tipo_doc_ref": str(fila["tipo_doc_ref"]) if fila.get("tipo_doc_ref") else None,
        "folio_doc_ref": fila.get("folio_doc_ref"),
    }

    if existente:
        for k, v in campos.items():
            setattr(existente, k, v)
        return False

    db.add(Documento(
        empresa_rut=empresa_rut, tipo=fila["tipo"], folio=fila["folio"],
        proveedor_rut=fila["proveedor_rut"], estado=EstadoDocumento.PENDIENTE, **campos,
    ))
    return True


@router.post("/documents/{documento_id}/send", response_model=EnviarResult)
def enviar_documento(documento_id: int, db: Session = Depends(get_db)):
    documento = db.get(Documento, documento_id)
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    if documento.estado == EstadoDocumento.ENVIADO:
        raise HTTPException(status_code=400, detail="Este documento ya fue enviado.")

    try:
        finnegans = FinnegansClient()
    except FinnegansConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        resultado = finnegans.send_document(documento)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    if resultado.ok:
        documento.estado = EstadoDocumento.ENVIADO
        documento.finnegans_id = resultado.finnegans_id
        documento.error_detalle = None
        documento.fecha_envio = datetime.now(timezone.utc)
    else:
        documento.estado = EstadoDocumento.ERROR
        documento.error_detalle = resultado.error_detalle
    db.commit()

    return EnviarResult(
        id=documento.id, estado=documento.estado,
        finnegans_id=documento.finnegans_id, error_detalle=documento.error_detalle,
    )
