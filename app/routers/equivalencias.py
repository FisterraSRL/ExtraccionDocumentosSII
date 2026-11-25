"""Administración del maestro de equivalencias."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import equivalencias
from app.auth import requiere_sesion_gestion
from app.db import get_db
from app.models import AparicionProductoSII, Documento
from app.routers.documents import _ruts_empresas_perfil
from app.routers.productos import _perfil_gestion


router = APIRouter(prefix="/api/equivalencias", tags=["equivalencias"],
                   dependencies=[Depends(requiere_sesion_gestion)])


class Decision(BaseModel):
    codigo: str | None = None
    estado: str = "confirmado"


class DecisionLote(Decision):
    producto_sii_id: int


class Lote(BaseModel):
    perfil_esperado: str
    decisiones: list[DecisionLote] = Field(min_length=1, max_length=5000)


def _productos_permitidos(db: Session) -> set[int] | None:
    ruts = _ruts_empresas_perfil(db)
    if ruts is None:
        return None
    return set(db.scalars(
        select(AparicionProductoSII.producto_sii_id).join(
            Documento, Documento.id == AparicionProductoSII.documento_id
        ).where(Documento.empresa_rut.in_(ruts)).distinct()
    ))


def _exigir_productos(db: Session, ids: set[int]) -> None:
    permitidos = _productos_permitidos(db)
    if permitidos is not None and not ids.issubset(permitidos):
        raise HTTPException(status_code=403, detail="Una descripción no pertenece al certificado seleccionado.")


@router.get("")
def listar(response: Response, q: str = "", estado: str = "todos", pagina: int = 1, limite: int = 50,
           perfil_id: str = Depends(_perfil_gestion),
           db: Session = Depends(get_db)):
    try:
        equivalencias.incorporar_historicos(db)
        resultado = equivalencias.listar(db, perfil_id, q[:120], estado, pagina, limite,
                                        _productos_permitidos(db))
        resultado["perfil_id"] = perfil_id
        response.headers["Cache-Control"] = "no-store"
        return resultado
    except equivalencias.ErrorEquivalencia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/lote")
def guardar_lote(lote: Lote, response: Response,
                 perfil_id: str = Depends(_perfil_gestion), db: Session = Depends(get_db)):
    if lote.perfil_esperado != perfil_id:
        raise HTTPException(status_code=409, detail="Cambió el certificado activo. Revisá las selecciones antes de guardar.")
    _exigir_productos(db, {d.producto_sii_id for d in lote.decisiones})
    try:
        cantidad = equivalencias.guardar_varias(
            db, perfil_id,
            [(d.producto_sii_id, d.codigo, d.estado) for d in lote.decisiones],
        )
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "guardadas": cantidad}
    except equivalencias.ErrorEquivalencia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{producto_sii_id}")
def guardar(producto_sii_id: int, decision: Decision, response: Response,
            perfil_id: str = Depends(_perfil_gestion), db: Session = Depends(get_db)):
    _exigir_productos(db, {producto_sii_id})
    try:
        equivalencias.guardar(db, perfil_id, producto_sii_id, decision.codigo, decision.estado)
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True}
    except equivalencias.ErrorEquivalencia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
