"""Endpoints que consume el portal (Bandeja SII).

Los nombres y la forma de estas rutas siguen el flujo ya definido en el prototipo:
listar documentos con filtros, sincronizar bajo demanda, y enviar (individual o en
lote, seleccionado por el usuario — el envío nunca es automático, ver requisitos).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.finnegans.client import FinnegansClient, FinnegansConfigError
from app.models import (
    Documento,
    DocumentoOut,
    EnviarResult,
    EstadoDocumento,
    SyncResult,
)
from app.sii.client import SIIAuthenticationError, SIIClient, SIIRCVError
from app.config import settings

router = APIRouter(prefix="/api", tags=["documentos"])


@router.get("/documents", response_model=list[DocumentoOut])
def listar_documentos(
    estado: EstadoDocumento | None = None,
    tipo: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Documento)
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


@router.post("/sync", response_model=SyncResult)
def sincronizar(
    periodo: str | None = None,
    meses: int = 3,
    rut_empresa: str | None = None,
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

    nuevos = actualizados = 0
    client = SIIClient(rut, cert_path, password)
    try:
        session = client.login_with_browser(headless=True)
        page, context, browser, playwright = session
        try:
            for p in periodos:
                for fila in client.get_rcv(p, rut_empresa=rut_empresa, session=session):
                    if _guardar_documento(db, fila):
                        nuevos += 1
                    else:
                        actualizados += 1
            db.commit()
        finally:
            context.close()
            browser.close()
            playwright.stop()
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

    total = db.scalar(select(func.count()).select_from(Documento)) or 0
    rango = periodos[0] if len(periodos) == 1 else f"{periodos[-1]} a {periodos[0]}"
    return SyncResult(
        documentos_nuevos=nuevos,
        documentos_totales=total,
        mensaje=(f"Período {rango}: {nuevos} documentos nuevos, "
                 f"{actualizados} ya conocidos. {total} en la bandeja."),
    )


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


def _guardar_documento(db: Session, fila: dict) -> bool:
    """Inserta o actualiza un documento del RCV. Devuelve True si era nuevo.

    Sobre un documento ya existente solo refresca los datos que vienen del SII; no toca
    estado, fecha_envio, finnegans_id ni error_detalle — esos son del ciclo de envío y
    resincronizar no debe perderlos.
    """
    existente = db.execute(
        select(Documento).where(
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
        tipo=fila["tipo"], folio=fila["folio"], proveedor_rut=fila["proveedor_rut"],
        estado=EstadoDocumento.PENDIENTE, **campos,
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
