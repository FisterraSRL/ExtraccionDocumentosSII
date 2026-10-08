"""Comprueba el flujo visible con datos sintéticos, sin tocar SII ni Finnegans."""
import json

from playwright.sync_api import sync_playwright


def test_seleccion_y_confirmacion():
    fila = {
        "id": 1, "descripcion_original": "Cemento Portland 50 KG",
        "descripcion_normalizada": "CEMENTO PORTLAND 50KG",
        "apariciones": 3, "primera_aparicion": "2026-10-01",
        "ultima_aparicion": "2026-10-03", "estado": "sugerido", "origen": None,
        "producto": None, "producto_codigo": None,
        "sugerencia": {"codigo": "P1", "nombre": "Cemento 50KG", "puntaje": 0.94},
    }
    otra_fila = {
        **fila, "id": 2, "descripcion_original": "Aceite motor",
        "descripcion_normalizada": "ACEITE MOTOR", "estado": "pendiente",
        "sugerencia": None,
    }
    guardadas = []
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("**/api/equivalencias?*", lambda ruta: ruta.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"total": 1 if "q=aceite" in ruta.request.url else 2,
                             "pagina": 1, "limite": 50,
                             "filas": [otra_fila] if "q=aceite" in ruta.request.url else [fila, otra_fila],
                             "catalogo_cantidad": 2,
                             "perfil_id": "perfil-prueba"}),
        ))
        pagina.route("**/api/equivalencias/lote", lambda ruta: (
            guardadas.append(ruta.request.post_data_json),
            ruta.fulfill(status=200, content_type="application/json", body='{"ok":true,"guardadas":2}'),
        ))
        pagina.route("**/api/productos?q=*", lambda ruta: ruta.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"productos": [{"codigo": "P2", "nombre": "Cemento especial"}]}),
        ))
        pagina.goto("http://127.0.0.1:8000/equivalencias")
        fila_visible = pagina.locator('tr[data-id="1"]')
        segunda = pagina.locator('tr[data-id="2"]')
        assert fila_visible.get_by_text("Cemento Portland 50 KG").is_visible()
        assert fila_visible.get_by_text("94%").is_visible()
        fila_visible.get_by_role("button", name="Sugerencia").click()
        pagina.get_by_role("searchbox", name="Buscar equivalencias").fill("aceite")
        fila_visible.wait_for(state="detached")
        segunda.get_by_role("searchbox", name="Buscar producto Finnegans").fill("aceite")
        segunda.get_by_role("button", name="Cemento especial").click()
        assert not guardadas
        assert pagina.get_by_text("2 relaciones pendientes de guardar").is_visible()
        pagina.get_by_role("button", name="Guardar relaciones").click()
        pagina.get_by_text("2 relaciones guardadas correctamente.").wait_for()
        assert len(guardadas) == 1
        assert guardadas[0] == {"perfil_esperado": "perfil-prueba", "decisiones": [
            {"producto_sii_id": 1, "codigo": "P1", "estado": "confirmado"},
            {"producto_sii_id": 2, "codigo": "P2", "estado": "confirmado"},
        ]}
        navegador.close()


if __name__ == "__main__":
    test_seleccion_y_confirmacion()
    print("Pantalla de equivalencias: OK")
