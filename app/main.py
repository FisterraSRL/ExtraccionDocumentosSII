import logging
import os
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.orm import Session
from typing import Literal

from app.db import get_db, init_db
from app.models import EmpresaPerfil, EmpresasPerfilEstado, Usuario
from app.version import __version__
from app import auth
from app.routers import documents
from app.routers import productos as productos_router
from app.routers import equivalencias as equivalencias_router
from app import configuracion_sii
from app import contexto_usuario
from app import usuarios
from app import limpieza_certificados

# Sin esto, lo que la app registra en el log no sale por ningún lado: uvicorn configura
# sus propios loggers y deja el resto en WARNING sin handler. Costó un diagnóstico: un
# envío a Finnegans falló y el motivo, que sí se registraba, no quedó en ninguna parte.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)

app = FastAPI(title="SII → Finnegans", version=__version__)

# El portal corre en el mismo origen. CORS conserva compatibilidad con consultas
# públicas de desarrollo; auth.requiere_sesion rechaza orígenes ajenos en las rutas
# protegidas, incluido el revelado de credenciales y los envíos al ERP.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(productos_router.router)
app.include_router(equivalencias_router.router)
app.include_router(usuarios.router)

STATIC_DIR = Path(__file__).parent / "static"


@app.middleware("http")
async def identidad_del_pedido(request: Request, siguiente):
    principal = auth.sesion_valida(request.cookies.get(auth.COOKIE)) or auth.sesion_automatica(request)
    token = contexto_usuario.establecer(principal)
    try:
        return await siguiente(request)
    finally:
        contexto_usuario.restaurar(token)


@app.on_event("startup")
def on_startup() -> None:
    configuracion_sii.restaurar_perfil_persistente()
    init_db()
    auth.sembrar_administrador()


class Credenciales(BaseModel):
    usuario: str
    password: str


class ConfiguracionSII(BaseModel):
    nombre: str
    rut: str
    password: str
    certificado_b64: str = Field(max_length=14 * 1024 * 1024)
    client_id: str = ""
    client_secret: str | None = ""


class EdicionConfiguracionSII(BaseModel):
    nombre: str
    rut: str
    password: str | None = None
    certificado_b64: str | None = Field(default=None, max_length=14 * 1024 * 1024)
    client_id: str | None = None
    client_secret: str | None = None


class SeleccionCertificado(BaseModel):
    perfil: str


class ConfirmacionEliminacion(BaseModel):
    confirmar: Literal[True]
    facturas_esperadas: int = Field(ge=0)
    empresas_esperadas: int = Field(ge=0)


class SolicitudRevelar(BaseModel):
    campo: Literal["password", "client_id", "client_secret"]


@app.post("/api/login")
def login(datos: Credenciales, response: Response, request: Request):
    """Valida una cuenta activa sin devolver información sobre usuarios inexistentes."""
    auth._validar_origen(request)
    try:
        principal = auth.autenticar(datos.usuario, datos.password)
        if principal is None:
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")
    except auth.ConfiguracionAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    auth.emitir_cookie(response, principal)
    response.headers["Cache-Control"] = "no-store"
    return {"usuario": principal.email, "administrador": principal.administrador}


@app.post("/api/logout")
def logout(response: Response, request: Request):
    auth._validar_origen(request)
    auth.borrar_cookie(response)
    return {"ok": True}


@app.get("/api/version")
def version():
    """Versión del portal. Sin sesión: la muestra el encabezado antes de entrar."""
    return {"version": __version__}


@app.get("/api/sesion")
def sesion(request: Request, portal_sesion: str | None = Cookie(default=None)):
    """Le dice al portal si ya hay sesión, para mostrar la bandeja o el formulario."""
    local = auth.sesion_automatica(request)
    principal = auth.sesion_valida(portal_sesion) or local
    return {
        "autenticado": principal is not None,
        "configurada": auth.auth_configurada(),
        "usuario": principal.email if principal else None,
        "administrador": principal.administrador if principal else False,
        # El portal usa esto para no mostrar un "Salir" que no podría cerrar nada.
        "sin_login": local is not None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/configuracion/sii", dependencies=[Depends(auth.requiere_administrador_gestion)])
def estado_configuracion_sii(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return configuracion_sii.estado()


@app.post("/api/configuracion/sii", dependencies=[Depends(auth.requiere_administrador_gestion)])
def guardar_configuracion_sii(datos: ConfiguracionSII, response: Response,
                             principal: contexto_usuario.Principal = Depends(auth.requiere_administrador_gestion),
                             db: Session = Depends(get_db)):
    try:
        response.headers["Cache-Control"] = "no-store"
        resultado = configuracion_sii.guardar(datos.nombre, datos.rut, datos.password, datos.certificado_b64,
                                              datos.client_id, datos.client_secret)
        if principal.id is not None and resultado.get("activo"):
            usuario = db.get(Usuario, principal.id)
            usuario.perfil_activo = resultado["activo"]
            db.commit()
        return resultado
    except configuracion_sii.CertificadoDuplicadoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/configuracion/sii/seleccionar", dependencies=[Depends(auth.requiere_administrador_gestion)])
def seleccionar_configuracion_sii(datos: SeleccionCertificado, response: Response,
                                  principal: contexto_usuario.Principal = Depends(auth.requiere_administrador_gestion),
                                  db: Session = Depends(get_db)):
    try:
        response.headers["Cache-Control"] = "no-store"
        usuarios.seleccionar_certificado(usuarios.SeleccionPerfil(perfil=datos.perfil), principal, db)
        estado = configuracion_sii.estado()
        estado["activo"] = datos.perfil
        estado["nombre_activo"] = configuracion_sii.detalle(datos.perfil)["nombre"]
        return estado
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/configuracion/sii/{perfil}", dependencies=[Depends(auth.requiere_administrador_gestion)])
def detalle_configuracion_sii(perfil: str, response: Response):
    try:
        response.headers["Cache-Control"] = "no-store"
        return configuracion_sii.detalle(perfil)
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/configuracion/sii/{perfil}/limpieza",
         dependencies=[Depends(auth.requiere_administrador_gestion)])
def resumen_limpieza_certificado(perfil: str, response: Response,
                                db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        resultado = limpieza_certificados.resumen(db, perfil)
        resultado.pop("_ruts_exclusivas")
        return resultado
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/configuracion/sii/{perfil}",
            dependencies=[Depends(auth.requiere_administrador_gestion)])
def eliminar_configuracion_sii(perfil: str, confirmacion: ConfirmacionEliminacion,
                              response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return limpieza_certificados.eliminar(db, perfil, confirmacion.facturas_esperadas,
                                             confirmacion.empresas_esperadas)
    except (limpieza_certificados.OperacionPerfilEnCurso,
            limpieza_certificados.AlcanceLimpiezaCambiado) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except limpieza_certificados.LimpiezaParcial as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/configuracion/sii/{perfil}/revelar", dependencies=[Depends(auth.requiere_administrador_gestion)])
def revelar_configuracion_sii(perfil: str, datos: SolicitudRevelar, response: Response):
    try:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {"valor": configuracion_sii.revelar(perfil, datos.campo)}
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/configuracion/sii/{perfil}", dependencies=[Depends(auth.requiere_administrador_gestion)])
def editar_configuracion_sii(
    perfil: str, datos: EdicionConfiguracionSII, response: Response,
    db: Session = Depends(get_db),
):
    try:
        response.headers["Cache-Control"] = "no-store"
        rut_anterior = configuracion_sii.detalle(perfil)["rut"]
        resultado = configuracion_sii.editar(perfil, datos.nombre, datos.rut, datos.password,
                                             datos.certificado_b64, datos.client_id, datos.client_secret)
        if datos.certificado_b64 or datos.rut.strip().upper() != rut_anterior:
            db.execute(delete(EmpresaPerfil).where(EmpresaPerfil.perfil_id == perfil))
            db.execute(delete(EmpresasPerfilEstado).where(EmpresasPerfilEstado.perfil_id == perfil))
            db.commit()
        return resultado
    except configuracion_sii.CertificadoDuplicadoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except configuracion_sii.ConfiguracionSIIError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/configuracion", include_in_schema=False, dependencies=[Depends(auth.requiere_administrador_gestion)])
def configuracion() -> FileResponse:
    return FileResponse(STATIC_DIR / "configuracion.html", headers={"Cache-Control": "no-store"})


@app.get("/usuarios", include_in_schema=False, dependencies=[Depends(auth.requiere_administrador_gestion)])
def pagina_usuarios() -> FileResponse:
    return FileResponse(STATIC_DIR / "usuarios.html", headers={"Cache-Control": "no-store"})


@app.get("/equivalencias", include_in_schema=False, dependencies=[Depends(auth.requiere_sesion_gestion)])
def pagina_equivalencias() -> FileResponse:
    return FileResponse(STATIC_DIR / "equivalencias.html", headers={"Cache-Control": "no-store"})


@app.get("/manual", include_in_schema=False, dependencies=[Depends(auth.requiere_sesion)])
def pagina_manual() -> FileResponse:
    return FileResponse(STATIC_DIR / "manual.html", headers={"Cache-Control": "no-store"})


@app.get("/equivalencias-icon.png", include_in_schema=False)
def icono_equivalencias() -> FileResponse:
    return FileResponse(STATIC_DIR / "equivalencias-icon.png", media_type="image/png")


@app.get("/selectores-buscables.js", include_in_schema=False)
def script_selectores_buscables() -> FileResponse:
    return FileResponse(STATIC_DIR / "selectores-buscables.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/configuracion-icon.jpg", include_in_schema=False)
def icono_configuracion() -> FileResponse:
    return FileResponse(STATIC_DIR / "configuracion-icon.jpg", media_type="image/jpeg")


@app.get("/", include_in_schema=False)
def portal() -> FileResponse:
    """Sirve el portal (Bandeja SII) directamente desde el backend, mismo origen que
    la API — así no hace falta CORS ni preocuparse por "mixed content" del navegador
    mientras todo corre local. Es el mismo diseño del artifact publicado en claude.ai,
    ahora conectado a datos reales en vez de datos de ejemplo."""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
