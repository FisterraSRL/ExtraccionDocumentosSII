import logging
import os
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import init_db
from app.version import __version__
from app import auth
from app.routers import documents

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

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


class Credenciales(BaseModel):
    usuario: str
    password: str


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
def sesion(portal_sesion: str | None = Cookie(default=None)):
    """Le dice al portal si ya hay sesión, para mostrar la bandeja o el formulario."""
    return {
        "autenticado": auth.sesion_valida(portal_sesion) is not None,
        "configurada": auth.auth_configurada(),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def portal() -> FileResponse:
    """Sirve el portal (Bandeja SII) directamente desde el backend, mismo origen que
    la API — así no hace falta CORS ni preocuparse por "mixed content" del navegador
    mientras todo corre local. Es el mismo diseño del artifact publicado en claude.ai,
    ahora conectado a datos reales en vez de datos de ejemplo."""
    return FileResponse(STATIC_DIR / "index.html")
