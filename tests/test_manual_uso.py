"""El manual se abre desde la barra y permite encontrar temas sin llamar al SII."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from app import auth
from app.main import app
from app.version import __version__


ESTATICOS = Path(__file__).parents[1] / "app" / "static"


def test_manual_exige_sesion():
    ruta = next(ruta for ruta in app.routes if getattr(ruta, "path", None) == "/manual")
    assert any(dependencia.call is auth.requiere_sesion
               for dependencia in ruta.dependant.dependencies)


def test_indice_y_busqueda_del_manual():
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("http://test.local/manual", lambda ruta: ruta.fulfill(
            status=200, content_type="text/html",
            body=(ESTATICOS / "manual.html").read_text(encoding="utf-8"),
        ))
        pagina.route("http://test.local/api/version", lambda ruta: ruta.fulfill(
            status=200, content_type="application/json", body=json.dumps({"version": __version__}),
        ))
        pagina.goto("http://test.local/manual")
        assert pagina.locator(".capitulo:visible").count() == 10
        assert pagina.locator("#manualVersion").inner_text() == "v" + __version__

        buscador = pagina.get_by_role("searchbox", name="Buscar en el manual")
        buscador.fill("No encuentro un producto Finnegans")
        assert pagina.locator(".capitulo:visible").count() == 1
        assert pagina.locator("#problemas").is_visible()
        assert pagina.locator("#indiceManual a:visible").count() == 1
        assert pagina.locator("#problemas details").filter(has_text="No encuentro un producto Finnegans").is_visible()
        assert pagina.locator("#problemas details").filter(has_text="No encuentro un producto Finnegans").get_attribute("open") is not None

        buscador.fill("palabra que no existe")
        assert pagina.locator("#sinResultados").is_visible()
        buscador.fill("")
        pagina.get_by_role("link", name="Certificados · administrador").click()
        assert pagina.url.endswith("#certificados")
        navegador.close()


def test_barra_de_bandeja_enlaza_manual():
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    assert 'href="/manual"' in html
    assert 'aria-label="Abrir manual de uso"' in html
    assert '<span aria-hidden="true">?</span>' in html
