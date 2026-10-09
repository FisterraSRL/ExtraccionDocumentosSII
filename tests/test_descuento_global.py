"""El descuento global del SII conserva los precios e iguala la base del comprobante."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.config import settings
from app.finnegans.client import FinnegansClient, FinnegansMapeoError
from app.models import Documento
from app.sii.pdf_dte import DetalleDTE, FIN_ITEMS, ItemDTE, _leer_totales, _verificar


def _detalle() -> DetalleDTE:
    cantidades = [2, 12, 1, 1, 1, 4]
    precios = [15789.92, 1134.45, 966.39, 966.39, 1252.1, 10663.87]
    subtotales = [31580, 13613, 966, 966, 1252, 42655]
    return DetalleDTE(
        items=[ItemDTE(desc=f"Producto de prueba {indice}", cant=cantidad,
                       precio=precio, subtotal=subtotal)
               for indice, (cantidad, precio, subtotal)
               in enumerate(zip(cantidades, precios, subtotales), 1)],
        totales={"neto": 81580, "exento": 0, "iva": 15500,
                 "total": 97080, "descuento_global": 9454},
    )


def _documento(detalle: DetalleDTE) -> Documento:
    return Documento(
        empresa_rut="00000000-0", tipo="33", folio=123456,
        proveedor_rut="11111111-1", proveedor_nombre="Proveedor de prueba",
        fecha=date(2025, 1, 2), neto=81580, iva=15500, exento=0,
        total=97080, items=[item.como_dict() for item in detalle.items],
    )


def _cliente_sin_bien_uso() -> FinnegansClient:
    cliente = object.__new__(FinnegansClient)
    cliente.requiere_bien_uso = lambda codigo: False
    return cliente


def test_pdf_reconoce_descuento_global_y_concilia_redondeos():
    texto = "@@Descuento Global $ 9.454"
    assert FIN_ITEMS.search(texto)
    totales = {}
    _leer_totales(texto, totales)
    assert totales == {"descuento_global": 9454}
    detalle = _detalle()
    assert sum(item.subtotal for item in detalle.items) == 91032
    assert sum(Decimal(str(item.cant)) * Decimal(str(item.precio))
               for item in detalle.items) == Decimal("91033.6")
    _verificar(detalle)
    assert detalle.cuadra
    assert detalle.observacion is None


def test_payload_mantiene_items_y_descuento_en_linea_separada():
    detalle = _detalle()
    documento = _documento(detalle)
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
        finnegans_producto_descuento_afecto="DESCUENTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion):
        payload = _cliente_sin_bien_uso().construir_payload(
            documento, empresa_codigo="EMPRESA_PRUEBA", descuento_global=9454,
            centros_por_indice={0: [{"codigo": "MANUAL", "porcentaje": 100}]},
            productos_con_centro_requerido={"PRODUCTO_PRUEBA"},
        )
    productos = payload["Productos"]
    assert [(p["Cantidad"], p["Precio"]) for p in productos[:6]] == [
        (item.cant, item.precio) for item in detalle.items
    ]
    assert productos[6]["ProductoCodigo"] == "DESCUENTO_PRUEBA"
    assert productos[6]["Cantidad"] == -1
    assert productos[6]["Precio"] == 9454
    assert productos[6]["ImporteExento"] == 0
    assert productos[6]["DimensionDistribucion"][0]["distribucionItems"] == [
        {"codigo": "5", "porcentaje": 100}
    ]
    assert productos[7]["ProductoCodigo"] == "PRODUCTO_PRUEBA"
    assert productos[7]["DimensionDistribucion"][0]["distribucionItems"] == [
        {"codigo": "5", "porcentaje": 100}
    ]
    assert productos[7]["Precio"] == 0.4
    assert payload["Conceptos"][0]["ConceptoImporteGravado"] == 81580
    assert sum(Decimal(str(p["Cantidad"])) * Decimal(str(p["Precio"]))
               for p in productos) + Decimal("15500") == Decimal("97080")


def test_sin_descuento_comprobado_no_se_compensa_solo():
    documento = _documento(_detalle())
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
        finnegans_producto_descuento_afecto="DESCUENTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion):
        for descuento in (None, 9000):
            try:
                _cliente_sin_bien_uso().construir_payload(
                    documento, empresa_codigo="EMPRESA_PRUEBA",
                    descuento_global=descuento,
                )
                assert False, "No debe inventar un ajuste a partir del saldo"
            except FinnegansMapeoError:
                pass


def test_todas_las_lineas_usan_centro_de_pruebas():
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
        finnegans_producto_descuento_afecto="DESCUENTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion):
        payload = _cliente_sin_bien_uso().construir_payload(
            _documento(_detalle()), empresa_codigo="EMPRESA_PRUEBA", descuento_global=9454,
            productos_con_centro_requerido={"DESCUENTO_PRUEBA"},
        )
    for linea in payload["Productos"]:
        assert linea["DimensionDistribucion"][0]["distribucionItems"] == [
            {"codigo": "5", "porcentaje": 100}
        ]


if __name__ == "__main__":
    test_pdf_reconoce_descuento_global_y_concilia_redondeos()
    test_payload_mantiene_items_y_descuento_en_linea_separada()
    test_sin_descuento_comprobado_no_se_compensa_solo()
    test_todas_las_lineas_usan_centro_de_pruebas()
    print("Descuento global: OK")
