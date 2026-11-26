"""La pantalla informa el alcance y exige confirmar la limpieza."""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_eliminar_certificado_muestra_facturas_y_permuta_la_lista():
    perfiles = [
        {"id": "perfil-a", "nombre": "Perfil A", "rut": "11111111-1"},
        {"id": "perfil-b", "nombre": "Perfil B", "rut": "22222222-2"},
    ]
    eliminaciones = []

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        metodo = ruta.request.method
        if path == "/api/configuracion/sii" and metodo == "GET":
            datos = {"activo": perfiles[0]["id"] if perfiles else None, "perfiles": perfiles}
        elif path == "/api/configuracion/sii/perfil-a/limpieza":
            datos = {"nombre": "Perfil A", "empresas_exclusivas": 2,
                     "empresas_compartidas": 1, "facturas_a_eliminar": 14,
                     "facturas_compartidas_conservadas": 3}
        elif path == "/api/configuracion/sii/perfil-a" and metodo == "DELETE":
            eliminaciones.append(ruta.request.post_data_json)
            perfiles.pop(0)
            datos = {"facturas_a_eliminar": 14, "archivos_pendientes": []}
        elif path == "/api/configuracion/sii/perfil-a":
            datos = {"nombre": "Perfil A", "rut": "11111111-1",
                     "certificado_configurado": True}
        else:
            datos = {}
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("http://test.local/configuracion", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(Path(__file__).parents[1] / "app" / "static" / "configuracion.html").read_text(encoding="utf-8"),
        ))
        pagina.route("**/api/**", responder)
        pagina.goto("http://test.local/configuracion")
        pagina.get_by_role("searchbox", name="Buscar certificados").fill("Perfil B")
        assert pagina.locator('.perfil[data-id="perfil-a"]').is_hidden()
        assert pagina.locator('.perfil[data-id="perfil-b"]').is_visible()
        pagina.get_by_role("searchbox", name="Buscar certificados").fill("")
        pagina.get_by_role("button", name="Perfil A").click()
        boton = pagina.get_by_role("button", name="Eliminar certificado y facturas")
        boton.wait_for(state="visible")

        with pagina.expect_event("dialog") as dialogo:
            boton.click()
        assert "14 facturas" in dialogo.value.message
        assert "3 facturas de empresas compartidas" in dialogo.value.message
        dialogo.value.dismiss()
        assert eliminaciones == []

        with pagina.expect_event("dialog") as dialogo:
            boton.click()
        dialogo.value.accept()
        pagina.get_by_text("Certificado eliminado. Se quitaron 14 facturas.").wait_for()
        assert eliminaciones == [{"confirmar": True, "facturas_esperadas": 14,
                                 "empresas_esperadas": 2}]
        assert pagina.locator('.perfil[data-id="perfil-a"]').count() == 0
        assert pagina.locator('.perfil[data-id="perfil-b"]').count() == 1
        assert pagina.locator("#form").is_hidden()
        navegador.close()
