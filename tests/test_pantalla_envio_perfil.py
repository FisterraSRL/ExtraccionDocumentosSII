"""La confirmación envía el perfil con el que se armó la vista previa."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def test_el_lote_conserva_el_perfil_confirmado():
    documento = {
        "id": 1, "empresa_rut": "00000000-0", "tipo": "33",
        "tipo_nombre": "Factura Electrónica", "folio": 123,
        "proveedor_rut": "11111111-1", "proveedor_nombre": "Proveedor de prueba",
        "fecha": "2025-01-02", "total": 1190, "neto": 1000, "iva": 190,
        "exento": 0, "items": [], "estado": "pendiente",
    }
    envios = []
    vistas = []

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        encabezados = {}
        if path == "/api/sesion":
            datos = {"autenticado": True, "configurada": True, "sin_login": True}
        elif path == "/api/version":
            datos = {"version": "1.0.5"}
        elif path == "/api/configuracion/sii":
            datos = {"nombre_activo": "Perfil A"}
        elif path == "/api/certificados-disponibles":
            datos = {"activo": "perfil-a", "perfiles": [{"id": "perfil-a", "nombre": "Perfil A"}]}
        elif path == "/api/finnegans/parametros-envio":
            datos = {"empresa_codigo": "", "workflow_codigo": "FLUJO_BASE"}
        elif path == "/api/finnegans/empresas-permitidas":
            datos = {"perfil": "perfil-a", "empresas": [{"codigo": "EMPRESA_A", "nombre": "Empresa A"}]}
        elif path == "/api/empresas":
            datos = [{"rut": "00000000-0", "nombre_mostrado": "Empresa de prueba", "documentos": 1}]
        elif path == "/api/sync":
            datos = {"mensaje": "Sincronización simulada", "documentos_nuevos": 0}
        elif path == "/api/documents":
            datos = [documento]
        elif path == "/api/documents/1/finnegans":
            consulta = parse_qs(urlparse(ruta.request.url).query)
            vistas.append(consulta)
            datos = {"EmpresaCodigo": consulta["empresa_codigo"][0], "WorkflowCodigo": consulta["workflow_codigo"][0]}
            encabezados["X-Perfil-Finnegans"] = "perfil-a"
        elif path == "/api/documents/send":
            envios.append(ruta.request.post_data_json)
            documento["estado"] = "enviado"
            datos = {"enviados": 1, "con_error": 0, "omitidos": 0,
                     "resultados": [{"id": 1, "estado": "enviado"}]}
        else:
            datos = {}
        ruta.fulfill(status=200, content_type="application/json",
                      headers=encabezados, body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("http://test.local/", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8"),
        ))
        pagina.route("**/api/**", responder)
        pagina.goto("http://test.local/")
        pagina.get_by_label("Empresa a consultar").select_option("00000000-0")
        pagina.get_by_role("button", name="Sincronizar con SII").click()
        pagina.locator('input[data-action="select"][data-id="1"]').check()
        pagina.get_by_label("Empresa Destino").locator('option[value="EMPRESA_A"]').wait_for(state="attached")
        assert pagina.get_by_label("Empresa Destino").input_value() == ""
        assert pagina.get_by_label("Workflow").input_value() == "FLUJO_BASE"
        pagina.get_by_label("Empresa Destino").select_option("EMPRESA_A")
        pagina.get_by_label("Workflow").fill("FLUJO_PRUEBA")
        for ancho, alto in [(1366, 768), (390, 844)]:
            pagina.set_viewport_size({"width": ancho, "height": alto})
            cabecera = pagina.locator(".cabecera-bandeja")
            assert cabecera.evaluate("(el) => el.scrollHeight <= el.clientHeight + 1")
            listado = pagina.locator("#listado").bounding_box()
            assert abs(listado["height"] - alto) <= 1
            assert listado["y"] + pagina.evaluate("window.scrollY") >= cabecera.bounding_box()["height"]
        pagina.get_by_role("button", name="Ampliar listado").click()
        assert pagina.locator(".cabecera-bandeja").is_hidden()
        pagina.locator("#enviarTodoBtn").click()
        pagina.locator("#confirmOkBtn:enabled").wait_for()
        pagina.keyboard.press("Escape")
        assert pagina.locator("#confirmOverlay").is_hidden()
        assert pagina.get_by_role("button", name="Restaurar vista").is_visible()
        pagina.keyboard.press("Escape")
        assert pagina.get_by_role("button", name="Ampliar listado").is_visible()
        assert pagina.get_by_label("Workflow").input_value() == "FLUJO_PRUEBA"
        assert pagina.locator('input[data-action="select"][data-id="1"]').is_checked()
        vistas.clear()
        pagina.locator("#enviarTodoBtn").click()
        pagina.locator("#confirmOkBtn").click()
        pagina.wait_for_function("document.querySelector('#toast').textContent.includes('1 enviado')")
        assert vistas == [{"empresa_codigo": ["EMPRESA_A"], "workflow_codigo": ["FLUJO_PRUEBA"]}]
        assert envios == [{"ids": [1], "perfil_esperado": "perfil-a",
                           "empresa_codigo": "EMPRESA_A", "workflow_codigo": "FLUJO_PRUEBA"}]
        navegador.close()


def test_fallo_de_vista_previa_muestra_error_y_no_habilita_envio():
    documento = {
        "id": 1, "tipo": "33", "tipo_nombre": "Factura Electrónica", "folio": 123,
        "proveedor_rut": "11111111-1", "proveedor_nombre": "Proveedor de prueba",
        "fecha": "2025-01-02", "total": 1190, "neto": 1000, "iva": 190,
        "exento": 0, "items": [], "estado": "pendiente",
    }

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        if path == "/api/documents/1/finnegans":
            ruta.abort("failed")
            return
        datos = {
            "/api/sesion": {"autenticado": True, "configurada": True, "sin_login": True},
            "/api/version": {"version": "prueba"},
            "/api/finnegans/parametros-envio": {"empresa_codigo":"", "workflow_codigo":"FLUJO_BASE"},
            "/api/finnegans/empresas-permitidas": {"perfil":"perfil-a", "empresas":[{"codigo":"EMPRESA_A","nombre":"Empresa A"}]},
            "/api/certificados-disponibles": {"activo":"perfil-a", "perfiles":[{"id":"perfil-a","nombre":"Perfil A"}]},
            "/api/configuracion/sii": {"nombre_activo": "Perfil de prueba"},
            "/api/empresas": [{"rut": "00000000-0", "nombre_mostrado": "Empresa de prueba",
                                "documentos": 1, "ultima_sincronizacion": "2025-01-02T10:00:00"}],
            "/api/documents": [documento],
        }.get(path, {})
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("http://test.local/", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8"),
        ))
        pagina.route("**/api/**", responder)
        pagina.goto("http://test.local/")
        pagina.locator("#empresaSelect").select_option("00000000-0")
        pagina.get_by_label("Empresa Destino").select_option("EMPRESA_A")
        pagina.locator('input[data-action="select"][data-id="1"]').check()
        pagina.locator("#enviarTodoBtn").click()
        pagina.get_by_text("No se pudo consultar la vista previa:").wait_for()
        assert pagina.locator("#confirmOkBtn").is_disabled()
        assert "Consultando" not in pagina.locator("#confirmAviso").inner_text()
        navegador.close()


if __name__ == "__main__":
    test_el_lote_conserva_el_perfil_confirmado()
    test_fallo_de_vista_previa_muestra_error_y_no_habilita_envio()
    print("Confirmación con perfil Finnegans: OK")
