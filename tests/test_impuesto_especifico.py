"""El impuesto específico impreso por el SII se reconcilia antes del envío."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from unittest.mock import patch

from fastapi import Response

from app.config import settings
from app.finnegans.client import FinnegansClient, FinnegansMapeoError, ajuste_importe_exento
from app.models import AsociacionItem, Documento, ProductoFinnegans
from app.productos import _firma
from app.routers.documents import _codigos_seleccionados, previsualizar_documento


def _documento(*, total=42150, items=None, neto=32407, iva=6157, exento=0) -> Documento:
    return Documento(
        empresa_rut="00000000-0", tipo="33", folio=123456,
        proveedor_rut="11111111-1", proveedor_nombre="Proveedor de prueba",
        fecha=date(2025, 1, 2), neto=neto, iva=iva, exento=exento,
        total=total, items=items if items is not None else [{
            "desc": "Combustible IE Base: 432.9060 - IE Variable: -299.2463",
            "cant": 26.83, "precio": 1207.86, "subtotal": 32407,
        }],
    )


def _payload(documento: Documento, codigos_por_indice: dict[int, str] | None = None,
             centros_por_indice: dict[int, list[dict]] | None = None) -> dict:
    configuracion_prueba = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion_prueba):
        return object.__new__(FinnegansClient).construir_payload(
            documento, empresa_codigo="EMPRESA_PRUEBA",
            codigos_por_indice=codigos_por_indice,
            centros_por_indice=centros_por_indice,
        )


def test_item_del_pdf_conserva_cantidad_y_precio():
    documento = _documento()
    lineas = object.__new__(FinnegansClient)._productos(documento, 32407, 6157, 0)
    assert len(lineas) == 1
    assert lineas[0]["Cantidad"] == 26.83
    assert lineas[0]["Precio"] == 1207.86
    assert lineas[0]["ImporteExento"] == 0


def test_payload_conserva_el_exento_del_sii():
    documento = _documento()
    payload = _payload(documento)
    assert len(payload["Productos"]) == 3
    assert payload["Productos"][0]["ImporteExento"] == 0
    assert payload["Productos"][1]["Precio"] == 0.1162
    assert payload["Productos"][1]["ImporteExento"] == 0
    assert payload["Productos"][2]["Precio"] == 3586
    assert payload["Productos"][2]["ImporteExento"] == 3586
    assert payload["Conceptos"][0]["ImporteEditable"] is True
    assert payload["Conceptos"][1]["ImporteEditable"] is True
    assert payload["Conceptos"][1]["ConceptoImporteGravado"] == 3586
    assert payload["ImporteTotalControl"] == 42150
    assert sum(
        Decimal(str(linea["Cantidad"])) * Decimal(str(linea["Precio"]))
        for linea in payload["Productos"]
    ) + sum(concepto["ConceptoImporte"] for concepto in payload["Conceptos"]) == 42150
    assert ajuste_importe_exento(documento, payload) == 3586
    json.dumps(payload)


def test_seleccion_de_producto_llega_a_la_vista_previa():
    documento = _documento()
    documento.id = 1
    asociacion = AsociacionItem(
        perfil_id="perfil_prueba", documento_id=1, indice=0,
        descripcion_firma=_firma(documento.items[0]),
        producto_codigo="COMBUSTIBLE_PRUEBA", origen="manual",
    )
    producto = ProductoFinnegans(
        perfil_id="perfil_prueba", codigo="COMBUSTIBLE_PRUEBA", nombre="Combustible",
        disponible=True,
    )

    class BasePrueba:
        def execute(self, consulta):
            return self

        def scalars(self):
            return self

        def all(self):
            return [asociacion]

        def get(self, modelo, clave):
            assert modelo is ProductoFinnegans and clave == ("perfil_prueba", "COMBUSTIBLE_PRUEBA")
            return producto

    with patch("app.routers.documents.settings", replace(settings, sii_perfil="perfil_prueba")):
        codigos = _codigos_seleccionados(BasePrueba(), documento)
    payload = _payload(documento, codigos)
    assert codigos == {0: "COMBUSTIBLE_PRUEBA"}
    assert [linea["ProductoCodigo"] for linea in payload["Productos"]] == [
        "COMBUSTIBLE_PRUEBA", "COMBUSTIBLE_PRUEBA", "COMBUSTIBLE_PRUEBA",
    ]


def test_centro_de_costo_cubre_tambien_las_lineas_de_ajuste():
    distribucion = {0: [{"codigo": "CC_PRUEBA", "porcentaje": 100}]}
    payload = _payload(_documento(), {0: "COMBUSTIBLE_PRUEBA"}, distribucion)
    esperado = [{
        "dimensionCodigo": "DIMCTC", "distribucionCodigo": "", "tipoCalculo": "2",
        "distribucionItems": [{"codigo": "CC_PRUEBA", "porcentaje": 100}],
    }]
    assert [linea["DimensionDistribucion"] for linea in payload["Productos"]] == [
        esperado, esperado, esperado,
    ]
    assert payload["ImporteTotalControl"] == 42150
    json.dumps(payload)


def test_diferencia_positiva_completa_importe_exento_sin_cambiar_precio():
    documento = _documento(total=43000)
    payload = _payload(documento)
    assert payload["Productos"][0]["Cantidad"] == 26.83
    assert payload["Productos"][0]["Precio"] == 1207.86
    assert [linea["ImporteExento"] for linea in payload["Productos"]] == [0, 0, 3586, 850]
    assert payload["ImporteTotalControl"] == 43000
    assert ajuste_importe_exento(documento, payload) == 4436


def test_ie_conserva_la_linea_correcta_y_el_redondeo():
    items = [
        {"desc": "Otro producto", "cant": 1, "precio": 100, "subtotal": 100},
        {"desc": "Combustible IE Base: 432.9060 - IE Variable: -299.2463",
         "cant": 26.83, "precio": 1204.13, "subtotal": 32307},
    ]
    lineas = object.__new__(FinnegansClient)._productos(_documento(items=items), 32407, 6157, 0)
    assert [linea["ImporteExento"] for linea in lineas] == [0, 0]
    assert [linea["Cantidad"] for linea in lineas] == [1, 26.83]
    assert [linea["Precio"] for linea in lineas] == [100, 1204.13]


def test_sin_ie_no_cambia_las_lineas():
    documento = _documento(total=38564, items=[
        {"desc": "Producto común", "cant": 1, "precio": 32407, "subtotal": 32407},
    ])
    lineas = object.__new__(FinnegansClient)._productos(documento, 32407, 6157, 0)
    assert lineas[0]["ImporteExento"] == 0


def test_exento_puro_se_suma_una_vez():
    documento = _documento(total=195, neto=0, iva=0, exento=195, items=[
        {"desc": "Cargo por servicio público", "cant": 1, "precio": 195, "subtotal": 195},
    ])
    lineas = object.__new__(FinnegansClient)._productos(documento, 0, 0, 195)
    assert len(lineas) == 1
    assert lineas[0]["Precio"] == 195
    assert lineas[0]["ImporteExento"] == 195
    payload = _payload(documento)
    assert len(payload["Productos"]) == 1
    assert payload["Productos"][0]["ImporteExento"] == payload["ImporteTotalControl"]
    assert ajuste_importe_exento(documento, payload) == 0


def test_neto_y_exento_sin_marca_por_item_pide_revision():
    documento = _documento(total=219, neto=100, iva=19, exento=100, items=[
        {"desc": "Dos conceptos sin marca de afectación", "cant": 1,
         "precio": 200, "subtotal": 200},
    ])
    try:
        object.__new__(FinnegansClient)._productos(documento, 100, 19, 100)
    except FinnegansMapeoError as exc:
        assert "qué ítems son exentos" in str(exc)
    else:
        raise AssertionError("Se modificaron los datos de un ítem mixto")


def test_precio_pdf_con_descuento_no_se_reescribe():
    documento = _documento(total=38564, items=[
        {"desc": "Producto con descuento", "cant": 2, "precio": 20000, "subtotal": 32407},
    ])
    try:
        _payload(documento)
    except FinnegansMapeoError as exc:
        assert "precios originales superan la base" in str(exc)
    else:
        raise AssertionError("Se compensó una diferencia negativa con exento")
    assert documento.items[0]["cant"] == 2
    assert documento.items[0]["precio"] == 20000


def test_diferencia_positiva_sin_ie_usa_importe_exento():
    documento = _documento(total=39000, items=[
        {"desc": "Producto común", "cant": 1, "precio": 32407, "subtotal": 32407},
    ])
    payload = _payload(documento)
    assert [linea["ImporteExento"] for linea in payload["Productos"]] == [0, 436]
    assert payload["Productos"][1]["Precio"] == 436
    assert ajuste_importe_exento(documento, payload) == 436


def test_ajuste_va_al_item_con_brecha():
    documento = _documento(total=357, neto=300, iva=57, items=[
        {"desc": "Producto con diferencia", "cant": 1, "precio": 80, "subtotal": 100},
        {"desc": "Otro producto", "cant": 1, "precio": 200, "subtotal": 200},
    ])
    payload = _payload(documento)
    assert [item["ImporteExento"] for item in payload["Productos"]] == [0, 0, 0]
    assert [item["Precio"] for item in payload["Productos"]] == [80, 200, 20]


def test_vista_previa_informa_el_ajuste_sin_alterar_el_json():
    documento = _documento(total=39000, items=[
        {"desc": "Producto común", "cant": 1, "precio": 32407, "subtotal": 32407},
    ])
    respuesta = Response()

    class BasePrueba:
        def get(self, modelo, identificador):
            assert modelo is Documento and identificador == 1
            return documento

    configuracion_prueba = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion_prueba), \
         patch("app.routers.documents._cliente_finnegans", return_value=object.__new__(FinnegansClient)), \
         patch("app.routers.documents._codigos_seleccionados", return_value={}), \
         patch("app.routers.documents._centros_seleccionados", return_value={}):
        payload = previsualizar_documento(1, respuesta, BasePrueba())
    assert respuesta.headers["X-Ajuste-Importe-Exento"] == "436"
    assert respuesta.headers["Cache-Control"] == "no-store"
    assert "X-Ajuste-Importe-Exento" not in payload


if __name__ == "__main__":
    test_item_del_pdf_conserva_cantidad_y_precio()
    test_payload_conserva_el_exento_del_sii()
    test_seleccion_de_producto_llega_a_la_vista_previa()
    test_centro_de_costo_cubre_tambien_las_lineas_de_ajuste()
    test_diferencia_positiva_completa_importe_exento_sin_cambiar_precio()
    test_ie_conserva_la_linea_correcta_y_el_redondeo()
    test_sin_ie_no_cambia_las_lineas()
    test_exento_puro_se_suma_una_vez()
    test_neto_y_exento_sin_marca_por_item_pide_revision()
    test_precio_pdf_con_descuento_no_se_reescribe()
    test_diferencia_positiva_sin_ie_usa_importe_exento()
    test_ajuste_va_al_item_con_brecha()
    test_vista_previa_informa_el_ajuste_sin_alterar_el_json()
    print("Cálculo y conciliación del impuesto específico: OK")
