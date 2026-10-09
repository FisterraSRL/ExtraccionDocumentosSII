"""Descuentos comprobados por ítem y globales, para cualquier subtipo compatible."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.config import settings
from app.descuentos import DescuentoNoConciliado, analizar_descuentos
from app.finnegans.client import FinnegansClient, FinnegansMapeoError
from app.models import Documento
from app.sii.pdf_dte import DetalleDTE, ItemDTE, _leer_totales, _porcentaje, _verificar


def _documento(tipo="33", *, items, neto, iva, exento=0, total=None):
    return Documento(
        empresa_rut="00000000-0", tipo=tipo, folio=123,
        proveedor_rut="11111111-1", proveedor_nombre="Prueba",
        fecha=date(2025, 1, 2), neto=neto, iva=iva, exento=exento,
        total=total if total is not None else neto + iva + exento,
        items=items,
    )


def _payload(documento, descuento_global=None):
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="ARTICULO_PRUEBA",
        finnegans_producto_descuento_afecto="DESCUENTO_AFECTO",
        finnegans_producto_descuento_exento="DESCUENTO_EXENTO",
        finnegans_producto_ajuste_exento="AACZ-2048",
    )
    with patch("app.finnegans.client.settings", configuracion):
        return object.__new__(FinnegansClient).construir_payload(
            documento, empresa_codigo="EMPRESA_PRUEBA", descuento_global=descuento_global,
        )


def test_descuento_porcentaje_y_global_coexisten_sin_duplicarse():
    items = [
        {"desc": "Artículo A", "cant": 1, "precio": 1000, "subtotal": 900, "descuento_pct": 10},
        {"desc": "Artículo B", "cant": 1, "precio": 500, "subtotal": 500},
    ]
    documento = _documento(items=items, neto=1300, iva=247)
    original = [dict(item) for item in documento.items]
    for tipo in ("33", "61"):
        documento.tipo = tipo
        primero = _payload(documento, descuento_global=100)
        segundo = _payload(documento, descuento_global=100)
        assert primero == segundo
        assert [(p["Cantidad"], p["Precio"]) for p in primero["Productos"][:2]] == [
            (1, 1000), (1, 500),
        ]
        descuentos = [p for p in primero["Productos"] if p["Cantidad"] < 0]
        assert [(p["ProductoCodigo"], p["Precio"]) for p in descuentos] == [
            ("DESCUENTO_AFECTO", 100), ("DESCUENTO_AFECTO", 100),
        ]
        assert sum(Decimal(str(p["Cantidad"])) * Decimal(str(p["Precio"]))
                   for p in primero["Productos"]) == 1300
    assert documento.items == original


def test_descuento_por_monto_se_infiere_solo_con_subtotal_y_cabecera():
    items = [{"desc": "Artículo", "cant": 2, "precio": 500, "subtotal": 850}]
    analisis = analizar_descuentos(items, 850)
    assert [(d.origen, d.indice, d.importe) for d in analisis.descuentos] == [
        ("item", 0, Decimal("150"))
    ]
    payload = _payload(_documento(items=items, neto=850, iva=161))
    assert payload["Productos"][1]["Cantidad"] == -1
    assert payload["Productos"][1]["Precio"] == 150
    assert payload["Productos"][1]["Descripcion"].startswith("Descuento ítem 1")


def test_descuento_global_ya_representado_no_se_repite():
    items = [
        {"desc": "Artículo", "cant": 1, "precio": 1000, "subtotal": 1000},
        {"desc": "Descuento existente", "cant": -1, "precio": 100, "subtotal": -100},
    ]
    documento = _documento(items=items, neto=900, iva=171)
    payload = _payload(documento, descuento_global=100)
    assert len(payload["Productos"]) == 2
    assert [p["Cantidad"] for p in payload["Productos"]] == [1, -1]


def test_descuento_global_en_subtotales_se_representa_una_sola_vez():
    items = [{"desc": "Artículo", "cant": 1, "precio": 1000, "subtotal": 900}]
    analisis = analizar_descuentos(items, 900, 100)
    assert [(d.origen, d.importe) for d in analisis.descuentos] == [
        ("global", Decimal(100))
    ]
    payload = _payload(_documento(items=items, neto=900, iva=171), 100)
    assert len([p for p in payload["Productos"] if p["Cantidad"] < 0]) == 1


def test_descuento_exento_reduce_producto_y_concepto_exento():
    items = [{"desc": "Artículo exento", "cant": 1, "precio": 1000, "subtotal": 900}]
    documento = _documento("34", items=items, neto=0, iva=0, exento=900)
    payload = _payload(documento)
    assert payload["Productos"][0]["ImporteExento"] == 1000
    assert payload["Productos"][1]["ProductoCodigo"] == "DESCUENTO_EXENTO"
    assert payload["Productos"][1]["ImporteExento"] == -100
    assert payload["Conceptos"][1]["ConceptoImporteGravado"] == 900
    assert sum(Decimal(str(p["Cantidad"])) * Decimal(str(p["Precio"]))
               for p in payload["Productos"]) == 900


def test_diferencia_sin_evidencia_detiene_el_envio_con_importes():
    items = [{"desc": "Artículo", "cant": 1, "precio": 1190, "subtotal": 1190}]
    try:
        _payload(_documento(items=items, neto=1000, iva=190, total=1190))
    except FinnegansMapeoError as exc:
        mensaje = str(exc)
        assert "folio 123" in mensaje and "1190" in mensaje and "1000" in mensaje
        assert "diferencia sin justificar" in mensaje
    else:
        raise AssertionError("El IVA incluido no puede convertirse en descuento")


def test_diferencia_sin_evidencia_no_hace_post():
    documento = _documento(
        items=[{"desc": "Artículo", "cant": 1, "precio": 1190, "subtotal": 1190}],
        neto=1000, iva=190,
    )
    cliente = object.__new__(FinnegansClient)
    llamadas = []
    cliente._pedir = lambda *args, **kwargs: llamadas.append(args)
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="ARTICULO_PRUEBA",
        finnegans_empresa_codigo="EMPRESA_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion):
        resultado = cliente.send_document(documento)
    assert resultado.ok is False
    assert "diferencia sin justificar" in resultado.error_detalle
    assert llamadas == []


def test_porcentaje_decimal_y_multiples_descuentos_globales():
    assert _porcentaje("10.00") == 10
    assert _porcentaje("10,50") == 10.5
    totales = {}
    _leer_totales("Descuento Global $ 100 Descuento Global $ 50", totales)
    assert totales["descuento_global"] == 150
    documento = _documento(
        items=[{"desc": "Artículo", "cant": 1, "precio": 1000, "subtotal": 1000}],
        neto=850, iva=162,
    )
    payload = _payload(documento, [{"importe": 100}, {"importe": 50}])
    descuentos = [p for p in payload["Productos"] if p["Cantidad"] < 0]
    assert [p["Precio"] for p in descuentos] == [100, 50]
    assert [p["Descripcion"] for p in descuentos] == [
        "Descuento global 1 del SII", "Descuento global 2 del SII",
    ]


def test_parser_anota_importe_de_descuento_por_item():
    detalle = DetalleDTE(
        items=[ItemDTE(desc="Artículo", cant=1, precio=1000, subtotal=900, cuadra=False)],
        totales={"neto": 900, "iva": 171, "total": 1071},
    )
    _verificar(detalle)
    assert detalle.cuadra
    assert detalle.items[0].cuadra
    assert detalle.items[0].descuento_monto == 100


def test_porcentaje_incompatible_no_inventa_descuento():
    items = [{"desc": "Artículo", "cant": 1, "precio": 1000,
              "subtotal": 600, "descuento_pct": 10}]
    try:
        analizar_descuentos(items, 600)
    except DescuentoNoConciliado as exc:
        assert "ítem 1" in str(exc)
    else:
        raise AssertionError("Un porcentaje erróneo no justifica la diferencia")


def test_porcentaje_historico_mal_escalado_se_valida_con_subtotal():
    items = [{"desc": "Artículo", "cant": 1, "precio": 1000,
              "subtotal": 900, "descuento_pct": 1000}]
    analisis = analizar_descuentos(items, 900)
    assert analisis.descuentos[0].importe == 100


def test_cargos_del_sii_se_compensan_sin_cambiar_base_iva_ni_total():
    items = [
        {"desc": "Producto A", "cant": 1, "precio": 1036, "subtotal": 1036},
        {"desc": "Producto B", "cant": 1, "precio": 6004, "subtotal": 6004},
        {"desc": "Producto C", "cant": 1, "precio": 44501, "subtotal": 44501},
        {"desc": "Cargo por Servicio P?blico", "cant": 1, "precio": 195, "subtotal": 195},
        {"desc": "Ajuste para facilitar el pago en efectivo, mes actual", "cant": 1, "precio": 96, "subtotal": 96},
        {"desc": "Ajuste para facilitar el pago en efectivo, mes anterior", "cant": 1, "precio": 67, "subtotal": 67},
    ]
    documento = _documento(items=items, neto=51541, iva=9793, total=61334)
    analisis = analizar_descuentos(items, 51541)
    assert analisis.items_exentos == (3, 4, 5)
    assert [(d.origen, d.importe) for d in analisis.descuentos] == [
        ("ajuste_exento", Decimal(358))
    ]
    primero = _payload(documento)
    assert primero == _payload(documento)
    assert len(primero["Productos"]) == 7
    assert [p["ImporteExento"] for p in primero["Productos"][3:]] == [195, 96, 67, -358]
    assert [(p["Cantidad"], p["Precio"]) for p in primero["Productos"][:6]] == [
        (1, 1036), (1, 6004), (1, 44501), (1, 195), (1, 96), (1, 67),
    ]
    assert primero["Productos"][-1]["ProductoCodigo"] == "AACZ-2048"
    assert (primero["Productos"][-1]["Cantidad"], primero["Productos"][-1]["Precio"]) == (-1, 358)
    assert primero["Conceptos"][0]["ConceptoImporteGravado"] == 51541
    assert primero["Conceptos"][1]["ConceptoImporteGravado"] == 0
    assert primero["ImporteTotalControl"] == 61334
    assert documento.items == items


def test_no_compensar_diferencia_sin_cargos_comprobados():
    items = [
        {"desc": "Producto", "cant": 1, "precio": 1000, "subtotal": 1000},
        {"desc": "Otro cargo", "cant": 1, "precio": 100, "subtotal": 100},
    ]
    try:
        analizar_descuentos(items, 1000)
    except DescuentoNoConciliado as exc:
        assert "diferencia sin justificar" in str(exc)
    else:
        raise AssertionError("Un cargo no identificado no justifica la diferencia")


def test_cargo_reconocido_no_se_compensa_si_la_base_ya_cuadra():
    items = [{"desc": "Cargo por Servicio Público", "cant": 1, "precio": 195, "subtotal": 195}]
    analisis = analizar_descuentos(items, 195)
    assert not analisis.descuentos and not analisis.items_exentos


if __name__ == "__main__":
    test_descuento_porcentaje_y_global_coexisten_sin_duplicarse()
    test_descuento_por_monto_se_infiere_solo_con_subtotal_y_cabecera()
    test_descuento_global_ya_representado_no_se_repite()
    test_descuento_global_en_subtotales_se_representa_una_sola_vez()
    test_descuento_exento_reduce_producto_y_concepto_exento()
    test_diferencia_sin_evidencia_detiene_el_envio_con_importes()
    test_diferencia_sin_evidencia_no_hace_post()
    test_porcentaje_decimal_y_multiples_descuentos_globales()
    test_parser_anota_importe_de_descuento_por_item()
    test_porcentaje_incompatible_no_inventa_descuento()
    test_porcentaje_historico_mal_escalado_se_valida_con_subtotal()
    test_cargos_del_sii_se_compensan_sin_cambiar_base_iva_ni_total()
    test_no_compensar_diferencia_sin_cargos_comprobados()
    test_cargo_reconocido_no_se_compensa_si_la_base_ya_cuadra()
    print("Regla de descuentos: OK")
