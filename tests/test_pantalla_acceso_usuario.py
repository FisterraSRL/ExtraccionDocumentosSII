"""El encabezado y los certificados visibles siguen el rol de la sesión."""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_ingreso_y_selector_respetan_permisos():
    administrador = False

    def responder(ruta):
        nonlocal administrador
        path = urlparse(ruta.request.url).path
        if path == "/api/sesion":
            datos = {"autenticado": False, "configurada": True, "sin_login": False,
                     "administrador": False}
        elif path == "/api/login":
            datos = {"usuario": "usuario@example.invalid", "administrador": administrador}
        elif path == "/api/certificados-disponibles":
            datos = {"activo": "permitido", "perfiles": [
                {"id": "permitido", "nombre": "Certificado permitido", "vencido": False},
            ]}
        elif path == "/api/empresas":
            datos = []
        elif path == "/api/finnegans/parametros-envio":
            datos = {"workflow_codigo": "PRUEBA"}
        elif path == "/api/finnegans/empresas-permitidas":
            datos = {"perfil": "permitido", "empresas": []}
        elif path == "/api/version":
            datos = {"version": "1.0.0"}
        elif path == "/api/configuracion/sii":
            datos = {"activo": "permitido", "perfiles": []}
        else:
            datos = {}
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("https://fonts.googleapis.com/**", lambda ruta: ruta.fulfill(
            status=200, content_type="text/css", body="",
        ))
        pagina.route("http://test.local/", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8"),
        ))
        pagina.route("**/api/**", responder)
        pagina.goto("http://test.local/", wait_until="domcontentloaded")
        pagina.locator("#loginOverlay").wait_for(state="visible")
        pagina.locator("#mostrarLoginPassword").click()
        assert pagina.locator("#loginPassword").get_attribute("type") == "text"
        pagina.locator("#loginUsuario").fill("usuario@example.invalid")
        pagina.locator("#loginPassword").fill("clave-de-prueba")
        pagina.locator("#loginBtn").click()
        pagina.locator('#perfilDisponible option[value="permitido"]').wait_for(state="attached")
        assert pagina.locator("#loginPassword").get_attribute("type") == "password"
        assert pagina.locator("#usuariosLink").is_hidden()
        assert pagina.locator("#certificadosBtn").is_hidden()
        assert pagina.locator("#perfilDisponible option").count() == 2

        pagina.locator("#logoutBtn").click()
        pagina.locator("#loginOverlay").wait_for(state="visible")
        administrador = True
        pagina.locator("#loginUsuario").fill("admin@example.invalid")
        pagina.locator("#loginPassword").fill("clave-de-prueba")
        pagina.locator("#loginBtn").click()
        pagina.locator("#usuariosLink").wait_for(state="visible")
        pagina.locator("#certificadosBtn").click()
        pagina.get_by_text("Agregar otro certificado").click()
        pagina.locator("#mostrarCertificadoPassword").click()
        assert pagina.locator("#certificadoPassword").get_attribute("type") == "text"
        pagina.locator("#certificadosCerrarBtn").click()
        assert pagina.locator("#certificadoPassword").get_attribute("type") == "password"
        navegador.close()
