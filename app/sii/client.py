"""Cliente de conexión al SII (Chile).

ACTUALIZACIÓN (16-sep-2026) — leer antes de tocar este archivo: se confirmó que
"Intercambio de información" NO está disponible para el certificado de producción
que estamos usando, así que el Camino A (servicio web SOAP) queda descartado. El
camino confirmado es automatizar un navegador real con el certificado instalado en
el sistema operativo — ver `login_with_browser()` más abajo.

También se confirmó algo importante sobre DÓNDE puede correr esto: el sandbox de
Claude en la nube sale a internet a través de un proxy que intercepta y re-firma el
tráfico HTTPS. Con un proxy así, la conexión TLS mutua queda entre el proxy y el
SII, no entre el navegador/cliente y el SII — el certificado del cliente nunca le
llega al SII. Se probó tanto con `requests` como con un Chromium real vía Playwright
desde ese sandbox y ambos fallaron igual (mismo error genérico del SII). Este mismo
código, corrido en un entorno SIN ese tipo de proxy (la compu de un desarrollador, o
el servidor de producción final), debería funcionar — así fue como el usuario logró
loguearse manualmente. Ver requisitos-portal-sii-finnegans.md, sección "Hallazgo
clave", para el detalle completo de las pruebas.

Requisito de infraestructura que se desprende de esto: el hosting elegido para
producción tiene que tener salida directa a internet, sin un proxy interceptor de
por medio.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

# Hosts públicos conocidos del SII para autenticación con certificado digital.
# No confirmado todavía cuál responde mejor a un cliente no-browser — test_connection
# los prueba a ambos y reporta qué pasó con cada uno.
SII_AUTH_HOSTS = [
    "https://zeusr.sii.cl",   # portal de autenticación (login humano, certificado o clave)
    "https://palena.sii.cl",  # servicios web de DTE en producción (histórico, a confirmar vigencia)
]


class SIICertificateError(RuntimeError):
    """El .pfx no se pudo leer con la contraseña dada, o el archivo no es un certificado válido."""


@dataclass
class ConnectionTestResult:
    host: str
    ok: bool
    status_code: int | None
    detalle: str


class SIIClient:
    def __init__(self, rut: str, cert_path: Path | str, cert_password: str):
        self.rut = rut
        self.cert_path = Path(cert_path)
        self._cert_password = cert_password
        self._cert_pem_file: Path | None = None
        self._key_pem_file: Path | None = None

    # ---------- Certificado ----------

    def _load_pkcs12(self) -> None:
        """Lee el .pfx y deja la clave privada + certificado como archivos PEM temporales,
        que es el formato que espera `requests` para autenticación TLS mutua.

        Esto es real y corre hoy — no depende de ninguna decisión pendiente del SII.
        """
        try:
            data = self.cert_path.read_bytes()
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                data, self._cert_password.encode("utf-8")
            )
        except FileNotFoundError as exc:
            raise SIICertificateError(f"No se encontró el certificado en '{self.cert_path}'.") from exc
        except Exception as exc:  # la librería lanza distintos tipos según qué falle
            raise SIICertificateError(
                "No se pudo leer el certificado. Motivo más probable: la contraseña "
                f"es incorrecta o el archivo no es un .pfx/.p12 válido. Detalle: {exc}"
            ) from exc

        if private_key is None or certificate is None:
            raise SIICertificateError(
                "El archivo cargó pero no contiene clave privada y certificado juntos "
                "(esperado en un .pfx exportado desde el navegador o la autoridad certificadora)."
            )

        key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        cert_pem = certificate.public_bytes(Encoding.PEM)

        # Archivos temporales porque `requests` pide rutas de archivo para cert=(cert, key),
        # no bytes en memoria. Se borran al cerrar el cliente (ver close()).
        key_f = tempfile.NamedTemporaryFile(suffix=".key.pem", delete=False)
        key_f.write(key_pem)
        key_f.close()
        cert_f = tempfile.NamedTemporaryFile(suffix=".cert.pem", delete=False)
        cert_f.write(cert_pem)
        cert_f.close()

        self._key_pem_file = Path(key_f.name)
        self._cert_pem_file = Path(cert_f.name)

    def close(self) -> None:
        for f in (self._cert_pem_file, self._key_pem_file):
            if f and f.exists():
                f.unlink()

    def __enter__(self) -> "SIIClient":
        self._load_pkcs12()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- Primer paso real: ¿el SII acepta este certificado? ----------

    def test_connection(self, timeout: float = 15.0) -> list[ConnectionTestResult]:
        """Intenta un handshake TLS mutuo (con el certificado cargado) contra los hosts
        conocidos del SII y reporta qué respondió cada uno.

        Esto NO confirma todavía que "Intercambio de información" esté habilitado ni
        cuál es el endpoint correcto para RCV/BHE — solo confirma que el certificado
        es válido y que el SII lo acepta a nivel de conexión. El resultado de esto
        es lo que decide si seguimos por el Camino A o el Camino B de arriba.
        """
        if not self._cert_pem_file:
            self._load_pkcs12()

        resultados = []
        for host in SII_AUTH_HOSTS:
            try:
                resp = requests.get(
                    host,
                    cert=(str(self._cert_pem_file), str(self._key_pem_file)),
                    timeout=timeout,
                )
                resultados.append(
                    ConnectionTestResult(
                        host=host, ok=resp.ok, status_code=resp.status_code,
                        detalle=f"Respondió {resp.status_code}.",
                    )
                )
            except requests.exceptions.SSLError as exc:
                resultados.append(
                    ConnectionTestResult(
                        host=host, ok=False, status_code=None,
                        detalle=f"El servidor rechazó el certificado en el handshake TLS: {exc}",
                    )
                )
            except requests.exceptions.RequestException as exc:
                resultados.append(
                    ConnectionTestResult(host=host, ok=False, status_code=None, detalle=str(exc))
                )
        return resultados

    # ---------- Camino confirmado: navegador real con el certificado instalado ----------

    def login_with_browser(self, nss_home: Path | str, headless: bool = True):
        """Loguea al SII con un Chromium real (Playwright), usando el certificado
        importado a una base de certificados NSS.

        Mecánica confirmada y funcional (probada en el sandbox de Claude, ver docstring
        del módulo para la limitación de red que impidió confirmar el resultado final
        del login desde ahí — este método debe probarse en un entorno sin proxy
        interceptor, por ejemplo la máquina de un desarrollador o el servidor final):

        1. `nss_home` debe ser un directorio con `.pki/nssdb` conteniendo el certificado
           ya importado (ver README del proyecto o `scripts/setup_nss_cert.sh` — pendiente
           de crear ese script de conveniencia) y, si aplica, la CA de cualquier proxy
           corporativo que el entorno real use.
        2. Se lanza Chromium con `--auto-select-certificate-for-urls` apuntando a
           `https://[*.]sii.cl`, para que no dependa de un diálogo humano de selección
           de certificado.
        3. Se navega a `https://misiir.sii.cl/cgi_misii/siihome.cgi` (el link real de
           "Ingresar a Mi Sii") y se hace clic en "Ingresar con Certificado Digital".

        Devuelve la instancia de `Page` de Playwright ya autenticada (o no — quien llama
        debe verificar el resultado buscando el nombre/RUT del contribuyente en la
        página, tal como se hizo en las pruebas), para poder seguir navegando el RCV/BHE
        desde el mismo contexto de navegador.
        """
        from playwright.sync_api import sync_playwright  # import diferido: dependencia pesada

        auto_select = json.dumps([{"pattern": "https://[*.]sii.cl", "filter": {}}])

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=headless,
            args=[f"--auto-select-certificate-for-urls={auto_select}"],
            env={"HOME": str(nss_home)},
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://misiir.sii.cl/cgi_misii/siihome.cgi", wait_until="networkidle", timeout=30000)
        page.click("text=Ingresar con Certificado Digital", force=True, timeout=15000)
        page.wait_for_load_state("networkidle", timeout=30000)
        return page, context, browser, playwright

    # ---------- Pendiente de validar con el resultado de login_with_browser() ----------

    def get_rcv(self, periodo: str) -> list[dict]:
        """Trae los documentos del RCV para un período (YYYY-MM). Pendiente: confirmar
        endpoint/servicio real (Camino A o B) antes de implementar."""
        raise NotImplementedError(
            "Pendiente de definir tras test_connection(): servicio web de Intercambio "
            "vs. automatización del portal RCV. Ver docstring del módulo."
        )

    def get_bhe(self, periodo: str) -> list[dict]:
        """Trae las boletas de honorarios electrónicas recibidas en un período (YYYY-MM).
        Módulo separado del RCV en el SII — ver DESIGN-SYSTEM/requisitos del proyecto."""
        raise NotImplementedError("Pendiente, mismo motivo que get_rcv().")

    def get_dte_xml(self, rut_emisor: str, tipo: str, folio: int) -> str:
        """Trae el XML completo de un documento puntual (para el detalle de ítems)."""
        raise NotImplementedError("Pendiente, mismo motivo que get_rcv().")
