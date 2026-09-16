"""Cliente de conexión al SII (Chile).

ACTUALIZACIÓN (16-sep-2026) — el login automatizado FUNCIONA, confirmado end-to-end
contra el SII real. Corrige dos conclusiones anteriores de este archivo:

1. "Intercambio de información" (servicio web SOAP) sigue descartado para este
   certificado. El camino es automatizar un navegador — eso no cambió.

2. **El proxy interceptor NO era la causa del fallo de login.** Esa hipótesis quedó
   descartada. Las causas reales eran dos, ambas del lado del cliente:

   a. La página `IngresoCertificado.html` del SII muestra un `confirm()` de
      JavaScript antes de enviar el formulario de autenticación. Su rama `else` es
      `location.replace('http://www.sii.cl')`. Playwright **descarta los diálogos
      automáticamente**, así que el `confirm()` devolvía `false` y el navegador se
      iba solo a la home pública del SII. Ese era el "redirect a www.sii.cl" que se
      venía interpretando como rechazo del certificado. Hay que registrar un handler
      `page.on("dialog", ...)` que lo acepte.

   b. El certificado hay que presentarlo con la opción `client_certificates` de
      Playwright, no con `--auto-select-certificate-for-urls`: ese no es un switch de
      línea de comandos de Chromium (es una política de empresa, se configura por
      registro en Windows), así que Chromium lo ignoraba en silencio.

   Además, el `.pfx` de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza por
   obsoleto, así que `pfxPath` falla con "Unsupported TLS certificate". Por eso se le
   pasan los PEM que ya produce `_load_pkcs12()` (`certPath`/`keyPath`).

Consecuencias prácticas, importantes para decidir el hosting:

- Ya no hace falta que el certificado esté en el almacén del sistema operativo: el
  `.pfx` se lee del disco. Esto vuelve el login **portable** (Linux, contenedor, cloud)
  y elimina el parámetro `nss_home` que tenía este método.
- El requisito de "salida a internet sin proxy interceptor" queda **sin confirmar**:
  se estableció a partir de la hipótesis que acabamos de descartar. Habrá que
  reprobarlo en el entorno de destino, pero ya no es una restricción conocida.

El endpoint que hace la autenticación TLS mutua es `herculesr.sii.cl` (confirmado:
acepta el handshake con este certificado y responde 200).
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
# Origen que hace la autenticación TLS mutua — al que hay que presentarle el
# certificado. Confirmado probando el handshake directo con requests.
SII_CERT_AUTH_ORIGIN = "https://herculesr.sii.cl"
SII_MISII_HOME = "https://misiir.sii.cl/cgi_misii/siihome.cgi"

SII_AUTH_HOSTS = [
    "https://zeusr.sii.cl",   # portal de autenticación (login humano, certificado o clave)
    "https://palena.sii.cl",  # servicios web de DTE en producción (histórico, a confirmar vigencia)
]


class SIIAuthenticationError(RuntimeError):
    """El navegador terminó el flujo de login pero la sesión no quedó iniciada."""


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

    def login_with_browser(self, headless: bool = True, timeout: float = 45000):
        """Loguea al SII con un Chromium real (Playwright) usando el certificado digital.

        Confirmado funcionando end-to-end contra el SII de producción. El flujo real es:

            misiir/siihome.cgi
              → zeusr/IngresoRutClave.html          (pantalla de RUT+clave)
              → [click "Ingresar con Certificado Digital"]
              → zeusr/IngresoCertificado.html       (muestra un confirm() y auto-envía
                                                     un form por POST)
              → herculesr/cgi_AUT2000/CAutInicio.cgi (TLS mutuo: acá va el certificado)

        Dos detalles sin los cuales esto falla en silencio, devolviendo la home pública
        del SII en vez de un error (ver el docstring del módulo para el detalle):

        - Hay que **aceptar el confirm()**; Playwright descarta los diálogos por defecto
          y la rama `else` del SII redirige a www.sii.cl.
        - Hay que presentar el certificado con `client_certificates`, en PEM (el .pfx
          de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza).

        Si el RUT está autorizado para representar a otros contribuyentes, el SII
        intercala una pantalla "ESCOJA COMO DESEA INGRESAR". Este método elige
        "Continuar" (trámites propios), que es el caso de uso del proyecto.

        Devuelve (page, context, browser, playwright) con la sesión ya iniciada, o
        lanza SIIAuthenticationError si no lo logró — nunca devuelve una página sin
        autenticar haciéndola pasar por buena.
        """
        from playwright.sync_api import sync_playwright  # import diferido: dependencia pesada

        if not self._cert_pem_file:
            self._load_pkcs12()

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            client_certificates=[
                {
                    "origin": SII_CERT_AUTH_ORIGIN,
                    "certPath": str(self._cert_pem_file),
                    "keyPath": str(self._key_pem_file),
                }
            ]
        )
        page = context.new_page()
        page.on("dialog", lambda dialogo: dialogo.accept())

        try:
            page.goto(SII_MISII_HOME, wait_until="domcontentloaded", timeout=timeout)
            page.click("text=Ingresar con Certificado Digital", timeout=timeout / 2)
            page.wait_for_load_state("networkidle", timeout=timeout)

            # Pantalla de representación: seguir como el propio contribuyente.
            # Por rol y texto exacto: un `text=Continuar` genérico engancha un
            # contenedor oculto del menú y el click se queda esperando visibilidad.
            continuar = page.get_by_role("link", name="Continuar", exact=True)
            if continuar.count() > 0:
                continuar.first.click()
                page.wait_for_load_state("networkidle", timeout=timeout)

            texto = page.inner_text("body")
            rut_plano = self.rut.replace(".", "").replace("-", "")
            autenticado = "Cerrar Sesión" in texto or rut_plano[:8] in texto.replace(".", "").replace("-", "")
            if not autenticado:
                raise SIIAuthenticationError(
                    "El flujo de login terminó sin sesión iniciada. "
                    f"URL final: {page.url}. Primeros 300 caracteres de la página: "
                    f"{texto[:300]!r}"
                )
        except Exception:
            context.close()
            browser.close()
            playwright.stop()
            raise

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
