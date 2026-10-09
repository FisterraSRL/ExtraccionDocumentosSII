"""Un comprobante repetido se muestra como existente en ERP sin volver a enviarlo."""
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, init_db
from app.finnegans.client import FinnegansSendResult, referencia_comprobante_repetido
from app.models import (
    ConfirmacionReenvio, Documento, EstadoDocumento, HistorialReenvio,
)
from app.routers.documents import _enviar, habilitar_reenvio


AVISO_REPETIDO = (
    'POST facturaCompra → HTTP 500: {"error":"Internal Server Error: Aviso \\n\\t'
    'Comprobante repetido \\n\\tEl número de comprobante 101415 del '
    'cliente/proveedor Proveedor de prueba ya existe en la transaccion FC - 46773.",'
    '"status":500}'
)


def _documento() -> Documento:
    return Documento(
        empresa_rut="00000000-0", tipo="33", folio=101415,
        proveedor_rut="11111111-1", proveedor_nombre="Proveedor de prueba",
        fecha=date(2025, 1, 2), total=1000,
        estado=EstadoDocumento.ERROR, error_detalle=AVISO_REPETIDO,
    )


def test_duplicado_exige_folio_y_subtipo_correctos():
    assert referencia_comprobante_repetido(AVISO_REPETIDO, 101415, "33") == "FC - 46773"
    assert referencia_comprobante_repetido(AVISO_REPETIDO, 101416, "33") is None
    assert referencia_comprobante_repetido(AVISO_REPETIDO, 101415, "61") is None
    assert referencia_comprobante_repetido("HTTP 500: otro error", 101415, "33") is None


def test_envio_duplicado_se_marca_externo_y_reenvio_guarda_origen():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor) as db:
        documento = _documento()
        db.add(documento)
        db.commit()

        class ClientePrueba:
            perfil_id = "perfil-prueba"

            def send_document(self, *args, **kwargs):
                return FinnegansSendResult(False, None, AVISO_REPETIDO)

        with patch("app.routers.documents._codigos_seleccionados", return_value={}), \
             patch("app.routers.documents._descuento_global", return_value=None):
            resultado = _enviar(db, documento, ClientePrueba())
        assert resultado.estado == EstadoDocumento.ENVIADO
        assert resultado.origen_envio == "externo"
        assert documento.finnegans_id == "FC - 46773"
        assert documento.fecha_envio is None
        assert documento.error_detalle == AVISO_REPETIDO

        habilitar_reenvio(documento.id, ConfirmacionReenvio(
            eliminado_en_finnegans=True, finnegans_id="FC - 46773",
        ), db)
        assert documento.estado == EstadoDocumento.PENDIENTE
        assert documento.origen_envio is None
        historial = db.query(HistorialReenvio).one()
        assert historial.finnegans_id_anterior == "FC - 46773"
        assert historial.origen_envio_anterior == "externo"


def test_inicio_reclasifica_errores_anteriores_sin_nuevo_post():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    sesiones = sessionmaker(bind=motor, autoflush=False)
    with sesiones() as db:
        repetido = _documento()
        otro = _documento()
        otro.folio = 101416
        otro.error_detalle = "POST facturaCompra → HTTP 500: importe total incorrecto"
        db.add_all([repetido, otro])
        db.commit()
        ids = repetido.id, otro.id

    with patch("app.db.engine", motor), patch("app.db.SessionLocal", sesiones):
        init_db()
        init_db()  # idempotente al reiniciar

    with sesiones() as db:
        repetido = db.get(Documento, ids[0])
        otro = db.get(Documento, ids[1])
        assert repetido.estado == EstadoDocumento.ENVIADO
        assert repetido.origen_envio == "externo"
        assert repetido.finnegans_id == "FC - 46773"
        assert repetido.error_detalle == AVISO_REPETIDO
        assert otro.estado == EstadoDocumento.ERROR
        assert otro.origen_envio is None


if __name__ == "__main__":
    test_duplicado_exige_folio_y_subtipo_correctos()
    test_envio_duplicado_se_marca_externo_y_reenvio_guarda_origen()
    test_inicio_reclasifica_errores_anteriores_sin_nuevo_post()
    print("Comprobantes ya existentes en Finnegans: OK")
