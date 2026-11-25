"""Cuentas del portal, sesiones firmadas y controles de acceso."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import Cookie, HTTPException, Request, Response
from sqlalchemy import func, select

from app import configuracion_sii
from app.configuracion_sii import es_entorno_local
from app.contexto_usuario import Principal, actual
from app.db import SessionLocal
from app.models import Usuario, UsuarioCertificado

COOKIE = "portal_sesion"
DURACION_SEGUNDOS = 12 * 60 * 60
ITERACIONES_PBKDF2 = 600_000
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_HASH_FICTICIO: str | None = None


class ConfiguracionAuthError(RuntimeError):
    """Falta configurar la autenticación del portal."""


def hash_password(password: str) -> str:
    if not password or len(password) > 1024:
        raise ValueError("Ingresá una contraseña de hasta 1024 caracteres.")
    sal = secrets.token_bytes(16)
    resumen = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), sal, ITERACIONES_PBKDF2)
    return "pbkdf2_sha256${}${}${}".format(
        ITERACIONES_PBKDF2,
        base64.urlsafe_b64encode(sal).decode("ascii"),
        base64.urlsafe_b64encode(resumen).decode("ascii"),
    )


def verificar_password(password: str, almacenado: str) -> bool:
    try:
        algoritmo, iteraciones, sal, resumen = almacenado.split("$", 3)
        if algoritmo != "pbkdf2_sha256" or not 600_000 <= int(iteraciones) <= 2_000_000:
            return False
        calculado = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.urlsafe_b64decode(sal), int(iteraciones)
        )
        return hmac.compare_digest(calculado, base64.urlsafe_b64decode(resumen))
    except (TypeError, ValueError, UnicodeError, base64.binascii.Error):
        return False


def _secreto() -> bytes:
    valor = os.getenv("SECRET_KEY", "")
    if not valor:
        raise ConfiguracionAuthError("Falta SECRET_KEY para firmar las sesiones del portal.")
    return valor.encode("utf-8")


def sembrar_administrador() -> None:
    """Crea la primera cuenta desde secretos del entorno, sin reescribirla al reiniciar."""
    email = os.getenv("PORTAL_ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("PORTAL_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Usuario)):
            return
        db.add(Usuario(
            email=email, password_hash=hash_password(password), rol="administrador",
            perfil_activo=configuracion_sii.estado()["activo"],
        ))
        db.commit()


def auth_configurada() -> bool:
    try:
        _secreto()
        with SessionLocal() as db:
            return bool(db.scalar(select(func.count()).select_from(Usuario)))
    except Exception:
        return False


def autenticar(email: str, password: str) -> Principal | None:
    global _HASH_FICTICIO
    _secreto()
    correo = email.strip().lower()
    with SessionLocal() as db:
        usuario = db.scalar(select(Usuario).where(Usuario.email == correo))
        if usuario is None:
            if _HASH_FICTICIO is None:
                _HASH_FICTICIO = hash_password("credencial-inexistente")
            verificar_password(password, _HASH_FICTICIO)
            return None
        ahora = datetime.utcnow()
        if usuario.bloqueado_hasta and usuario.bloqueado_hasta > ahora:
            return None
        if not verificar_password(password, usuario.password_hash) or not usuario.activo:
            usuario.intentos_fallidos += 1
            if usuario.intentos_fallidos >= 5:
                usuario.bloqueado_hasta = ahora + timedelta(minutes=15)
                usuario.intentos_fallidos = 0
            db.commit()
            return None
        usuario.intentos_fallidos = 0
        usuario.bloqueado_hasta = None
        db.commit()
        return Principal(usuario.id, usuario.email, usuario.rol, usuario.perfil_activo)


def _firmar(datos: bytes, secreto: bytes) -> str:
    return hmac.new(secreto, datos, hashlib.sha256).hexdigest()


def emitir_cookie(response: Response, principal: Principal) -> None:
    if principal.id is None:
        raise ValueError("Las sesiones automáticas no emiten cookies.")
    with SessionLocal() as db:
        usuario = db.get(Usuario, principal.id)
        if usuario is None or not usuario.activo:
            raise ValueError("La cuenta ya no está activa.")
        version = usuario.version_sesion
    datos = base64.urlsafe_b64encode(json.dumps({
        "uid": principal.id, "sv": version, "exp": int(time.time()) + DURACION_SEGUNDOS,
    }, separators=(",", ":")).encode("utf-8")).decode("ascii")
    valor = f"{datos}.{_firmar(datos.encode('ascii'), _secreto())}"
    response.set_cookie(
        COOKIE, valor, max_age=DURACION_SEGUNDOS, httponly=True, samesite="lax",
        secure=os.getenv("PORTAL_COOKIE_INSEGURA", "").lower() not in ("1", "true", "si"),
        path="/",
    )


def borrar_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def sesion_valida(valor: str | None) -> Principal | None:
    if not valor or "." not in valor:
        return None
    try:
        datos, _, firma = valor.rpartition(".")
        if not hmac.compare_digest(firma, _firmar(datos.encode("ascii"), _secreto())):
            return None
        payload = json.loads(base64.urlsafe_b64decode(datos.encode("ascii")))
        if int(payload["exp"]) < time.time():
            return None
        with SessionLocal() as db:
            usuario = db.get(Usuario, int(payload["uid"]))
            if not usuario or not usuario.activo or usuario.version_sesion != int(payload["sv"]):
                return None
            perfil = usuario.perfil_activo
            if usuario.rol != "administrador" and perfil and not db.get(UsuarioCertificado, (usuario.id, perfil)):
                perfil = None
            return Principal(usuario.id, usuario.email, usuario.rol, perfil)
    except (ConfiguracionAuthError, KeyError, ValueError, TypeError, UnicodeError, OverflowError):
        return None


def sesion_automatica(request: Request) -> Principal | None:
    if os.getenv("PORTAL_SIN_LOGIN", "").lower() not in ("1", "true", "si"):
        return None
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("PORTAL_ADMIN_ORIGIN"):
        return None
    cliente = getattr(request, "client", None)
    if cliente is None or cliente.host not in _LOOPBACK:
        return None
    return Principal(None, "local", "administrador", None)


def _validar_origen(request: Request, origen_publicado: str | None = None) -> None:
    origen = request.headers.get("origin")
    host = request.headers.get("host")
    if origen_publicado:
        host_publicado = urlsplit(origen_publicado).netloc
        origen_invalido = (
            not host_publicado or origen_publicado != f"https://{host_publicado}"
            or host != host_publicado or (origen is not None and origen != origen_publicado)
        )
    else:
        origen_invalido = origen and urlsplit(origen).netloc != host
    if origen_invalido or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="El origen de la solicitud no está permitido.")


def requiere_sesion(request: Request, portal_sesion: str | None = Cookie(default=None)) -> Principal:
    _validar_origen(request)
    principal = actual() or sesion_valida(portal_sesion) or sesion_automatica(request)
    if not principal:
        raise HTTPException(status_code=401, detail="Hay que iniciar sesión.")
    return principal


def requiere_sesion_gestion(request: Request, portal_sesion: str | None = Cookie(default=None)) -> Principal:
    origen_publicado = os.getenv("PORTAL_ADMIN_ORIGIN")
    if not origen_publicado:
        principal = requiere_sesion(request, portal_sesion)
        if not es_entorno_local(request):
            raise HTTPException(status_code=404, detail="No disponible.")
        return principal
    _validar_origen(request, origen_publicado)
    principal = actual() or sesion_valida(portal_sesion)
    if not principal:
        raise HTTPException(status_code=401, detail="Hay que iniciar sesión.")
    return principal


def requiere_administrador(request: Request, portal_sesion: str | None = Cookie(default=None)) -> Principal:
    principal = requiere_sesion(request, portal_sesion)
    if not principal.administrador:
        raise HTTPException(status_code=403, detail="Solo un administrador puede realizar esta acción.")
    return principal


def requiere_administrador_gestion(request: Request, portal_sesion: str | None = Cookie(default=None)) -> Principal:
    principal = requiere_sesion_gestion(request, portal_sesion)
    if not principal.administrador:
        raise HTTPException(status_code=403, detail="Solo un administrador puede realizar esta acción.")
    return principal
