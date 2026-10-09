"""La confirmación envía el perfil con el que se armó la vista previa."""
import json
from urllib.parse import urlparse

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

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        encabezados = {}
        if path == "/api/sesion":
            datos = {"autenticado": True, "configurada": True, "sin_login": True}
        elif path == "/api/version":
            datos = {"version": "1.0.5"}
        elif path == "/api/configuracion/sii":
            datos = {"nombre_activo": "Perfil A"}
        elif path == "/api/empresas":
            datos = [{"rut": "00000000-0", "nombre_mostrado": "Empresa de prueba", "documentos": 1}]
        elif path == "/api/sync":
            datos = {"mensaje": "Sincronización simulada", "documentos_nuevos": 0}
        elif path == "/api/documents":
            datos = [documento]
        elif path == "/api/documents/1/finnegans":
            datos = {"EmpresaCodigo": "EMPRESA_A", "WorkflowCodigo": "FLUJO_PRUEBA"}
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
        pagina.route("**/api/**", responder)
        pagina.goto("http://127.0.0.1:8000/")
        pagina.get_by_label("Empresa a consultar").select_option("00000000-0")
        pagina.get_by_role("button", name="Sincronizar con SII").click()
        pagina.locator('input[data-action="select"][data-id="1"]').check()
        pagina.locator("#enviarTodoBtn").click()
        pagina.locator("#confirmOkBtn").click()
        pagina.wait_for_function("document.querySelector('#toast').textContent.includes('1 enviado')")
        assert envios == [{"ids": [1], "perfil_esperado": "perfil-a"}]
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
            "/api/configuracion/sii": {"nombre_activo": "Perfil de prueba"},
            "/api/empresas": [{"rut": "00000000-0", "nombre_mostrado": "Empresa de prueba",
                                "documentos": 1, "ultima_sincronizacion": "2025-01-02T10:00:00"}],
            "/api/documents": [documento],
        }.get(path, {})
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("**/api/**", responder)
        pagina.goto("http://127.0.0.1:8000/")
        pagina.locator("#empresaSelect").select_option("00000000-0")
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
