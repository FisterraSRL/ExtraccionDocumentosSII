"""La eliminación borra datos exclusivos y conserva los compartidos."""
import json
import os
from datetime import date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import configuracion_sii, limpieza_certificados
from app.db import Base
from app.models import (
    AparicionProductoSII, AsociacionItem, DescuentoGlobalDocumento, Documento,
    Empresa, EmpresaPerfil, EquivalenciaProducto, HistorialReenvio, ProductoFinnegans,
    ProductoSII, Usuario, UsuarioCertificado,
)


def _documento(rut, folio, pdf):
    return Documento(empresa_rut=rut, tipo="33", folio=folio,
                     proveedor_rut="99999999-9", proveedor_nombre="Proveedor de prueba",
                     fecha=date(2025, 1, 1), total=100, pdf_contenido=pdf)


def test_limpia_facturas_exclusivas_y_conserva_las_compartidas():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sesion = sessionmaker(engine)
    try:
        with sesion() as db:
            db.add_all([Empresa(rut=r) for r in ("11111111-1", "22222222-2", "33333333-3")])
            db.add_all([
                EmpresaPerfil(perfil_id="perfil-a", empresa_rut="11111111-1"),
                EmpresaPerfil(perfil_id="perfil-a", empresa_rut="22222222-2"),
                EmpresaPerfil(perfil_id="perfil-b", empresa_rut="22222222-2"),
                EmpresaPerfil(perfil_id="perfil-b", empresa_rut="33333333-3"),
            ])
            exclusivo = _documento("11111111-1", 1, b"pdf-exclusivo")
            compartido = _documento("22222222-2", 2, b"pdf-compartido")
            otro = _documento("33333333-3", 3, b"pdf-otro")
            db.add_all([exclusivo, compartido, otro])
            db.flush()
            producto = ProductoSII(descripcion_original="Item prueba",
                                   descripcion_normalizada="ITEM PRUEBA", apariciones=2)
            ajeno = ProductoSII(descripcion_original="Otro item",
                                descripcion_normalizada="OTRO ITEM", apariciones=0)
            db.add_all([producto, ajeno])
            db.flush()
            db.add_all([
                AparicionProductoSII(documento_id=exclusivo.id, indice=0, producto_sii_id=producto.id),
                AparicionProductoSII(documento_id=compartido.id, indice=0, producto_sii_id=producto.id),
                DescuentoGlobalDocumento(documento_id=exclusivo.id, importe=10),
                HistorialReenvio(documento_id=exclusivo.id, habilitado_at=datetime.utcnow()),
                ProductoFinnegans(perfil_id="perfil-a", codigo="P-A", nombre="Producto A"),
                ProductoFinnegans(perfil_id="perfil-b", codigo="P-B", nombre="Producto B"),
                AsociacionItem(perfil_id="perfil-a", documento_id=exclusivo.id, indice=0,
                               descripcion_firma="Item prueba", producto_codigo="P-A", origen="manual"),
                AsociacionItem(perfil_id="perfil-a", documento_id=compartido.id, indice=0,
                               descripcion_firma="Item prueba", producto_codigo="P-A", origen="manual"),
                AsociacionItem(perfil_id="perfil-b", documento_id=compartido.id, indice=0,
                               descripcion_firma="Item prueba", producto_codigo="P-B", origen="manual"),
                EquivalenciaProducto(perfil_id="perfil-a", producto_sii_id=producto.id,
                                     producto_codigo="P-A", estado="confirmada"),
            ])
            admin = Usuario(email="admin@example.invalid", password_hash="hash",
                            rol="administrador", perfil_activo="perfil-a")
            usuario = Usuario(email="usuario@example.invalid", password_hash="hash",
                              rol="usuario", perfil_activo="perfil-a")
            db.add_all([admin, usuario])
            db.flush()
            db.add_all([
                UsuarioCertificado(usuario_id=usuario.id, perfil_id="perfil-a"),
                UsuarioCertificado(usuario_id=usuario.id, perfil_id="perfil-b"),
            ])
            db.commit()
            ids = exclusivo.id, compartido.id, otro.id
            with patch.object(configuracion_sii, "detalle", return_value={"nombre": "Perfil A"}), patch.object(
                configuracion_sii, "estado", return_value={"perfiles": [
                    {"id": "perfil-a"}, {"id": "perfil-b"}]}
            ), patch.object(configuracion_sii, "eliminar", return_value={
                "activo": "perfil-b", "archivos_pendientes": []
            }) as quitar_archivos:
                resumen = limpieza_certificados.resumen(db, "perfil-a")
                assert (resumen["facturas_a_eliminar"],
                        resumen["facturas_compartidas_conservadas"]) == (1, 1)
                with pytest.raises(limpieza_certificados.AlcanceLimpiezaCambiado):
                    limpieza_certificados.eliminar(db, "perfil-a", 0, 1)
                assert db.get(Documento, exclusivo.id) is not None
                quitar_archivos.assert_not_called()
                ocupado = limpieza_certificados.tomar_bloqueo("perfil-a")
                try:
                    with pytest.raises(limpieza_certificados.OperacionPerfilEnCurso):
                        limpieza_certificados.eliminar(db, "perfil-a", 1, 1)
                finally:
                    ocupado.release()
                resultado = limpieza_certificados.eliminar(db, "perfil-a", 1, 1)
                assert resultado["facturas_a_eliminar"] == 1
                quitar_archivos.assert_called_once_with("perfil-a")

            assert db.get(Documento, ids[0]) is None
            assert db.get(Documento, ids[1]).pdf_contenido == b"pdf-compartido"
            assert db.get(Documento, ids[2]).pdf_contenido == b"pdf-otro"
            assert db.get(Empresa, "11111111-1") is None
            assert db.get(Empresa, "22222222-2") is not None
            assert not db.scalars(select(HistorialReenvio)).all()
            assert not db.scalars(select(DescuentoGlobalDocumento)).all()
            assert [(a.perfil_id, a.documento_id) for a in db.scalars(select(AsociacionItem))] == [
                ("perfil-b", ids[1])]
            assert db.get(ProductoFinnegans, ("perfil-a", "P-A")) is None
            assert db.get(ProductoFinnegans, ("perfil-b", "P-B")) is not None
            assert db.get(ProductoSII, producto.id).apariciones == 1
            assert db.get(ProductoSII, ajeno.id) is not None
            assert db.get(Usuario, admin.id).perfil_activo == "perfil-b"
            assert db.get(Usuario, usuario.id).perfil_activo == "perfil-b"
    finally:
        engine.dispose()


def test_quita_archivo_y_sesion_del_perfil_sin_tocar_el_otro(tmp_path):
    secretos = tmp_path / "secrets"
    secretos.mkdir()
    perfiles = secretos / "sii_certificados.json"
    perfiles.write_text(json.dumps({"activo": "perfil-a", "perfiles": {
        "perfil-a": {"nombre": "A", "rut": "11111111-1", "archivo": "certificado-a.pfx",
                     "password": "solo-prueba"},
        "perfil-b": {"nombre": "B", "rut": "22222222-2", "archivo": "certificado-b.pfx",
                     "password": "solo-prueba"},
    }}), encoding="utf-8")
    for nombre in ("certificado-a.pfx", "certificado-b.pfx",
                   "sii_sesion_perfil-a.json", "sii_estado_perfil-a.json"):
        (secretos / nombre).write_bytes(b"datos-de-prueba")
    with patch.dict(os.environ, {"SII_PERFILES_PERSISTENTES": "1"}), patch.object(
        configuracion_sii, "DIRECTORIO_SECRETOS", secretos
    ), patch.object(configuracion_sii, "ARCHIVO_PERFILES", perfiles), patch.object(
        configuracion_sii, "_configurar_proceso"
    ):
        resultado = configuracion_sii.eliminar("perfil-a")
    assert resultado == {"activo": "perfil-b", "archivos_pendientes": []}
    assert not (secretos / "certificado-a.pfx").exists()
    assert not (secretos / "sii_sesion_perfil-a.json").exists()
    assert not (secretos / "sii_estado_perfil-a.json").exists()
    assert (secretos / "certificado-b.pfx").exists()
    assert list(json.loads(perfiles.read_text(encoding="utf-8"))["perfiles"]) == ["perfil-b"]


def test_al_eliminar_activo_local_cambia_las_variables_al_perfil_restante(tmp_path):
    secretos = tmp_path / "secrets"
    secretos.mkdir()
    perfiles = secretos / "sii_certificados.json"
    entorno = tmp_path / ".env"
    perfiles.write_text(json.dumps({"activo": "perfil-a", "perfiles": {
        "perfil-a": {"nombre": "A", "rut": "11111111-1", "archivo": "certificado-a.pfx",
                     "password": "clave-vieja-de-prueba"},
        "perfil-b": {"nombre": "B", "rut": "22222222-2", "archivo": "certificado-b.pfx",
                     "password": "clave-nueva-de-prueba"},
    }}), encoding="utf-8")
    entorno.write_text("SII_PERFIL=perfil-a\nSII_CERT_PASSWORD=clave-vieja-de-prueba\n",
                       encoding="utf-8")
    with patch.dict(os.environ, {"SII_PERFILES_PERSISTENTES": "0"}), patch.object(
        configuracion_sii, "DIRECTORIO_SECRETOS", secretos
    ), patch.object(configuracion_sii, "ARCHIVO_PERFILES", perfiles), patch.object(
        configuracion_sii, "ARCHIVO_ENV", entorno
    ), patch.object(configuracion_sii, "_configurar_proceso"):
        configuracion_sii.eliminar("perfil-a")
    contenido = entorno.read_text(encoding="utf-8")
    assert "SII_PERFIL=perfil-b" in contenido
    assert "SII_CERT_PASSWORD=clave-nueva-de-prueba" in contenido
    assert "clave-vieja-de-prueba" not in contenido
