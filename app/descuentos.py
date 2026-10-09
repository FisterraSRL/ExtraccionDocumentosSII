"""Conciliación única de descuentos del desglose del SII.

Trabaja con los ítems originales y nunca los modifica. El mismo análisis sirve al
control del PDF y al armado de las líneas adicionales para Finnegans.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


class DescuentoNoConciliado(ValueError):
    """El origen no justifica una diferencia monetaria como descuento."""


@dataclass(frozen=True)
class DescuentoDetectado:
    origen: str  # "item", "global" o "ajuste_exento"
    importe: Decimal
    indice: int | None = None
    evidencia: str = ""


@dataclass(frozen=True)
class ConciliacionDescuentos:
    bruto_items: Decimal
    subtotales_items: Decimal
    base_documento: Decimal
    descuento_global_informado: Decimal
    descuentos: tuple[DescuentoDetectado, ...]
    diferencia_restante: Decimal
    items_exentos: tuple[int, ...] = ()


def _es_cargo_exento_compensable(descripcion: object) -> bool:
    texto = " ".join(str(descripcion or "").casefold().split())
    return bool(
        re.fullmatch(r"cargo por servicio p.blico", texto)
        or re.fullmatch(r"ajuste para facilitar el pago en efectivo(?:[ ,].*)?", texto)
    )


def _decimal(valor: object, campo: str) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DescuentoNoConciliado(f"No se pudo leer {campo} del desglose.") from exc
    if not numero.is_finite():
        raise DescuentoNoConciliado(f"{campo} no tiene un importe finito.")
    return numero


def _porcentaje(valor: object, bruto: Decimal, subtotal: Decimal) -> Decimal:
    porcentaje = _decimal(valor, "el porcentaje de descuento")
    # Las versiones anteriores leyeron "10.00" como 1000 por tratar el punto como
    # separador de miles. Solo se recupera esa escala si el subtotal la confirma.
    opciones = [porcentaje]
    if porcentaje > 100:
        opciones.append(porcentaje / 100)
    for opcion in opciones:
        if 0 < opcion <= 100 and abs(bruto * (1 - opcion / 100) - subtotal) <= 1:
            return opcion
    raise DescuentoNoConciliado(
        f"el porcentaje impreso ({porcentaje} %) no explica el subtotal del ítem"
    )


def analizar_descuentos(
    items: list[dict], base_documento: object,
    descuento_global: object = 0, referencia: str = "Documento",
) -> ConciliacionDescuentos:
    """Distingue descuentos comprobados de diferencias sin explicación.

    La cabecera debe coincidir con la suma de subtotales tras el descuento global,
    o con los precios originales menos descuentos explícitos. Un descuento por
    monto no impreso solo se infiere cuando *ambos* controles (ítem y cabecera) lo
    corroboran. Las diferencias pequeñas de impresión quedan para el redondeo.
    """
    base = _decimal(base_documento, "la base del documento")
    entradas_globales = (
        descuento_global if isinstance(descuento_global, list)
        else ([{"importe": descuento_global}] if descuento_global else [])
    )
    globales: list[DescuentoDetectado] = []
    for indice, entrada in enumerate(entradas_globales):
        valor = entrada.get("importe") if isinstance(entrada, dict) else entrada
        importe = _decimal(valor, f"el descuento global {indice + 1}")
        if importe <= 0:
            raise DescuentoNoConciliado(
                f"{referencia}: el descuento global {indice + 1} debe ser positivo."
            )
        globales.append(DescuentoDetectado(
            "global", importe, indice, f"Descuento global {indice + 1} del SII",
        ))
    global_impreso = sum((d.importe for d in globales), Decimal(0))
    if not items:
        return ConciliacionDescuentos(Decimal(0), Decimal(0), base, global_impreso, (), Decimal(0))

    bruto = Decimal(0)
    subtotales = Decimal(0)
    explicitos: list[DescuentoDetectado] = []
    candidatos: list[DescuentoDetectado] = []
    cargos_exentos: list[tuple[int, Decimal]] = []
    for indice, item in enumerate(items):
        cantidad = _decimal(item.get("cant"), f"la cantidad del ítem {indice + 1}")
        precio = _decimal(item.get("precio"), f"el precio del ítem {indice + 1}")
        bruto_item = cantidad * precio
        subtotal = _decimal(
            item.get("subtotal") if item.get("subtotal") is not None else bruto_item,
            f"el subtotal del ítem {indice + 1}",
        )
        bruto += bruto_item
        subtotales += subtotal
        diferencia = bruto_item - subtotal
        if (
            _es_cargo_exento_compensable(item.get("desc"))
            and bruto_item > 0 and abs(diferencia) <= 1
        ):
            cargos_exentos.append((indice, bruto_item))
        porcentaje = item.get("descuento_pct")
        if porcentaje is not None and _decimal(porcentaje, "el porcentaje de descuento") != 0:
            try:
                _porcentaje(porcentaje, bruto_item, subtotal)
            except DescuentoNoConciliado as exc:
                raise DescuentoNoConciliado(f"{referencia}, ítem {indice + 1}: {exc}") from exc
            if diferencia > 0:
                explicitos.append(DescuentoDetectado("item", diferencia, indice, "porcentaje impreso"))
        elif diferencia > max(Decimal(1), abs(subtotal) * Decimal("0.01")) and bruto_item > 0:
            candidatos.append(DescuentoDetectado("item", diferencia, indice, "subtotal impreso"))

    tolerancia = max(Decimal(1), Decimal(len(items)) / 2)
    total_explicitos = sum((d.importe for d in explicitos), Decimal(0))
    residual = bruto - total_explicitos - base
    descuentos = list(explicitos)

    # El descuento global puede estar ya incorporado en una línea negativa o en
    # precios netos. Solo se agrega si los precios originales aún lo contienen.
    if global_impreso > 0 and abs(residual - global_impreso) <= tolerancia:
        descuentos.extend(globales)
    elif abs(residual) > tolerancia and candidatos:
        total_candidatos = sum((d.importe for d in candidatos), Decimal(0))
        con_global = global_impreso > 0 and abs(residual - total_candidatos - global_impreso) <= tolerancia
        sin_global = abs(residual - total_candidatos) <= tolerancia
        if con_global and abs(subtotales - global_impreso - base) <= tolerancia:
            descuentos.extend(candidatos)
            descuentos.extend(globales)
        elif sin_global and abs(subtotales - base) <= tolerancia:
            descuentos.extend(candidatos)

    aplicado = sum((d.importe for d in descuentos), Decimal(0))
    residual = bruto - aplicado - base
    importe_exento_compensado = Decimal(0)
    items_exentos: tuple[int, ...] = ()
    if residual > tolerancia and cargos_exentos:
        importe_cargos = sum((importe for _, importe in cargos_exentos), Decimal(0))
        if abs(residual - importe_cargos) <= tolerancia:
            importe_exento_compensado = importe_cargos
            items_exentos = tuple(indice for indice, _ in cargos_exentos)
            descuentos.append(DescuentoDetectado(
                "ajuste_exento", importe_cargos, None,
                "Compensación exenta de cargos y ajustes del SII",
            ))
            aplicado += importe_cargos
    restante = bruto - aplicado - base
    global_aplicado = any(d.origen == "global" for d in descuentos)
    subtotal_valido = (
        abs(subtotales - base - importe_exento_compensado) <= tolerancia
        or (global_aplicado and abs(
            subtotales - global_impreso - base - importe_exento_compensado
        ) <= tolerancia)
    )
    if abs(restante) > tolerancia or (
        not subtotal_valido
    ):
        raise DescuentoNoConciliado(
            f"{referencia}: suma de precios ${bruto}, suma de subtotales ${subtotales}, "
            f"base del documento ${base}, descuento global informado ${global_impreso}; "
            f"diferencia sin justificar ${restante}. Revisá el PDF y los descuentos antes de enviar."
        )
    return ConciliacionDescuentos(
        bruto, subtotales, base, global_impreso, tuple(descuentos), restante, items_exentos
    )
