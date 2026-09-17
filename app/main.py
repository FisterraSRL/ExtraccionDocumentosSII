from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.db import init_db
from app.version import __version__
from app.routers import documents

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
