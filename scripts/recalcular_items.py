#!/usr/bin/env python3
"""Recalcula el control cantidad x precio = subtotal sobre los ítems ya guardados.

Para qué: cuando cambian las reglas de control o de deducción, los documentos que ya
están en la base quedan con el desglose viejo. Volver a bajar cada PDF del SII sería
lento y presionaría el límite de autenticaciones (ver AGENTS.md) — y en la mayoría de
los casos es innecesario, porque tanto el control como las deducciones se calculan a
partir de números que ya están guardados.

Lo que **sí** necesita el PDF es un ítem mal leído (por ejemplo una cantidad a la que se
le pegó un pedazo del código). Este script no los arregla: los deja marcados para que
`POST /api/sync?reprocesar_items=true` los vuelva a leer.

    python scripts/recalcular_items.py           # muestra qué haría
    python scripts/recalcular_items.py --aplicar # lo guarda
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.db import SessionLocal  # noqa: E402
from app.descuentos import DescuentoNoConciliado, analizar_descuentos  # noqa: E402
from app.models import DescuentoGlobalDocumento, Documento  # noqa: E402
from app.sii.pdf_dte import _armar_item  # noqa: E402


def _observacion(documento, items: list[dict], descuento_global: float | None) -> str | None:
    """Reutiliza exactamente el control aplicado al PDF y al JSON de envío."""
    base = (documento.neto or 0) + (documento.exento or 0)
    try:
        conciliacion = analizar_descuentos(
            items, base, descuento_global, f"Folio {documento.folio}"
        )
    except DescuentoNoConciliado as exc:
        return str(exc)
    for descuento in conciliacion.descuentos:
        if descuento.origen == "item" and descuento.indice is not None:
            items[descuento.indice]["descuento_monto"] = float(descuento.importe)
            items[descuento.indice]["cuadra"] = True
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true", help="guardar los cambios")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        tocados = derivados = descuadrados = 0
        total_items = 0
        for documento in db.query(Documento).filter(Documento.items.isnot(None)).all():
            nuevos = []
            for viejo in documento.items:
                total_items += 1
                item = _armar_item(
                    desc=viejo.get("desc") or "",
                    codigo=viejo.get("codigo"),
                    cant=viejo.get("cant"),
                    precio=viejo.get("precio"),
                    descuento=viejo.get("descuento_pct"),
                    subtotal=viejo.get("subtotal"),
                ).como_dict()
                if item["descuento_monto"] is None and "descuento_monto" not in viejo:
                    item.pop("descuento_monto")
                if item["derivado"]:
                    derivados += 1
                nuevos.append(item)
            # La observación se recalcula siempre, aunque los ítems no hayan cambiado:
            # los controles pueden haber cambiado de reglas y ese era justamente el caso
            # que dejaba documentos sin marcar.
            registro = db.get(DescuentoGlobalDocumento, documento.id)
            observacion = _observacion(
                documento, nuevos, registro.importe if registro else None
            )
            descuadrados += sum(i.get("cuadra") is False for i in nuevos)
            cambio = nuevos != documento.items
            if cambio or observacion != documento.items_observacion:
                tocados += 1
                documento.items = nuevos
                documento.items_observacion = observacion

        print(f"ítems revisados        : {total_items}")
        print(f"  con datos deducidos  : {derivados}")
        print(f"  que no cuadran       : {descuadrados}  (estos sí necesitan releer el PDF)")
        print(f"documentos modificados : {tocados}")

        if args.aplicar:
            db.commit()
            print("\nGuardado.")
        else:
            db.rollback()
            print("\nSimulación: no se guardó nada. Volvé a correrlo con --aplicar.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
