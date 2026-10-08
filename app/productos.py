"""Catálogo local y asociaciones entre ítems del SII y productos de Finnegans."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.finnegans.catalogo import obtener_productos
from app.models import AsociacionItem, Documento, EstadoDocumento, ProductoFinnegans, SincronizacionProductos


class ErrorProductos(RuntimeError):
    """La selección o la configuración del catálogo no es válida."""


_IGNORAR = {"DE", "DEL", "LA", "LAS", "EL", "LOS", "UN", "UNA", "Y", "CON", "PARA", "POR", "EN"}
_UNIDADES = {"LITROS": "L", "LITRO": "L", "LTS": "L", "LT": "L",
             "KILOS": "KG", "KILO": "KG", "KGS": "KG", "GRAMOS": "G", "GR": "G"}


def _tokens(texto: str) -> set[str]:
    limpio = "".join(
        letra for letra in unicodedata.normalize("NFKD", texto.upper())
        if not unicodedata.combining(letra)
    )
    limpio = re.sub(r"[^A-Z0-9]+", " ", limpio)
    partes = [_UNIDADES.get(p, p) for p in limpio.split()]
    unidos = []
    for parte in partes:
        if parte in {"L", "KG", "G", "ML", "CC"} and unidos and unidos[-1].isdigit():
            unidos[-1] += parte
        else:
            unidos.append(parte)
    return {parte for parte in unidos if parte not in _IGNORAR}


def _firma(item: dict) -> str:
    return " ".join(sorted(_tokens(str(item.get("desc") or ""))))


def _codigo_sii(item: dict) -> str:
    return re.sub(r"\s+", "", str(item.get("codigo") or "").upper())


def _historial_manual(db: Session, perfil_id: str, proveedor_rut: str) -> list[tuple[str, str, str | None]]:
    """Reconstruye la memoria desde decisiones vigentes, sin contar elecciones automáticas."""
    filas = db.execute(
        select(
            AsociacionItem.indice, AsociacionItem.descripcion_firma,
            AsociacionItem.producto_codigo, Documento.items,
        ).join(Documento, AsociacionItem.documento_id == Documento.id).where(
            AsociacionItem.perfil_id == perfil_id,
            AsociacionItem.origen == "manual",
            Documento.proveedor_rut == proveedor_rut,
        )
    )
    historial = []
    for indice, firma, producto_codigo, items in filas:
        if not isinstance(items, list) or indice >= len(items):
            continue
        item = items[indice]
        if not isinstance(item, dict) or _firma(item) != firma:
            continue
        historial.append((firma, _codigo_sii(item), producto_codigo))
    return historial


def _recordar(item: dict, historial: list[tuple[str, str, str | None]]) -> tuple[Counter, bool]:
    firma = _firma(item)
    codigo = _codigo_sii(item)
    elegidos: Counter[str | None] = Counter()
    hubo_decision = False
    for firma_anterior, codigo_anterior, producto_codigo in historial:
        if firma_anterior != firma or (codigo and codigo_anterior and codigo != codigo_anterior):
            continue
        hubo_decision = True
        # Quitar una selección manual también es una corrección: impide volver a elegirla solo.
        elegidos[producto_codigo] += 1
    return elegidos, hubo_decision


def _peso(token: str) -> float:
    return 2.0 if any(c.isdigit() for c in token) else 1.0


def _puntaje(buscados: set[str], candidatos: set[str]) -> float:
    if not buscados or not candidatos:
        return 0.0
    comunes = buscados & candidatos
    if not comunes:
        return 0.0
    total = sum(map(_peso, buscados)) + sum(map(_peso, candidatos))
    puntuacion = 2 * sum(map(_peso, comunes)) / total
    numeros_a = {t for t in buscados if any(c.isdigit() for c in t)}
    numeros_b = {t for t in candidatos if any(c.isdigit() for c in t)}
    if numeros_a and numeros_b and numeros_a != numeros_b:
        puntuacion *= 0.55
    return puntuacion


def _umbral() -> float:
    try:
        valor = float(settings.finnegans_match_umbral)
    except ValueError as exc:
        raise ErrorProductos("FINNEGANS_MATCH_UMBRAL debe ser un número entre 0 y 1.") from exc
    if not 0 < valor <= 1:
        raise ErrorProductos("FINNEGANS_MATCH_UMBRAL debe estar entre 0 y 1.")
    return valor


def _resumen(producto: ProductoFinnegans, puntaje: float | None = None) -> dict:
    salida = {
        "codigo": producto.codigo, "nombre": producto.nombre,
        "unidad": producto.unidad, "rubro": producto.rubro,
        "disponible": producto.disponible,
    }
    if puntaje is not None:
        salida["puntaje"] = round(puntaje, 3)
    return salida


def _ordenar(texto: str, productos: list[ProductoFinnegans], limite: int = 8,
             codigo_sii: str | None = None) -> list[dict]:
    buscados = _tokens(texto)
    consulta = texto.strip().upper()
    codigo_buscado = (codigo_sii or "").strip().upper()
    encontrados = []
    for producto in productos:
        puntaje = _puntaje(buscados, _tokens(producto.nombre))
        if producto.codigo.upper() in {codigo_buscado, consulta} and (codigo_buscado or consulta):
            puntaje = 1.0
        elif consulta and consulta in producto.codigo.upper():
            puntaje = max(puntaje, 0.7)
        if puntaje >= 0.15:
            encontrados.append((puntaje, producto))
    encontrados.sort(key=lambda par: (-par[0], par[1].nombre.casefold(), par[1].codigo))
    return [_resumen(producto, puntaje) for puntaje, producto in encontrados[:limite]]


def buscar(db: Session, perfil_id: str, texto: str, limite: int = 8) -> list[dict]:
    if not texto.strip():
        return []
    productos = db.execute(
        select(ProductoFinnegans).where(
            ProductoFinnegans.perfil_id == perfil_id, ProductoFinnegans.disponible.is_(True)
        )
    ).scalars().all()
    return _ordenar(texto[:120], productos, min(max(limite, 1), 20))


def estado_catalogo(db: Session, perfil_id: str) -> dict:
    cantidad = db.scalar(
        select(func.count()).select_from(ProductoFinnegans).where(
            ProductoFinnegans.perfil_id == perfil_id, ProductoFinnegans.disponible.is_(True)
        )
    ) or 0
    sincronizacion = db.get(SincronizacionProductos, perfil_id)
    return {
        "cantidad": cantidad,
        "ultima_sincronizacion": sincronizacion.fecha.isoformat() if sincronizacion else None,
    }


def sincronizar(db: Session, perfil_id: str, client_id: str, client_secret: str) -> dict:
    datos = obtener_productos(perfil_id, client_id, client_secret)
    existentes = {
        producto.codigo: producto for producto in db.execute(
            select(ProductoFinnegans).where(ProductoFinnegans.perfil_id == perfil_id)
        ).scalars()
    }
    if not datos and existentes:
        raise ErrorProductos("Finnegans devolvió un catálogo vacío; se conservó la copia anterior.")
    ahora = datetime.utcnow()
    vistos = set()
    for fila in datos:
        if not isinstance(fila, dict):
            raise ErrorProductos("Finnegans devolvió un producto con estructura inesperada.")
        codigo = str(fila.get("Codigo") or "").strip()
        if not codigo:
            continue
        vistos.add(codigo)
        producto = existentes.get(codigo)
        if producto is None:
            producto = ProductoFinnegans(perfil_id=perfil_id, codigo=codigo, nombre=codigo)
            db.add(producto)
        producto.nombre = str(fila.get("Nombre") or codigo).strip()
        producto.unidad_id_compra = fila.get("UnidadIDCompra")
        producto.unidad = fila.get("Unidad")
        producto.rubro = fila.get("NombreRubro")
        producto.familia = fila.get("NombreFamilia")
        producto.activo = fila.get("Activo") if isinstance(fila.get("Activo"), bool) else None
        producto.disponible = True
        producto.actualizado_at = ahora
    for codigo, producto in existentes.items():
        if codigo not in vistos:
            producto.disponible = False
    registro = db.get(SincronizacionProductos, perfil_id)
    if registro is None:
        registro = SincronizacionProductos(perfil_id=perfil_id, fecha=ahora, cantidad=len(vistos))
        db.add(registro)
    else:
        registro.fecha = ahora
        registro.cantidad = len(vistos)
    db.commit()
    return estado_catalogo(db, perfil_id)


def preparar(db: Session, perfil_id: str, documento: Documento) -> list[dict]:
    # Import tardío: el maestro reutiliza el algoritmo de sugerencias de este módulo.
    from app.equivalencias import equivalencia_para
    productos = db.execute(
        select(ProductoFinnegans).where(ProductoFinnegans.perfil_id == perfil_id)
    ).scalars().all()
    por_codigo = {p.codigo: p for p in productos}
    disponibles = [p for p in productos if p.disponible and p.activo is not False]
    asociaciones = {
        a.indice: a for a in db.execute(
            select(AsociacionItem).where(
                AsociacionItem.perfil_id == perfil_id,
                AsociacionItem.documento_id == documento.id,
            )
        ).scalars()
    }
    historial = _historial_manual(db, perfil_id, documento.proveedor_rut)
    salida = []
    cambios = False
    for indice, item in enumerate(documento.items or []):
        firma = _firma(item)
        registro = asociaciones.get(indice)
        asociacion = registro if registro and registro.descripcion_firma == firma else None
        existe_en_maestro, maestro = equivalencia_para(db, perfil_id, str(item.get("desc") or ""))
        sugerencias = _ordenar(
            str(item.get("desc") or ""), disponibles, 5, item.get("codigo")
        ) if disponibles else []
        recordados, hubo_decision = _recordar(item, historial)
        memoria_sugerida = []
        for codigo, veces in sorted(recordados.items(), key=lambda par: (-par[1], str(par[0]))):
            producto_recordado = por_codigo.get(codigo)
            if producto_recordado and producto_recordado.disponible and producto_recordado.activo is not False:
                resumen = _resumen(producto_recordado)
                resumen["memoria"] = veces
                memoria_sugerida.append(resumen)
        vistos = {s["codigo"] for s in memoria_sugerida}
        sugerencias = (memoria_sugerida + [s for s in sugerencias if s["codigo"] not in vistos])[:5]

        if documento.estado != EstadoDocumento.ENVIADO and (asociacion is None or (
            asociacion.origen != "manual" and
            (existe_en_maestro or hubo_decision or asociacion.origen in {"memoria", "maestro"})
        )):
            codigo_elegido = None
            origen = "memoria" if hubo_decision else "maestro" if existe_en_maestro else "automatico"
            puntaje = None
            if maestro is not None:
                origen = "maestro"
                if maestro.estado == "confirmado" and maestro.producto_codigo in {
                    p.codigo for p in disponibles
                }:
                    codigo_elegido = maestro.producto_codigo
            elif hubo_decision:
                # Un historial contradictorio, o una selección quitada, requiere criterio humano.
                if len(recordados) == 1 and None not in recordados and (
                    len(_tokens(str(item.get("desc") or ""))) >= 2 or _codigo_sii(item)
                ):
                    unico = next(iter(recordados))
                    if unico in {s["codigo"] for s in memoria_sugerida}:
                        codigo_elegido = unico
            elif not existe_en_maestro and len(_tokens(str(item.get("desc") or ""))) >= 2 and sugerencias:
                primero = sugerencias[0]
                segundo = sugerencias[1]["puntaje"] if len(sugerencias) > 1 else 0.0
                if primero["puntaje"] >= _umbral() and primero["puntaje"] - segundo >= 0.12:
                    codigo_elegido = primero["codigo"]
                    puntaje = primero["puntaje"]
            if registro and (
                registro.descripcion_firma != firma or registro.producto_codigo != codigo_elegido
                or registro.origen != origen or registro.puntaje != puntaje
            ):
                registro.descripcion_firma = firma
                registro.producto_codigo = codigo_elegido
                registro.origen = origen
                registro.puntaje = puntaje
                asociacion = registro
                cambios = True
            elif registro:
                asociacion = registro
            elif codigo_elegido:
                asociacion = AsociacionItem(
                    perfil_id=perfil_id, documento_id=documento.id, indice=indice,
                    descripcion_firma=firma, producto_codigo=codigo_elegido,
                    origen=origen, puntaje=puntaje,
                )
                db.add(asociacion)
                cambios = True
        producto = por_codigo.get(asociacion.producto_codigo) if asociacion and asociacion.producto_codigo else None
        salida.append({
            "indice": indice,
            "producto": _resumen(producto) if producto else None,
            "origen": asociacion.origen if asociacion else None,
            "estado": ("asociado" if producto and producto.disponible
                       else "revisar" if asociacion and asociacion.producto_codigo
                       else "pendiente"),
            "sugerencias": sugerencias,
        })
    if cambios:
        db.commit()
    return salida


def guardar_asociacion(
    db: Session, perfil_id: str, documento: Documento, indice: int, codigo: str | None,
) -> dict:
    items = documento.items or []
    if indice < 0 or indice >= len(items):
        raise ErrorProductos("El ítem seleccionado no existe en esta factura.")
    producto = None
    if codigo is not None:
        producto = db.get(ProductoFinnegans, (perfil_id, codigo))
        if producto is None or not producto.disponible:
            raise ErrorProductos("El producto no está en el catálogo local. Sincronizá productos y elegilo otra vez.")
    asociacion = db.get(AsociacionItem, (perfil_id, documento.id, indice))
    if asociacion is None:
        asociacion = AsociacionItem(perfil_id=perfil_id, documento_id=documento.id, indice=indice)
        db.add(asociacion)
    asociacion.descripcion_firma = _firma(items[indice])
    asociacion.producto_codigo = codigo
    asociacion.origen = "manual"
    asociacion.puntaje = None
    asociacion.actualizado_at = datetime.utcnow()
    db.commit()
    return {
        "indice": indice, "producto": _resumen(producto) if producto else None,
        "origen": "manual", "estado": "asociado" if producto else "pendiente",
    }
