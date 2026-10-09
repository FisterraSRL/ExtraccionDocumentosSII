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
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from copy import deepcopy

from app.config import settings
from app.descuentos import DescuentoNoConciliado, analizar_descuentos
from app.models import Documento

log = logging.getLogger(__name__)

PATRON_IE = re.compile(
    r"\bIE\s*Base\s*:\s*([+-]?[\d.,]+)\s*[-–]?\s*IE\s*Variable\s*:\s*([+-]?[\d.,]+)",
    re.IGNORECASE,
)


def _decimal_ie(texto: str) -> Decimal:
    # El PDF puede imprimir coma o punto decimal; cuando trae ambos, el punto es miles.
    limpio = texto.replace(".", "").replace(",", ".") if "," in texto else texto
    try:
        return Decimal(limpio)
    except InvalidOperation as exc:
        raise FinnegansMapeoError("No se pudieron leer los componentes IE del combustible.") from exc


def _importes_ie(documento: Documento, items: list[dict]) -> list[int]:
    """Calcula el IE conocido por ítem antes de completar otros importes exentos."""
    calculados: list[Decimal] = []
    for item in items:
        descripcion = str(item.get("desc") or "")
        coincidencia = PATRON_IE.search(descripcion)
        if not coincidencia:
            if re.search(r"\bIE\s*(?:Base|Variable)\s*:", descripcion, re.IGNORECASE):
                raise FinnegansMapeoError(
                    f"Folio {documento.folio}: el detalle menciona IE, pero no se pudieron "
                    "leer sus componentes base y variable. Revisá el PDF antes de enviar."
                )
            calculados.append(Decimal(0))
            continue
        cantidad = item.get("cant")
        try:
            cantidad_ie = Decimal(str(cantidad))
        except InvalidOperation:
            cantidad_ie = Decimal(0)
        if cantidad_ie <= 0:
            raise FinnegansMapeoError(
                f"Folio {documento.folio}: falta la cantidad de combustible para calcular IE."
            )
        tasa = _decimal_ie(coincidencia.group(1)) + _decimal_ie(coincidencia.group(2))
        importe = tasa * cantidad_ie
        if importe <= 0:
            raise FinnegansMapeoError(
                f"Folio {documento.folio}: el IE resultante no es positivo; revisá "
                "cómo imputarlo antes de enviar."
            )
        calculados.append(importe)

    if not any(calculados):
        return [0] * len(items)

    esperado = sum(calculados).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    importes = [int(valor.to_integral_value(rounding=ROUND_FLOOR)) for valor in calculados]
    faltan = int(esperado) - sum(importes)
    if not 0 <= faltan <= sum(valor > 0 for valor in calculados):
        raise FinnegansMapeoError(
            f"Folio {documento.folio}: no se pudo distribuir el redondeo del IE entre los ítems."
        )
    orden = sorted(
        (indice for indice, valor in enumerate(calculados) if valor > 0),
        key=lambda indice: calculados[indice] - importes[indice], reverse=True,
    )
    for indice in orden[:faltan]:
        importes[indice] += 1
    return importes


def _importe(valor: object) -> Decimal:
    try:
        return Decimal(str(valor or 0))
    except InvalidOperation as exc:
        raise FinnegansMapeoError("Un importe del documento no tiene formato numérico.") from exc


def _dimension_centro_costo(centros: list[dict]) -> list[dict]:
    if not centros or sum(_importe(c.get("porcentaje")) for c in centros) != 100:
        raise FinnegansMapeoError(
            "La distribución de Centros de Costo debe sumar exactamente 100 %."
        )
    items = []
    for centro in centros:
        codigo = str(centro.get("codigo") or "").strip()
        porcentaje = _importe(centro.get("porcentaje"))
        if not codigo or porcentaje <= 0:
            raise FinnegansMapeoError(
                "Cada Centro de Costo debe tener código y porcentaje positivo."
            )
        items.append({
            "codigo": codigo,
            "porcentaje": int(porcentaje) if porcentaje == porcentaje.to_integral_value()
            else float(porcentaje),
        })
    return [{
        "dimensionCodigo": "DIMCTC",
        "distribucionCodigo": "",
        "tipoCalculo": "2",
        "distribucionItems": items,
    }]


def _total_calculado(payload: dict) -> Decimal:
    # En el ejemplo oficial de facturaCompra, ImporteExento es parte del precio
    # (10 × 1500 contiene 10000 exentos), no un importe adicional al precio.
    return sum(
        _importe(linea["Cantidad"]) * _importe(linea["Precio"])
        for linea in payload["Productos"]
    ) + sum(_importe(concepto["ConceptoImporte"]) for concepto in payload["Conceptos"])


def _agregar_lineas_de_ajuste(documento: Documento, payload: dict,
                             descuento_global: float | list[dict] | None = None) -> None:
    productos = payload["Productos"]
    neto = _importe(documento.neto)
    iva = _importe(documento.iva)
    exento = _importe(documento.exento)
    total = _importe(documento.total)

    def agregar_linea(
        importe: Decimal, descripcion: str, es_exento: bool,
        codigo: str | None = None, copiar_centro: bool = True,
    ) -> None:
        if not importe:
            return
        absoluto = abs(importe)
        numero = int(absoluto) if absoluto == absoluto.to_integral_value() else float(absoluto)
        linea = {
            "ProductoCodigo": codigo or productos[0]["ProductoCodigo"],
            "Cantidad": 1 if importe > 0 else -1,
            "Precio": numero,
            "PrecioBase": numero,
            "ImporteExento": (
                int(importe) if importe == importe.to_integral_value() else float(importe)
            ) if es_exento else 0,
            "Descripcion": descripcion,
        }
        if copiar_centro and productos[0].get("DimensionDistribucion"):
            linea["DimensionDistribucion"] = deepcopy(productos[0]["DimensionDistribucion"])
        productos.append(linea)

    if documento.items:
        base_esperada = neto + exento
        try:
            conciliacion = analizar_descuentos(
                documento.items, base_esperada, descuento_global,
                f"{documento.tipo_nombre} · folio {documento.folio}",
            )
        except DescuentoNoConciliado as exc:
            raise FinnegansMapeoError(str(exc)) from exc

        es_exento = bool(exento and not neto)
        codigo_descuento = (
            settings.finnegans_producto_descuento_exento if es_exento
            else settings.finnegans_producto_descuento_afecto
        )
        if any(d.origen in ("item", "global") for d in conciliacion.descuentos) and not codigo_descuento:
            variable = (
                "FINNEGANS_PRODUCTO_DESCUENTO_EXENTO" if es_exento
                else "FINNEGANS_PRODUCTO_DESCUENTO_AFECTO"
            )
            raise FinnegansMapeoError(
                f"Folio {documento.folio}: falta {variable}. Configurá un producto "
                "de descuento con la imputación tributaria correspondiente antes de enviar."
            )
        for indice in conciliacion.items_exentos:
            # Estos cargos figuran entre los ítems del PDF pero no forman parte de
            # la base imponible. La línea negativa exenta los compensa sin cambiar
            # neto, IVA ni total de la cabecera.
            importe = _importe(productos[indice]["Cantidad"]) * _importe(productos[indice]["Precio"])
            productos[indice]["ImporteExento"] = (
                int(importe) if importe == importe.to_integral_value() else float(importe)
            )
        cantidad_globales = sum(d.origen == "global" for d in conciliacion.descuentos)
        for descuento in conciliacion.descuentos:
            if descuento.origen == "item":
                descripcion = str(documento.items[descuento.indice].get("desc") or "").strip()
                codigo_item = str(documento.items[descuento.indice].get("codigo") or "").strip()
                referencia = f" ({codigo_item})" if codigo_item else ""
                etiqueta = f"Descuento ítem {descuento.indice + 1}{referencia}: {descripcion}"[:200]
                codigo_linea = codigo_descuento
                linea_exenta = es_exento
            elif descuento.origen == "ajuste_exento":
                etiqueta = descuento.evidencia
                codigo_linea = settings.finnegans_producto_ajuste_exento
                linea_exenta = True
            else:
                etiqueta = (
                    descuento.evidencia if cantidad_globales > 1
                    else "Descuento global del SII"
                )
                codigo_linea = codigo_descuento
                linea_exenta = es_exento
            agregar_linea(
                -descuento.importe, etiqueta, linea_exenta,
                codigo=codigo_linea, copiar_centro=False,
            )

        base_productos = sum(
            _importe(linea["Cantidad"]) * _importe(linea["Precio"])
            for linea in productos
        )
        diferencia_base = base_esperada - base_productos
        if diferencia_base:
            # El importe impreso por ítem puede estar redondeado a pesos. La
            # conciliación ya comprobó que el remanente es pequeño y que los
            # subtotales del PDF justifican la base de la cabecera.
            codigo_redondeo = (
                settings.finnegans_producto_ajuste_exento if conciliacion.items_exentos
                else codigo_descuento
            )
            agregar_linea(
                diferencia_base, "Ajuste de redondeo de base del SII",
                es_exento or bool(conciliacion.items_exentos and diferencia_base < 0),
                codigo=(codigo_redondeo if diferencia_base < 0 and conciliacion.descuentos else None),
                copiar_centro=not (diferencia_base < 0 and conciliacion.descuentos),
            )

    if not documento.items and not (neto or iva or exento):
        return

    adicional = total - neto - iva - exento
    ie = sum(_importes_ie(documento, documento.items or []))
    if adicional < ie or adicional < 0:
        raise FinnegansMapeoError(
            f"Folio {documento.folio}: los impuestos y la base superan el total del SII. "
            "Revisá el PDF antes de enviar a Finnegans."
        )
    agregar_linea(Decimal(ie), "Impuesto específico del combustible", es_exento=True)
    agregar_linea(adicional - ie, "Ajuste exento del total del SII", es_exento=True)


def ajuste_importe_exento(documento: Documento, payload: dict) -> Decimal:
    """Importe de las líneas exentas adicionales, sin contar los ítems del PDF."""
    originales = len(documento.items) if documento.items else 1
    return sum(_importe(item["ImporteExento"]) for item in payload["Productos"][originales:])


def _validar_total_control(documento: Documento, payload: dict) -> None:
    calculado = _total_calculado(payload)
    total = _importe(payload["ImporteTotalControl"])
    if calculado != total:
        raise FinnegansMapeoError(
            f"Folio {documento.folio}: los productos y conceptos suman "
            f"${calculado}, pero el total del SII es ${total}. "
            "Se conservaron la cantidad y el precio del PDF; revisá sus descuentos "
            "o impuestos antes de enviar a Finnegans."
        )

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
        self._centro_requerido_por_producto: dict[str, bool] = {}
        self._centro_requerido_por_cuenta: dict[str, bool] = {}

    def requiere_centro_costo(self, codigo_producto: str) -> bool:
        """Consulta la cuenta de compra del producto, sin modificar Finnegans.

        Una cuenta con la dimensión DIMCTC requiere distribuir sus líneas. El
        resultado se conserva durante la vista previa o el lote de envíos actual.
        """
        if codigo_producto not in self._centro_requerido_por_producto:
            ruta = "producto/" + urllib.parse.quote(codigo_producto, safe="")
            try:
                producto = self._pedir("GET", ruta)
                cuenta = producto.get("CuentaCodigoCompra") if isinstance(producto, dict) else None
                if not cuenta:
                    raise FinnegansMapeoError(
                        f"El producto {codigo_producto} no tiene cuenta de compra en Finnegans. "
                        "Revisá su imputación antes de enviar."
                    )
                if cuenta not in self._centro_requerido_por_cuenta:
                    detalle = self._pedir(
                        "GET", "cuenta/" + urllib.parse.quote(str(cuenta), safe="")
                    )
                    if not isinstance(detalle, dict) or not isinstance(detalle.get("CuentaDimension"), list):
                        raise FinnegansMapeoError(
                            f"No se pudo determinar si la cuenta {cuenta} requiere Centro de Costo."
                        )
                    self._centro_requerido_por_cuenta[cuenta] = any(
                        dimension.get("DimensionCodigo") == "DIMCTC"
                        for dimension in detalle["CuentaDimension"]
                        if isinstance(dimension, dict)
                    )
                self._centro_requerido_por_producto[codigo_producto] = (
                    self._centro_requerido_por_cuenta[cuenta]
                )
            except FinnegansAPIError as exc:
                raise FinnegansMapeoError(
                    f"No se pudo consultar la cuenta de compra del producto {codigo_producto} "
                    "para verificar su Centro de Costo. Intentá nuevamente."
                ) from exc
        return self._centro_requerido_por_producto[codigo_producto]

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

    def construir_payload(self, documento: Documento, empresa_codigo: str | None = None,
                         codigos_por_indice: dict[int, str] | None = None,
                         centros_por_indice: dict[int, list[dict]] | None = None,
                         descuento_global: float | list[dict] | None = None,
                         productos_con_centro_requerido: set[str] | None = None) -> dict:
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
            "Productos": self._productos(
                documento, neto, iva, exento, codigos_por_indice, centros_por_indice
            ),
            "Conceptos": [
                {
                    "ConceptoCodigo": settings.finnegans_concepto_iva,
                    "ImporteEditable": True,
                    "ConceptoImporte": iva,
                    "ConceptoImporteGravado": neto,
                },
                {
                    "ConceptoCodigo": settings.finnegans_concepto_exento,
                    "ImporteEditable": True,
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
        _agregar_lineas_de_ajuste(documento, payload, descuento_global)
        if productos_con_centro_requerido:
            centro = [{"codigo": settings.finnegans_centro_costo_predeterminado, "porcentaje": 100}]
            for linea in payload["Productos"]:
                if (linea["ProductoCodigo"] in productos_con_centro_requerido
                        and not linea.get("DimensionDistribucion")):
                    linea["DimensionDistribucion"] = _dimension_centro_costo(centro)
        # El IE va en una línea exenta; el concepto exento debe reflejar la misma
        # base para que Finnegans no recalcule una clasificación inconsistente.
        base_exenta = sum(
            _importe(linea["ImporteExento"]) for linea in payload["Productos"]
        )
        payload["Conceptos"][1]["ConceptoImporteGravado"] = (
            int(base_exenta) if base_exenta == base_exenta.to_integral_value()
            else float(base_exenta)
        )
        _validar_total_control(documento, payload)
        return payload

    def _productos(self, documento: Documento, neto: float, iva: float, exento: float,
                   codigos_por_indice: dict[int, str] | None = None,
                   centros_por_indice: dict[int, list[dict]] | None = None) -> list[dict]:
        """Líneas del comprobante, una por ítem leído del PDF del SII.

        Todas van contra el mismo producto genérico: los ítems del SII son texto libre
        del emisor y no tienen ningún código que exista en el maestro de Finnegans. La
        descripción real del ítem queda en la línea, que es donde sirve.

        Cada ítem conserva la cantidad y el precio unitario del PDF. El control
        monetario usa el producto exacto: Finnegans puede controlar centavos aunque
        el PDF del SII muestre subtotales redondeados al peso.
        ImporteExento clasifica una parte del precio de la misma línea: no es una
        suma adicional. El IE y otros cargos se agregan como líneas exentas propias.
        """
        codigo = settings.finnegans_producto
        items = documento.items or []

        if items and neto and exento:
            raise FinnegansMapeoError(
                f"Folio {documento.folio}: hay neto y exento, pero el detalle no indica "
                "qué ítems son exentos. Revisá el PDF antes de enviar."
            )

        if not items:
            precio = (neto + exento) or (documento.total if not iva else 0)
            producto = {
                "ProductoCodigo": codigo,
                "Cantidad": 1,
                "Precio": precio,
                "PrecioBase": precio,
                "ImporteExento": exento,
                "Descripcion": f"{documento.tipo_nombre} {documento.folio}"[:200],
            }
            if centros_por_indice and centros_por_indice.get(0):
                producto["DimensionDistribucion"] = _dimension_centro_costo(
                    centros_por_indice[0]
                )
            return [producto]
        productos = []
        for indice, item in enumerate(items):
            cantidad = item.get("cant")
            precio = item.get("precio")
            if cantidad is None or precio is None:
                raise FinnegansMapeoError(
                    f"Folio {documento.folio}: un ítem no tiene cantidad o precio en el PDF. "
                    "Revisá el detalle antes de enviar."
                )
            importe_item = _importe(cantidad) * _importe(precio)
            importe_exento = (
                int(importe_item) if importe_item == importe_item.to_integral_value()
                else float(importe_item)
            ) if exento else 0
            producto = {
                "ProductoCodigo": (codigos_por_indice or {}).get(indice) or codigo,
                "Cantidad": cantidad,
                "Precio": precio,
                "PrecioBase": precio,
                # En una línea exenta, ImporteExento clasifica el importe de
                # Cantidad × Precio. Si el subtotal ya incluye un descuento,
                # usarlo aquí restaría ese descuento dos veces al concepto exento.
                "ImporteExento": importe_exento,
                "Descripcion": (item.get("desc") or "")[:200],
            }
            if centros_por_indice and centros_por_indice.get(indice):
                producto["DimensionDistribucion"] = _dimension_centro_costo(
                    centros_por_indice[indice]
                )
            productos.append(producto)
        return productos

    # ---------- Envío ----------

    def send_document(self, documento: Documento,
                      codigos_por_indice: dict[int, str] | None = None,
                      centros_por_indice: dict[int, list[dict]] | None = None,
                      descuento_global: float | list[dict] | None = None,
                      productos_con_centro_requerido: set[str] | None = None) -> FinnegansSendResult:
        """Registra el documento en Finnegans como factura de compra.

        **Escribe en el ERP productivo.** Antes de llamarlo conviene revisar el resultado
        de `construir_payload()` para el mismo documento.
        """
        try:
            payload = self.construir_payload(
                documento, codigos_por_indice=codigos_por_indice,
                centros_por_indice=centros_por_indice,
                descuento_global=descuento_global,
                productos_con_centro_requerido=productos_con_centro_requerido,
            )
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
