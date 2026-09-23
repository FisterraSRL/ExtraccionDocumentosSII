"""Autenticación del portal.

Por qué existe: la bandeja muestra documentos tributarios de clientes. Mientras todo
corría en `localhost` eso no era un problema; publicado en internet, sí. Esto es lo
mínimo para que la URL no quede abierta.

Cómo funciona: usuario y contraseña se configuran por variables de entorno
(`PORTAL_USUARIO`, `PORTAL_PASSWORD`). Al entrar se emite una cookie firmada con HMAC
sobre `SECRET_KEY`, con vencimiento. No hay base de usuarios ni registro: es un portal
interno para el equipo de administración, y agregar una tabla de usuarios sin necesidad
sería más superficie para mantener y para equivocarse.

Deliberadamente **no** se guarda la contraseña en ningún lado ni se compara con `==`
(se usa `compare_digest`, que no filtra información por el tiempo que tarda).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Cookie, HTTPException, Response

COOKIE = "portal_sesion"
DURACION_SEGUNDOS = 12 * 60 * 60  # una jornada de trabajo


class ConfiguracionAuthError(RuntimeError):
    """Falta configurar el acceso al portal."""


def _config() -> tuple[str, str, bytes]:
    usuario = os.getenv("PORTAL_USUARIO", "")
    password = os.getenv("PORTAL_PASSWORD", "")
    secreto = os.getenv("SECRET_KEY", "")
    if not usuario or not password:
        raise ConfiguracionAuthError(
            "Faltan PORTAL_USUARIO y/o PORTAL_PASSWORD. Sin eso el portal no puede "
            "validar a nadie, y se niega a abrirse en vez de quedar sin protección."
        )
    if not secreto:
        raise ConfiguracionAuthError(
            "Falta SECRET_KEY: es lo que firma la cookie de sesión. Generá una con "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"`."
        )
    return usuario, password, secreto.encode("utf-8")


def auth_configurada() -> bool:
    try:
        _config()
        return True
    except ConfiguracionAuthError:
        return False


def _firmar(datos: bytes, secreto: bytes) -> str:
    return hmac.new(secreto, datos, hashlib.sha256).hexdigest()


def credenciales_validas(usuario: str, password: str) -> bool:
    esperado_usuario, esperado_password, _ = _config()
    # Se comparan las dos siempre, sin cortocircuito, para no revelar cuál falló.
    ok_usuario = hmac.compare_digest(usuario or "", esperado_usuario)
    ok_password = hmac.compare_digest(password or "", esperado_password)
    return ok_usuario and ok_password


def emitir_cookie(response: Response, usuario: str) -> None:
    _, _, secreto = _config()
    payload = json.dumps({"u": usuario, "exp": int(time.time()) + DURACION_SEGUNDOS})
    datos = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    valor = f"{datos}.{_firmar(datos.encode('ascii'), secreto)}"
    response.set_cookie(
        COOKIE,
        valor,
        max_age=DURACION_SEGUNDOS,
        httponly=True,   # que el JavaScript de la página no pueda leerla
        samesite="lax",
        # En producción va por HTTPS; en desarrollo sobre localhost tiene que poder
        # viajar sin TLS o no habría forma de entrar.
        secure=os.getenv("PORTAL_COOKIE_INSEGURA", "").lower() not in ("1", "true", "si"),
        path="/",
    )


def borrar_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def sesion_valida(valor: str | None) -> str | None:
    """Devuelve el usuario si la cookie es legítima y no venció; si no, None."""
    if not valor or "." not in valor:
        return None
    try:
        _, _, secreto = _config()
    except ConfiguracionAuthError:
        return None
    datos, _, firma = valor.rpartition(".")
    if not hmac.compare_digest(firma, _firmar(datos.encode("ascii"), secreto)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(datos.encode("ascii")))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload.get("u")


def requiere_sesion(portal_sesion: str | None = Cookie(default=None)) -> str:
    """Dependencia de FastAPI: corta el pedido si no hay sesión iniciada."""
    usuario = sesion_valida(portal_sesion)
    if not usuario:
        raise HTTPException(status_code=401, detail="Hay que iniciar sesión.")
    return usuario
