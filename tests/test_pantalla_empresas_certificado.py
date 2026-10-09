"""Elegir un certificado actualiza sus empresas antes de abrir la bandeja."""
import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_usar_certificado_actualiza_empresas_de_la_bandeja():
    activo = "perfil-a"
    actualizado = False
    acciones = []

    def responder(ruta):
        nonlocal activo, actualizado
        path = urlparse(ruta.request.url).path
        if path == "/api/configuracion/sii/seleccionar":
            activo = ruta.request.post_data_json["perfil"]
            acciones.append("seleccionar")
        elif path == "/api/empresas/asegurar":
            actualizado = True
            acciones.append("refrescar")
        if path == "/api/configuracion/sii":
            datos = {"activo": activo, "nombre_activo": activo, "perfiles": [
                {"id": "perfil-a", "nombre": "Perfil A", "rut": "00000000-0", "certificado_configurado": True},
                {"id": "perfil-b", "nombre": "Perfil B", "rut": "11111111-1", "certificado_configurado": True},
            ]}
        elif path == "/api/configuracion/sii/perfil-b":
            datos = {"nombre": "Perfil B", "rut": "11111111-1", "certificado_configurado": True}
        elif path == "/api/empresas":
            rut = "11111111-1" if actualizado else "00000000-0"
            datos = [{"rut": rut, "nombre_mostrado": "Empresa de prueba", "documentos": 0}]
        elif path == "/api/sesion":
            datos = {"autenticado": True, "configurada": True, "sin_login": True}
        elif path == "/api/version":
            datos = {"version": "1.0.5"}
        else:
            datos = {}
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("**/api/**", responder)
        pagina.goto("http://127.0.0.1:8000/configuracion")
        pagina.get_by_role("button", name="Perfil B").click()
        pagina.get_by_role("button", name="Usar este certificado").click()
        pagina.wait_for_url("http://127.0.0.1:8000/")
        pagina.locator('#empresaSelect option[value="11111111-1"]').wait_for(state="attached")
        assert acciones == ["seleccionar", "refrescar"]
        assert pagina.locator('#empresaSelect option[value="00000000-0"]').count() == 0
        assert pagina.locator("#perfilActivo").inner_text() == "SII: perfil-b"
        navegador.close()


if __name__ == "__main__":
    test_usar_certificado_actualiza_empresas_de_la_bandeja()
    print("Pantalla de empresas por certificado: OK")
