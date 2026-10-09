"""Habilitar otro envío no toca Finnegans y conserva el antecedente local."""
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ConfirmacionReenvio, Documento, EstadoDocumento, HistorialReenvio
from app.routers.documents import habilitar_reenvio


def _documento() -> Documento:
    return Documento(
        empresa_rut="00000000-0", tipo="33", folio=123456,
        proveedor_rut="11111111-1", proveedor_nombre="Proveedor de prueba",
        fecha=date(2025, 1, 2), total=1000,
        estado=EstadoDocumento.ENVIADO,
        finnegans_id="COMPROBANTE_PRUEBA",
        fecha_envio=datetime(2025, 1, 3, tzinfo=timezone.utc),
    )


def test_reenvio_exige_confirmacion_y_guarda_antecedente():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor) as db:
        documento = _documento()
        db.add(documento)
        db.commit()

        for confirmacion, estado_esperado in [
            (ConfirmacionReenvio(finnegans_id="COMPROBANTE_PRUEBA"), 400),
            (ConfirmacionReenvio(eliminado_en_finnegans=True, finnegans_id="OTRO"), 409),
        ]:
            try:
                habilitar_reenvio(documento.id, confirmacion, db)
                assert False, "La habilitación debió rechazarse"
            except HTTPException as error:
                assert error.status_code == estado_esperado
            assert documento.estado == EstadoDocumento.ENVIADO
            assert db.execute(select(HistorialReenvio)).scalars().all() == []

        with patch("app.routers.documents._cliente_finnegans", side_effect=AssertionError("No contactar al ERP")):
            habilitar_reenvio(documento.id, ConfirmacionReenvio(
                eliminado_en_finnegans=True, finnegans_id="COMPROBANTE_PRUEBA",
            ), db)
        assert documento.estado == EstadoDocumento.PENDIENTE
        assert documento.finnegans_id is None
        assert documento.fecha_envio is None
        historial = db.execute(select(HistorialReenvio)).scalar_one()
        assert historial.documento_id == documento.id
        assert historial.finnegans_id_anterior == "COMPROBANTE_PRUEBA"
        assert historial.fecha_envio_anterior.date() == date(2025, 1, 3)

        try:
            habilitar_reenvio(documento.id, ConfirmacionReenvio(eliminado_en_finnegans=True), db)
            assert False, "No debe repetirse la habilitación"
        except HTTPException as error:
            assert error.status_code == 409
        assert len(db.execute(select(HistorialReenvio)).scalars().all()) == 1


if __name__ == "__main__":
    test_reenvio_exige_confirmacion_y_guarda_antecedente()
    print("Reenvío: OK")
