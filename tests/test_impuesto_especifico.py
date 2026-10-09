"""El impuesto específico impreso por el SII se reconcilia antes del envío."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from unittest.mock import patch

from fastapi import Response

from app.config import settings
from app.descuentos import DescuentoNoConciliado, analizar_descuentos
from app.finnegans.client import (
    FinnegansClient, FinnegansMapeoError, _importes_ie, ajuste_importe_exento,
)
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
             centros_por_indice: dict[int, list[dict]] | None = None,
             productos_con_centro_requerido: set[str] | None = None) -> dict:
    configuracion_prueba = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
        finnegans_producto_descuento_afecto="DESCUENTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion_prueba):
        cliente = object.__new__(FinnegansClient)
        cliente.requiere_bien_uso = lambda codigo: False
        return cliente.construir_payload(
            documento, empresa_codigo="EMPRESA_PRUEBA",
            codigos_por_indice=codigos_por_indice,
            centros_por_indice=centros_por_indice,
            productos_con_centro_requerido=productos_con_centro_requerido,
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


def test_impuesto_especifico_negativo_impreso_como_montos_concilia_total():
    documento = _documento(
        total=2836000, neto=2566123, iva=487563,
        items=[{
            "desc": (
                "Petróleo Diesel Impto.Especifico Base 215.164.- "
                "Impto.Especifico Variable ( 432.850).-"
            ),
            "cant": 2000, "precio": 1283.06, "subtotal": 2566123,
        }],
    )
    assert _importes_ie(documento, documento.items) == [-217686]
    payload = _payload(documento)
    assert payload["Productos"][0]["Cantidad"] == 2000
    assert payload["Productos"][0]["Precio"] == 1283.06
    assert any(
        linea["Cantidad"] == -1 and linea["Precio"] == 217686
        and linea["ImporteExento"] == -217686
        for linea in payload["Productos"]
    )
    assert payload["Conceptos"][1]["ConceptoImporteGravado"] == -217686
    assert payload["ImporteTotalControl"] == 2836000
    assert sum(
        Decimal(str(linea["Cantidad"])) * Decimal(str(linea["Precio"]))
        for linea in payload["Productos"]
    ) + sum(Decimal(str(c["ConceptoImporte"])) for c in payload["Conceptos"]) == 2836000


def test_impuesto_especifico_negativo_no_oculta_otra_diferencia():
    documento = _documento(
        total=2835999, neto=2566123, iva=487563,
        items=[{
            "desc": "Diesel Impto.Especifico Base 215.164.- Impto.Especifico Variable (432.850).-",
            "cant": 2000, "precio": 1283.06, "subtotal": 2566123,
        }],
    )
    try:
        _payload(documento)
    except FinnegansMapeoError as exc:
        assert "impuesto específico negativo" in str(exc)
        assert "$-217687" in str(exc)
    else:
        raise AssertionError("No se debe completar arbitrariamente otra diferencia")


def test_redondeo_unitario_no_permite_una_diferencia_mayor():
    try:
        analizar_descuentos(
            [{"desc": "Diesel", "cant": 2000, "precio": 1283.06, "subtotal": 2566135}],
            2566135,
        )
    except DescuentoNoConciliado as exc:
        assert "diferencia sin justificar" in str(exc)
    else:
        raise AssertionError("Un importe fuera del margen de redondeo no puede compensarse")


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
        "distribucionItems": [{"codigo": "5", "porcentaje": 100}],
    }]
    assert [linea["DimensionDistribucion"] for linea in payload["Productos"]] == [
        esperado, esperado, esperado,
    ]
    assert payload["ImporteTotalControl"] == 42150
    json.dumps(payload)


def test_centro_de_pruebas_tambien_para_producto_que_no_lo_requiere():
    documento = _documento(items=[
        {"desc": "Gasto A", "cant": 1, "precio": 16000, "subtotal": 16000},
        {"desc": "Gasto B", "cant": 1, "precio": 16407, "subtotal": 16407},
    ], total=38564)
    payload = _payload(
        documento, {0: "REQUIERE_CENTRO", 1: "SIN_CENTRO"},
        productos_con_centro_requerido={"REQUIERE_CENTRO"},
    )
    assert payload["Productos"][0]["DimensionDistribucion"][0]["distribucionItems"] == [
        {"codigo": "5", "porcentaje": 100}
    ]
    assert payload["Productos"][1]["DimensionDistribucion"][0]["distribucionItems"] == [
        {"codigo": "5", "porcentaje": 100}
    ]


def test_centro_de_pruebas_prevalece_sobre_manual():
    payload = _payload(
        _documento(), {0: "REQUIERE_CENTRO"},
        {0: [{"codigo": "OTRO", "porcentaje": 100}]},
        {"REQUIERE_CENTRO"},
    )
    assert all(
        linea["DimensionDistribucion"][0]["distribucionItems"] == [
            {"codigo": "5", "porcentaje": 100}
        ] for linea in payload["Productos"]
    )


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
    payload = _payload(documento)
    assert payload["Productos"][0]["Cantidad"] == 2
    assert payload["Productos"][0]["Precio"] == 20000
    assert payload["Productos"][1]["ProductoCodigo"] == payload["Productos"][0]["ProductoCodigo"]
    assert payload["Productos"][1]["Cantidad"] == -1
    assert payload["Productos"][1]["Precio"] == 7593
    assert payload["Productos"][1]["ImporteExento"] == 0
    assert payload["ImporteTotalControl"] == 38564
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


def test_diferencia_positiva_sin_causa_no_se_compensa():
    documento = _documento(total=357, neto=300, iva=57, items=[
        {"desc": "Producto con diferencia", "cant": 1, "precio": 80, "subtotal": 100},
        {"desc": "Otro producto", "cant": 1, "precio": 200, "subtotal": 200},
    ])
    try:
        _payload(documento)
    except FinnegansMapeoError as exc:
        assert "diferencia sin justificar" in str(exc)
        assert "$-20" in str(exc)
    else:
        raise AssertionError("Una brecha positiva sin causa no puede imputarse sola")


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
    cliente_prueba = object.__new__(FinnegansClient)
    cliente_prueba.perfil_id = "perfil-prueba"
    cliente_prueba.empresa_para_rut = lambda rut: "EMPRESA_PRUEBA"
    cliente_prueba.requiere_bien_uso = lambda codigo: False
    with patch("app.finnegans.client.settings", configuracion_prueba), \
         patch("app.routers.documents._cliente_finnegans", return_value=cliente_prueba), \
         patch("app.routers.documents._codigos_seleccionados", return_value={}), \
         patch("app.routers.documents._centros_seleccionados", return_value={}), \
         patch("app.routers.documents._productos_con_centro_requerido", return_value=set()), \
         patch("app.routers.documents._descuento_global", return_value=None):
        payload = previsualizar_documento(1, respuesta, BasePrueba())
    assert respuesta.headers["X-Ajuste-Importe-Exento"] == "436"
    assert respuesta.headers["Cache-Control"] == "no-store"
    assert "X-Ajuste-Importe-Exento" not in payload


if __name__ == "__main__":
    test_item_del_pdf_conserva_cantidad_y_precio()
    test_payload_conserva_el_exento_del_sii()
    test_impuesto_especifico_negativo_impreso_como_montos_concilia_total()
    test_impuesto_especifico_negativo_no_oculta_otra_diferencia()
    test_redondeo_unitario_no_permite_una_diferencia_mayor()
    test_seleccion_de_producto_llega_a_la_vista_previa()
    test_centro_de_costo_cubre_tambien_las_lineas_de_ajuste()
    test_centro_de_pruebas_tambien_para_producto_que_no_lo_requiere()
    test_centro_de_pruebas_prevalece_sobre_manual()
    test_diferencia_positiva_completa_importe_exento_sin_cambiar_precio()
    test_ie_conserva_la_linea_correcta_y_el_redondeo()
    test_sin_ie_no_cambia_las_lineas()
    test_exento_puro_se_suma_una_vez()
    test_neto_y_exento_sin_marca_por_item_pide_revision()
    test_precio_pdf_con_descuento_no_se_reescribe()
    test_diferencia_positiva_sin_ie_usa_importe_exento()
    test_diferencia_positiva_sin_causa_no_se_compensa()
    test_vista_previa_informa_el_ajuste_sin_alterar_el_json()
    print("Cálculo y conciliación del impuesto específico: OK")
