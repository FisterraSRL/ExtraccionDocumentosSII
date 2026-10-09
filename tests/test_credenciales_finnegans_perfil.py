"""Las consultas y los envíos Finnegans quedan vinculados al certificado elegido."""
from dataclasses import replace
from datetime import date
from urllib.parse import parse_qs
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import configuracion_sii
from app.config import settings
from app.db import Base
from app.finnegans.client import FinnegansClient
from app.models import Documento, EnviarLote
from app.routers.documents import _cliente_finnegans, enviar_seleccion


class _RespuestaToken:
    def __init__(self, token):
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.token.encode("utf-8")


def test_credenciales_salen_del_perfil_activo_y_detectan_cambio():
    perfiles = {"activo": "perfil-a", "perfiles": {
        "perfil-a": {"client_id": "id_a", "client_secret": "clave_a"},
        "perfil-b": {"client_id": "id_b", "client_secret": "clave_b"},
    }}
    with patch.object(configuracion_sii, "_leer_perfiles", return_value=perfiles):
        assert configuracion_sii.credenciales_finnegans_activas("perfil-a") == (
            "perfil-a", "id_a", "clave_a",
        )
        try:
            configuracion_sii.credenciales_finnegans_activas("perfil-b")
        except configuracion_sii.PerfilActivoCambiadoError:
            pass
        else:
            raise AssertionError("No debe usar el secreto de otro certificado")

    with patch.object(
        configuracion_sii, "credenciales_finnegans_activas",
        return_value=("perfil-a", "id_a", "clave_a"),
    ) as credenciales:
        cliente = _cliente_finnegans("perfil-a")
        credenciales.assert_called_once_with("perfil-a")
        assert (cliente.perfil_id, cliente._client_id, cliente._client_secret) == (
            "perfil-a", "id_a", "clave_a",
        )
    with patch.object(
        configuracion_sii, "credenciales_finnegans_activas",
        side_effect=configuracion_sii.PerfilActivoCambiadoError("Perfil cambiado"),
    ):
        try:
            _cliente_finnegans("perfil-a")
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("El lote no puede enviarse bajo otro perfil")


def test_token_de_cada_perfil_usa_su_id_y_secreto_sin_url():
    solicitudes = []

    def urlopen(solicitud, timeout):
        solicitudes.append(solicitud)
        return _RespuestaToken("token_perfil_a_largo" if len(solicitudes) == 1 else "token_perfil_b_largo")

    with patch("app.finnegans.client.urllib.request.urlopen", side_effect=urlopen):
        a = FinnegansClient("perfil-a", "id_a", "clave_a")
        b = FinnegansClient("perfil-b", "id_b", "clave_b")
        assert a._autenticar() != b._autenticar()
        assert a._autenticar() == "token_perfil_a_largo"
    assert len(solicitudes) == 2
    for solicitud, client_id, secret in zip(solicitudes, ("id_a", "id_b"), ("clave_a", "clave_b")):
        assert solicitud.get_method() == "POST"
        assert solicitud.full_url.endswith("/oauth/token")
        assert secret not in solicitud.full_url
        assert parse_qs(solicitud.data.decode("utf-8"))["client_id"] == [client_id]
        assert solicitud.get_header("Authorization") == "Basic " + secret


def test_empresa_destino_de_pruebas_es_igual_en_vista_previa_y_envio():
    documento = Documento(
        empresa_rut="00000000-0", tipo="33", folio=123,
        proveedor_rut="11111111-1", proveedor_nombre="Prueba",
        fecha=date(2025, 1, 2), neto=1000, iva=190, exento=0, total=1190,
    )
    cliente = FinnegansClient("perfil-prueba", "id-prueba", "clave-prueba")
    consultas = []
    cliente.empresa_para_rut = lambda rut: consultas.append(rut) or "EMPRESA_PERFIL"
    enviados = []

    def pedir(metodo, ruta, datos=None):
        if (metodo, ruta) == ("GET", "producto/PRODUCTO_PRUEBA"):
            return {"CuentaCodigoCompra": "CUENTA_PRUEBA"}
        if (metodo, ruta) == ("GET", "cuenta/CUENTA_PRUEBA"):
            return {"CuentaDimension": []}
        enviados.append((metodo, ruta, datos))
        return {"Codigo": "123"}

    cliente._pedir = pedir
    configuracion = replace(
        settings, finnegans_workflow="FLUJO_PRUEBA", finnegans_producto="PRODUCTO_PRUEBA",
    )
    with patch("app.finnegans.client.settings", configuracion):
        payload = cliente.construir_payload(documento)
        resultado = cliente.send_document(documento)
    assert payload["EmpresaCodigo"] == "PRUEBA39"
    assert resultado.ok
    assert len(enviados) == 1
    assert enviados[0][0:2] == ("POST", "facturaCompra")
    assert enviados[0][2]["EmpresaCodigo"] == "PRUEBA39"
    assert consultas == []


def test_la_misma_empresa_puede_tener_codigo_distinto_en_dos_cuentas():
    for perfil, codigo in (("perfil-a", "EMPRESA_A"), ("perfil-b", "EMPRESA_B")):
        cliente = FinnegansClient(perfil, "id_" + perfil, "clave_" + perfil)
        cliente.catalogo = lambda ruta, codigo=codigo: [{"codigo": codigo}]
        cliente._autenticar = lambda: "token-prueba"
        cliente._pedir = lambda metodo, ruta, timeout=30: {
            "Activo": True, "NumeroIdentificacion": "00000000-0",
        }
        assert cliente.empresa_para_rut("00000000-0") == codigo


def test_empresa_se_resuelve_una_vez_por_credenciales_y_conserva_el_codigo_menor():
    detalles = {
        "empresaChile/1": {"Activo": False, "NumeroIdentificacion": "00000000-0"},
        "empresaChile/2": {"Activo": True, "NumeroIdentificacion": "00000000-0"},
        "empresaChile/9": {"Activo": True, "NumeroIdentificacion": "00000000-0"},
    }
    consultas = []
    primero = FinnegansClient("perfil-cache-prueba", "id-a", "clave-a")
    primero.catalogo = lambda _: [{"codigo": codigo} for codigo in ("9", "1", "2")]
    primero._autenticar = lambda: "token-prueba"
    primero._pedir = lambda metodo, ruta, timeout=30: consultas.append(ruta) or detalles[ruta]
    assert primero.empresa_para_rut("00000000-0") == "2"
    assert len(consultas) == 3

    segundo = FinnegansClient("perfil-cache-prueba", "id-a", "clave-a")
    segundo.catalogo = lambda _: (_ for _ in ()).throw(AssertionError("Reconsultó el catálogo"))
    assert segundo.empresa_para_rut("00000000-0") == "2"

    otras_credenciales = FinnegansClient("perfil-cache-prueba", "id-b", "clave-b")
    otras_credenciales.catalogo = lambda _: [{"codigo": "OTRA"}]
    otras_credenciales._autenticar = lambda: "token-prueba"
    otras_credenciales._pedir = lambda metodo, ruta, timeout=30: {
        "Activo": True, "NumeroIdentificacion": "00000000-0"
    }
    assert otras_credenciales.empresa_para_rut("00000000-0") == "OTRA"


def test_un_cambio_de_perfil_corta_el_lote_antes_de_contactar_erp():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor, autoflush=False) as db:
        documento = Documento(
            empresa_rut="00000000-0", tipo="33", folio=123,
            proveedor_rut="11111111-1", proveedor_nombre="Prueba",
            fecha=date(2025, 1, 2), neto=1000, iva=190, exento=0, total=1190,
        )
        db.add(documento)
        db.commit()
        with patch(
            "app.routers.documents._cliente_finnegans",
            side_effect=HTTPException(status_code=409, detail="Perfil cambiado"),
        ) as crear_cliente:
            try:
                enviar_seleccion(EnviarLote(ids=[documento.id], perfil_esperado="perfil-a"), db)
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("No debe iniciar el lote tras cambiar de certificado")
            crear_cliente.assert_called_once_with("perfil-a")


if __name__ == "__main__":
    test_credenciales_salen_del_perfil_activo_y_detectan_cambio()
    test_token_de_cada_perfil_usa_su_id_y_secreto_sin_url()
    test_empresa_destino_de_pruebas_es_igual_en_vista_previa_y_envio()
    test_la_misma_empresa_puede_tener_codigo_distinto_en_dos_cuentas()
    test_empresa_se_resuelve_una_vez_por_credenciales_y_conserva_el_codigo_menor()
    test_un_cambio_de_perfil_corta_el_lote_antes_de_contactar_erp()
    print("Credenciales Finnegans por perfil: OK")
