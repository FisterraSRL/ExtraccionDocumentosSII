"""Catálogo local y selección de productos para ítems del SII."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import configuracion_sii, productos
from app.auth import requiere_sesion
from app.db import get_db
from app.finnegans.catalogo import ErrorCatalogoFinnegans
from app.finnegans.client import FinnegansAPIError, FinnegansClient, FinnegansConfigError
from app.models import DistribucionCentroCosto, Documento


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


class CentroCostoEntrada(BaseModel):
    codigo: str
    porcentaje: float


class DistribucionEntrada(BaseModel):
    centros: list[CentroCostoEntrada]


@router.get("/documents/{documento_id}/centros-costo")
def consultar_centros_costo(
    documento_id: int, response: Response, perfil_id: str = Depends(_perfil_local),
    db: Session = Depends(get_db),
):
    documento = db.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    respuesta = {}
    for indice, item in enumerate(documento.items or []):
        registro = db.get(DistribucionCentroCosto, (perfil_id, documento_id, indice))
        if registro and registro.descripcion_firma == productos._firma(item):
            respuesta[str(indice)] = registro.centros
    response.headers["Cache-Control"] = "no-store"
    return {"distribuciones": respuesta}


@router.put("/documents/{documento_id}/items/{indice}/centros-costo")
def guardar_centros_costo(
    documento_id: int, indice: int, datos: DistribucionEntrada, response: Response,
    perfil_id: str = Depends(_perfil_local), db: Session = Depends(get_db),
):
    documento = db.get(Documento, documento_id)
    if documento is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    if not documento.items or not 0 <= indice < len(documento.items):
        raise HTTPException(status_code=422, detail="El ítem indicado no existe en el documento.")
    centros = [{"codigo": item.codigo.strip(), "porcentaje": item.porcentaje}
               for item in datos.centros]
    if len(centros) > 10 or any(not item["codigo"] or item["porcentaje"] <= 0 for item in centros):
        raise HTTPException(status_code=422, detail="Indicá hasta 10 centros con código y porcentaje positivo.")
    if centros and sum(Decimal(str(item["porcentaje"])) for item in centros) != 100:
        raise HTTPException(status_code=422, detail="Los porcentajes de Centros de Costo deben sumar 100 %.")
    if len({item["codigo"] for item in centros}) != len(centros):
        raise HTTPException(status_code=422, detail="No repitas un código de Centro de Costo.")

    if centros:
        try:
            cliente = FinnegansClient()
            for item in centros:
                detalle = cliente._pedir("GET", "centroCosto/" + quote(item["codigo"], safe=""))
                if not isinstance(detalle, dict) or detalle.get("Activo") is False:
                    raise HTTPException(
                        status_code=422,
                        detail=f"El Centro de Costo {item['codigo']} no está activo.",
                    )
        except FinnegansConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except FinnegansAPIError as exc:
            raise HTTPException(
                status_code=422,
                detail="No se pudo validar un Centro de Costo en Finnegans. Revisá el código.",
            ) from exc

    registro = db.get(DistribucionCentroCosto, (perfil_id, documento_id, indice))
    if centros:
        if registro is None:
            registro = DistribucionCentroCosto(
                perfil_id=perfil_id, documento_id=documento_id, indice=indice,
            )
            db.add(registro)
        registro.descripcion_firma = productos._firma(documento.items[indice])
        registro.centros = centros
        registro.actualizado_at = datetime.utcnow()
    elif registro is not None:
        db.delete(registro)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"indice": indice, "centros": centros}


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
