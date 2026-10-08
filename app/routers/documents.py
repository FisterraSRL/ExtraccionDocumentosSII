"""Endpoints que consume el portal (Bandeja SII).

Los nombres y la forma de estas rutas siguen el flujo ya definido en el prototipo:
listar documentos con filtros, sincronizar bajo demanda, y enviar (individual o en
lote, seleccionado por el usuario — el envío nunca es automático, ver requisitos).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import requiere_sesion
from app.db import get_db
from app.finnegans.client import (
    FinnegansClient,
    FinnegansConfigError,
    FinnegansMapeoError,
    FinnegansSendResult,
    ajuste_importe_exento,
)
from app.models import (
    AsociacionItem,
    DistribucionCentroCosto,
    Documento,
    Empresa,
    EmpresaOut,
    EmpresaUpdate,
    DocumentoOut,
    EnvioLoteResult,
    EnviarLote,
    EnviarResult,
    EstadoDocumento,
    ProductoFinnegans,
    SyncResult,
)
from app.config import settings

log = logging.getLogger(__name__)


def _sii():
    """Importa el cliente del SII recién cuando se lo necesita.

    Importarlo arriba obligaría a tener instalados requests, cryptography, playwright y
    pdfplumber para levantar la API, aunque solo se quiera leer documentos. El despliegue
    en Vercel no los tiene (Chromium no entra en una función serverless), así que el
    import tiene que ser diferido o la aplicación entera no arranca allá.
    """
    from app.sii import client as modulo

    return modulo


def _sin_navegador() -> str | None:
    """Motivo por el cual este entorno no puede sincronizar, o None si sí puede.

    La sincronización necesita lanzar un Chromium real con el certificado digital. En un
    entorno serverless eso no existe, y conviene decirlo con todas las letras en vez de
    fallar con un ImportError incomprensible.
    """
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return (
            "Este despliegue no puede sincronizar con el SII: la sincronización necesita "
            "abrir un navegador con el certificado digital, y en un entorno serverless "
            "no hay navegador ni disco donde mantener la sesión. Corré la sincronización "
            "desde la máquina que tiene el certificado; los documentos que traiga van a "
            "esta misma base y se ven acá."
        )
    return None

# Todo lo que sirve o modifica datos de clientes exige sesión. El login y la versión
# viven en main.py justamente porque tienen que ser accesibles sin estar autenticado.
router = APIRouter(
    prefix="/api", tags=["documentos"], dependencies=[Depends(requiere_sesion)]
)


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
            en_portal_fe=e.en_portal_fe,
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
    motivo = _sin_navegador()
    if motivo:
        raise HTTPException(status_code=501, detail=motivo)
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    sii = _sii()
    client = sii.SIIClient(rut, cert_path, password)
    try:
        session = client.abrir_sesion(headless=True)
        try:
            ruts = client.get_empresas(session=session)
            nombradas = client.get_empresas_con_nombre(session=session)
        finally:
            session[1].close()
            session[2].close()
            session[3].stop()
    except sii.SIIBloqueadoError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except sii.SIIAuthenticationError as exc:
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
    en_fe = {e["rut"] for e in nombradas}
    for e in nombradas:
        empresa = conocidas.get(e["rut"])
        if empresa is None:
            empresa = Empresa(rut=e["rut"], autorizada=True)
            db.add(empresa)
            conocidas[e["rut"]] = empresa
        if e["nombre"] and not empresa.nombre:
            empresa.nombre = e["nombre"]
    for r, empresa in conocidas.items():
        empresa.en_portal_fe = r in en_fe
    db.commit()
    return listar_empresas(db)


@router.get("/documents/{documento_id}/pdf")
def ver_pdf(documento_id: int, db: Session = Depends(get_db)):
    """Devuelve la representación impresa del documento, tal como la genera el SII.

    Se sirve desde la base y no desde el SII: así funciona aunque no haya sesión con el
    SII —que es el caso del despliegue en la nube— y no se le pide el mismo archivo una
    y otra vez.
    """
    documento = db.get(Documento, documento_id)
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    contenido = documento.pdf_contenido
    if contenido is None:
        contenido = _traer_pdf_del_sii(db, documento)

    nombre = f"{documento.tipo}-{documento.folio}-{documento.proveedor_rut}.pdf"
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{nombre}"'},
    )


def _traer_pdf_del_sii(db: Session, documento: Documento) -> bytes:
    """Va a buscar el PDF al SII en el momento y, salvo que se pida lo contrario, lo deja
    guardado para la próxima vez.

    Es el modo por defecto: así solo ocupan espacio en la base los documentos que a
    alguien le interesó abrir, en vez de los ~170 KB de cada uno de los miles que hay.
    """
    if not documento.pdf_codigo:
        raise HTTPException(
            status_code=404,
            detail=("El SII no publica el PDF de este documento: no aparece en el Portal "
                    "de Facturación Electrónica."),
        )

    motivo = _sin_navegador()
    if motivo:
        raise HTTPException(
            status_code=501,
            detail=("El PDF de este documento no está guardado y este despliegue no puede "
                    f"ir a buscarlo. {motivo}"),
        )

    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    sii = _sii()
    client = sii.SIIClient(rut, cert_path, password)
    try:
        session = client.abrir_sesion(headless=True)
        page, context, browser, playwright = session
        try:
            client._entrar_portal_fe(page, documento.empresa_rut)
            contenido = client.get_pdf_documento(documento.pdf_codigo, session)
        finally:
            context.close()
            browser.close()
            playwright.stop()
    except sii.SIIBloqueadoError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("No se pudo traer el PDF del documento %s", documento.id)
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo traer el PDF desde el SII: {type(exc).__name__}: {str(exc)[:160]}",
        ) from exc
    finally:
        client.close()

    if settings.guardar_pdf_al_verlo:
        documento.pdf_contenido = contenido
        db.commit()
    return contenido


@router.post("/sync", response_model=SyncResult)
def sincronizar(
    empresa: str | None = None,
    periodo: str | None = None,
    meses: int = 3,
    con_items: bool = True,
    reprocesar_items: bool = False,
    descargar_pdf: bool = False,
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
    motivo = _sin_navegador()
    if motivo:
        raise HTTPException(status_code=501, detail=motivo)
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    periodos = [periodo] if periodo else _ultimos_periodos(meses)
    objetivos = _empresas_a_sincronizar(db, empresa, rut)

    nuevos = actualizados = 0
    sii = _sii()
    client = sii.SIIClient(rut, cert_path, password)
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
                    _completar_items(
                        db, client, session, rut_objetivo, periodos,
                        reprocesar=reprocesar_items,
                        descargar_pdf=descargar_pdf,
                    )
                    # El registro es local e idempotente: no provoca más consultas al SII.
                    from app.equivalencias import aplicar_confirmadas, registrar_documentos
                    db.flush()
                    registrar_documentos(db, rut_objetivo)
                    if settings.sii_perfil:
                        aplicar_confirmadas(db, settings.sii_perfil, rut_objetivo)
                # Se confirma empresa por empresa: si el SII falla en la quinta, las
                # cuatro anteriores ya quedaron guardadas en vez de perderse.
                db.commit()
        finally:
            context.close()
            browser.close()
            playwright.stop()
    except sii.SIIBloqueadoError as exc:
        db.rollback()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except sii.SIIAuthenticationError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"No se pudo iniciar sesión en el SII: {exc}") from exc
    except sii.SIIRCVError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"El SII rechazó la consulta: {exc}") from exc
    except NotImplementedError as exc:
        db.rollback()
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        # Cualquier otra cosa (el navegador que se cae, el SII que expira) no debe salir
        # como un 500 opaco: se informa el motivo y se conserva lo ya confirmado.
        db.rollback()
        log.exception("Falló la sincronización de %s", objetivos)
        raise HTTPException(
            status_code=502,
            detail=(f"La sincronización se interrumpió: {type(exc).__name__}: "
                    f"{str(exc)[:200]}. Lo que alcanzó a traerse quedó guardado; "
                    "volvé a sincronizar para continuar."),
        ) from exc
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


def _completar_items(
    db: Session,
    client,
    session,
    rut_empresa: str,
    periodos: list[str],
    reprocesar: bool = False,
    descargar_pdf: bool = False,
) -> None:
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
    if reprocesar:
        # Se relee el PDF también de los documentos que ya tienen desglose pero incompleto
        # o que no pasa el control cantidad x precio = subtotal. Sirve para recuperar
        # documentos guardados con una versión anterior del parser.
        candidatos = db.execute(
            select(Documento).where(Documento.empresa_rut == rut_empresa)
        ).scalars().all()
        pendientes = [d for d in candidatos if _necesita_items(d)]
    else:
        pendientes = db.execute(
            select(Documento).where(
                Documento.empresa_rut == rut_empresa,
                Documento.items.is_(None),
            )
        ).scalars().all()

    if descargar_pdf:
        # Documentos a los que ya se les leyó el desglose pero de los que no se guardó
        # el archivo (se sincronizaron antes de que el portal pudiera mostrarlo).
        ya = {d.id for d in pendientes}
        faltan_pdf = db.execute(
            select(Documento).where(
                Documento.empresa_rut == rut_empresa,
                Documento.pdf_codigo.isnot(None),
                Documento.pdf_contenido.is_(None),
            )
        ).scalars().all()
        pendientes = list(pendientes) + [d for d in faltan_pdf if d.id not in ya]
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

    sii = _sii()
    empresa = db.get(Empresa, rut_empresa)
    try:
        recibidos = client.listar_recibidos_portal(
            rut_empresa, desde=desde, hasta=hasta, session=session
        )
    except sii.SIIPortalFEError as exc:
        # La empresa no está en el Portal de Facturación: no hay PDF que leer. Se deja
        # anotado para que el portal pueda explicarlo en vez de mostrar filas mudas.
        log.info("Sin detalle de ítems para %s: %s", rut_empresa, exc)
        if empresa:
            empresa.en_portal_fe = False
        return
    except Exception:
        # Best-effort: la sincronización ya trajo las cabeceras y no debe caerse por
        # esto, pero el motivo tiene que quedar en el log y no desaparecer.
        log.exception("Falló el listado del Portal FE para %s", rut_empresa)
        return

    if empresa:
        empresa.en_portal_fe = True
    indice = {(_norm_rut(r["rut_emisor"]), str(r["folio"]).strip()): r for r in recibidos}

    for documento in pendientes:
        # Si ya se sabe el código del PDF de una vez anterior, se usa directamente: la
        # grilla filtra por fecha de emisión y hay documentos que quedan fuera de la
        # ventana consultada, con lo cual un reproceso nunca los alcanzaría.
        fila = indice.get((_norm_rut(documento.proveedor_rut), str(documento.folio)))
        codigo = documento.pdf_codigo or (fila["codigo"] if fila else None)
        if not codigo:
            continue
        try:
            pdf = client.get_pdf_documento(codigo, session)
            detalle = client.extraer_detalle_pdf(pdf)
        except Exception:
            log.warning(
                "No se pudo leer el PDF del documento %s (folio %s de %s)",
                codigo, documento.folio, documento.proveedor_rut, exc_info=True,
            )
            continue
        documento.pdf_codigo = codigo
        if settings.guardar_pdf_al_sincronizar or descargar_pdf:
            documento.pdf_contenido = pdf
        if not detalle.items:
            continue
        documento.items = [i.como_dict() for i in detalle.items]
        documento.items_observacion = None if detalle.cuadra else detalle.observacion


def _necesita_items(documento: Documento) -> bool:
    """¿Al desglose de este documento le falta algo que valga la pena volver a leer?"""
    if not documento.items:
        return True
    for item in documento.items:
        if item.get("cant") is None or item.get("precio") is None:
            return True
        if item.get("cuadra") is False:
            return True
        # Guardado por una versión del parser anterior al control: no trae el campo.
        if "cuadra" not in item:
            return True
    return False


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


def _cliente_finnegans() -> FinnegansClient:
    try:
        return FinnegansClient()
    except FinnegansConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _codigos_seleccionados(db: Session, documento: Documento) -> dict[int, str]:
    """Usa las elecciones vigentes del perfil activo al previsualizar y al enviar."""
    if not documento.items or not settings.sii_perfil:
        return {}
    from app.productos import _firma

    asociaciones = db.execute(select(AsociacionItem).where(
        AsociacionItem.perfil_id == settings.sii_perfil,
        AsociacionItem.documento_id == documento.id,
    )).scalars().all()
    codigos = {}
    for asociacion in asociaciones:
        indice = asociacion.indice
        if not (0 <= indice < len(documento.items)) or not asociacion.producto_codigo:
            continue
        if asociacion.descripcion_firma != _firma(documento.items[indice]):
            continue
        producto = db.get(ProductoFinnegans, (settings.sii_perfil, asociacion.producto_codigo))
        if not producto or not producto.disponible or producto.activo is False:
            raise FinnegansMapeoError(
                f"Folio {documento.folio}: el producto seleccionado en el ítem {indice + 1} "
                "ya no está disponible. Elegí otro producto antes de enviar."
            )
        codigos[indice] = producto.codigo
    return codigos


def _centros_seleccionados(db: Session, documento: Documento) -> dict[int, list[dict]]:
    if not documento.items or not settings.sii_perfil:
        return {}
    from app.productos import _firma

    filas = db.execute(select(DistribucionCentroCosto).where(
        DistribucionCentroCosto.perfil_id == settings.sii_perfil,
        DistribucionCentroCosto.documento_id == documento.id,
    )).scalars().all()
    return {
        fila.indice: fila.centros
        for fila in filas
        if 0 <= fila.indice < len(documento.items)
        and fila.descripcion_firma == _firma(documento.items[fila.indice])
    }


def _enviar(db: Session, documento: Documento, finnegans: FinnegansClient) -> EnviarResult:
    """Manda un documento y deja escrito en la base cómo le fue.

    Cada documento se confirma por separado: si el quinto de una selección falla, los
    cuatro anteriores ya quedaron marcados como enviados y no se reintentan —repetirlos
    crearía comprobantes duplicados en el ERP, que es mucho peor que tener que volver a
    apretar el botón.
    """
    try:
        codigos = _codigos_seleccionados(db, documento)
        centros = _centros_seleccionados(db, documento)
        resultado = finnegans.send_document(
            documento, codigos_por_indice=codigos, centros_por_indice=centros
        )
    except FinnegansMapeoError as exc:
        resultado = FinnegansSendResult(ok=False, finnegans_id=None, error_detalle=str(exc))

    if resultado.ok:
        documento.estado = EstadoDocumento.ENVIADO
        documento.finnegans_id = resultado.finnegans_id
        documento.error_detalle = None
        documento.fecha_envio = datetime.now(timezone.utc)
        log.info("Documento %s folio %s → Finnegans %s",
                 documento.tipo, documento.folio, resultado.finnegans_id)
    else:
        documento.estado = EstadoDocumento.ERROR
        documento.error_detalle = resultado.error_detalle
        # También al log, y no solo a la fila: el motivo del rechazo es lo único que
        # explica qué corregir, y si algo pisa el campo en la base se pierde para siempre.
        log.warning("Finnegans rechazó el documento %s (%s folio %s): %s",
                    documento.id, documento.tipo, documento.folio, resultado.error_detalle)
    db.commit()

    return EnviarResult(
        id=documento.id, estado=documento.estado,
        finnegans_id=documento.finnegans_id, error_detalle=documento.error_detalle,
    )


@router.get("/documents/{documento_id}/finnegans")
def previsualizar_documento(documento_id: int, response: Response,
                            db: Session = Depends(get_db)):
    """El JSON exacto que se le mandaría a Finnegans, sin mandarlo.

    Sirve para revisar el mapeo antes de escribir en el ERP, que es una operación que no
    se puede deshacer desde acá.
    """
    documento = db.get(Documento, documento_id)
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    finnegans = _cliente_finnegans()
    try:
        payload = finnegans.construir_payload(
            documento, codigos_por_indice=_codigos_seleccionados(db, documento),
            centros_por_indice=_centros_seleccionados(db, documento),
        )
        # El JSON depende de reglas y configuración que pueden cambiar sin que cambie
        # el id del documento. Una copia HTTP anterior no sirve para decidir un envío.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Ajuste-Importe-Exento"] = str(ajuste_importe_exento(documento, payload))
        return payload
    except (FinnegansMapeoError, FinnegansConfigError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/documents/{documento_id}/send", response_model=EnviarResult)
def enviar_documento(documento_id: int, db: Session = Depends(get_db)):
    documento = db.get(Documento, documento_id)
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    if documento.estado == EstadoDocumento.ENVIADO:
        raise HTTPException(status_code=400, detail="Este documento ya fue enviado.")

    return _enviar(db, documento, _cliente_finnegans())


@router.post("/documents/send", response_model=EnvioLoteResult)
def enviar_seleccion(lote: EnviarLote, db: Session = Depends(get_db)):
    """Registra en Finnegans los documentos que el usuario marcó en la bandeja.

    **Escribe en el ERP.** Va de a uno y en el orden pedido, con un solo cliente para
    toda la tanda: así el token y el cruce de empresas se resuelven una vez y no una vez
    por documento.
    """
    if not lote.ids:
        raise HTTPException(
            status_code=400,
            detail="No hay documentos seleccionados. Marcá al menos uno en la bandeja.",
        )

    documentos = db.execute(
        select(Documento).where(Documento.id.in_(lote.ids))
    ).scalars().all()
    if not documentos:
        raise HTTPException(status_code=404, detail="Ninguno de esos documentos existe.")

    por_id = {d.id: d for d in documentos}
    faltan = [i for i in lote.ids if i not in por_id]
    if faltan:
        log.warning("Se pidió enviar documentos inexistentes: %s", faltan)

    finnegans = _cliente_finnegans()
    resultados: list[EnviarResult] = []
    omitidos = 0
    for documento_id in lote.ids:
        documento = por_id.get(documento_id)
        if documento is None:
            continue
        if documento.estado == EstadoDocumento.ENVIADO:
            omitidos += 1
            continue
        resultados.append(_enviar(db, documento, finnegans))

    enviados = sum(1 for r in resultados if r.estado == EstadoDocumento.ENVIADO)
    log.info(
        "Envío a Finnegans (empresa %s): %s enviados, %s con error, %s ya estaban.",
        settings.finnegans_empresa_codigo or "según RUT",
        enviados, len(resultados) - enviados, omitidos,
    )
    return EnvioLoteResult(
        enviados=enviados,
        con_error=len(resultados) - enviados,
        omitidos=omitidos,
        empresa_finnegans=settings.finnegans_empresa_codigo,
        resultados=resultados,
    )
