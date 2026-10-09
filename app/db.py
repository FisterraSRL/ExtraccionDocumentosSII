"""Engine y sesión de SQLAlchemy. SQLite en desarrollo; DATABASE_URL define el motor real."""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)

def _url_normalizada(url: str) -> str:
    """Acepta las URL que entregan los proveedores de Postgres tal como vienen.

    Neon, Supabase y Vercel dan `postgres://...` o `postgresql://...`, pero SQLAlchemy
    necesita que el driver sea explícito o usa psycopg2, que no está instalado. Se
    reescribe acá para que el que despliega no tenga que saber esto.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _url_normalizada(settings.database_url)
es_sqlite = DATABASE_URL.startswith("sqlite")

connect_args = {"check_same_thread": False} if es_sqlite else {}
# En serverless cada invocación puede abrir su propia conexión y las de Postgres son
# un recurso escaso: no se mantiene un pool entre invocaciones.
opciones: dict = {"connect_args": connect_args}
if not es_sqlite:
    opciones["pool_pre_ping"] = True
    opciones["pool_recycle"] = 300

engine = create_engine(DATABASE_URL, **opciones)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Crea las tablas que falten. Idempotente.

    No revienta si la base no está disponible o el usuario no puede crear tablas: en un
    despliegue serverless eso tiraría abajo el portal entero en el arranque, cuando lo
    correcto es que la página cargue y muestre el error al consultar.
    """
    # Import tardío para registrar los modelos en Base antes de crear las tablas.
    from app import models  # noqa: F401

    try:
        Base.metadata.create_all(bind=engine)
        columnas_nuevas = (
            ("documentos", "origen_envio"),
            ("historial_reenvios", "origen_envio_anterior"),
        )
        with engine.begin() as conexion:
            for tabla, columna in columnas_nuevas:
                existentes = {dato["name"] for dato in inspect(conexion).get_columns(tabla)}
                if columna not in existentes:
                    condicion = " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
                    conexion.execute(text(
                        f"ALTER TABLE {tabla} ADD COLUMN{condicion} {columna} VARCHAR(20)"
                    ))

        # Los intentos anteriores quedaron como error, aunque Finnegans confirmó
        # que el comprobante ya estaba registrado. Se reclasifican una sola vez.
        from app.finnegans.client import referencia_comprobante_repetido
        from app.models import Documento, EstadoDocumento

        with SessionLocal() as sesion:
            errores = sesion.scalars(select(Documento).where(
                Documento.estado == EstadoDocumento.ERROR,
                Documento.error_detalle.is_not(None),
            )).all()
            corregidos = 0
            for documento in errores:
                referencia = referencia_comprobante_repetido(
                    documento.error_detalle, documento.folio, documento.tipo,
                )
                if referencia is None:
                    continue
                documento.estado = EstadoDocumento.ENVIADO
                documento.origen_envio = "externo"
                documento.finnegans_id = referencia
                corregidos += 1
            if corregidos:
                sesion.commit()
                log.info("Reclasificados %s comprobantes ya existentes en Finnegans.", corregidos)
    except Exception:
        log.exception("No se pudieron crear las tablas en %s", engine.url.render_as_string())
