"""La renovación de cookies y los fallos del proxy no deben forzar otro login."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from app.sii import client as modulo_sii
from app.sii.client import SIIClient, SIIConnectionError


def _cliente() -> SIIClient:
    return SIIClient("00000000-0", Path("certificado-de-prueba.pfx"), "clave-de-prueba", perfil="prueba")


def test_navegacion_reintenta_una_vez_si_falla_el_proxy_local():
    pagina = Mock()
    pagina.goto.side_effect = [RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED"), None]

    _cliente()._navegar_con_certificado(pagina, "https://sii.example", timeout=1000)

    assert pagina.goto.call_count == 2
    pagina.wait_for_timeout.assert_called_once_with(1000)


def test_proxy_persistente_no_se_interpreta_como_cookie_vencida():
    pagina = Mock()
    pagina.goto.side_effect = RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED")

    with TestCase().assertRaisesRegex(SIIConnectionError, "proxy local"):
        _cliente()._sesion_viva(pagina)

    assert pagina.goto.call_count == 2


def test_fallo_de_proxy_no_dispara_login_nuevo():
    cliente = _cliente()
    with TemporaryDirectory() as temporal:
        cliente.archivo_cookies = Path(temporal) / "cookies.json"
        cliente.archivo_cookies.write_text("{}", encoding="utf-8")
        playwright = Mock()
        navegador = playwright.chromium.launch.return_value
        contexto = navegador.new_context.return_value
        inicio = Mock()
        inicio.start.return_value = playwright

        with patch("playwright.sync_api.sync_playwright", return_value=inicio), patch.object(
            cliente, "_sesion_viva", side_effect=SIIConnectionError("proxy local")
        ), patch.object(cliente, "login_with_browser") as login:
            with TestCase().assertRaises(SIIConnectionError):
                cliente.abrir_sesion()

        contexto.close.assert_called_once()
        navegador.close.assert_called_once()
        playwright.stop.assert_called_once()
        login.assert_not_called()


def test_error_de_chromium_durante_login_no_expone_url_del_sii():
    cliente = _cliente()
    cliente._cert_pem_file = Path("certificado-de-prueba.pem")
    cliente._key_pem_file = Path("clave-de-prueba.pem")
    playwright = Mock()
    pagina = playwright.chromium.launch.return_value.new_context.return_value.new_page.return_value
    pagina.url = "chrome-error://chromewebdata/"
    pagina.get_by_role.return_value.count.return_value = 0
    solicitud = Mock()
    solicitud.failure = "net::ERR_SOCKS_CONNECTION_FAILED"
    solicitud.url = "https://sii.example/?dato-sensible"

    def fallo_al_click(*_args, **_kwargs):
        callbacks = [call.args[1] for call in pagina.on.call_args_list if call.args[0] == "requestfailed"]
        callbacks[0](solicitud)

    pagina.click.side_effect = fallo_al_click
    inicio = Mock()
    inicio.start.return_value = playwright

    with patch("playwright.sync_api.sync_playwright", return_value=inicio), patch.object(
        cliente, "_navegar_con_certificado"
    ):
        with TestCase().assertRaisesRegex(SIIConnectionError, "ERR_SOCKS_CONNECTION_FAILED") as captura:
            cliente.login_with_browser()

    assert "dato-sensible" not in str(captura.exception)
    pagina.inner_text.assert_not_called()
    playwright.chromium.launch.return_value.close.assert_called_once()
    playwright.stop.assert_called_once()


def test_cierre_guarda_cookies_antes_de_cerrar_contexto():
    cliente = _cliente()
    orden = []
    contexto = Mock()
    navegador = Mock()
    playwright = Mock()
    contexto.close.side_effect = lambda: orden.append("contexto")
    navegador.close.side_effect = lambda: orden.append("navegador")
    playwright.stop.side_effect = lambda: orden.append("playwright")

    with patch.object(cliente, "_guardar_cookies", side_effect=lambda _: orden.append("cookies")):
        cliente.cerrar_sesion((None, contexto, navegador, playwright))

    assert orden == ["cookies", "contexto", "navegador", "playwright"]


def test_guardado_atomico_no_pisa_sesion_anterior_si_falla():
    cliente = _cliente()
    with TemporaryDirectory() as temporal:
        carpeta = Path(temporal)
        cliente.archivo_cookies = carpeta / "sesion.json"
        cliente.archivo_cookies.write_text("anterior", encoding="utf-8")
        contexto = Mock()
        contexto.storage_state.side_effect = RuntimeError("sin estado")

        with patch.object(modulo_sii, "DIR_ESTADO", carpeta):
            cliente._guardar_cookies(contexto)

        assert cliente.archivo_cookies.read_text(encoding="utf-8") == "anterior"
        assert list(carpeta.iterdir()) == [cliente.archivo_cookies]


def test_guardado_atomico_publica_estado_nuevo_completo():
    cliente = _cliente()
    with TemporaryDirectory() as temporal:
        carpeta = Path(temporal)
        cliente.archivo_cookies = carpeta / "sesion.json"
        cliente.archivo_cookies.write_text("anterior", encoding="utf-8")
        contexto = Mock()
        contexto.storage_state.side_effect = lambda path: Path(path).write_text("nueva", encoding="utf-8")

        with patch.object(modulo_sii, "DIR_ESTADO", carpeta):
            cliente._guardar_cookies(contexto)

        assert cliente.archivo_cookies.read_text(encoding="utf-8") == "nueva"
        assert list(carpeta.iterdir()) == [cliente.archivo_cookies]


if __name__ == "__main__":
    test_navegacion_reintenta_una_vez_si_falla_el_proxy_local()
    test_proxy_persistente_no_se_interpreta_como_cookie_vencida()
    test_fallo_de_proxy_no_dispara_login_nuevo()
    test_error_de_chromium_durante_login_no_expone_url_del_sii()
    test_cierre_guarda_cookies_antes_de_cerrar_contexto()
    test_guardado_atomico_no_pisa_sesion_anterior_si_falla()
    test_guardado_atomico_publica_estado_nuevo_completo()
    print("Sesión SII: OK")
