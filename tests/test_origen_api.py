"""El modo local no revela credenciales a páginas de otro origen."""
import os
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException, Response

from app import auth
from app.auth import requiere_sesion


def test_solo_el_origen_del_portal_puede_usar_la_api_protegida():
    def solicitud(**headers):
        return SimpleNamespace(headers=headers)

    with patch("app.auth.sesion_valida", return_value=None), patch(
        "app.auth.sesion_automatica", return_value="local",
    ):
        assert requiere_sesion(solicitud(host="localhost:8000"), None) == "local"
        assert requiere_sesion(solicitud(host="localhost:8000", origin="http://localhost:8000"), None) == "local"
        for origen in ("https://otro.example", "null", "http://127.0.0.1:8000"):
            try:
                requiere_sesion(solicitud(host="localhost:8000", origin=origen), None)
            except HTTPException as exc:
                assert exc.status_code == 403
            else:
                raise AssertionError("Otro origen no debe acceder a credenciales locales")
        try:
            requiere_sesion(solicitud(host="localhost:8000", **{"sec-fetch-site": "cross-site"}), None)
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("Una solicitud cross-site no debe acceder al portal")


if __name__ == "__main__":
    test_solo_el_origen_del_portal_puede_usar_la_api_protegida()
    print("Origen de API protegida: OK")


def test_gestion_remota_exige_host_configurado_y_cookie_firmada():
    origen = "https://sii.example.test"
    host = "sii.example.test"

    def solicitud(**headers):
        return SimpleNamespace(headers={"host": host, **headers}, client=SimpleNamespace(host="127.0.0.1"))

    with patch.dict(os.environ, {"PORTAL_ADMIN_ORIGIN": origen, "PORTAL_USUARIO": "test",
                                 "PORTAL_PASSWORD": "test", "SECRET_KEY": "test-signing-key"}), patch.object(
        auth, "sesion_automatica", return_value="local"
    ):
        response = Response()
        auth.emitir_cookie(response, "test")
        cookie = SimpleCookie(response.headers["set-cookie"])[auth.COOKIE].value
        assert auth.requiere_sesion_gestion(solicitud(), cookie) == "test"
        assert auth.requiere_sesion_gestion(solicitud(origin=origen), cookie) == "test"

        with TestCase().assertRaises(HTTPException) as no_autorizado:
            auth.requiere_sesion_gestion(solicitud(), None)
        assert no_autorizado.exception.status_code == 401

        for headers in ({"host": "otro.example.test"}, {"origin": "http://sii.example.test"},
                        {"origin": "https://otro.example.test"}, {"sec-fetch-site": "cross-site"}):
            with TestCase().assertRaises(HTTPException) as origen_incorrecto:
                auth.requiere_sesion_gestion(solicitud(**headers), cookie)
            assert origen_incorrecto.exception.status_code == 403


def test_gestion_por_defecto_conserva_el_acceso_local():
    local = SimpleNamespace(headers={"host": "localhost:8000"}, client=SimpleNamespace(host="127.0.0.1"))
    remoto = SimpleNamespace(headers={"host": "localhost:8000"}, client=SimpleNamespace(host="192.0.2.10"))
    with patch.dict(os.environ, {"PORTAL_ADMIN_ORIGIN": ""}), patch.object(
        auth, "requiere_sesion", return_value="usuario"
    ):
        assert auth.requiere_sesion_gestion(local, None) == "usuario"
        with TestCase().assertRaises(HTTPException) as no_disponible:
            auth.requiere_sesion_gestion(remoto, None)
        assert no_disponible.exception.status_code == 404
