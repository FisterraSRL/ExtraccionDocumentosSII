"""Administración local del maestro de equivalencias."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import equivalencias
from app.auth import requiere_sesion
from app.db import get_db
from app.routers.productos import _perfil_local


router = APIRouter(prefix="/api/equivalencias", tags=["equivalencias"],
                   dependencies=[Depends(requiere_sesion)])


class Decision(BaseModel):
    codigo: str | None = None
    estado: str = "confirmado"


class DecisionLote(Decision):
    producto_sii_id: int


class Lote(BaseModel):
    perfil_esperado: str
    decisiones: list[DecisionLote] = Field(min_length=1, max_length=5000)


@router.get("")
def listar(response: Response, q: str = "", estado: str = "todos", pagina: int = 1, limite: int = 50,
           perfil_id: str = Depends(_perfil_local),
           db: Session = Depends(get_db)):
    try:
        equivalencias.incorporar_historicos(db)
        resultado = equivalencias.listar(db, perfil_id, q[:120], estado, pagina, limite)
        resultado["perfil_id"] = perfil_id
        response.headers["Cache-Control"] = "no-store"
        return resultado
    except equivalencias.ErrorEquivalencia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/lote")
def guardar_lote(lote: Lote, response: Response,
                 perfil_id: str = Depends(_perfil_local), db: Session = Depends(get_db)):
    if lote.perfil_esperado != perfil_id:
        raise HTTPException(status_code=409, detail="Cambió el certificado activo. Revisá las selecciones antes de guardar.")
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
            perfil_id: str = Depends(_perfil_local), db: Session = Depends(get_db)):
    try:
        equivalencias.guardar(db, perfil_id, producto_sii_id, decision.codigo, decision.estado)
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True}
    except equivalencias.ErrorEquivalencia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
