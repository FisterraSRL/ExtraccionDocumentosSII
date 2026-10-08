"""Configuracion local del certificado con el que Playwright se autentica ante el SII."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import tempfile
import json
from pathlib import Path

from cryptography.hazmat.primitives.serialization import pkcs12

from app.config import settings

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_ENV = RAIZ / ".env"
DIRECTORIO_SECRETOS = RAIZ / "secrets"
ARCHIVO_CERTIFICADO = DIRECTORIO_SECRETOS / "certificado.pfx"
ARCHIVO_PERFILES = DIRECTORIO_SECRETOS / "sii_certificados.json"
MAXIMO_CERTIFICADO = 10 * 1024 * 1024
_RUT = re.compile(r"^[0-9.]+-[0-9Kk]$")


class ConfiguracionSIIError(RuntimeError):
    """La configuracion local del certificado no es valida."""


class CertificadoDuplicadoError(ConfiguracionSIIError):
    """El archivo ya fue cargado en otro perfil local."""


def es_entorno_local(request) -> bool:
    cliente = getattr(request, "client", None)
    return (
        not os.getenv("VERCEL")
        and not os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        and cliente is not None
        and cliente.host in {"127.0.0.1", "::1", "localhost"}
    )


def _leer_perfiles() -> dict:
    try:
        datos = json.loads(ARCHIVO_PERFILES.read_text(encoding="utf-8"))
        if isinstance(datos.get("perfiles"), dict) and isinstance(datos.get("activo"), str):
            return datos
    except Exception:
        pass
    # Migra la configuración existente como primer perfil sin tocar el certificado.
    if settings.sii_rut and settings.sii_cert_path and settings.sii_cert_password:
        ruta = Path(settings.sii_cert_path)
        huella = None
        if ruta.is_file():
            huella = _huella(ruta.read_bytes())
        return {"activo": "actual", "perfiles": {"actual": {
            "nombre": "Certificado actual", "rut": settings.sii_rut,
            "archivo": Path(settings.sii_cert_path).name,
            "password": settings.sii_cert_password,
            "huella": huella,
        }}}
    return {"activo": None, "perfiles": {}}


def _escribir_perfiles(datos: dict) -> None:
    DIRECTORIO_SECRETOS.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix="sii-certificados-", suffix=".json", dir=DIRECTORIO_SECRETOS)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, ensure_ascii=False)
        os.replace(temporal, ARCHIVO_PERFILES)
    except Exception:
        Path(temporal).unlink(missing_ok=True)
        raise


def _archivo_perfil(perfil: dict) -> Path:
    nombre = Path(str(perfil["archivo"])).name
    return DIRECTORIO_SECRETOS / nombre


def _huella(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


def estado() -> dict:
    datos = _leer_perfiles()
    activo = datos["activo"]
    perfil = datos["perfiles"].get(activo, {}) if activo else {}
    ruta = _archivo_perfil(perfil) if perfil else ARCHIVO_CERTIFICADO
    return {
        "activo": activo,
        "nombre_activo": perfil.get("nombre") if perfil else None,
        "perfiles": [
            {"id": identificador, "nombre": datos_perfil["nombre"], "rut": datos_perfil["rut"],
             "certificado_configurado": _archivo_perfil(datos_perfil).is_file(),
             "client_id_configurado": bool(datos_perfil.get("client_id")),
             "client_secret_configurado": bool(datos_perfil.get("client_secret"))}
            for identificador, datos_perfil in datos["perfiles"].items()
        ],
        "rut_configurado": bool(perfil),
        "certificado_configurado": ruta.is_file(),
        "password_configurada": bool(settings.sii_cert_password),
        "ruta": str(ruta.name),
    }


def detalle(identificador: str) -> dict:
    """Devuelve los datos editables sin enviar contraseñas al navegador."""
    datos = _leer_perfiles()
    perfil = datos["perfiles"].get(identificador)
    if not perfil:
        raise ConfiguracionSIIError("El certificado seleccionado no existe.")
    ruta = _archivo_perfil(perfil)
    return {
        "id": identificador,
        "nombre": perfil.get("nombre", ""),
        "rut": perfil.get("rut", ""),
        "certificado_configurado": ruta.is_file(),
        "password_configurada": bool(perfil.get("password")),
        "client_id_configurado": bool(perfil.get("client_id")),
        "client_secret_configurado": bool(perfil.get("client_secret")),
        "activo": datos.get("activo") == identificador,
    }


def revelar(identificador: str, campo: str) -> str:
    """Entrega un solo dato sensible tras una solicitud explícita desde la pantalla local."""
    claves = {
        "password": "password",
        "client_id": "client_id",
    }
    if campo not in claves:
        raise ConfiguracionSIIError("El campo solicitado no se puede mostrar.")
    perfil = _leer_perfiles()["perfiles"].get(identificador)
    if not perfil:
        raise ConfiguracionSIIError("El certificado seleccionado no existe.")
    valor = perfil.get(claves[campo])
    if not valor:
        raise ConfiguracionSIIError("Este campo aún no tiene un valor guardado.")
    return valor


def perfil_activo_id() -> str:
    datos = _leer_perfiles()
    identificador = datos.get("activo")
    if not identificador or identificador not in datos["perfiles"]:
        raise ConfiguracionSIIError("Seleccioná un certificado SII en Configuración antes de continuar.")
    return identificador


def credenciales_finnegans_activas() -> tuple[str, str, str]:
    """Lee las credenciales del perfil activo solo para uso interno del backend."""
    datos = _leer_perfiles()
    identificador = perfil_activo_id()
    perfil = datos["perfiles"][identificador]
    client_id = perfil.get("client_id")
    client_secret = perfil.get("client_secret")
    if not client_id or not client_secret:
        raise ConfiguracionSIIError(
            "Completá Client_ID y Client_Secret del certificado activo en Configuración."
        )
    return identificador, client_id, client_secret


def _id_perfil(nombre: str, existentes: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", nombre.lower()).strip("-")[:40] or "certificado"
    identificador = base
    numero = 2
    while identificador in existentes:
        identificador = f"{base}-{numero}"
        numero += 1
    return identificador


def _activar(identificador: str, datos: dict) -> dict:
    perfil = datos["perfiles"].get(identificador)
    if not perfil:
        raise ConfiguracionSIIError("El certificado seleccionado no existe.")
    ruta = _archivo_perfil(perfil)
    if not ruta.is_file():
        raise ConfiguracionSIIError("No se encontró el archivo del certificado seleccionado.")
    ruta_env = f"secrets/{ruta.name}"
    _escribir_env({"SII_RUT": perfil["rut"], "SII_CERT_PATH": ruta_env,
                   "SII_CERT_PASSWORD": perfil["password"], "SII_PERFIL": identificador})
    datos["activo"] = identificador
    _escribir_perfiles(datos)
    object.__setattr__(settings, "sii_rut", perfil["rut"])
    object.__setattr__(settings, "sii_cert_path", ruta_env)
    object.__setattr__(settings, "sii_cert_password", perfil["password"])
    object.__setattr__(settings, "sii_perfil", identificador)
    os.environ.update({"SII_RUT": perfil["rut"], "SII_CERT_PATH": ruta_env,
                       "SII_CERT_PASSWORD": perfil["password"], "SII_PERFIL": identificador})
    return estado()


def _validar(rut: str, password: str, contenido: bytes) -> str:
    rut_limpio = rut.strip().upper()
    if not _RUT.fullmatch(rut_limpio):
        raise ConfiguracionSIIError("Ingresá un RUT válido, por ejemplo 12345678-9.")
    if not password:
        raise ConfiguracionSIIError("Ingresá la contraseña del certificado.")
    if not contenido or len(contenido) > MAXIMO_CERTIFICADO:
        raise ConfiguracionSIIError("El certificado debe ser un archivo .pfx/.p12 de hasta 10 MB.")
    try:
        clave, certificado, _ = pkcs12.load_key_and_certificates(contenido, password.encode("utf-8"))
    except Exception as exc:
        raise ConfiguracionSIIError("No se pudo abrir el certificado. Revisá el archivo y la contraseña.") from exc
    if clave is None or certificado is None:
        raise ConfiguracionSIIError("El archivo no contiene una clave privada y certificado juntos.")
    return rut_limpio


def _escribir_env(valores: dict[str, str]) -> None:
    lineas = ARCHIVO_ENV.read_text(encoding="utf-8") if ARCHIVO_ENV.exists() else ""
    for clave, valor in valores.items():
        patron = re.compile(rf"(?m)^{re.escape(clave)}=.*$")
        nueva = f"{clave}={valor}"
        lineas, cambios = patron.subn(nueva, lineas, count=1)
        if not cambios:
            lineas += ("" if not lineas or lineas.endswith("\n") else "\n") + nueva + "\n"
    descriptor, temporal = tempfile.mkstemp(prefix=".env-", dir=RAIZ)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as archivo:
            archivo.write(lineas)
        os.replace(temporal, ARCHIVO_ENV)
    except Exception:
        Path(temporal).unlink(missing_ok=True)
        raise


def guardar(nombre: str, rut: str, password: str, certificado_b64: str,
            client_id: str = "", client_secret: str | None = "") -> dict:
    try:
        contenido = base64.b64decode(certificado_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfiguracionSIIError("El archivo del certificado llegó incompleto. Elegilo otra vez.") from exc
    rut_limpio = _validar(rut, password, contenido)
    nombre_limpio = nombre.strip()[:80]
    if not nombre_limpio:
        raise ConfiguracionSIIError("Dale un nombre para distinguir este certificado.")
    datos = _leer_perfiles()
    huella = _huella(contenido)
    for perfil in datos["perfiles"].values():
        huella_existente = perfil.get("huella")
        if not huella_existente:
            archivo_existente = _archivo_perfil(perfil)
            if archivo_existente.is_file():
                huella_existente = _huella(archivo_existente.read_bytes())
        if huella_existente == huella:
            raise CertificadoDuplicadoError(
                f"Este certificado ya está subido en el perfil «{perfil['nombre']}»."
            )
    identificador = _id_perfil(nombre_limpio, datos["perfiles"])
    DIRECTORIO_SECRETOS.mkdir(parents=True, exist_ok=True)
    archivo_final = DIRECTORIO_SECRETOS / f"certificado-{identificador}.pfx"
    descriptor, temporal = tempfile.mkstemp(prefix="certificado-", suffix=".pfx", dir=DIRECTORIO_SECRETOS)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)
        os.replace(temporal, archivo_final)
        datos["perfiles"][identificador] = {"nombre": nombre_limpio, "rut": rut_limpio,
                                              "archivo": archivo_final.name, "password": password,
                                              "huella": huella, "client_id": client_id.strip(),
                                              "client_secret": client_secret}
        _activar(identificador, datos)
    except Exception as exc:
        Path(temporal).unlink(missing_ok=True)
        raise ConfiguracionSIIError("No se pudo guardar la configuración local.") from exc

    return estado()


def editar(identificador: str, nombre: str, rut: str, password: str | None,
           certificado_b64: str | None, client_id: str | None,
           client_secret: str | None) -> dict:
    """Actualiza un perfil; los secretos omitidos se conservan sin devolverlos."""
    datos = _leer_perfiles()
    perfil = datos["perfiles"].get(identificador)
    if not perfil:
        raise ConfiguracionSIIError("El certificado seleccionado no existe.")
    nombre_limpio = nombre.strip()[:80]
    if not nombre_limpio:
        raise ConfiguracionSIIError("Dale un nombre para distinguir este certificado.")
    rut_limpio = rut.strip().upper()
    if not _RUT.fullmatch(rut_limpio):
        raise ConfiguracionSIIError("Ingresá un RUT válido, por ejemplo 12345678-9.")

    contenido = None
    if certificado_b64:
        try:
            contenido = base64.b64decode(certificado_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfiguracionSIIError("El archivo del certificado llegó incompleto. Elegilo otra vez.") from exc
        clave_password = password if password is not None and password != "" else perfil.get("password", "")
        _validar(rut_limpio, clave_password, contenido)
        huella = _huella(contenido)
        for otro_id, otro in datos["perfiles"].items():
            if otro_id == identificador:
                continue
            existente = otro.get("huella")
            archivo = _archivo_perfil(otro)
            if not existente and archivo.is_file():
                existente = _huella(archivo.read_bytes())
            if existente == huella:
                raise CertificadoDuplicadoError(
                    f"Este certificado ya está subido en el perfil «{otro['nombre']}»."
                )
        archivo_final = _archivo_perfil(perfil)
        archivo_temporal = archivo_final.with_suffix(archivo_final.suffix + ".tmp")
        archivo_temporal.write_bytes(contenido)
        os.replace(archivo_temporal, archivo_final)
        perfil["huella"] = huella
        perfil["archivo"] = archivo_final.name
    elif password is not None and password != "":
        archivo_existente = _archivo_perfil(perfil)
        if not archivo_existente.is_file():
            raise ConfiguracionSIIError("No se encontró el certificado. Cargá el archivo junto con la contraseña.")
        _validar(rut_limpio, password, archivo_existente.read_bytes())

    perfil.update({"nombre": nombre_limpio, "rut": rut_limpio})
    if password is not None and password != "":
        perfil["password"] = password
    if client_id is not None:
        perfil["client_id"] = client_id.strip()
    if client_secret is not None:
        perfil["client_secret"] = client_secret
    _escribir_perfiles(datos)
    if datos.get("activo") == identificador:
        _activar(identificador, datos)
    return detalle(identificador)


def seleccionar(identificador: str) -> dict:
    datos = _leer_perfiles()
    return _activar(identificador, datos)
