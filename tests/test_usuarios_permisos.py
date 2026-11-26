"""Límites de acceso del portal sin contactar al SII ni al ERP."""
import os
import unittest
from datetime import date, datetime
from http.cookies import SimpleCookie
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth, usuarios
from app.contexto_usuario import Principal, actual, establecer, restaurar
from app.db import Base
from app.models import Documento, Empresa, EmpresaPerfil, EmpresasPerfilEstado, Usuario, UsuarioCertificado
from app.routers.documents import _empresas_a_sincronizar, _exigir_documento_permitido, enviar_documento, listar_empresas


class PermisosDeUsuario(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sesiones = sessionmaker(self.engine)
        self.entorno = patch.dict(os.environ, {
            "PORTAL_ADMIN_EMAIL": "admin@example.invalid",
            "PORTAL_ADMIN_PASSWORD": "clave-de-prueba-inicial",
            "SECRET_KEY": "firma-de-prueba-no-productiva",
        })
        self.entorno.start()
        self.patches = [
            patch.object(auth, "SessionLocal", self.sesiones),
            patch.object(usuarios.configuracion_sii, "estado", return_value={
                "activo": "uno", "perfiles": [
                    {"id": "uno", "nombre": "Uno"},
                    {"id": "dos", "nombre": "Dos"},
                ],
            }),
            patch.object(usuarios.configuracion_sii, "detalle", side_effect=lambda p: {
                "id": p, "nombre": p, "certificado_configurado": True,
            }),
            patch.object(usuarios.configuracion_sii, "perfil_activo_id", side_effect=lambda: actual().perfil_id),
            patch.object(usuarios, "empresas_finnegans", return_value=[
                {"codigo": "ERP-A", "nombre": "A"},
                {"codigo": "ERP-B", "nombre": "B"},
            ]),
        ]
        for parche in self.patches:
            parche.start()
        auth.sembrar_administrador()

    def tearDown(self):
        for parche in reversed(self.patches):
            parche.stop()
        self.entorno.stop()
        self.engine.dispose()

    def test_administrador_inicial_y_sesion_revocable(self):
        with self.sesiones() as db:
            admin = db.query(Usuario).one()
            self.assertEqual(admin.rol, "administrador")
            self.assertNotEqual(admin.password_hash, os.environ["PORTAL_ADMIN_PASSWORD"])
            self.assertTrue(auth.verificar_password(os.environ["PORTAL_ADMIN_PASSWORD"], admin.password_hash))
        auth.sembrar_administrador()
        with self.sesiones() as db:
            self.assertEqual(db.query(Usuario).count(), 1)
        principal = auth.autenticar("admin@example.invalid", "clave-de-prueba-inicial")
        self.assertTrue(principal.administrador)
        self.assertIsNone(auth.autenticar("admin@example.invalid", "incorrecta"))
        respuesta = Response()
        auth.emitir_cookie(respuesta, principal)
        cookie = SimpleCookie()
        cookie.load(respuesta.headers["set-cookie"])
        valor = cookie[auth.COOKIE].value
        self.assertEqual(auth.sesion_valida(valor).id, principal.id)
        with self.sesiones() as db:
            admin = db.get(Usuario, principal.id)
            admin.version_sesion += 1
            db.commit()
        self.assertIsNone(auth.sesion_valida(valor))

    def test_asignaciones_se_aplican_a_certificados_empresas_y_documentos(self):
        with self.sesiones() as db:
            usuario = Usuario(email="usuario@example.invalid", password_hash=auth.hash_password("clave-de-prueba-regular"),
                              rol="usuario", perfil_activo="uno")
            db.add(usuario)
            db.flush()
            db.add_all([
                Empresa(rut="11111111-1"), Empresa(rut="22222222-2"),
                EmpresaPerfil(perfil_id="uno", empresa_rut="11111111-1", en_portal_fe=True),
                EmpresaPerfil(perfil_id="uno", empresa_rut="22222222-2", en_portal_fe=True),
                EmpresasPerfilEstado(perfil_id="uno", consultado_at=datetime.utcnow()),
            ])
            db.flush()
            usuarios._guardar_permisos(db, usuario, [usuarios.PermisoCertificado(
                perfil_id="uno", empresas=["ERP-A"], empresas_sii=["11111111-1"],
            )])
            db.commit()
            principal = Principal(usuario.id, usuario.email, usuario.rol, "uno")
            self.assertEqual([p["id"] for p in usuarios.perfiles_permitidos(principal, db)], ["uno"])
            self.assertEqual(
                [p["id"] for p in usuarios.certificados_disponibles(principal, db)["perfiles"]], ["uno"],
            )
            with self.assertRaises(HTTPException) as error:
                usuarios.seleccionar_certificado(usuarios.SeleccionPerfil(perfil="dos"), principal, db)
            self.assertEqual(error.exception.status_code, 403)
            self.assertEqual(usuarios.exigir_empresa(principal, db, "uno", "ERP-A"), "ERP-A")
            with self.assertRaises(HTTPException) as error:
                usuarios.exigir_empresa(principal, db, "uno", "ERP-B")
            self.assertEqual(error.exception.status_code, 403)
            with self.assertRaises(HTTPException) as error:
                usuarios.exigir_perfil(principal, db, "dos")
            self.assertEqual(error.exception.status_code, 403)
            token = establecer(principal)
            try:
                permitido = Documento(empresa_rut="11111111-1")
                prohibido = Documento(empresa_rut="22222222-2")
                _exigir_documento_permitido(db, permitido)
                with self.assertRaises(HTTPException) as error:
                    _exigir_documento_permitido(db, prohibido)
                self.assertEqual(error.exception.status_code, 403)
                self.assertEqual([e.rut for e in listar_empresas(db)], ["11111111-1"])
                self.assertEqual(_empresas_a_sincronizar(db, "todas", "11111111-1"), ["11111111-1"])
                with self.assertRaises(HTTPException) as error:
                    _empresas_a_sincronizar(db, "22222222-2", "11111111-1")
                self.assertEqual(error.exception.status_code, 403)
            finally:
                restaurar(token)

    def test_empresa_prohibida_detiene_envio_antes_del_cliente_erp(self):
        with self.sesiones() as db:
            usuario = Usuario(email="usuario@example.invalid", password_hash=auth.hash_password("clave-de-prueba-regular"),
                              rol="usuario", perfil_activo="uno")
            db.add(usuario)
            db.flush()
            db.add(Empresa(rut="11111111-1"))
            db.add(EmpresasPerfilEstado(perfil_id="uno", consultado_at=datetime.utcnow()))
            db.add(EmpresaPerfil(perfil_id="uno", empresa_rut="11111111-1", en_portal_fe=True))
            db.flush()
            usuarios._guardar_permisos(db, usuario, [usuarios.PermisoCertificado(
                perfil_id="uno", empresas=["ERP-A"], empresas_sii=["11111111-1"],
            )])
            documento = Documento(
                empresa_rut="11111111-1", tipo="33", folio=123,
                proveedor_rut="22222222-2", proveedor_nombre="Proveedor de prueba",
                fecha=date(2025, 1, 2), neto=100, iva=19, exento=0, total=119,
            )
            db.add(documento)
            db.commit()
            token = establecer(Principal(usuario.id, usuario.email, usuario.rol, "uno"))
            try:
                with patch("app.routers.documents._cliente_finnegans") as cliente:
                    with self.assertRaises(HTTPException) as error:
                        enviar_documento(documento.id, db, empresa_codigo="ERP-B")
                    self.assertEqual(error.exception.status_code, 403)
                    cliente.assert_not_called()
            finally:
                restaurar(token)

    def test_catalogo_sii_y_asignacion_rechazan_empresas_ajenas(self):
        with self.sesiones() as db:
            db.add_all([
                Empresa(rut="11111111-1", nombre="Empresa A"),
                Empresa(rut="22222222-2", nombre="Empresa B"),
                EmpresaPerfil(perfil_id="uno", empresa_rut="11111111-1"),
                EmpresaPerfil(perfil_id="dos", empresa_rut="22222222-2"),
                EmpresasPerfilEstado(perfil_id="uno", consultado_at=datetime.utcnow()),
            ])
            db.flush()
            self.assertEqual(usuarios.catalogo_empresas_sii("uno", db), {
                "consultado": True,
                "empresas": [{"rut": "11111111-1", "nombre": "Empresa A"}],
            })
            usuario = Usuario(email="usuario@example.invalid", password_hash="hash", rol="usuario")
            db.add(usuario)
            db.flush()
            with self.assertRaises(HTTPException) as error:
                usuarios._guardar_permisos(db, usuario, [usuarios.PermisoCertificado(
                    perfil_id="uno", empresas_sii=["22222222-2"],
                )])
            self.assertEqual(error.exception.status_code, 422)

    def test_asignacion_anterior_conserva_acceso_y_admin_tiene_acceso_total(self):
        with self.sesiones() as db:
            db.add_all([
                Empresa(rut="11111111-1"), Empresa(rut="22222222-2"),
                EmpresaPerfil(perfil_id="uno", empresa_rut="11111111-1"),
                EmpresaPerfil(perfil_id="uno", empresa_rut="22222222-2"),
            ])
            usuario = Usuario(email="usuario@example.invalid", password_hash="hash", rol="usuario")
            db.add(usuario)
            db.flush()
            db.add(UsuarioCertificado(usuario_id=usuario.id, perfil_id="uno"))
            db.commit()
            principal = Principal(usuario.id, usuario.email, usuario.rol, "uno")
            esperado = ["11111111-1", "22222222-2"]
            self.assertEqual(usuarios.ruts_sii_permitidos(principal, db, "uno"), esperado)
            self.assertEqual(usuarios._usuario_out(usuario, db)["certificados"][0]["empresas_sii"], esperado)
            admin = Principal(999, "admin@example.invalid", "administrador", "uno")
            self.assertEqual(usuarios.ruts_sii_permitidos(admin, db, "uno"), esperado)


def test_catalogo_de_destinos_usa_sucursales_activas():
    usuarios._cache_empresas.clear()
    filas = [
        {"codigo": "B", "nombre": "Sucursal B", "activo": True},
        {"codigo": "A", "nombre": "Sucursal A", "activo": True},
        {"codigo": "C", "nombre": "Sucursal C", "activo": False},
    ]
    with patch.object(usuarios.configuracion_sii, "credenciales_finnegans_para_perfil",
                      return_value=("perfil-prueba", "id-prueba", "secreto-prueba")), patch.object(
        usuarios, "FinnegansClient"
    ) as cliente:
        cliente.return_value.catalogo.return_value = filas
        empresas = usuarios.empresas_finnegans("perfil-prueba")
    assert empresas == [
        {"codigo": "A", "nombre": "Sucursal A"},
        {"codigo": "B", "nombre": "Sucursal B"},
    ]
    cliente.return_value.catalogo.assert_called_once_with("empresaSucursal")
    usuarios._cache_empresas.clear()


def test_sugerencia_por_rut_respeta_empresas_sii_y_destinos_permitidos():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sesiones = sessionmaker(engine)
    try:
        with sesiones() as db:
            usuario = Usuario(email="sugerencia@example.invalid", password_hash="hash", rol="usuario")
            db.add(usuario)
            db.flush()
            db.add_all([
                Empresa(rut="11111111-1"), Empresa(rut="22222222-2"),
                EmpresaPerfil(perfil_id="uno", empresa_rut="11111111-1", en_portal_fe=True),
                EmpresaPerfil(perfil_id="uno", empresa_rut="22222222-2", en_portal_fe=True),
            ])
            db.flush()
            with patch.object(usuarios.configuracion_sii, "estado", return_value={
                "activo": "uno", "perfiles": [{"id": "uno", "nombre": "Uno"}]
            }), patch.object(usuarios, "empresas_finnegans", return_value=[
                {"codigo": "ERP-A", "nombre": "Sucursal A"},
                {"codigo": "ERP-B", "nombre": "Sucursal B"},
            ]):
                usuarios._guardar_permisos(db, usuario, [usuarios.PermisoCertificado(
                    perfil_id="uno", empresas=["ERP-A"], empresas_sii=["11111111-1"],
                )])
                db.commit()
                principal = Principal(usuario.id, usuario.email, usuario.rol, "uno")
                with patch.object(usuarios.configuracion_sii, "credenciales_finnegans_para_perfil",
                                  return_value=("uno", "id", "secreto")), patch.object(
                    usuarios, "FinnegansClient"
                ) as cliente:
                    cliente.return_value.empresa_para_rut.return_value = "ERP-A"
                    assert usuarios.sugerir_empresa_finnegans(principal, db, "uno", "11111111-1") == "ERP-A"
                    cliente.return_value.empresa_para_rut.return_value = "ERP-B"
                    assert usuarios.sugerir_empresa_finnegans(principal, db, "uno", "11111111-1") == ""
                    with pytest.raises(HTTPException) as denegado:
                        usuarios.sugerir_empresa_finnegans(principal, db, "uno", "22222222-2")
                    assert denegado.value.status_code == 403
                with patch.object(usuarios, "empresas_finnegans", return_value=[
                    {"codigo": "ERP-A", "nombre": "Sucursal A", "rut": "11.111.111-1"},
                ]), patch.object(usuarios, "FinnegansClient") as cliente:
                    assert usuarios.sugerir_empresa_finnegans(principal, db, "uno", "11111111-1") == "ERP-A"
                    cliente.assert_not_called()
    finally:
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
