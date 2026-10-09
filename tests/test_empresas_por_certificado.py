"""La bandeja muestra solo las empresas consultadas con el certificado activo."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import Response

from app.config import Settings, settings
from app.db import Base
from app.main import EdicionConfiguracionSII, editar_configuracion_sii
from app.models import Empresa, EmpresasPerfilEstado
from app.routers import documents
from app import configuracion_sii


class _Cerrar:
    def close(self):
        pass

    def stop(self):
        pass


class _SIIPrueba:
    ruts = []

    def __init__(self, *args):
        pass

    def abrir_sesion(self, **kwargs):
        return None, _Cerrar(), _Cerrar(), _Cerrar()

    def get_empresas(self, **kwargs):
        return self.ruts

    def get_empresas_con_nombre(self, **kwargs):
        return [{"rut": rut, "nombre": "Empresa de prueba"} for rut in self.ruts]

    def close(self):
        pass


def test_la_lista_y_sincronizacion_se_aislan_por_certificado():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    sii_falso = SimpleNamespace(
        SIIClient=_SIIPrueba,
        SIIBloqueadoError=type("Bloqueado", (Exception,), {}),
        SIIAuthenticationError=type("Autenticacion", (Exception,), {}),
        SIIRCVError=type("RCV", (Exception,), {}),
    )
    with Session(motor, autoflush=False) as db, patch.object(
        Settings, "require_sii_credentials",
        return_value=("00000000-0", "archivo_prueba.pfx", "clave_prueba"),
    ), patch.object(documents, "_sin_navegador", return_value=None), patch.object(
        documents, "_sii", return_value=sii_falso,
    ):
        db.add(Empresa(rut="00000000-0"))
        db.commit()
        with patch.object(documents, "settings", replace(settings, sii_perfil="perfil-a")):
            _SIIPrueba.ruts = ["00000000-0"]
            documents.refrescar_empresas(db)
            assert [e.rut for e in documents.listar_empresas(db)] == ["00000000-0"]
            with patch.object(documents, "refrescar_empresas", side_effect=AssertionError("No consultar SII")):
                assert [e.rut for e in documents.asegurar_empresas_perfil(db)] == ["00000000-0"]

        with patch.object(documents, "settings", replace(settings, sii_perfil="perfil-b")):
            assert documents.listar_empresas(db) == []
            _SIIPrueba.ruts = ["11111111-1"]
            documents.refrescar_empresas(db)
            assert [e.rut for e in documents.listar_empresas(db)] == ["11111111-1"]
            assert documents._empresas_a_sincronizar(db, "todas", "00000000-0") == ["11111111-1"]

        with patch.object(documents, "settings", replace(settings, sii_perfil="perfil-a")):
            assert [e.rut for e in documents.listar_empresas(db)] == ["00000000-0"]
            assert documents._empresas_a_sincronizar(db, "todas", "00000000-0") == ["00000000-0"]

        with patch.object(configuracion_sii, "detalle", return_value={"rut": "00000000-0"}), patch.object(
            configuracion_sii, "editar", return_value={"rut": "00000000-0"},
        ):
            editar_configuracion_sii(
                "perfil-a", EdicionConfiguracionSII(nombre="Nuevo nombre", rut="00000000-0"),
                None, Response(), "", None, db,
            )
            assert db.get(EmpresasPerfilEstado, "perfil-a") is not None
            editar_configuracion_sii(
                "perfil-a", EdicionConfiguracionSII(nombre="Nuevo nombre", rut="11111111-1"),
                None, Response(), "", None, db,
            )
            assert db.get(EmpresasPerfilEstado, "perfil-a") is None


if __name__ == "__main__":
    test_la_lista_y_sincronizacion_se_aislan_por_certificado()
    print("Empresas por certificado: OK")
