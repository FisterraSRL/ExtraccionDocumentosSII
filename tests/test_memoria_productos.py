"""La memoria aprende correcciones manuales sin propagar decisiones ambiguas."""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Documento, ProductoFinnegans
from app.productos import guardar_asociacion, preparar


def _documento(folio: int, proveedor: str = "11111111-1", codigo: str = "SVC") -> Documento:
    return Documento(
        empresa_rut="00000000-0", tipo="33", folio=folio,
        proveedor_rut=proveedor, proveedor_nombre="Proveedor de prueba",
        fecha=date(2026, 1, 1), total=100,
        items=[{"desc": "SERVICIO DE MANTENCION MENSUAL", "codigo": codigo}],
    )


def test_memoria_manual_y_conflictos():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor) as db:
        db.add_all([
            ProductoFinnegans(perfil_id="perfil-a", codigo="A", nombre="Gastos generales", disponible=True),
            ProductoFinnegans(perfil_id="perfil-a", codigo="B", nombre="Servicio de mantención mensual", disponible=True),
        ])
        origen, repetido, otra_eleccion, futuro = [_documento(n) for n in range(1, 5)]
        otro_proveedor = _documento(5, proveedor="22222222-2")
        otro_codigo = _documento(6, codigo="OTRO")
        db.add_all([origen, repetido, otra_eleccion, futuro, otro_proveedor, otro_codigo])
        db.commit()

        # La coincidencia por nombre se había asignado antes de la corrección humana.
        assert preparar(db, "perfil-a", repetido)[0]["producto"]["codigo"] == "B"
        guardar_asociacion(db, "perfil-a", origen, 0, "A")
        aprendido = preparar(db, "perfil-a", repetido)[0]
        assert aprendido["producto"]["codigo"] == "A"
        assert aprendido["origen"] == "memoria"
        assert aprendido["sugerencias"][0]["memoria"] == 1

        # Dos elecciones distintas impiden tanto la memoria como el matching por nombre.
        guardar_asociacion(db, "perfil-a", otra_eleccion, 0, "B")
        assert preparar(db, "perfil-a", repetido)[0]["producto"] is None
        assert preparar(db, "perfil-a", futuro)[0]["producto"] is None
        assert preparar(db, "perfil-a", origen)[0]["producto"]["codigo"] == "A"

        # Limpiar una elección sigue siendo una señal de que no hay acuerdo.
        guardar_asociacion(db, "perfil-a", otra_eleccion, 0, None)
        assert preparar(db, "perfil-a", futuro)[0]["producto"] is None
        guardar_asociacion(db, "perfil-a", otra_eleccion, 0, "A")
        assert preparar(db, "perfil-a", futuro)[0]["producto"]["codigo"] == "A"

        # El proveedor, el código SII y el perfil delimitan lo aprendido.
        assert preparar(db, "perfil-a", otro_proveedor)[0]["producto"]["codigo"] == "B"
        assert preparar(db, "perfil-a", otro_codigo)[0]["producto"]["codigo"] == "B"
        assert preparar(db, "perfil-b", futuro)[0]["producto"] is None


if __name__ == "__main__":
    test_memoria_manual_y_conflictos()
    print("Memoria manual, correcciones, conflictos y aislamiento: OK")
