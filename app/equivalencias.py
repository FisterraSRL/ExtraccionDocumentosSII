"""Maestro de descripciones SII y equivalencias confirmadas con Finnegans."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AparicionProductoSII, AsociacionItem, Documento, EquivalenciaProducto, EstadoDocumento, EstadoMaestro,
    ProductoFinnegans, ProductoSII, SincronizacionProductos, SugerenciaProducto,
)
from app.productos import _firma, _ordenar, _tokens


class ErrorEquivalencia(ValueError):
    pass


def normalizar(descripcion: str) -> str:
    """Unifica escritura y unidades sin borrar palabras que distinguen presentaciones."""
    texto = "".join(c for c in unicodedata.normalize("NFKD", descripcion.upper())
                    if not unicodedata.combining(c))
    texto = re.sub(r"[^A-Z0-9]+", " ", texto).strip()
    texto = re.sub(r"\b(\d+)\s+(KG|KGS|G|GR|L|LT|LTS|ML|CC)\b", r"\1\2", texto)
    return re.sub(r"\s+", " ", texto)


def registrar_documentos(db: Session, empresa_rut: str | None = None) -> int:
    """Incluye ítems históricos y nuevos; una segunda pasada no aumenta apariciones."""
    consulta = select(Documento.id, Documento.fecha, Documento.items)
    if empresa_rut is not None:
        consulta = consulta.where(Documento.empresa_rut == empresa_rut)
    documentos = db.execute(consulta).all()
    productos = {p.descripcion_normalizada: p for p in db.execute(select(ProductoSII)).scalars()}
    ids = [fila.id for fila in documentos]
    apariciones = {}
    if ids:
        apariciones = {(a.documento_id, a.indice): a for a in db.execute(
            select(AparicionProductoSII).where(AparicionProductoSII.documento_id.in_(ids))
        ).scalars()}
    nuevas = 0
    vistas = set()
    for documento_id, _, items in documentos:
        if not isinstance(items, list):
            continue
        for indice, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            original = str(item.get("desc") or "").strip()
            clave = normalizar(original)
            if not clave:
                continue
            producto = productos.get(clave)
            if producto is None:
                producto = ProductoSII(descripcion_original=original, descripcion_normalizada=clave)
                db.add(producto)
                db.flush()
                productos[clave] = producto
                nuevas += 1
            llave = (documento_id, indice)
            vistas.add(llave)
            aparicion = apariciones.get(llave)
            if aparicion is None:
                db.add(AparicionProductoSII(documento_id=documento_id, indice=indice,
                                             producto_sii_id=producto.id))
            elif aparicion.producto_sii_id != producto.id:
                aparicion.producto_sii_id = producto.id
    # Un PDF reprocesado puede traer menos ítems; su conteo anterior deja de ser válido.
    for llave, aparicion in apariciones.items():
        if llave not in vistas:
            db.delete(aparicion)
    db.flush()
    totales = {id_sii: (cantidad, primera, ultima) for id_sii, cantidad, primera, ultima in db.execute(
        select(AparicionProductoSII.producto_sii_id, func.count(),
               func.min(Documento.fecha), func.max(Documento.fecha))
        .join(Documento, Documento.id == AparicionProductoSII.documento_id)
        .group_by(AparicionProductoSII.producto_sii_id)
    )}
    for producto in productos.values():
        producto.apariciones, producto.primera_aparicion, producto.ultima_aparicion = (
            totales.get(producto.id, (0, None, None))
        )
    db.commit()
    return nuevas


def incorporar_historicos(db: Session) -> None:
    if db.get(EstadoMaestro, "historicos_v1") is not None:
        return
    registrar_documentos(db)
    db.add(EstadoMaestro(clave="historicos_v1", fecha=datetime.utcnow()))
    db.commit()


def aplicar_confirmadas(db: Session, perfil_id: str, empresa_rut: str) -> int:
    """Aplica solo decisiones humanas vigentes a ítems no enviados de la empresa."""
    decisiones = {normalizada: codigo for normalizada, codigo in db.execute(
        select(ProductoSII.descripcion_normalizada, EquivalenciaProducto.producto_codigo)
        .join(EquivalenciaProducto, EquivalenciaProducto.producto_sii_id == ProductoSII.id)
        .join(ProductoFinnegans,
              (ProductoFinnegans.perfil_id == EquivalenciaProducto.perfil_id) &
              (ProductoFinnegans.codigo == EquivalenciaProducto.producto_codigo))
        .where(EquivalenciaProducto.perfil_id == perfil_id,
               EquivalenciaProducto.estado == "confirmado",
               ProductoFinnegans.disponible.is_(True),
               ProductoFinnegans.activo.is_not(False))
    )}
    if not decisiones:
        return 0
    documentos = db.execute(select(Documento.id, Documento.items).where(
        Documento.empresa_rut == empresa_rut, Documento.estado != EstadoDocumento.ENVIADO,
        Documento.items.is_not(None),
    )).all()
    ids = [documento_id for documento_id, _ in documentos]
    asociaciones = {}
    if ids:
        asociaciones = {(a.documento_id, a.indice): a for a in db.execute(
            select(AsociacionItem).where(AsociacionItem.perfil_id == perfil_id,
                                         AsociacionItem.documento_id.in_(ids))
        ).scalars()}
    cambios = 0
    for documento_id, items in documentos:
        if not isinstance(items, list):
            continue
        for indice, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            codigo = decisiones.get(normalizar(str(item.get("desc") or "")))
            if codigo is None:
                continue
            firma = _firma(item)
            asociacion = asociaciones.get((documento_id, indice))
            if asociacion and asociacion.origen == "manual" and asociacion.descripcion_firma == firma:
                continue
            if asociacion is None:
                asociacion = AsociacionItem(perfil_id=perfil_id, documento_id=documento_id,
                                           indice=indice, descripcion_firma=firma,
                                           producto_codigo=codigo, origen="maestro")
                db.add(asociacion)
            elif (asociacion.descripcion_firma != firma or asociacion.producto_codigo != codigo
                  or asociacion.origen != "maestro"):
                asociacion.descripcion_firma = firma
                asociacion.producto_codigo = codigo
                asociacion.origen = "maestro"
                asociacion.puntaje = None
                asociacion.actualizado_at = datetime.utcnow()
            else:
                continue
            cambios += 1
    if cambios:
        db.commit()
    return cambios


def equivalencia_para(db: Session, perfil_id: str, descripcion: str) -> tuple[bool, EquivalenciaProducto | None]:
    clave = normalizar(descripcion)
    if not clave:
        return False, None
    producto = db.execute(
        select(ProductoSII).where(ProductoSII.descripcion_normalizada == clave)
    ).scalar_one_or_none()
    return (producto is not None,
            db.get(EquivalenciaProducto, (perfil_id, producto.id)) if producto else None)


def listar(db: Session, perfil_id: str, q: str = "", estado: str = "todos",
           pagina: int = 1, limite: int = 50) -> dict:
    if estado not in {"todos", "pendiente", "sugerido", "confirmado", "sin_equivalencia", "deshabilitado"}:
        raise ErrorEquivalencia("El filtro de estado no es válido.")
    pagina, limite = max(1, pagina), min(max(1, limite), 100)
    productos = db.execute(select(ProductoFinnegans).where(
        ProductoFinnegans.perfil_id == perfil_id,
        ProductoFinnegans.disponible.is_(True),
        ProductoFinnegans.activo.is_not(False),
    )).scalars().all()
    catalogo = {p.codigo: p for p in productos}
    indice_productos = {}
    for producto in productos:
        for token in _tokens(producto.nombre) | _tokens(producto.codigo):
            indice_productos.setdefault(token, []).append(producto)
    registro_catalogo = db.get(SincronizacionProductos, perfil_id)
    fecha_catalogo = registro_catalogo.fecha if registro_catalogo else None
    cache = {s.producto_sii_id: s for s in db.execute(
        select(SugerenciaProducto).where(SugerenciaProducto.perfil_id == perfil_id)
    ).scalars()}
    decisiones = {e.producto_sii_id: e for e in db.execute(
        select(EquivalenciaProducto).where(EquivalenciaProducto.perfil_id == perfil_id)
    ).scalars()}
    consulta = select(ProductoSII).where(ProductoSII.apariciones > 0).order_by(
        ProductoSII.apariciones.desc(), ProductoSII.descripcion_normalizada
    )
    candidatas = []
    busqueda = normalizar(q)
    palabras_buscadas = _tokens(q) if busqueda else set()
    for sii in db.execute(consulta).scalars():
        eq = decisiones.get(sii.id)
        producto = catalogo.get(eq.producto_codigo) if eq and eq.producto_codigo else None
        if busqueda and not palabras_buscadas.issubset(_tokens(sii.descripcion_normalizada)) and not (
            producto and (busqueda in normalizar(producto.nombre) or busqueda in normalizar(producto.codigo))
        ):
            continue
        if estado in {"confirmado", "sin_equivalencia", "deshabilitado"} and (
            eq is None or eq.estado != estado
        ):
            continue
        if estado in {"pendiente", "sugerido"} and eq is not None:
            continue
        candidatas.append((sii, eq, producto))

    actualizado = False

    def sugerir(sii: ProductoSII) -> dict | None:
        nonlocal actualizado
        guardada = cache.get(sii.id)
        if guardada is None or guardada.catalogo_fecha != fecha_catalogo:
            candidatos_posibles = {p.codigo: p for token in _tokens(sii.descripcion_original)
                                    for p in indice_productos.get(token, [])}
            encontrados = _ordenar(sii.descripcion_original, list(candidatos_posibles.values()), 1)
            mejor = encontrados[0] if encontrados else None
            if guardada is None:
                guardada = SugerenciaProducto(perfil_id=perfil_id, producto_sii_id=sii.id)
                db.add(guardada)
                cache[sii.id] = guardada
            guardada.producto_codigo = mejor["codigo"] if mejor else None
            guardada.puntaje = mejor["puntaje"] if mejor else None
            guardada.catalogo_fecha = fecha_catalogo
            actualizado = True
        elegido = catalogo.get(guardada.producto_codigo) if guardada.producto_codigo else None
        if elegido and guardada.puntaje is not None and guardada.puntaje >= 0.6:
            return {"codigo": elegido.codigo, "nombre": elegido.nombre,
                    "puntaje": guardada.puntaje}
        return None

    if estado in {"pendiente", "sugerido"}:
        candidatas = [(sii, eq, producto) for sii, eq, producto in candidatas
                      if ("sugerido" if sugerir(sii) else "pendiente") == estado]
    total = len(candidatas)
    inicio = (pagina - 1) * limite
    filas = []
    for sii, eq, producto in candidatas[inicio:inicio + limite]:
        sugerencia = sugerir(sii) if eq is None else None
        situacion = eq.estado if eq else "sugerido" if sugerencia else "pendiente"
        filas.append({
            "id": sii.id, "descripcion_original": sii.descripcion_original,
            "descripcion_normalizada": sii.descripcion_normalizada,
            "apariciones": sii.apariciones,
            "primera_aparicion": sii.primera_aparicion.isoformat() if sii.primera_aparicion else None,
            "ultima_aparicion": sii.ultima_aparicion.isoformat() if sii.ultima_aparicion else None,
            "estado": situacion, "origen": eq.origen if eq else None,
            "producto": {"codigo": producto.codigo, "nombre": producto.nombre} if producto else None,
            "producto_codigo": eq.producto_codigo if eq else None,
            "sugerencia": sugerencia,
        })
    if actualizado:
        db.commit()
    return {"total": total, "pagina": pagina, "limite": limite,
            "filas": filas, "catalogo_cantidad": len(productos)}


def _aplicar_decision(db: Session, perfil_id: str, producto_sii_id: int,
                      codigo: str | None, estado: str) -> None:
    if estado not in {"confirmado", "sin_equivalencia", "deshabilitado", "pendiente"}:
        raise ErrorEquivalencia("Seleccioná un estado válido.")
    sii = db.get(ProductoSII, producto_sii_id)
    if sii is None or sii.apariciones == 0:
        raise ErrorEquivalencia("La descripción SII ya no está disponible.")
    if estado == "confirmado":
        if not codigo:
            raise ErrorEquivalencia("Seleccioná un producto de Finnegans antes de confirmar.")
        producto = db.get(ProductoFinnegans, (perfil_id, codigo))
        if producto is None or not producto.disponible or producto.activo is False:
            raise ErrorEquivalencia("El producto no está disponible. Actualizá el catálogo y elegí otro.")
    else:
        codigo = None
    decision = db.get(EquivalenciaProducto, (perfil_id, producto_sii_id))
    if estado == "pendiente":
        if decision:
            db.delete(decision)
    elif decision is None:
        db.add(EquivalenciaProducto(perfil_id=perfil_id, producto_sii_id=producto_sii_id,
                                    producto_codigo=codigo, estado=estado, origen="manual"))
    else:
        decision.producto_codigo = codigo
        decision.estado = estado
        decision.origen = "manual"
        decision.actualizado_at = datetime.utcnow()


def guardar(db: Session, perfil_id: str, producto_sii_id: int,
            codigo: str | None, estado: str) -> None:
    try:
        _aplicar_decision(db, perfil_id, producto_sii_id, codigo, estado)
        db.commit()
    except Exception:
        db.rollback()
        raise


def guardar_varias(db: Session, perfil_id: str, decisiones: list[tuple[int, str | None, str]]) -> int:
    if not decisiones:
        raise ErrorEquivalencia("Elegí al menos una equivalencia antes de guardar.")
    if len(decisiones) != len({identificador for identificador, _, _ in decisiones}):
        raise ErrorEquivalencia("Hay descripciones SII repetidas en la selección.")
    try:
        for identificador, codigo, estado in decisiones:
            _aplicar_decision(db, perfil_id, identificador, codigo, estado)
        db.commit()
    except Exception:
        # La selección es una sola operación: un código inválido no guarda las filas anteriores.
        db.rollback()
        raise
    return len(decisiones)
