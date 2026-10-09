#!/usr/bin/env python3
"""Vuelve a dejar pendientes documentos que figuran como enviados a Finnegans.

Para qué: durante las pruebas todo se registra en la empresa PRUEBA39 (configurada
temporalmente en app/finnegans/client.py). Esos documentos quedan marcados como enviados y el
portal no los vuelve a ofrecer, así que cuando llegue el momento de mandarlos a la
empresa que corresponde nunca saldrían. Este script los reabre.

**No borra nada en Finnegans.** Los comprobantes de prueba que ya estén creados allá
siguen estando; darlos de baja es algo que se hace desde el ERP. Acá solo se corrige el
estado de nuestra base para poder volver a enviarlos.

    python scripts/reabrir_envios.py                      # muestra qué haría, todo
    python scripts/reabrir_envios.py --empresa XXXXXXXX-X  # solo una empresa
    python scripts/reabrir_envios.py --ids 171,172         # documentos puntuales
    python scripts/reabrir_envios.py --aplicar             # lo guarda
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Documento, EstadoDocumento  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--empresa", help="RUT de la empresa del SII (ej. XXXXXXXX-X)")
    parser.add_argument("--ids", help="IDs separados por coma")
    parser.add_argument("--aplicar", action="store_true", help="guarda los cambios")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stmt = select(Documento).where(Documento.estado == EstadoDocumento.ENVIADO)
        if args.empresa:
            stmt = stmt.where(Documento.empresa_rut == args.empresa)
        if args.ids:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
            stmt = stmt.where(Documento.id.in_(ids))

        documentos = db.execute(stmt.order_by(Documento.fecha)).scalars().all()
        if not documentos:
            print("No hay documentos marcados como enviados con ese filtro.")
            return 0

        print(f"{len(documentos)} documento(s) figuran como enviados:\n")
        for d in documentos:
            print(f"  [{d.id:>5}] {d.empresa_rut}  {d.tipo_nombre} {d.folio}  "
                  f"{d.proveedor_nombre[:40]:<40} Finnegans: {d.finnegans_id or '—'}")

        if not args.aplicar:
            print("\nEsto fue una simulación. Agregá --aplicar para reabrirlos.")
            return 0

        for d in documentos:
            d.estado = EstadoDocumento.PENDIENTE
            d.finnegans_id = None
            d.fecha_envio = None
            d.error_detalle = None
        db.commit()
        print(f"\nListo: {len(documentos)} documento(s) vuelven a estar pendientes.")
        print("Recordá que los comprobantes ya creados en Finnegans siguen allá.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
