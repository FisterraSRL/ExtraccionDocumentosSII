import logging
import os
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal

from app.db import init_db
from app.version import __version__
from app import auth
from app.routers import documents
from app.routers import productos as productos_router
from app.routers import equivalencias as equivalencias_router
from app import configuracion_sii

# Sin esto, lo que la app registra en el log no sale por ningún lado: uvicorn configura
# sus propios loggers y deja el resto en WARNING sin handler. Costó un diagnóstico: un
# envío a Finnegans falló y el motivo, que sí se registraba, no quedó en ninguna parte.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)

app = FastAPI(title="SII → Finnegans", version=__version__)

# Abierto en desarrollo; restringir a los orígenes reales del portal antes de producción.
# Nota: como el portal ahora se sirve desde esta misma app (ver más abajo), en la práctica
# ya corre en el mismo origen — este CORS permisivo queda como red de seguridad para cuando
# se pruebe el frontend servido aparte (p. ej. abierto directo como archivo).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(productos_router.router)
app.include_router(equivalencias_router.router)

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


class Credenciales(BaseModel):
    usuario: str
    password: str


class ConfiguracionSII(BaseModel):
    nombre: str
    rut: str
    password: str
    certificado_b64: str = Field(max_length=14 * 1024 * 1024)
    client_id: str = ""
    client_secret: str | None = ""


class EdicionConfiguracionSII(BaseModel):
    nombre: str
    rut: str
    password: str | None = None
    certificado_b64: str | None = Field(default=None, max_length=14 * 1024 * 1024)
    client_id: str | None = None
    client_secret: str | None = None


class SeleccionCertificado(BaseModel):
    perfil: str


class SolicitudRevelar(BaseModel):
    campo: Literal["password", "client_id"]


@app.post("/api/login")
def login(datos: Credenciales, response: Response):
    """Valida usuario y contraseña contra las variables de entorno del despliegue."""
    try:
        if not auth.credenciales_validas(datos.usuario, datos.password):
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")
    except auth.ConfiguracionAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    auth.emitir_cookie(response, datos.usuario)
    return {"usuario": datos.usuario}


@app.post("/api/logout")
def logout(response: Response):
    auth.borrar_cookie(response)
    return {"ok": True}


@app.get("/api/version")
def version():
    """Versión del portal. Sin sesión: la muestra el encabezado antes de entrar."""
    return {"version": __version__}


@app.get("/api/sesion")
def sesion(request: Request, portal_sesion: str | None = Cookie(default=None)):
    """Le dice al portal si ya hay sesión, para mostrar la bandeja o el formulario."""
    sin_login = auth.sesion_automatica(request) is not None
    return {
        "autenticado": sin_login or auth.sesion_valida(portal_sesion) is not None,
        "configurada": auth.auth_configurada(),
        # El portal usa esto para no mostrar un "Salir" que no podría cerrar nada.
        "sin_login": sin_login,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def _solo_local(request: Request) -> None:
    if not configuracion_sii.es_entorno_local(request):
        raise HTTPException(status_code=404, detail="No disponible.")


@app.get("/api/configuracion/sii")
def estado_configuracion_sii(
    request: Request, response: Response, _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    response.headers["Cache-Control"] = "no-store"
    return configuracion_sii.estado()


@app.post("/api/configuracion/sii")
def guardar_configuracion_sii(
    datos: ConfiguracionSII, request: Request, response: Response, _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return configuracion_sii.guardar(datos.nombre, datos.rut, datos.password, datos.certificado_b64,
                                         datos.client_id, datos.client_secret)
    except configuracion_sii.CertificadoDuplicadoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/configuracion/sii/seleccionar")
def seleccionar_configuracion_sii(
    datos: SeleccionCertificado, request: Request, response: Response, _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return configuracion_sii.seleccionar(datos.perfil)
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/configuracion/sii/{perfil}")
def detalle_configuracion_sii(
    perfil: str, request: Request, response: Response, _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return configuracion_sii.detalle(perfil)
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/configuracion/sii/{perfil}/revelar")
def revelar_configuracion_sii(
    perfil: str, datos: SolicitudRevelar, request: Request, response: Response,
    _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    try:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {"valor": configuracion_sii.revelar(perfil, datos.campo)}
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/configuracion/sii/{perfil}")
def editar_configuracion_sii(
    perfil: str, datos: EdicionConfiguracionSII, request: Request, response: Response,
    _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return configuracion_sii.editar(perfil, datos.nombre, datos.rut, datos.password,
                                        datos.certificado_b64, datos.client_id, datos.client_secret)
    except configuracion_sii.CertificadoDuplicadoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/configuracion", include_in_schema=False)
def configuracion(request: Request, _: str = Depends(auth.requiere_sesion), __: None = Depends(_solo_local)) -> FileResponse:
    return FileResponse(STATIC_DIR / "configuracion.html", headers={"Cache-Control": "no-store"})


@app.get("/equivalencias", include_in_schema=False)
def pagina_equivalencias(request: Request, _: str = Depends(auth.requiere_sesion),
                         __: None = Depends(_solo_local)) -> FileResponse:
    return FileResponse(STATIC_DIR / "equivalencias.html", headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def portal() -> FileResponse:
    """Sirve el portal (Bandeja SII) directamente desde el backend, mismo origen que
    la API — así no hace falta CORS ni preocuparse por "mixed content" del navegador
    mientras todo corre local. Es el mismo diseño del artifact publicado en claude.ai,
    ahora conectado a datos reales en vez de datos de ejemplo."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
