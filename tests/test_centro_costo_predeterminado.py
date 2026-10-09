"""La cuenta de compra determina si se usa Administración al 100 %."""
from dataclasses import replace
from datetime import date
from unittest.mock import patch

from app.config import settings
from app.finnegans.client import FinnegansClient, FinnegansMapeoError
from app.models import Documento
from app.routers.documents import _productos_con_centro_requerido


def _cliente_con_catalogo():
    cliente = object.__new__(FinnegansClient)
    cliente._centro_requerido_por_producto = {}
    cliente._centro_requerido_por_cuenta = {}
    llamadas = []

    def pedir(metodo, ruta):
        llamadas.append((metodo, ruta))
        datos = {
            "producto/CON_CENTRO": {"CuentaCodigoCompra": "GASTO"},
            "producto/SIN_CENTRO": {"CuentaCodigoCompra": "ACTIVO"},
            "producto/OTRO_CON_CENTRO": {"CuentaCodigoCompra": "GASTO"},
            "producto/DESCUENTO_CON_CENTRO": {"CuentaCodigoCompra": "GASTO"},
            "cuenta/GASTO": {"CuentaDimension": [{"DimensionCodigo": "DIMCTC"}]},
            "cuenta/ACTIVO": {"CuentaDimension": []},
        }
        return datos[ruta]

    cliente._pedir = pedir
    return cliente, llamadas


def test_lee_dimension_de_cuenta_y_reutiliza_consultas():
    cliente, llamadas = _cliente_con_catalogo()
    assert cliente.requiere_centro_costo("CON_CENTRO") is True
    assert cliente.requiere_centro_costo("SIN_CENTRO") is False
    assert cliente.requiere_centro_costo("OTRO_CON_CENTRO") is True
    assert cliente.requiere_centro_costo("CON_CENTRO") is True
    assert llamadas.count(("GET", "cuenta/GASTO")) == 1
    assert llamadas.count(("GET", "producto/CON_CENTRO")) == 1


def test_no_inventa_requisito_si_faltan_datos_de_cuenta():
    cliente, _ = _cliente_con_catalogo()
    cliente._pedir = lambda metodo, ruta: {"CuentaCodigoCompra": "GASTO"}
    try:
        cliente.requiere_centro_costo("CON_CENTRO")
    except FinnegansMapeoError as exc:
        assert "No se pudo determinar" in str(exc)
    else:
        assert False, "No debe enviar si la cuenta no informa sus dimensiones"


def test_resuelve_los_productos_del_documento_y_del_descuento():
    cliente, _ = _cliente_con_catalogo()
    documento = Documento(
        empresa_rut="00000000-0", tipo="33", folio=123,
        proveedor_rut="11111111-1", proveedor_nombre="Prueba",
        fecha=date(2025, 1, 2), neto=100, iva=19, exento=0, total=119,
        items=[
            {"desc": "A", "cant": 1, "precio": 60, "subtotal": 60},
            {"desc": "B", "cant": 1, "precio": 50, "subtotal": 50},
        ],
    )
    configuracion = replace(
        settings, finnegans_producto="CON_CENTRO",
        finnegans_producto_descuento_afecto="SIN_CENTRO",
    )
    with patch("app.routers.documents.settings", configuracion):
        assert _productos_con_centro_requerido(
            cliente, documento, {1: "SIN_CENTRO"}, 10
        ) == {"CON_CENTRO"}


def test_descuento_por_item_consulta_la_cuenta_de_su_producto():
    cliente, _ = _cliente_con_catalogo()
    documento = Documento(
        empresa_rut="00000000-0", tipo="33", folio=124,
        proveedor_rut="11111111-1", proveedor_nombre="Prueba",
        fecha=date(2025, 1, 2), neto=900, iva=171, exento=0, total=1071,
        items=[{"desc": "A", "cant": 1, "precio": 1000, "subtotal": 900}],
    )
    configuracion = replace(
        settings, finnegans_producto="SIN_CENTRO",
        finnegans_producto_descuento_afecto="DESCUENTO_CON_CENTRO",
    )
    with patch("app.routers.documents.settings", configuracion):
        assert _productos_con_centro_requerido(cliente, documento, {}, None) == {
            "DESCUENTO_CON_CENTRO"
        }


if __name__ == "__main__":
    test_lee_dimension_de_cuenta_y_reutiliza_consultas()
    test_no_inventa_requisito_si_faltan_datos_de_cuenta()
    test_resuelve_los_productos_del_documento_y_del_descuento()
    test_descuento_por_item_consulta_la_cuenta_de_su_producto()
    print("Centro de costo predeterminado: OK")
