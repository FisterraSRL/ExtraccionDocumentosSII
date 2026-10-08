"""El maestro deduplica apariciones y respeta decisiones e historia."""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.equivalencias import (
    ErrorEquivalencia, aplicar_confirmadas, guardar, guardar_varias, listar,
    normalizar, registrar_documentos,
)
from app.models import AsociacionItem, Documento, EquivalenciaProducto, EstadoDocumento, ProductoFinnegans
from app.productos import preparar


def _documento(folio: int, descripcion: str, estado=EstadoDocumento.PENDIENTE):
    return Documento(empresa_rut="00000000-0", tipo="33", folio=folio,
                     proveedor_rut="11111111-1", proveedor_nombre="Proveedor de prueba",
                     fecha=date(2026, 10, folio), total=100, estado=estado,
                     items=[{"desc": descripcion}])


def test_maestro_reutiliza_sin_duplicar_ni_reescribir_enviados():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor) as db:
        db.add_all([
            ProductoFinnegans(perfil_id="perfil-a", codigo="P1", nombre="Cemento Portland 50KG", disponible=True),
            ProductoFinnegans(perfil_id="perfil-a", codigo="P2", nombre="Otro cemento", disponible=True),
            _documento(1, "Cemento Portland - 50 Kg.", EstadoDocumento.ENVIADO),
            _documento(2, "CEMENTO PORTLAND 50KG"),
        ])
        db.commit()
        assert normalizar("Cemento Portland - 50 Kg.") == normalizar("CEMENTO PORTLAND 50KG")
        assert registrar_documentos(db) == 1
        assert registrar_documentos(db) == 0
        maestro = listar(db, "perfil-a")
        assert maestro["total"] == 1
        fila = maestro["filas"][0]
        assert fila["apariciones"] == 2
        assert fila["primera_aparicion"] == "2026-10-01"
        assert fila["ultima_aparicion"] == "2026-10-02"
        pendiente = db.query(Documento).filter_by(folio=2).one()
        assert preparar(db, "perfil-a", pendiente)[0]["producto"] is None
        guardar(db, "perfil-a", fila["id"], "P1", "confirmado")
        enviado = db.query(Documento).filter_by(folio=1).one()
        assert preparar(db, "perfil-a", pendiente)[0]["producto"]["codigo"] == "P1"
        assert preparar(db, "perfil-a", enviado)[0]["producto"] is None
        guardar(db, "perfil-a", fila["id"], "P2", "confirmado")
        nuevo = _documento(3, "CEMENTO PORTLAND 50 KG")
        db.add(nuevo)
        db.commit()
        registrar_documentos(db)
        assert aplicar_confirmadas(db, "perfil-a", "00000000-0") >= 1
        assert db.get(AsociacionItem, ("perfil-a", nuevo.id, 0)).producto_codigo == "P2"
        assert db.get(AsociacionItem, ("perfil-a", enviado.id, 0)) is None
        assert preparar(db, "perfil-a", nuevo)[0]["producto"]["codigo"] == "P2"
        assert preparar(db, "perfil-a", enviado)[0]["producto"] is None
        assert preparar(db, "perfil-b", nuevo)[0]["producto"] is None
        guardar(db, "perfil-a", fila["id"], None, "sin_equivalencia")
        assert preparar(db, "perfil-a", nuevo)[0]["producto"] is None


def test_guardado_conjunto_es_indivisible():
    motor = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(motor)
    with Session(motor) as db:
        db.add(ProductoFinnegans(perfil_id="perfil-a", codigo="P1", nombre="Producto de prueba",
                                 disponible=True))
        db.add_all([_documento(1, "Articulo A"), _documento(2, "Articulo B")])
        db.commit()
        registrar_documentos(db)
        ids = [fila["id"] for fila in listar(db, "perfil-a")["filas"]]
        try:
            guardar_varias(db, "perfil-a", [(ids[0], "P1", "confirmado"),
                                            (ids[1], "NO_EXISTE", "confirmado")])
        except ErrorEquivalencia:
            pass
        else:
            raise AssertionError("El lote inválido no debe guardarse")
        assert db.query(EquivalenciaProducto).count() == 0
        assert guardar_varias(db, "perfil-a", [(ids[0], "P1", "confirmado"),
                                                   (ids[1], None, "sin_equivalencia")]) == 2
        assert db.query(EquivalenciaProducto).count() == 2


if __name__ == "__main__":
    test_maestro_reutiliza_sin_duplicar_ni_reescribir_enviados()
    test_guardado_conjunto_es_indivisible()
    print("Maestro de equivalencias: OK")
