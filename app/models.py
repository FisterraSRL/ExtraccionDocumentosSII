"""Modelo de Documento (tabla) + esquemas Pydantic de la API.

Los campos siguen lo que ya usa el prototipo "Bandeja SII" (ver el artifact publicado
y requisitos-portal-sii-finnegans.md): documentos del RCV (facturas, notas de
crédito/débito, guías de despacho, boletas) y BHE (boletas de honorarios), cada
uno con su detalle completo (XML) y un estado de envío a Finnegans.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from pydantic import BaseModel
from sqlalchemy import JSON, Date, DateTime, Enum, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


# Catálogo de tipos de documento del SII, tal como lo devuelve el propio RCV
# (servicio getDatosInicio, capturado el 16-sep-2026). Son 45 y el SII puede agregar
# más, así que `tipo` se guarda como el código string y esto es solo para mostrar
# el nombre: un código nuevo entra igual y se muestra como "Tipo N" en vez de
# reventar la sincronización, que es lo que pasaría con un enum cerrado.
TIPOS_DOCUMENTO: dict[str, str] = {
    "29": "Factura de Inicio",
    "30": "Factura",
    "32": "Factura no Afecta o Exenta",
    "33": "Factura Electrónica",
    "34": "Factura no Afecta o Exenta Electrónica",
    "35": "Total Oper. del mes Boleta Afecta",
    "38": "Total Oper. del mes Boleta Exenta",
    "39": "Total Oper. del mes Boleta Electrónica",
    "40": "Liquidación Factura",
    "41": "Total Op. del mes Boleta Exenta Electrónica",
    "43": "Liquidación-Factura Electrónica",
    "45": "Factura de Compra",
    "46": "Factura de Compra Electrónica",
    "48": "Total mes Comprobantes Pago Electrónico",
    "55": "Nota de Débito",
    "56": "Nota de Débito Electrónica",
    "60": "Nota de Crédito",
    "61": "Nota de Crédito Electrónica",
    "101": "Factura de Exportación",
    "102": "Factura vta. Exenta a Zona Franca Prim.",
    "103": "Liquidación",
    "104": "Nota de Débito de Exportación",
    "106": "Nota de Crédito de Exportación",
    "109": "Factura Turista",
    "110": "Factura de Exportación Electrónica",
    "111": "Nota de Débito de Exportación Electrónica",
    "112": "Nota de Crédito de Exportación Electrónica",
    "914": "Declaración de Ingreso (DIN)",
    "BHE": "Boleta de Honorarios Electrónica",
}


def nombre_tipo(codigo: str) -> str:
    return TIPOS_DOCUMENTO.get(codigo, f"Tipo {codigo}")


class EstadoDocumento(str, enum.Enum):
    PENDIENTE = "pendiente"
    ENVIANDO = "enviando"
    ENVIADO = "enviado"
    ERROR = "error"


def _por_valor(enum_class) -> list[str]:
    """SQLAlchemy persiste por defecto el NOMBRE del miembro del enum ("FACTURA_AFECTA"),
    no su valor ("33"). Como el valor es el código real del SII —y es lo que viaja por la
    API y usa el portal— guardamos el valor en la base: así `?tipo=33` filtra sin traducir
    nada en el medio."""
    return [m.value for m in enum_class]


class Documento(Base):
    """Un documento traído del SII (RCV o BHE), con su detalle completo y su estado de envío."""

    __tablename__ = "documentos"
    __table_args__ = (
        UniqueConstraint("tipo", "folio", "proveedor_rut", name="uq_documento_sii"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identificación en el SII — folio + tipo + RUT emisor deben ser únicos entre sí,
    # es la clave natural para no duplicar un documento ya sincronizado.
    tipo: Mapped[str] = mapped_column(String(4), nullable=False)  # código del SII, ver TIPOS_DOCUMENTO
    folio: Mapped[int] = mapped_column(Integer, nullable=False)
    proveedor_rut: Mapped[str] = mapped_column(String(12), nullable=False)
    proveedor_nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)

    # Montos RCV (None para guía de despacho sin desglose de IVA)
    neto: Mapped[float | None] = mapped_column(Float, nullable=True)
    iva: Mapped[float | None] = mapped_column(Float, nullable=True)
    exento: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float] = mapped_column(Float, nullable=False)

    # Campos específicos de BHE
    servicio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bruto: Mapped[float | None] = mapped_column(Float, nullable=True)
    retencion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    retencion: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquido: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Documento que corrige una nota de crédito/débito (lo entrega el RCV).
    tipo_doc_ref: Mapped[str | None] = mapped_column(String(4), nullable=True)
    folio_doc_ref: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Notas (motivo de NC/ND) e ítems (detalle del XML, lista de dicts: desc/cant/precio/subtotal)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    items: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # XML original del DTE, guardado tal cual para trazabilidad (ver requisitos: "guardar
    # el XML original de cada documento como respaldo", dado que no hay notificaciones/alertas
    # separadas y el portal es la única fuente de verdad).
    xml_original: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Estado de envío a Finnegans
    estado: Mapped[EstadoDocumento] = mapped_column(
        Enum(EstadoDocumento, values_callable=_por_valor),
        nullable=False,
        default=EstadoDocumento.PENDIENTE,
    )
    fecha_envio: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finnegans_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # id del comprobante ya creado en Finnegans
    error_detalle: Mapped[str | None] = mapped_column(Text, nullable=True)

    sincronizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def tipo_nombre(self) -> str:
        """Nombre legible del tipo; lo consume el portal para no duplicar el catálogo."""
        return nombre_tipo(self.tipo)


# ---------- Esquemas Pydantic (API) ----------

class ItemSchema(BaseModel):
    desc: str
    cant: float
    precio: float
    subtotal: float


class DocumentoOut(BaseModel):
    id: int
    tipo: str
    tipo_nombre: str
    folio: int
    proveedor_rut: str
    proveedor_nombre: str
    fecha: date
    neto: float | None = None
    iva: float | None = None
    exento: float | None = None
    total: float
    tipo_doc_ref: str | None = None
    folio_doc_ref: int | None = None
    servicio: str | None = None
    bruto: float | None = None
    retencion_pct: float | None = None
    retencion: float | None = None
    liquido: float | None = None
    motivo: str | None = None
    items: list[ItemSchema] | None = None
    estado: EstadoDocumento
    fecha_envio: datetime | None = None
    finnegans_id: str | None = None
    error_detalle: str | None = None

    class Config:
        from_attributes = True


class SyncResult(BaseModel):
    documentos_nuevos: int
    documentos_totales: int
    mensaje: str


class EnviarResult(BaseModel):
    id: int
    estado: EstadoDocumento
    finnegans_id: str | None = None
    error_detalle: str | None = None
