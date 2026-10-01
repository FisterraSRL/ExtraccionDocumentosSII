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

from fastapi import Cookie, HTTPException, Request, Response

COOKIE = "portal_sesion"
DURACION_SEGUNDOS = 12 * 60 * 60  # una jornada de trabajo

# Entrar sin pasar por el login. Es para el desarrollo local, donde volver a
# autenticarse cada 12 horas no protege de nada: el portal escucha en la propia
# máquina. Apagado salvo que se lo pida a propósito, y aun así no alcanza para abrir
# el portal publicado — ver sesion_automatica().
SIN_LOGIN = os.getenv("PORTAL_SIN_LOGIN", "").lower() in ("1", "true", "si")

# Con quién se registra la actividad cuando se entró sin login.
USUARIO_LOCAL = "local"

# El pedido tiene que venir de la propia máquina. Se compara contra el socket, no
# contra cabeceras como X-Forwarded-For, que las escribe quien llama.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


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


def sesion_automatica(request: Request) -> str | None:
    """Usuario con el que entrar sin login, o None si no corresponde.

    Tienen que darse las tres condiciones, y cada una tapa un agujero distinto:

    1. `PORTAL_SIN_LOGIN` activado. Nadie se queda sin login por descuido.
    2. El pedido viene de la propia máquina. Vale aunque el servidor se levante con
       `--host 0.0.0.0`: lo que se mira es de dónde vino la conexión.
    3. No estamos en el despliegue serverless, donde el portal tiene URL pública y
       muestra documentos tributarios de clientes.

    Con las tres, una variable que se escape al entorno equivocado no abre nada.
    """
    if not SIN_LOGIN:
        return None
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return None
    cliente = getattr(request, "client", None)
    if cliente is None or cliente.host not in _LOOPBACK:
        return None
    return USUARIO_LOCAL


def requiere_sesion(
    request: Request, portal_sesion: str | None = Cookie(default=None)
) -> str:
    """Dependencia de FastAPI: corta el pedido si no hay sesión iniciada."""
    usuario = sesion_valida(portal_sesion) or sesion_automatica(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Hay que iniciar sesión.")
    return usuario
