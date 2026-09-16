"""Cliente de conexión al SII (Chile).

Lo único que este módulo puede dar por confirmado hoy es la forma del certificado
digital (.pfx) y cómo cargarlo — eso es criptografía estándar, no depende de que el
SII no cambie nada. Todo lo que sigue (a qué URL exacta se autentica, qué servicio
da el RCV, cómo se pide el XML de un documento puntual) son DOS caminos posibles,
no uno confirmado, y hay que validarlo en vivo con el certificado real antes de
construir el resto sobre un supuesto:

  Camino A — Servicio web de "Intercambio de información" (SOAP, mutual TLS con el
  certificado). Es el camino pensado para automatización: pedís el XML de un
  documento puntual (RUT emisor + tipo + folio) sin simular un navegador. Requiere
  que el certificado de la empresa esté habilitado para eso en el SII.

  Camino B — Automatización del portal web (login con certificado en
  misiir.sii.cl / palena, navegar el RCV y el módulo de Boletas de Honorarios,
  descargar el XML o el detalle desde ahí). Más frágil: pensado para un humano,
  no para un cliente HTTP — cambios de layout, captchas, sesiones cortas.

`test_connection()` es el primer paso real y sí es completamente funcional: valida
que el .pfx cargue con la contraseña dada y que el SII acepte un handshake TLS
mutuo con ese certificado contra su host de autenticación. Confirma que el
certificado "sirve", no todavía qué API específica vamos a usar — eso se decide
con el resultado de esa prueba y se documenta en requisitos-portal-sii-finnegans.md.
"""
from __future__ import annotations

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

    # ---------- Pendiente de validar con el resultado de test_connection() ----------

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
