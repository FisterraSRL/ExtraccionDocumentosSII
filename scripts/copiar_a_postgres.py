#!/usr/bin/env python3
"""Copia empresas, documentos y descuentos globales de una base a otra. Pensado para SQLite → Postgres.

Para qué: al pasar el portal a la nube, la base deja de ser el `sii_finnegans.db` local
y pasa a ser un Postgres alojado. Sin esto habría que volver a sincronizar todo contra
el SII, que además de lento presiona el límite de autenticaciones (ver AGENTS.md).

Uso:

    python scripts/copiar_a_postgres.py "postgresql://usuario:clave@host/base"

El origen es el `DATABASE_URL` del `.env` (por defecto el SQLite local); se puede
cambiar con `--origen`. Es **idempotente**: los documentos ya presentes en el destino
se actualizan en vez de duplicarse, así que se puede correr más de una vez sin miedo.

No borra nada en el destino ni toca el origen.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import settings  # noqa: E402
from app.db import Base, _url_normalizada  # noqa: E402
from app.models import DescuentoGlobalDocumento, DescuentoGlobalLinea, Documento, Empresa  # noqa: E402

COLUMNAS_DOC = [c.name for c in Documento.__table__.columns if c.name != "id"]
COLUMNAS_EMP = [c.name for c in Empresa.__table__.columns]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destino", help="URL de la base de destino (Postgres)")
    parser.add_argument("--origen", default=settings.database_url,
                        help="URL de la base de origen (por defecto, la del .env)")
    args = parser.parse_args()

    origen_url = _url_normalizada(args.origen)
    destino_url = _url_normalizada(args.destino)
    if origen_url == destino_url:
        print("El origen y el destino son la misma base; no hay nada que copiar.")
        return 1

    print(f"origen : {origen_url.split('@')[-1]}")
    print(f"destino: {destino_url.split('@')[-1]}")

    origen = sessionmaker(bind=create_engine(origen_url))()
    motor_destino = create_engine(destino_url)
    Base.metadata.create_all(bind=motor_destino)
    destino = sessionmaker(bind=motor_destino)()

    try:
        empresas_nuevas = empresas_act = 0
        for e in origen.execute(select(Empresa)).scalars():
            existente = destino.get(Empresa, e.rut)
            if existente is None:
                destino.add(Empresa(**{c: getattr(e, c) for c in COLUMNAS_EMP}))
                empresas_nuevas += 1
            else:
                for c in COLUMNAS_EMP:
                    if c != "rut":
                        setattr(existente, c, getattr(e, c))
                empresas_act += 1
        destino.flush()

        docs_nuevos = docs_act = 0
        for d in origen.execute(select(Documento)).scalars():
            # La clave natural, la misma que usa la sincronización para no duplicar.
            existente = destino.execute(
                select(Documento).where(
                    Documento.empresa_rut == d.empresa_rut,
                    Documento.tipo == d.tipo,
                    Documento.folio == d.folio,
                    Documento.proveedor_rut == d.proveedor_rut,
                )
            ).scalar_one_or_none()
            valores = {c: getattr(d, c) for c in COLUMNAS_DOC}
            if existente is None:
                destino.add(Documento(**valores))
                docs_nuevos += 1
            else:
                for c, v in valores.items():
                    setattr(existente, c, v)
                docs_act += 1

        destino.flush()
        descuentos = (origen.execute(select(DescuentoGlobalDocumento)).scalars()
                      if inspect(origen.bind).has_table("descuentos_globales_documentos") else [])
        for ajuste in descuentos:
            documento_origen = origen.get(Documento, ajuste.documento_id)
            documento_destino = destino.execute(
                select(Documento).where(
                    Documento.empresa_rut == documento_origen.empresa_rut,
                    Documento.tipo == documento_origen.tipo,
                    Documento.folio == documento_origen.folio,
                    Documento.proveedor_rut == documento_origen.proveedor_rut,
                )
            ).scalar_one()
            destino.merge(DescuentoGlobalDocumento(
                documento_id=documento_destino.id, importe=ajuste.importe,
            ))
        lineas_descuento = (
            origen.execute(select(DescuentoGlobalLinea)).scalars()
            if inspect(origen.bind).has_table("descuentos_globales_lineas") else []
        )
        for linea in lineas_descuento:
            documento_origen = origen.get(Documento, linea.documento_id)
            documento_destino = destino.execute(
                select(Documento).where(
                    Documento.empresa_rut == documento_origen.empresa_rut,
                    Documento.tipo == documento_origen.tipo,
                    Documento.folio == documento_origen.folio,
                    Documento.proveedor_rut == documento_origen.proveedor_rut,
                )
            ).scalar_one()
            destino.merge(DescuentoGlobalLinea(
                documento_id=documento_destino.id, indice=linea.indice,
                importe=linea.importe,
            ))

        destino.commit()
        print(f"\nempresas : {empresas_nuevas} nuevas, {empresas_act} actualizadas")
        print(f"documentos: {docs_nuevos} nuevos, {docs_act} actualizados")
        print("\nListo. Apuntá DATABASE_URL del despliegue a la base de destino.")
        return 0
    except Exception as exc:
        destino.rollback()
        print(f"\nFalló la copia y no se guardó nada: {type(exc).__name__}: {exc}")
        return 1
    finally:
        origen.close()
        destino.close()


if __name__ == "__main__":
    sys.exit(main())
