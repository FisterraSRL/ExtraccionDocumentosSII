"""Cliente de la API de Finnegans (Teamplace).

Los documentos que extraemos del SII son **compras recibidas**, así que van al endpoint
`POST /facturaCompra`, que es el que pide `Proveedor` y trae los campos chilenos. Los de
venta (`/facturaVenta`, `/facturaVentaChile`) piden `Cliente` y son para documentos que
la empresa emite: no es nuestro caso.

Autenticación: `GET /oauth/token?grant_type=client_credentials&client_id=…&client_secret=…`
devuelve un token en **texto plano** (no JSON). Se manda después como
`Authorization: Bearer <token>`. También se acepta `?ACCESS_TOKEN=`, pero el header evita
que el token quede escrito en URLs y logs.

Mapeos comprobados contra la instancia real (22-sep-2026) y contra un documento de
ejemplo que el dueño del proyecto exportó desde el propio ERP:

- **Proveedor**: el código de proveedor en Finnegans **es el RUT con puntos**
  ("XX.XXX.XXX-X"). Nosotros lo guardamos sin puntos, así que hay que formatearlo.
- **Empresa**: `empresaChile/{codigo}` expone `NumeroIdentificacion` con el RUT, lo que
  permite cruzar automáticamente la empresa del SII con la de Finnegans en vez de
  mantener una tabla a mano.
- **Tipo de documento**: lo identifica `TransaccionSubtipoCodigo` (FC, FCEX, NCCPRA…).
  El catálogo `comprobanteTipoImpositivo` sí tiene los códigos del SII chileno, pero en
  el documento real ese campo va en `null`, así que no se completa.
- **Moneda**: `PES`. El catálogo también tiene `CLP`, pero la instancia usa `PES` como
  moneda local — así viene en el documento real y en `MonedaPrincipalCodigo` de las
  empresas. Configurable por si eso cambia.

Cuidado con dos cosas que la documentación no deja ver y el documento real sí:
`Conceptos` es el **desglose impositivo** (IVA y base gravada), no las líneas de gasto;
las líneas van en `Productos` con un código del maestro.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

from app.config import settings
from app.models import Documento

log = logging.getLogger(__name__)

# El tipo de transacción es siempre "OPER"; lo que distingue un documento de otro es el
# subtipo. Estos códigos los entregó el dueño del proyecto desde su instancia.
SUBTIPO_POR_TIPO_SII = {
    "33": "FC",        # Factura Electrónica (afecta)
    "34": "FCEX",      # Factura No Afecta o Exenta Electrónica
    "46": "FC",        # Factura de Compra Electrónica
    "43": "LC",        # Liquidación-Factura Electrónica
    "56": "NDCPRA",    # Nota de Débito Electrónica
    "61": "NCCPRA",    # Nota de Crédito Electrónica
    "30": "FC",        # Factura (papel)
    "32": "FCEX",      # Factura no Afecta o Exenta (papel)
    "55": "NDCPRA",    # Nota de Débito (papel)
    "60": "NCCPRA",    # Nota de Crédito (papel)
    "BHE": "BO",       # Boleta de Honorarios Electrónica
}


class FinnegansConfigError(RuntimeError):
    """Falta configuración para poder enviar documentos a Finnegans."""


class FinnegansAPIError(RuntimeError):
    """La API de Finnegans rechazó la operación."""


class FinnegansMapeoError(RuntimeError):
    """El documento no se puede traducir a lo que Finnegans espera."""


@dataclass
class FinnegansSendResult:
    ok: bool
    finnegans_id: str | None
    error_detalle: str | None


def formatear_rut(rut: str) -> str:
    """'XXXXXXXX-X' → 'XX.XXX.XXX-X', que es como Finnegans codifica a los proveedores."""
    limpio = re.sub(r"[^0-9kK]", "", rut or "").upper()
    if len(limpio) < 2:
        raise FinnegansMapeoError(f"RUT con formato inesperado: {rut!r}")
    cuerpo, dv = limpio[:-1], limpio[-1]
    partes = []
    while len(cuerpo) > 3:
        partes.insert(0, cuerpo[-3:])
        cuerpo = cuerpo[:-3]
    partes.insert(0, cuerpo)
    return f"{'.'.join(partes)}-{dv}"


class FinnegansClient:
    def __init__(self) -> None:
        if not settings.finnegans_client_id or not settings.finnegans_client_secret:
            raise FinnegansConfigError(
                "Faltan FINNEGANS_CLIENT_ID y/o FINNEGANS_CLIENT_SECRET en .env."
            )
        self.base = (settings.finnegans_api_url or "https://api.finneg.com/api").rstrip("/")
        self._token: str | None = None
        self._token_vence: float = 0.0
        self._empresas_por_rut: dict[str, str] | None = None

    # ---------- Transporte ----------

    def _autenticar(self) -> str:
        """Pide un token. Se cachea: la API los emite por un rato y no hace falta uno nuevo
        en cada llamada."""
        if self._token and time.time() < self._token_vence:
            return self._token
        query = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": settings.finnegans_client_id,
            "client_secret": settings.finnegans_client_secret,
        })
        try:
            with urllib.request.urlopen(f"{self.base}/oauth/token?{query}", timeout=45) as r:
                token = r.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            raise FinnegansAPIError(
                f"No se pudo autenticar contra Finnegans: HTTP {exc.code} "
                f"{exc.read().decode('utf-8', 'replace')[:160]}"
            ) from exc
        if not token or len(token) < 10:
            raise FinnegansAPIError(f"Finnegans devolvió un token inesperado: {token[:40]!r}")
        self._token = token
        self._token_vence = time.time() + 20 * 60  # margen amplio, se revalida sola
        return token

    def _pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None, timeout: int = 90):
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        req = urllib.request.Request(
            f"{self.base}/{ruta.lstrip('/')}",
            data=datos,
            method=metodo,
            headers={
                "Authorization": f"Bearer {self._autenticar()}",
                **({"Content-Type": "application/json"} if datos else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                texto = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detalle = exc.read().decode("utf-8", "replace")[:400]
            raise FinnegansAPIError(f"{metodo} {ruta} → HTTP {exc.code}: {detalle}") from exc
        except Exception as exc:
            raise FinnegansAPIError(f"{metodo} {ruta} → {type(exc).__name__}: {exc}") from exc
        try:
            return json.loads(texto)
        except json.JSONDecodeError:
            return texto

    def catalogo(self, nombre: str) -> list[dict]:
        """Lista un catálogo (`moneda`, `condicionPago`, `proveedor`, …)."""
        datos = self._pedir("GET", f"{nombre}/list")
        return datos if isinstance(datos, list) else []

    # ---------- Resolución de la empresa ----------

    def empresa_para_rut(self, rut_empresa: str) -> str:
        """Código de empresa en Finnegans a partir del RUT con que consultamos el SII.

        Se resuelve contra `empresaChile`, que expone el RUT en `NumeroIdentificacion`.
        Hay RUT con más de un registro (la instancia tiene duplicados); se toma el
        activo de código más bajo, que es el original, y se deja constancia en el log.
        """
        if self._empresas_por_rut is None:
            self._empresas_por_rut = {}
            candidatos: dict[str, list[str]] = {}
            for empresa in self.catalogo("empresaChile"):
                codigo = str(empresa.get("codigo") or "")
                if not codigo:
                    continue
                detalle = self._pedir("GET", f"empresaChile/{urllib.parse.quote(codigo)}")
                if not isinstance(detalle, dict) or not detalle.get("Activo"):
                    continue
                clave = re.sub(r"[^0-9kK]", "", str(detalle.get("NumeroIdentificacion") or "")).upper()
                if clave:
                    candidatos.setdefault(clave, []).append(codigo)
            for clave, codigos in candidatos.items():
                orden = sorted(codigos, key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else 0))
                if len(orden) > 1:
                    log.info("El RUT %s tiene varias empresas en Finnegans (%s); se usa %s",
                             clave, ", ".join(orden), orden[0])
                self._empresas_por_rut[clave] = orden[0]

        clave = re.sub(r"[^0-9kK]", "", rut_empresa or "").upper()
        codigo = self._empresas_por_rut.get(clave)
        if not codigo:
            raise FinnegansMapeoError(
                f"No hay ninguna empresa en Finnegans con RUT {rut_empresa}. "
                "Hay que crearla ahí, o indicar su código a mano."
            )
        return codigo

    # ---------- Armado del documento ----------

    def construir_payload(self, documento: Documento, empresa_codigo: str | None = None) -> dict:
        """Traduce un Documento nuestro al OperacionVO de `POST /facturaCompra`.

        La forma está calcada de un documento real de la instancia, que corrigió varias
        suposiciones que había hecho leyendo solo la documentación:

        - **`Conceptos` es el desglose impositivo, no las líneas de gasto.** Lleva el IVA
          y la base gravada (`COMPRA_IVA_19`), no una línea por ítem.
        - **Las líneas van en `Productos`**, con un código del maestro de productos.
        - `MonedaCodigo` es `PES`: la instancia usa ese código para la moneda local
          aunque el catálogo también tenga `CLP`.
        - `ComprobanteTipoImpositivoCodigo` va en `null` y `CAE` vacío: son campos de
          AFIP argentino que en Chile no se completan. Lo que identifica al tipo de
          documento es `TransaccionSubtipoCodigo`.
        - `Cotizaciones` no puede ir vacío: lleva al menos la moneda local en 1.

        `Vencimientos` no se manda: el documento de ejemplo lo trae porque el ERP lo
        generó al grabarlo, pero armarlo desde acá significaría elegir la cuenta contable
        de proveedores, y esa es una decisión de imputación que no nos corresponde. Sin el
        campo, Finnegans lo arma con la condición de pago del comprobante.

        No manda nada: sirve para revisar qué se enviaría antes de escribir en el ERP.
        """
        subtipo = SUBTIPO_POR_TIPO_SII.get(documento.tipo)
        if not subtipo:
            raise FinnegansMapeoError(
                f"No hay subtipo de transacción definido para el tipo de documento "
                f"{documento.tipo} ({documento.tipo_nombre}). Ver SUBTIPO_POR_TIPO_SII."
            )
        faltan = [
            nombre for nombre, valor in (
                ("FINNEGANS_WORKFLOW", settings.finnegans_workflow),
                ("FINNEGANS_PRODUCTO", settings.finnegans_producto),
            ) if not valor
        ]
        if faltan:
            raise FinnegansConfigError(
                "Faltan parámetros de imputación en .env: " + ", ".join(faltan) +
                ". No se completan con valores por defecto porque elegirlos mal deja "
                "asientos mal imputados en el ERP."
            )

        fecha = documento.fecha.isoformat() if isinstance(documento.fecha, date) else str(documento.fecha)
        neto = documento.neto or 0
        iva = documento.iva or 0
        exento = documento.exento or 0
        identificacion = f"SII-{documento.empresa_rut}-{documento.tipo}-{documento.folio}"

        payload = {
            "IdentificacionExterna": identificacion,
            "Nombre": f"{subtipo} - {documento.folio}",
            "Descripcion": documento.proveedor_nombre[:200],
            "EmpresaCodigo": (
                empresa_codigo
                or settings.finnegans_empresa_codigo
                or self.empresa_para_rut(documento.empresa_rut)
            ),
            "Fecha": fecha,
            "FechaComprobante": fecha,
            "Proveedor": formatear_rut(documento.proveedor_rut),
            "CondicionPagoCodigo": settings.finnegans_condicion_pago,
            "TransaccionTipoCodigo": "OPER",
            "TransaccionSubtipoCodigo": subtipo,
            "WorkflowCodigo": settings.finnegans_workflow,
            "NumeroComprobante": str(documento.folio),
            "MonedaCodigo": settings.finnegans_moneda,
            # Campos de AFIP: en Chile no se completan (confirmado con un documento real).
            "ComprobanteTipoImpositivoCodigo": None,
            "CAE": "",
            "CAEFechaVto": None,
            "Productos": self._productos(documento, neto, exento),
            "Conceptos": [
                {
                    "ConceptoCodigo": settings.finnegans_concepto_iva,
                    "ImporteEditable": False,
                    "ConceptoImporte": iva,
                    "ConceptoImporteGravado": neto,
                },
                {
                    "ConceptoCodigo": settings.finnegans_concepto_exento,
                    "ImporteEditable": False,
                    "ConceptoImporte": 0,
                    "ConceptoImporteGravado": exento,
                },
            ],
            "Retenciones": [],
            "Cotizaciones": [{"MonedaCodigo": settings.finnegans_moneda, "Cotizacion": 1}],
            "ImporteTotalControl": documento.total,
            "IdentificacionExternaPadre": "",
        }

        # Las notas de crédito y débito del SII referencian al documento que corrigen.
        if documento.folio_doc_ref:
            payload["CHL_FolioRef"] = str(documento.folio_doc_ref)
            payload["CHL_FechaRef"] = fecha
        return payload

    def _productos(self, documento: Documento, neto: float, exento: float) -> list[dict]:
        """Líneas del comprobante, una por ítem leído del PDF del SII.

        Todas van contra el mismo producto genérico: los ítems del SII son texto libre
        del emisor y no tienen ningún código que exista en el maestro de Finnegans. La
        descripción real del ítem queda en la línea, que es donde sirve.

        Si el SII no publicó el desglose del documento, va una sola línea por el total.
        """
        codigo = settings.finnegans_producto
        items = documento.items or []

        # Las líneas tienen que sumar la base imponible, o el comprobante queda mal: el
        # ERP calcula el total como base + IVA. Hay emisores cuyo PDF imprime los valores
        # de cada ítem **con IVA incluido** (la suma da neto x 1,19), y mandarlos como
        # base gravada inflaría la factura. Cuando no cuadran no se reparte el importe a
        # ojo: va una sola línea por la base real, que es el dato del que sí estamos
        # seguros, y el desglose informativo sigue estando en el portal.
        base = neto + exento
        if items and base:
            suma = sum(i.get("subtotal") or 0 for i in items)
            if abs(suma - base) > max(2.0, abs(base) * 0.01):
                log.info(
                    "Documento %s folio %s: los ítems suman %.0f y la base imponible es "
                    "%.0f; se envía una sola línea por la base.",
                    documento.tipo, documento.folio, suma, base,
                )
                items = []

        if not items:
            return [{
                "ProductoCodigo": codigo,
                "Cantidad": 1,
                "Precio": neto or documento.total,
                "PrecioBase": neto or documento.total,
                "ImporteExento": exento,
                "Descripcion": f"{documento.tipo_nombre} {documento.folio}"[:200],
            }]
        return [
            {
                "ProductoCodigo": codigo,
                "Cantidad": item.get("cant") or 1,
                "Precio": item.get("precio") or item.get("subtotal") or 0,
                "PrecioBase": item.get("precio") or item.get("subtotal") or 0,
                "ImporteExento": 0,
                "Descripcion": (item.get("desc") or "")[:200],
            }
            for item in items
        ]

    # ---------- Envío ----------

    def send_document(self, documento: Documento) -> FinnegansSendResult:
        """Registra el documento en Finnegans como factura de compra.

        **Escribe en el ERP productivo.** Antes de llamarlo conviene revisar el resultado
        de `construir_payload()` para el mismo documento.
        """
        try:
            payload = self.construir_payload(documento)
        except (FinnegansMapeoError, FinnegansConfigError) as exc:
            return FinnegansSendResult(ok=False, finnegans_id=None, error_detalle=str(exc))

        try:
            respuesta = self._pedir("POST", "facturaCompra", payload)
        except FinnegansAPIError as exc:
            return FinnegansSendResult(ok=False, finnegans_id=None, error_detalle=str(exc))

        identificador = None
        if isinstance(respuesta, dict):
            for clave in ("Codigo", "codigo", "id", "TransaccionID", "NumeroComprobante"):
                if respuesta.get(clave):
                    identificador = str(respuesta[clave])
                    break
        elif isinstance(respuesta, str) and respuesta.strip():
            identificador = respuesta.strip()[:100]

        if identificador:
            return FinnegansSendResult(ok=True, finnegans_id=identificador, error_detalle=None)
        return FinnegansSendResult(
            ok=False,
            finnegans_id=None,
            error_detalle=f"Finnegans respondió sin identificador: {str(respuesta)[:300]}",
        )
