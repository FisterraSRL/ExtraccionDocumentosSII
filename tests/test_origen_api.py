"""El modo local no revela credenciales a páginas de otro origen."""
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

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
