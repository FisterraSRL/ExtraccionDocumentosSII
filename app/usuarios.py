"""Permisos por cuenta, certificado SII y empresa destino de Finnegans."""
from __future__ import annotations

import hashlib
import re
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import auth, configuracion_sii
from app.contexto_usuario import Principal, actual
from app.db import get_db
from app.finnegans.client import FinnegansAPIError, FinnegansClient, FinnegansConfigError
from app.models import Empresa, EmpresaPerfil, EmpresasPerfilEstado, Usuario, UsuarioCertificado, UsuarioEmpresaFinnegans, UsuarioEmpresaSII

router = APIRouter(prefix="/api", tags=["usuarios"])
_cache_empresas: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SeleccionPerfil(BaseModel):
    perfil: str


class PermisoCertificado(BaseModel):
    perfil_id: str
    empresas: list[str] = Field(default_factory=list)
    empresas_sii: list[str] = Field(default_factory=list)


class DatosUsuario(BaseModel):
    email: str
    password: str | None = None
    rol: str = "usuario"
    activo: bool = True
    certificados: list[PermisoCertificado] = Field(default_factory=list)


def _error_perfil() -> HTTPException:
    return HTTPException(status_code=403, detail="No tenés acceso a este certificado.")


def principal_actual() -> Principal:
    # Las llamadas directas de tareas locales no pasan por el middleware HTTP.
    return actual() or Principal(None, "local", "administrador", None)


def perfiles_permitidos(principal: Principal, db: Session) -> list[dict]:
    perfiles = configuracion_sii.estado()["perfiles"]
    if principal.administrador:
        return perfiles
    ids = set(db.scalars(select(UsuarioCertificado.perfil_id).where(
        UsuarioCertificado.usuario_id == principal.id
    )))
    return [perfil for perfil in perfiles if perfil["id"] in ids]


def exigir_perfil(principal: Principal, db: Session, perfil_id: str | None = None) -> str:
    perfil = perfil_id or principal.perfil_id
    if principal.id is None:
        perfil = perfil or configuracion_sii.perfil_activo_id()
    if not perfil or perfil not in {p["id"] for p in perfiles_permitidos(principal, db)}:
        raise _error_perfil()
    return perfil


def empresas_sii_del_perfil(db: Session, perfil_id: str) -> list[dict]:
    filas = db.execute(
        select(Empresa.rut, Empresa.nombre)
        .join(EmpresaPerfil, EmpresaPerfil.empresa_rut == Empresa.rut)
        .where(EmpresaPerfil.perfil_id == perfil_id)
    ).all()
    return sorted(
        ({"rut": rut, "nombre": nombre or rut} for rut, nombre in filas),
        key=lambda empresa: (empresa["nombre"].casefold(), empresa["rut"]),
    )


def ruts_sii_permitidos(principal: Principal, db: Session, perfil_id: str) -> list[str]:
    disponibles = set(db.scalars(select(EmpresaPerfil.empresa_rut).where(
        EmpresaPerfil.perfil_id == perfil_id
    )))
    if principal.administrador:
        return sorted(disponibles)
    permiso = db.get(UsuarioCertificado, (principal.id, perfil_id)) if principal.id else None
    if not permiso:
        return []
    if not permiso.empresas_sii_restringidas:
        return sorted(disponibles)
    asignadas = set(db.scalars(select(UsuarioEmpresaSII.empresa_rut).where(
        UsuarioEmpresaSII.usuario_id == principal.id,
        UsuarioEmpresaSII.perfil_id == perfil_id,
    )))
    return sorted(disponibles & asignadas)


def empresas_finnegans(perfil_id: str) -> list[dict]:
    try:
        _, client_id, client_secret = configuracion_sii.credenciales_finnegans_para_perfil(perfil_id)
        firma = hashlib.sha256((client_id + "\0" + client_secret).encode("utf-8")).hexdigest()
        llave = (perfil_id, firma)
        with _cache_lock:
            anterior = _cache_empresas.get(llave)
            if anterior and anterior[0] > time.monotonic():
                return anterior[1]
        cliente = FinnegansClient(perfil_id, client_id, client_secret)
        filas = cliente.catalogo("empresaSucursal")
    except (configuracion_sii.ConfiguracionSIIError, FinnegansAPIError, FinnegansConfigError) as exc:
        raise HTTPException(status_code=503, detail="No se pudieron consultar las empresas de Finnegans para este certificado.") from exc
    empresas = []
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        codigo = str(fila.get("codigo") or fila.get("Codigo") or "").strip()
        if not codigo or fila.get("Activo") is False or fila.get("activo") is False:
            continue
        nombre = str(fila.get("nombre") or fila.get("Nombre") or codigo).strip()
        empresas.append({"codigo": codigo, "nombre": nombre})
    empresas.sort(key=lambda e: (e["nombre"].casefold(), e["codigo"]))
    with _cache_lock:
        _cache_empresas[llave] = (time.monotonic() + 600, empresas)
    return empresas


def empresas_permitidas(principal: Principal, db: Session, perfil_id: str) -> list[dict]:
    exigir_perfil(principal, db, perfil_id)
    if principal.administrador:
        return empresas_finnegans(perfil_id)
    codigos = set(db.scalars(select(UsuarioEmpresaFinnegans.empresa_codigo).where(
        UsuarioEmpresaFinnegans.usuario_id == principal.id,
        UsuarioEmpresaFinnegans.perfil_id == perfil_id,
    )))
    if not codigos:
        return []
    empresas = empresas_finnegans(perfil_id)
    return [empresa for empresa in empresas if empresa["codigo"] in codigos]


def exigir_empresa(principal: Principal, db: Session, perfil_id: str, codigo: str | None) -> str:
    if not codigo:
        raise HTTPException(status_code=422, detail="Seleccioná una empresa destino de Finnegans.")
    if codigo not in {e["codigo"] for e in empresas_permitidas(principal, db, perfil_id)}:
        raise HTTPException(status_code=403, detail="La empresa destino no está permitida para este certificado.")
    return codigo


def _usuario_out(usuario: Usuario, db: Session) -> dict:
    perfiles = list(db.scalars(select(UsuarioCertificado).where(
        UsuarioCertificado.usuario_id == usuario.id
    )))
    empresas = list(db.scalars(select(UsuarioEmpresaFinnegans).where(
        UsuarioEmpresaFinnegans.usuario_id == usuario.id
    )))
    return {
        "id": usuario.id, "email": usuario.email, "rol": usuario.rol,
        "activo": usuario.activo, "perfil_activo": usuario.perfil_activo,
        "certificados": [
            {"perfil_id": permiso.perfil_id,
             "empresas": sorted(e.empresa_codigo for e in empresas if e.perfil_id == permiso.perfil_id),
             "empresas_sii": ruts_sii_permitidos(
                 Principal(usuario.id, usuario.email, usuario.rol, permiso.perfil_id), db, permiso.perfil_id,
             )}
            for permiso in perfiles
        ],
    }


def _validar_datos(datos: DatosUsuario) -> tuple[str, list[PermisoCertificado]]:
    email = datos.email.strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Ingresá un correo electrónico válido.")
    if datos.rol not in {"usuario", "administrador"}:
        raise HTTPException(status_code=422, detail="El rol indicado no existe.")
    ids = [p.perfil_id for p in datos.certificados]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="No repitas certificados en la asignación.")
    return email, datos.certificados


def _guardar_permisos(db: Session, usuario: Usuario, permisos: list[PermisoCertificado]) -> None:
    db.execute(delete(UsuarioEmpresaSII).where(UsuarioEmpresaSII.usuario_id == usuario.id))
    db.execute(delete(UsuarioEmpresaFinnegans).where(UsuarioEmpresaFinnegans.usuario_id == usuario.id))
    db.execute(delete(UsuarioCertificado).where(UsuarioCertificado.usuario_id == usuario.id))
    if usuario.rol == "administrador":
        return
    disponibles = {p["id"] for p in configuracion_sii.estado()["perfiles"]}
    for permiso in permisos:
        if permiso.perfil_id not in disponibles:
            raise HTTPException(status_code=422, detail="Se indicó un certificado inexistente.")
        codigos = set(permiso.empresas)
        validos = {e["codigo"] for e in empresas_finnegans(permiso.perfil_id)} if codigos else set()
        if len(codigos) != len(permiso.empresas) or not codigos.issubset(validos):
            raise HTTPException(status_code=422, detail="Una empresa Finnegans no pertenece al certificado indicado.")
        ruts = set(permiso.empresas_sii)
        disponibles_sii = set(db.scalars(select(EmpresaPerfil.empresa_rut).where(
            EmpresaPerfil.perfil_id == permiso.perfil_id
        )))
        if len(ruts) != len(permiso.empresas_sii) or not ruts.issubset(disponibles_sii):
            raise HTTPException(status_code=422, detail="Una empresa SII no pertenece al certificado indicado. Actualizá la lista y volvé a elegirla.")
        db.add(UsuarioCertificado(
            usuario_id=usuario.id, perfil_id=permiso.perfil_id, empresas_sii_restringidas=True,
        ))
        for rut in ruts:
            db.add(UsuarioEmpresaSII(
                usuario_id=usuario.id, perfil_id=permiso.perfil_id, empresa_rut=rut,
            ))
        for codigo in codigos:
            db.add(UsuarioEmpresaFinnegans(
                usuario_id=usuario.id, perfil_id=permiso.perfil_id, empresa_codigo=codigo,
            ))
    if usuario.perfil_activo not in {p.perfil_id for p in permisos}:
        usuario.perfil_activo = permisos[0].perfil_id if permisos else None


@router.get("/certificados-disponibles", dependencies=[Depends(auth.requiere_sesion)])
def certificados_disponibles(principal: Principal = Depends(auth.requiere_sesion),
                             db: Session = Depends(get_db)):
    perfiles = perfiles_permitidos(principal, db)
    activo = principal.perfil_id
    if principal.id is None:
        activo = configuracion_sii.estado()["activo"]
    return {"activo": activo, "perfiles": [
        {"id": p["id"], "nombre": p["nombre"], "vencido": p.get("certificado_vencido", False)}
        for p in perfiles
    ]}


@router.post("/certificados-disponibles/seleccionar", dependencies=[Depends(auth.requiere_sesion)])
def seleccionar_certificado(datos: SeleccionPerfil, principal: Principal = Depends(auth.requiere_sesion),
                            db: Session = Depends(get_db)):
    perfil = exigir_perfil(principal, db, datos.perfil)
    detalle = configuracion_sii.detalle(perfil)
    if not detalle.get("certificado_configurado") or detalle.get("certificado_vencido") or detalle.get("certificado_aun_no_vigente"):
        raise HTTPException(status_code=422, detail="El certificado no está vigente.")
    if principal.id is None:
        configuracion_sii.seleccionar(perfil)
    else:
        usuario = db.get(Usuario, principal.id)
        usuario.perfil_activo = perfil
        db.commit()
    return {"activo": perfil, "nombre_activo": detalle["nombre"]}


@router.get("/finnegans/empresas-permitidas", dependencies=[Depends(auth.requiere_sesion)])
def listar_empresas_permitidas(principal: Principal = Depends(auth.requiere_sesion),
                              db: Session = Depends(get_db)):
    perfil = exigir_perfil(principal, db)
    return {"perfil": perfil, "empresas": empresas_permitidas(principal, db, perfil)}


@router.get("/administracion/usuarios", dependencies=[Depends(auth.requiere_administrador_gestion)])
def listar_usuarios(db: Session = Depends(get_db)):
    return [_usuario_out(u, db) for u in db.scalars(select(Usuario).order_by(Usuario.email))]


@router.get("/administracion/certificados/{perfil_id}/empresas",
            dependencies=[Depends(auth.requiere_administrador_gestion)])
def catalogo_empresas(perfil_id: str):
    if perfil_id not in {p["id"] for p in configuracion_sii.estado()["perfiles"]}:
        raise HTTPException(status_code=404, detail="El certificado no existe.")
    return empresas_finnegans(perfil_id)


@router.get("/administracion/certificados/{perfil_id}/empresas-sii",
            dependencies=[Depends(auth.requiere_administrador_gestion)])
def catalogo_empresas_sii(perfil_id: str, db: Session = Depends(get_db)):
    if perfil_id not in {p["id"] for p in configuracion_sii.estado()["perfiles"]}:
        raise HTTPException(status_code=404, detail="El certificado no existe.")
    return {
        "consultado": db.get(EmpresasPerfilEstado, perfil_id) is not None,
        "empresas": empresas_sii_del_perfil(db, perfil_id),
    }


@router.post("/administracion/usuarios", status_code=201,
             dependencies=[Depends(auth.requiere_administrador_gestion)])
def crear_usuario(datos: DatosUsuario, db: Session = Depends(get_db)):
    email, permisos = _validar_datos(datos)
    if not datos.password or len(datos.password) < 12:
        raise HTTPException(status_code=422, detail="La contraseña inicial debe tener al menos 12 caracteres.")
    if db.scalar(select(Usuario.id).where(Usuario.email == email)):
        raise HTTPException(status_code=409, detail="Ese correo ya tiene una cuenta.")
    usuario = Usuario(email=email, password_hash=auth.hash_password(datos.password),
                      rol=datos.rol, activo=datos.activo)
    db.add(usuario)
    db.flush()
    _guardar_permisos(db, usuario, permisos)
    db.commit()
    return _usuario_out(usuario, db)


@router.put("/administracion/usuarios/{usuario_id}",
            dependencies=[Depends(auth.requiere_administrador_gestion)])
def editar_usuario(usuario_id: int, datos: DatosUsuario,
                   principal: Principal = Depends(auth.requiere_administrador_gestion),
                   db: Session = Depends(get_db)):
    email, permisos = _validar_datos(datos)
    usuario = db.get(Usuario, usuario_id)
    if not usuario:
        raise HTTPException(status_code=404, detail="El usuario no existe.")
    if principal.id == usuario.id and (datos.rol != "administrador" or not datos.activo):
        raise HTTPException(status_code=409, detail="No podés quitarte el acceso de administrador.")
    if usuario.rol == "administrador" and (datos.rol != "administrador" or not datos.activo):
        cantidad = db.scalar(select(func.count()).select_from(Usuario).where(
            Usuario.rol == "administrador", Usuario.activo.is_(True),
        ))
        if cantidad <= 1:
            raise HTTPException(status_code=409, detail="Debe quedar al menos un administrador activo.")
    duplicado = db.scalar(select(Usuario.id).where(Usuario.email == email, Usuario.id != usuario_id))
    if duplicado:
        raise HTTPException(status_code=409, detail="Ese correo ya tiene una cuenta.")
    usuario.email = email
    usuario.rol = datos.rol
    usuario.activo = datos.activo
    if datos.password:
        if len(datos.password) < 12:
            raise HTTPException(status_code=422, detail="La contraseña debe tener al menos 12 caracteres.")
        usuario.password_hash = auth.hash_password(datos.password)
        usuario.intentos_fallidos = 0
        usuario.bloqueado_hasta = None
    _guardar_permisos(db, usuario, permisos)
    usuario.version_sesion += 1
    db.commit()
    return _usuario_out(usuario, db)
