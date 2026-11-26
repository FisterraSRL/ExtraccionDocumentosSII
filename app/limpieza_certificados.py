"""Limpieza de un certificado y de las facturas sin otro perfil que las conserve."""
from __future__ import annotations

import threading

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import configuracion_sii
from app.models import (
    AparicionProductoSII, AsociacionItem, DescuentoGlobalDocumento,
    DescuentoGlobalLinea, DistribucionCentroCosto, Documento, Empresa, EmpresaPerfil,
    EmpresasPerfilEstado, EquivalenciaProducto, HistorialReenvio, ProductoFinnegans,
    ProductoSII, SincronizacionProductos, SugerenciaProducto, Usuario,
    UsuarioCertificado, UsuarioEmpresaFinnegans, UsuarioEmpresaSII,
)

_candados: dict[str, threading.Lock] = {}
_candados_guardia = threading.Lock()


class OperacionPerfilEnCurso(RuntimeError):
    """No se limpia un certificado mientras se sincroniza."""


class AlcanceLimpiezaCambiado(RuntimeError):
    """La vista previa ya no describe lo que se borraría."""


class LimpiezaParcial(RuntimeError):
    """La base quedó limpia, pero falta retirar los secretos del perfil."""


def tomar_bloqueo(perfil_id: str) -> threading.Lock:
    with _candados_guardia:
        candado = _candados.setdefault(perfil_id, threading.Lock())
    if not candado.acquire(blocking=False):
        raise OperacionPerfilEnCurso(
            "Hay una sincronización o limpieza en curso con este certificado. Intentá de nuevo al terminar."
        )
    return candado


def resumen(db: Session, perfil_id: str) -> dict:
    """Calcula el alcance con las relaciones guardadas, sin cambiar datos."""
    perfil = configuracion_sii.detalle(perfil_id)
    ruts = set(db.scalars(select(EmpresaPerfil.empresa_rut).where(
        EmpresaPerfil.perfil_id == perfil_id
    )))
    compartidas = set(db.scalars(select(EmpresaPerfil.empresa_rut).where(
        EmpresaPerfil.perfil_id != perfil_id,
        EmpresaPerfil.empresa_rut.in_(ruts),
    ))) if ruts else set()
    exclusivas = ruts - compartidas
    facturas = db.scalar(select(func.count()).select_from(Documento).where(
        Documento.empresa_rut.in_(exclusivas)
    )) or 0
    conservadas = db.scalar(select(func.count()).select_from(Documento).where(
        Documento.empresa_rut.in_(compartidas)
    )) or 0
    return {
        "perfil": perfil_id,
        "nombre": perfil["nombre"],
        "empresas": len(ruts),
        "empresas_exclusivas": len(exclusivas),
        "empresas_compartidas": len(compartidas),
        "facturas_a_eliminar": facturas,
        "facturas_compartidas_conservadas": conservadas,
        "_ruts_exclusivas": exclusivas,
    }


def eliminar(db: Session, perfil_id: str, facturas_esperadas: int,
            empresas_esperadas: int) -> dict:
    candado = tomar_bloqueo(perfil_id)
    try:
        return _eliminar_bajo_bloqueo(db, perfil_id, facturas_esperadas, empresas_esperadas)
    finally:
        candado.release()


def _eliminar_bajo_bloqueo(db: Session, perfil_id: str, facturas_esperadas: int,
                          empresas_esperadas: int) -> dict:
    impacto = resumen(db, perfil_id)
    if (impacto["facturas_a_eliminar"] != facturas_esperadas or
            impacto["empresas_exclusivas"] != empresas_esperadas):
        raise AlcanceLimpiezaCambiado(
            "Las facturas o empresas cambiaron desde la confirmación. Revisá la limpieza y confirmá otra vez."
        )
    ruts = impacto.pop("_ruts_exclusivas")
    ids = select(Documento.id).where(Documento.empresa_rut.in_(ruts))
    try:
        productos_afectados = set(db.scalars(select(AparicionProductoSII.producto_sii_id).where(
            AparicionProductoSII.documento_id.in_(ids)
        )))
        # Primero se borran los datos que referencian documentos o productos.
        for modelo in (HistorialReenvio, DescuentoGlobalDocumento, DescuentoGlobalLinea,
                       AparicionProductoSII, AsociacionItem, DistribucionCentroCosto):
            db.execute(delete(modelo).where(modelo.documento_id.in_(ids)),
                       execution_options={"synchronize_session": False})
        for modelo in (UsuarioEmpresaSII, UsuarioEmpresaFinnegans, UsuarioCertificado,
                       EquivalenciaProducto, SugerenciaProducto, AsociacionItem,
                       DistribucionCentroCosto, ProductoFinnegans, SincronizacionProductos,
                       EmpresaPerfil, EmpresasPerfilEstado):
            db.execute(delete(modelo).where(modelo.perfil_id == perfil_id),
                       execution_options={"synchronize_session": False})
        db.execute(delete(Documento).where(Documento.empresa_rut.in_(ruts)),
                   execution_options={"synchronize_session": False})
        db.execute(delete(Empresa).where(Empresa.rut.in_(ruts)),
                   execution_options={"synchronize_session": False})

        # El maestro de descripciones es global: conserva entradas usadas por otros
        # documentos o perfiles, y actualiza sus conteos tras quitar apariciones.
        totales = {producto_id: (cantidad, primera, ultima)
                   for producto_id, cantidad, primera, ultima in db.execute(
                       select(AparicionProductoSII.producto_sii_id, func.count(),
                              func.min(Documento.fecha), func.max(Documento.fecha))
                       .join(Documento, Documento.id == AparicionProductoSII.documento_id)
                       .where(AparicionProductoSII.producto_sii_id.in_(productos_afectados))
                       .group_by(AparicionProductoSII.producto_sii_id)
                   )}
        usados = set(db.scalars(select(EquivalenciaProducto.producto_sii_id).where(
            EquivalenciaProducto.producto_sii_id.in_(productos_afectados)
        )))
        usados.update(db.scalars(select(SugerenciaProducto.producto_sii_id).where(
            SugerenciaProducto.producto_sii_id.in_(productos_afectados)
        )))
        for producto in db.scalars(select(ProductoSII).where(ProductoSII.id.in_(productos_afectados))):
            cantidad, primera, ultima = totales.get(producto.id, (0, None, None))
            if not cantidad and producto.id not in usados:
                db.delete(producto)
            else:
                producto.apariciones = cantidad
                producto.primera_aparicion = primera
                producto.ultima_aparicion = ultima

        restantes = [p["id"] for p in configuracion_sii.estado()["perfiles"]
                     if p["id"] != perfil_id]
        for usuario in db.scalars(select(Usuario).where(Usuario.perfil_activo == perfil_id)):
            if usuario.rol == "administrador":
                usuario.perfil_activo = restantes[0] if restantes else None
            else:
                usuario.perfil_activo = db.scalar(select(UsuarioCertificado.perfil_id).where(
                    UsuarioCertificado.usuario_id == usuario.id,
                    UsuarioCertificado.perfil_id.in_(restantes),
                ).order_by(UsuarioCertificado.perfil_id))
        db.commit()
    except Exception:
        db.rollback()
        raise

    # El archivo del certificado y las cookies se quitan después de confirmar la
    # transacción: un error de base nunca debe dejar un perfil sin respaldo de datos.
    try:
        archivos = configuracion_sii.eliminar(perfil_id)
    except Exception as exc:
        raise LimpiezaParcial(
            "Se limpiaron las facturas y permisos, pero no se pudo quitar el certificado. "
            "Volvé a intentar la eliminación desde Configuración."
        ) from exc
    return {**impacto, **archivos}
