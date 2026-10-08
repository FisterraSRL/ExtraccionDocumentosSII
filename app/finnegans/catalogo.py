"""Autenticación y lectura del catálogo de Finnegans para el perfil SII activo."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from app.config import settings


class ErrorCatalogoFinnegans(RuntimeError):
    """No se pudo consultar el catálogo de productos."""


_tokens: dict[str, tuple[str, float, str]] = {}
_bloqueo = threading.Lock()


def _token(perfil_id: str, client_id: str, client_secret: str, renovar: bool = False) -> str:
    firma = hashlib.sha256((client_id + "\0" + client_secret).encode("utf-8")).hexdigest()
    with _bloqueo:
        anterior = _tokens.get(perfil_id)
        if not renovar and anterior and anterior[0] == firma and time.monotonic() < anterior[1]:
            return anterior[2]
        base = settings.finnegans_api_url.rstrip("/")
        cuerpo = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": client_id,
        }).encode("utf-8")
        solicitud = urllib.request.Request(
            f"{base}/oauth/token", data=cuerpo, method="POST",
            headers={
                "Authorization": f"Basic {client_secret}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(solicitud, timeout=45) as respuesta:
                texto = respuesta.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            raise ErrorCatalogoFinnegans(
                f"Finnegans rechazó la autenticación (HTTP {exc.code}). Revisá Client_ID y Client_Secret."
            ) from None
        except urllib.error.URLError:
            raise ErrorCatalogoFinnegans("No se pudo conectar con Finnegans para obtener el token.") from None
        try:
            objeto = json.loads(texto)
        except json.JSONDecodeError:
            objeto = None
        token = objeto.get("access_token") if isinstance(objeto, dict) else texto
        if not isinstance(token, str) or not token:
            raise ErrorCatalogoFinnegans("Finnegans respondió sin un token válido.")
        # La documentación indica una hora; se renueva antes para evitar el vencimiento.
        _tokens[perfil_id] = (firma, time.monotonic() + 55 * 60, token)
        return token


def obtener_productos(
    perfil_id: str, client_id: str, client_secret: str, desde: date | None = None,
) -> list[dict]:
    """Lee producto/list. 'desde' queda listo para la sincronización incremental."""
    base = settings.finnegans_api_url.rstrip("/")
    ruta = f"{base}/producto/list"
    if desde:
        ruta += "?" + urllib.parse.urlencode({"desde": desde.isoformat()})
    for intento in range(2):
        token = _token(perfil_id, client_id, client_secret, renovar=bool(intento))
        solicitud = urllib.request.Request(
            ruta, headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(solicitud, timeout=120) as respuesta:
                datos = json.load(respuesta)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and intento == 0:
                continue
            raise ErrorCatalogoFinnegans(
                f"Finnegans no entregó el catálogo de productos (HTTP {exc.code})."
            ) from None
        except urllib.error.URLError:
            raise ErrorCatalogoFinnegans("No se pudo conectar con Finnegans para leer productos.") from None
        except json.JSONDecodeError:
            raise ErrorCatalogoFinnegans("Finnegans devolvió un catálogo que no es JSON válido.") from None
    if not isinstance(datos, list):
        raise ErrorCatalogoFinnegans("Finnegans devolvió un catálogo con estructura inesperada.")
    return datos
