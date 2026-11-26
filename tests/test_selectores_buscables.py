"""Los selectores filtran mientras se escribe y conservan el valor original."""
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).parents[1] / "app" / "static" / "selectores-buscables.js"


def test_busqueda_teclado_y_actualizacion_dinamica():
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.set_content("""
            <label for="empresa">Empresa destino</label>
            <select id="empresa" required>
              <option value="">Elegí una empresa</option>
              <option value="PIL">Agrícola del Pilar · 111</option>
              <option value="CEN">Empresa de prueba Sur · 222</option>
            </select>
            <label>Rol<select id="rol"><option value="usuario">Usuario</option>
              <option value="administrador">Administrador</option></select></label>
            <script>
              window.cambios=[];
              document.querySelector('#empresa').addEventListener('change',
                e=>window.cambios.push(e.target.value));
            </script>
        """)
        pagina.add_script_tag(path=str(SCRIPT))
        campo = pagina.get_by_role("combobox", name="Empresa destino")
        assert pagina.get_by_role("combobox", name="Rol").count() == 1
        campo.fill("pilar")
        assert pagina.locator(".selector-buscable-lista button").count() == 1
        campo.press("Enter")
        assert pagina.locator("#empresa").input_value() == "PIL"
        assert campo.input_value() == "Agrícola del Pilar · 111"
        assert pagina.evaluate("window.cambios") == ["PIL"]

        pagina.locator("#empresa").evaluate("e => e.value = 'CEN'")
        assert campo.input_value() == "Empresa de prueba Sur · 222"
        pagina.locator("#empresa").evaluate("e => { e.innerHTML = '<option value=\"X\">Empresa nueva · 333</option>'; e.value = 'X'; }")
        assert campo.input_value() == "Empresa nueva · 333"
        campo.fill("nueva")
        assert pagina.locator(".selector-buscable-lista button").count() == 1
        campo.press("Escape")
        assert campo.input_value() == "Empresa nueva · 333"
        navegador.close()
