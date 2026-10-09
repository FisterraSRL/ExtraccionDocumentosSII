"""La navegación recupera documentos guardados sin volver a consultar al SII."""
import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_bandeja_recupera_documentos_al_volver_de_equivalencias():
    sincronizaciones = []
    consultas_documentos = []
    consultas_productos = []
    empresa_guardada = "00000000-0"
    empresa_nueva = "11111111-1"
    documento = {
        "id": 7, "tipo": "33", "tipo_nombre": "Factura Electrónica", "folio": "123",
        "proveedor_nombre": "Proveedor de prueba", "proveedor_rut": "22222222-2",
        "fecha": "2026-10-05", "total": 119, "neto": 100, "iva": 19, "exento": 0,
        "estado": "pendiente", "items": [{"desc": "Ítem de prueba", "cant": 1,
            "precio": 100, "subtotal": 100}], "pdf_disponible": False,
    }

    def responder(ruta):
        direccion = urlparse(ruta.request.url)
        if direccion.path == "/api/sync":
            sincronizaciones.append(ruta.request.url)
        if direccion.path == "/api/documents":
            consultas_documentos.append(direccion.query)
        if direccion.path == "/api/documents/7/productos/preparar":
            consultas_productos.append(direccion.path)
        datos = {
            "/api/sesion": {"autenticado": True, "configurada": True, "sin_login": True},
            "/api/version": {"version": "prueba"},
            "/api/configuracion/sii": {"nombre_activo": "Perfil de prueba"},
            "/api/empresas": [
                {"rut": empresa_guardada, "nombre_mostrado": "Empresa guardada",
                 "documentos": 1, "ultima_sincronizacion": "2026-10-05T10:00:00"},
                {"rut": empresa_nueva, "nombre_mostrado": "Empresa nueva",
                 "documentos": 0, "ultima_sincronizacion": None},
            ],
            "/api/documents": [documento],
            "/api/documents/7/productos/preparar": {"items": [{"indice": 0,
                "producto": {"codigo": "ABC", "nombre": "Producto guardado"},
                "estado": "asociado"}], "catalogo": {"cantidad": 1}},
            "/api/equivalencias": {"perfil_id": "perfil-prueba", "filas": [], "total": 0},
        }.get(direccion.path, {})
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("**/api/**", responder)
        pagina.goto("http://127.0.0.1:8000/")
        pagina.evaluate("localStorage.setItem('empresaActual', '00000000-0')")
        pagina.reload()
        pagina.locator('[data-row="7"]').wait_for()
        assert pagina.locator("#kpiTotalCount").inner_text() == "1"
        pagina.locator('[data-action="toggle"][data-id="7"]').click()
        pagina.get_by_role("button", name="Producto Finnegans del ítem 1: Producto guardado").wait_for()

        pagina.goto("http://127.0.0.1:8000/equivalencias")
        pagina.goto("http://127.0.0.1:8000/")
        pagina.locator('[data-row="7"]').wait_for()
        assert pagina.locator("#kpiTotalCount").inner_text() == "1"
        pagina.locator('[data-action="toggle"][data-id="7"]').click()
        pagina.get_by_role("button", name="Producto Finnegans del ítem 1: Producto guardado").wait_for()
        assert len(consultas_documentos) == 2
        assert all("empresa=" + empresa_guardada in q for q in consultas_documentos)
        assert not sincronizaciones

        pagina.locator("#empresaSelect").select_option(empresa_nueva)
        assert pagina.locator("#kpiTotalCount").inner_text() == "–"
        assert len(consultas_documentos) == 2
        pagina.locator("#empresaSelect").select_option(empresa_guardada)
        pagina.locator('[data-row="7"]').wait_for()
        pagina.locator('[data-action="toggle"][data-id="7"]').click()
        pagina.get_by_role("button", name="Producto Finnegans del ítem 1: Producto guardado").wait_for()
        assert len(consultas_productos) == 3
        assert not sincronizaciones
        navegador.close()


if __name__ == "__main__":
    test_bandeja_recupera_documentos_al_volver_de_equivalencias()
    print("Persistencia de bandeja: OK")
