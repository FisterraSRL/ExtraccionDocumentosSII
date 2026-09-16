"""Endpoints que consume el portal (Bandeja SII).

Los nombres y la forma de estas rutas siguen el flujo ya definido en el prototipo:
listar documentos con filtros, sincronizar bajo demanda, y enviar (individual o en
lote, seleccionado por el usuario — el envío nunca es automático, ver requisitos).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.finnegans.client import FinnegansClient, FinnegansConfigError
from app.models import (
    Documento,
    DocumentoOut,
    EnviarResult,
    EstadoDocumento,
    SyncResult,
    TipoDocumento,
)
from app.sii.client import SIIClient
from app.config import settings

router = APIRouter(prefix="/api", tags=["documentos"])


@router.get("/documents", response_model=list[DocumentoOut])
def listar_documentos(
    estado: EstadoDocumento | None = None,
    tipo: TipoDocumento | None = None,
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
def sincronizar(db: Session = Depends(get_db)):
    """Dispara la sincronización bajo demanda con el SII (RCV + BHE).

    Todavía no funcional: SIIClient.get_rcv()/get_bhe() están pendientes de la
    validación de conexión (ver app/sii/client.py). Devuelve un error claro en
    vez de simular datos, para no esconder que falta esa pieza.
    """
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with SIIClient(rut, cert_path, password) as client:
        try:
            rcv = client.get_rcv(periodo="actual")
            bhe = client.get_bhe(periodo="actual")
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    # TODO: persistir rcv + bhe como Documento, evitando duplicados por (tipo, folio, proveedor_rut).
    return SyncResult(documentos_nuevos=0, documentos_totales=0, mensaje="No implementado todavía.")


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
