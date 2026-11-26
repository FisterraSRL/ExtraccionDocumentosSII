"""El administrador asigna certificado y empresa desde una sola pantalla."""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_crear_y_editar_usuario_con_permisos():
    usuarios = []
    guardados = []

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        status = 200
        if path == "/api/administracion/usuarios":
            if ruta.request.method == "POST":
                datos = ruta.request.post_data_json
                guardados.append(datos)
                usuario = {**datos, "id": 1, "perfil_activo": "perfil-uno"}
                usuario.pop("password")
                usuarios.append(usuario)
                status = 201
                respuesta = usuario
            else:
                respuesta = usuarios
        elif path == "/api/certificados-disponibles":
            respuesta = {"activo": "perfil-uno", "perfiles": [
                {"id": "perfil-uno", "nombre": "Certificado de prueba"},
            ]}
        elif path == "/api/administracion/certificados/perfil-uno/empresas":
            respuesta = [
                {"codigo": "EMPRESA-A", "nombre": "Empresa A"},
                {"codigo": "EMPRESA-B", "nombre": "Empresa B"},
            ]
        elif path == "/api/administracion/certificados/perfil-uno/empresas-sii":
            respuesta = {"consultado": True, "empresas": [
                {"rut": "11111111-1", "nombre": "Receptora A"},
                {"rut": "22222222-2", "nombre": "Receptora B"},
            ]}
        else:
            respuesta = {}
        ruta.fulfill(status=status, content_type="application/json", body=json.dumps(respuesta))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("http://test.local/usuarios", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(Path(__file__).parents[1] / "app" / "static" / "usuarios.html").read_text(encoding="utf-8"),
        ))
        pagina.route("**/api/**", responder)
        pagina.goto("http://test.local/usuarios")
        pagina.locator("#email").fill("tester@example.invalid")
        pagina.locator("#password").fill("clave-de-prueba-regular")
        pagina.locator("#mostrarPassword").click()
        assert pagina.locator("#password").get_attribute("type") == "text"
        pagina.locator("#mostrarPassword").click()
        assert pagina.locator("#password").get_attribute("type") == "password"
        pagina.locator(".perfil-check").check()
        pagina.locator('.empresas-sii input[value="11111111-1"]').check()
        pagina.locator('.empresas-finnegans input[value="EMPRESA-A"]').check()
        pagina.get_by_role("searchbox", name="Buscar en Sucursales Finnegans para el envío").fill("Empresa B")
        assert pagina.locator('.empresas-finnegans input[value="EMPRESA-A"]').is_hidden()
        assert pagina.locator('.empresas-finnegans input[value="EMPRESA-B"]').is_visible()
        pagina.locator("#guardar").click()
        pagina.get_by_text("Usuario guardado.").wait_for()
        assert guardados == [{
            "email": "tester@example.invalid", "password": "clave-de-prueba-regular",
            "rol": "usuario", "activo": True,
            "certificados": [{"perfil_id": "perfil-uno", "empresas_sii": ["11111111-1"],
                              "empresas": ["EMPRESA-A"]}],
        }]
        assert pagina.locator(".item.active").count() == 1
        assert pagina.locator('.empresas-sii input[value="11111111-1"]').is_checked()
        assert not pagina.locator('.empresas-sii input[value="22222222-2"]').is_checked()
        assert pagina.locator('.empresas-finnegans input[value="EMPRESA-A"]').is_checked()
        assert not pagina.locator('.empresas-finnegans input[value="EMPRESA-B"]').is_checked()
        navegador.close()
