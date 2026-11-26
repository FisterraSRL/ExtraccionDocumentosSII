"""La bandeja propone solo destinos permitidos y conserva la elección manual."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def test_sugerencia_por_rut_codigo_y_descripcion_sin_pisar_eleccion_manual():
    empresas_sii = [
        {"rut": "11111111-1", "nombre_mostrado": "Café del Centro", "nombre": "Café del Centro"},
        {"rut": "22222222-2", "nombre_mostrado": "Demo del Norte SpA", "nombre": "Demo del Norte SpA"},
        {"rut": "33333333-3", "nombre_mostrado": "Sin relación", "nombre": "Sin relación"},
        {"rut": "44444444-4", "nombre_mostrado": "Cafe del Centro", "nombre": "Cafe del Centro"},
    ]
    destinos = [
        {"codigo": "ERP-NOMBRE", "nombre": "Cafe del Centro SpA"},
        {"codigo": "ERP-RUT", "nombre": "Otra razón social"},
        {"codigo": "DEMODELNORTE", "nombre": "Demo del Norte"},
    ]

    def responder(ruta):
        url = urlparse(ruta.request.url)
        path = url.path
        if path == "/api/sesion":
            datos = {"autenticado": True, "configurada": True, "sin_login": True}
        elif path == "/api/certificados-disponibles":
            datos = {"activo": "perfil-a", "perfiles": [{"id": "perfil-a", "nombre": "Perfil A"}]}
        elif path == "/api/finnegans/parametros-envio":
            datos = {"workflow_codigo": "FLUJO"}
        elif path == "/api/finnegans/empresas-permitidas":
            datos = {"perfil": "perfil-a", "empresas": destinos}
        elif path == "/api/finnegans/sugerencia-empresa":
            rut = parse_qs(url.query)["empresa_rut"][0]
            datos = {"codigo": "ERP-RUT" if rut == "11111111-1" else ""}
        elif path == "/api/empresas":
            datos = empresas_sii
        elif path == "/api/sync":
            datos = {"mensaje": "Sincronización simulada"}
        elif path == "/api/documents":
            datos = []
        else:
            datos = {}
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
        origen = pagina.locator("#empresaSelect")
        destino = pagina.locator("#empresaDestino")
        origen.locator('option[value="11111111-1"]').wait_for(state="attached")
        destino.locator('option[value="ERP-RUT"]').wait_for(state="attached")

        origen.select_option("11111111-1")
        pagina.wait_for_function("document.querySelector('#empresaDestino').value === 'ERP-RUT'")
        assert pagina.locator("#empresaDestinoSugerencia").is_visible()

        origen.select_option("22222222-2")
        pagina.wait_for_function("document.querySelector('#empresaDestino').value === 'DEMODELNORTE'")
        destino.select_option("ERP-NOMBRE")
        assert pagina.locator("#empresaDestinoSugerencia").is_hidden()
        pagina.locator("#syncBtn").click()
        pagina.wait_for_function("document.querySelector('#toast').textContent.includes('Sincronización simulada')")
        assert destino.input_value() == "ERP-NOMBRE"

        origen.select_option("33333333-3")
        assert destino.input_value() == ""
        assert pagina.locator("#empresaDestinoSugerencia").is_hidden()

        origen.select_option("44444444-4")
        pagina.wait_for_function("document.querySelector('#empresaDestino').value === 'ERP-NOMBRE'")
        assert pagina.locator("#empresaDestinoSugerencia").is_visible()

        pagina.evaluate("localStorage.setItem('empresaActual', '22222222-2')")
        pagina.reload()
        pagina.wait_for_function("document.querySelector('#empresaDestino').value === 'DEMODELNORTE'")
        assert pagina.locator("#empresaSelect").input_value() == "22222222-2"

        pagina.evaluate("""() => {
          const fetchOriginal = window.fetch;
          window.sugerenciaRespondida = false;
          window.fetch = (url, opciones) => {
            if (!String(url).includes('/api/finnegans/sugerencia-empresa')) {
              return fetchOriginal(url, opciones);
            }
            return new Promise(resolve => setTimeout(() => {
              fetchOriginal(url, opciones).then(respuesta => {
                window.sugerenciaRespondida = true;
                resolve(respuesta);
              });
            }, 300));
          };
        }""")
        origen.select_option("11111111-1")
        assert destino.input_value() == "ERP-NOMBRE"
        destino.select_option("DEMODELNORTE")
        pagina.wait_for_function("window.sugerenciaRespondida")
        assert destino.input_value() == "DEMODELNORTE"
        navegador.close()
