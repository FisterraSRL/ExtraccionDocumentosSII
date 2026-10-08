"""Catálogo local y selección de productos para ítems del SII."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import configuracion_sii, productos
from app.auth import requiere_sesion
from app.db import get_db
from app.finnegans.catalogo import ErrorCatalogoFinnegans
from app.models import Documento


router = APIRouter(
    prefix="/api", tags=["productos"], dependencies=[Depends(requiere_sesion)]
)


def _perfil_local(request: Request) -> str:
    if not configuracion_sii.es_entorno_local(request):
        raise HTTPException(status_code=404, detail="No disponible.")
    try:
        return configuracion_sii.perfil_activo_id()
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class SeleccionProducto(BaseModel):
    codigo: str | None = None


@router.get("/productos/estado")
def estado_productos(
    response: Response, perfil_id: str = Depends(_perfil_local), db: Session = Depends(get_db)
):
    response.headers["Cache-Control"] = "no-store"
    return productos.estado_catalogo(db, perfil_id)


@router.post("/productos/sincronizar")
def sincronizar_productos(
    request: Request, response: Response, perfil_id: str = Depends(_perfil_local),
    db: Session = Depends(get_db),
):
    try:
        _, client_id, client_secret = configuracion_sii.credenciales_finnegans_activas()
        resultado = productos.sincronizar(db, perfil_id, client_id, client_secret)
        response.headers["Cache-Control"] = "no-store"
        return resultado
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ErrorCatalogoFinnegans as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except productos.ErrorProductos as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/productos")
def buscar_productos(
    q: str = "", limite: int = 8, perfil_id: str = Depends(_perfil_local),
    db: Session = Depends(get_db),
):
    return {"productos": productos.buscar(db, perfil_id, q, limite)}


@router.post("/documents/{documento_id}/productos/preparar")
def preparar_productos(
    documento_id: int, response: Response, perfil_id: str = Depends(_perfil_local),
    db: Session = Depends(get_db),
):
    documento = db.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    try:
        items = productos.preparar(db, perfil_id, documento)
    except productos.ErrorProductos as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return {"items": items, "catalogo": productos.estado_catalogo(db, perfil_id)}


@router.put("/documents/{documento_id}/items/{indice}/producto")
def elegir_producto(
    documento_id: int, indice: int, datos: SeleccionProducto, response: Response,
    perfil_id: str = Depends(_perfil_local), db: Session = Depends(get_db),
):
    documento = db.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    try:
        resultado = productos.guardar_asociacion(
            db, perfil_id, documento, indice, datos.codigo
        )
    except productos.ErrorProductos as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return resultado
