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
from sqlalchemy import (
    JSON,
    Boolean,
    LargeBinary,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class Empresa(Base):
    """Una empresa que el titular del certificado puede consultar en el SII.

    El SII las lista en el RCV (servicio getDcvEmpresasAutorizadas) pero **solo por RUT**:
    el campo de razón social viene vacío para todas. El nombre se completa de dos formas,
    ninguna obligatoria para que el sistema funcione:

    - Automática: al sincronizar, se lee la razón social del receptor desde el detalle de
      un documento del período (es el único lugar del portal que la expone).
    - Manual: el usuario le pone el nombre con el que la conoce desde el portal
      (`PATCH /api/empresas/{rut}`), que es lo que hace que "Centraliza" sea buscable
      aunque el SII nunca devuelva ese nombre.
    """

    __tablename__ = "empresas"

    rut: Mapped[str] = mapped_column(String(12), primary_key=True)
    nombre: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # False cuando el SII deja de listarla: se conserva para no perder sus documentos.
    autorizada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ¿Está en el Portal de Facturación Electrónica? Ahí viven los PDF, o sea el detalle
    # de ítems. Solo 30 de las 55 empresas del RCV lo están, y para el resto no hay
    # desglose posible: conviene decirlo una vez y no dejar filas sin explicación.
    # None = todavía no se comprobó.
    en_portal_fe: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ultima_sincronizacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def nombre_mostrado(self) -> str:
        return self.nombre or self.rut


class Documento(Base):
    """Un documento traído del SII (RCV o BHE), con su detalle completo y su estado de envío."""

    __tablename__ = "documentos"
    __table_args__ = (
        # La empresa entra en la clave: dos empresas distintas pueden recibir documentos
        # con el mismo tipo y folio del mismo proveedor, y no son el mismo documento.
        UniqueConstraint(
            "empresa_rut", "tipo", "folio", "proveedor_rut", name="uq_documento_sii"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Empresa receptora: de cuál de las empresas representadas es este documento.
    empresa_rut: Mapped[str] = mapped_column(String(12), nullable=False, index=True)

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

    # Notas (motivo de NC/ND) e ítems (desc/cant/precio/subtotal, leídos del PDF del SII)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none_as_null: sin esto SQLAlchemy guarda Python None como el texto JSON 'null',
    # no como NULL de SQL, y el filtro `items IS NULL` que usa la sincronización para
    # saber a qué documentos les falta el desglose dejaría de encontrarlos.
    items: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    # Identificador del documento en el Portal de Facturación Electrónica, que es de
    # donde sale su PDF. Se guarda para poder volver a pedirlo sin rehacer la búsqueda.
    pdf_codigo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # El PDF en sí. Se guarda para poder mostrarlo en el portal sin depender de tener
    # sesión en el SII: en el despliegue en la nube no hay forma de ir a buscarlo, y aun
    # en local significa no volver a pedírselo al SII cada vez que alguien lo abre.
    # Pesan ~170 KB cada uno y no comprimen (ya vienen comprimidos), así que el tamaño
    # de la base crece de forma notoria: se puede apagar con GUARDAR_PDF=0.
    pdf_contenido: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Queda escrito cuando la suma de los ítems no cuadra con los totales del documento
    # (descuentos globales, otros cobros). El desglose se muestra igual, con la salvedad.
    items_observacion: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    def tiene_pdf(self) -> bool:
        """El archivo ya está en la base: se muestra al instante."""
        return self.pdf_contenido is not None

    @property
    def pdf_disponible(self) -> bool:
        """Hay PDF para mostrar, esté guardado o haya que ir a buscarlo al SII.

        Es lo que decide si el portal ofrece el botón. Que esté guardado o no cambia
        cuánto tarda, no si existe.
        """
        return self.pdf_contenido is not None or self.pdf_codigo is not None

    @property
    def tipo_nombre(self) -> str:
        """Nombre legible del tipo; lo consume el portal para no duplicar el catálogo."""
        return nombre_tipo(self.tipo)


class ProductoFinnegans(Base):
    """Copia local del catálogo; el código identifica al producto en la API de Finnegans."""

    __tablename__ = "productos_finnegans"

    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    codigo: Mapped[str] = mapped_column(String(200), primary_key=True)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    unidad_id_compra: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unidad: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rubro: Mapped[str | None] = mapped_column(String(200), nullable=True)
    familia: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # producto/list no devuelve Activo. No se inventa un estado a partir del listado.
    activo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    disponible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SincronizacionProductos(Base):
    __tablename__ = "sincronizaciones_productos"

    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False)


class AsociacionItem(Base):
    """Elección de producto separada del JSON del SII, que una sincronización puede releer."""

    __tablename__ = "asociaciones_items"

    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    documento_id: Mapped[int] = mapped_column(ForeignKey("documentos.id"), primary_key=True)
    indice: Mapped[int] = mapped_column(Integer, primary_key=True)
    descripcion_firma: Mapped[str] = mapped_column(Text, nullable=False)
    producto_codigo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origen: Mapped[str] = mapped_column(String(20), nullable=False)
    puntaje: Mapped[float | None] = mapped_column(Float, nullable=True)
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DistribucionCentroCosto(Base):
    """Imputación elegida para un ítem, separada del detalle original del SII."""

    __tablename__ = "distribuciones_centros_costo"

    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    documento_id: Mapped[int] = mapped_column(ForeignKey("documentos.id"), primary_key=True)
    indice: Mapped[int] = mapped_column(Integer, primary_key=True)
    descripcion_firma: Mapped[str] = mapped_column(Text, nullable=False)
    centros: Mapped[list] = mapped_column(JSON, nullable=False)
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProductoSII(Base):
    """Descripción única del SII; el texto original se conserva para consulta."""

    __tablename__ = "productos_sii"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    descripcion_original: Mapped[str] = mapped_column(Text, nullable=False)
    descripcion_normalizada: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    apariciones: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    primera_aparicion: Mapped[date | None] = mapped_column(Date, nullable=True)
    ultima_aparicion: Mapped[date | None] = mapped_column(Date, nullable=True)


class AparicionProductoSII(Base):
    """Cada ítem se cuenta una sola vez aunque el documento se sincronice otra vez."""

    __tablename__ = "apariciones_productos_sii"
    documento_id: Mapped[int] = mapped_column(ForeignKey("documentos.id"), primary_key=True)
    indice: Mapped[int] = mapped_column(Integer, primary_key=True)
    producto_sii_id: Mapped[int] = mapped_column(ForeignKey("productos_sii.id"), nullable=False, index=True)


class EquivalenciaProducto(Base):
    """Decisión vigente por perfil; no modifica las asociaciones de documentos enviados."""

    __tablename__ = "equivalencias_productos"
    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    producto_sii_id: Mapped[int] = mapped_column(ForeignKey("productos_sii.id"), primary_key=True)
    producto_codigo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    estado: Mapped[str] = mapped_column(String(24), nullable=False)
    origen: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EstadoMaestro(Base):
    """Marca la incorporación inicial de los ítems anteriores a este maestro."""

    __tablename__ = "estado_maestro"
    clave: Mapped[str] = mapped_column(String(40), primary_key=True)
    fecha: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SugerenciaProducto(Base):
    """Resultado reproducible del matching para el catálogo vigente del perfil."""

    __tablename__ = "sugerencias_productos"
    perfil_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    producto_sii_id: Mapped[int] = mapped_column(ForeignKey("productos_sii.id"), primary_key=True)
    producto_codigo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    puntaje: Mapped[float | None] = mapped_column(Float, nullable=True)
    catalogo_fecha: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------- Esquemas Pydantic (API) ----------

class ItemSchema(BaseModel):
    desc: str
    # Nulos posibles: hay documentos cuyo PDF no imprime cantidad ni precio unitario.
    cant: float | None = None
    precio: float | None = None
    subtotal: float | None = None
    codigo: str | None = None
    descuento_pct: float | None = None
    # Qué se dedujo del subtotal en vez de leerse del PDF, si algo.
    derivado: str | None = None
    # cantidad x precio (menos descuento) coincide con el subtotal impreso.
    cuadra: bool = True


class EmpresaOut(BaseModel):
    rut: str
    nombre: str | None = None
    nombre_mostrado: str
    autorizada: bool
    en_portal_fe: bool | None = None
    ultima_sincronizacion: datetime | None = None
    documentos: int = 0
    pendientes: int = 0

    class Config:
        from_attributes = True


class EmpresaUpdate(BaseModel):
    """Nombre con el que el usuario conoce a la empresa (el SII no lo entrega)."""

    nombre: str


class DocumentoOut(BaseModel):
    id: int
    empresa_rut: str
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
    items_observacion: str | None = None
    tiene_pdf: bool = False
    pdf_disponible: bool = False
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


class EnviarLote(BaseModel):
    """Los documentos que el usuario marcó en la bandeja.

    El envío es siempre explícito y sobre una selección: nunca se manda una tanda entera
    por el solo hecho de estar pendiente.
    """

    ids: list[int]


class EnvioLoteResult(BaseModel):
    enviados: int
    con_error: int
    # Ya estaban enviados: no se reintentan para no duplicar el comprobante en el ERP.
    omitidos: int = 0
    # Empresa de Finnegans en la que quedaron registrados. Importa decirlo porque
    # durante las pruebas todos van a una empresa fija (PRUEBA39) y no a la que
    # corresponde por RUT.
    empresa_finnegans: str | None = None
    resultados: list[EnviarResult] = []
