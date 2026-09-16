"""Extrae el detalle de ítems de la representación impresa (PDF) de un DTE recibido.

Por qué esto existe: el Registro de Compras y Ventas del SII entrega solo cabeceras, sin
detalle de ítems (ver `SIIClient.get_dte_xml()`). Pero el Portal de Facturación
Electrónica del SII, en "Historial de DTE y respuesta a documentos recibidos", sí ofrece
la representación impresa en PDF de cada documento recibido, y **ese PDF lo genera el
propio SII** a partir del XML que tiene guardado. Eso es lo que lo hace parseable de
forma razonablemente confiable: el formato es el mismo para todos los emisores, no el
diseño de cada empresa. Se verificó contra documentos de Banco de Chile, Entel,
Falabella, una estación de servicio y una consultora: misma plantilla en todos.

Cómo se parsea: **por coordenadas, no por texto**. Partir las líneas por espacios no
sirve — las descripciones tienen espacios y la cantidad puede traer unidad ("38,17 Lt",
"1 UN") — pero repartir en siete columnas fijas tampoco: las descripciones largas
invaden la columna de al lado y las cantidades con unidad se desbordan sobre Precio.

Lo que sí es estable es la forma de cada fila: un bloque de texto a la izquierda y una
cola numérica a la derecha. De la fila de encabezado se toman solo dos fronteras (dónde
termina el texto y dónde empieza `Valor`), y la cola se interpreta por contenido: el
importe es lo que cae bajo `Valor`, el precio unitario es el último número antes de él,
y lo que quede por delante es la cantidad. Probado contra cinco emisores con formatos
de fila distintos.

Limitación conocida: en varios PDF el SII incrusta las fuentes de forma que los
caracteres acentuados no se pueden mapear y salen como "?" ("asesor?a", "El?ctrica").
Es un defecto del PDF de origen, no del parseo; el texto se entrega tal cual llega.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber

# Palabras que marcan el fin de la tabla de ítems y el comienzo de los totales.
FIN_ITEMS = re.compile(
    r"MONTO\s*(NETO|EXENTO)|TOTAL\$|I\.V\.A\.|IMPUESTO\s*ADICIONAL|"
    r"Timbre\s*Electr|Verifique\s*documento|Otros\s*cobros",
    re.IGNORECASE,
)
class DTEPdfError(RuntimeError):
    """El PDF no tiene la forma esperada de una representación impresa del SII."""


@dataclass
class ItemDTE:
    desc: str
    cant: float | None
    precio: float | None
    subtotal: float | None
    codigo: str | None = None

    def como_dict(self) -> dict:
        return {
            "desc": self.desc,
            "cant": self.cant,
            "precio": self.precio,
            "subtotal": self.subtotal,
            "codigo": self.codigo,
        }


@dataclass
class DetalleDTE:
    items: list[ItemDTE] = field(default_factory=list)
    totales: dict[str, float] = field(default_factory=dict)
    # False cuando la suma de los ítems no cuadra con los totales del propio documento.
    cuadra: bool = True
    observacion: str | None = None


def _numero(texto: str) -> float | None:
    """'58.815,13' → 58815.13. Formato chileno: punto de miles, coma decimal."""
    if not texto:
        return None
    limpio = re.sub(r"[^\d,.\-]", "", texto)
    if not limpio or limpio in ("-", ".", ","):
        return None
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def _agrupar_en_lineas(palabras: list[dict], tolerancia: float = 3.0) -> list[list[dict]]:
    """Agrupa palabras por su coordenada vertical, tolerando pequeñas diferencias."""
    lineas: list[list[dict]] = []
    for palabra in sorted(palabras, key=lambda w: (w["top"], w["x0"])):
        if lineas and abs(lineas[-1][0]["top"] - palabra["top"]) <= tolerancia:
            lineas[-1].append(palabra)
        else:
            lineas.append([palabra])
    return [sorted(linea, key=lambda w: w["x0"]) for linea in lineas]


def _referencias(lineas: list[list[dict]]) -> tuple[dict[str, float], int]:
    """Busca la fila de encabezado y devuelve las dos fronteras verticales que importan.

    Repartir en siete columnas por puntos medios entre encabezados no funciona: las
    descripciones largas invaden la columna siguiente y las cantidades con unidad
    ("38,17 Lt") se desbordan sobre Precio. Lo que sí es estable en esta plantilla es
    que hay un bloque de texto a la izquierda y una cola numérica a la derecha, así
    que se usan solo dos fronteras y la cola se interpreta por contenido.
    """
    for indice, linea in enumerate(lineas):
        textos = {w["text"].strip(): w for w in linea}
        faltan = [e for e in ("Codigo", "Descripcion", "Cantidad", "Valor") if e not in textos]
        if faltan:
            continue
        return {
            # Fin del bloque de texto / comienzo de la cola numérica.
            "texto": (textos["Descripcion"]["x1"] + textos["Cantidad"]["x0"]) / 2,
            # Comienzo de la columna Valor, que es la única siempre alineada a la derecha.
            "valor": (
                (textos["%Desc."]["x1"] + textos["Valor"]["x0"]) / 2
                if "%Desc." in textos
                else textos["Valor"]["x0"] - 12
            ),
            # Comienzo de la columna de descuento, si el documento la usa.
            "descuento": textos["%Desc."]["x0"] - 6 if "%Desc." in textos else float("inf"),
        }, indice
    raise DTEPdfError(
        "No se encontró la fila de encabezado (Codigo/Descripcion/Cantidad/Valor) en el "
        "PDF. Puede que no sea una representación impresa del SII, o que el SII haya "
        "cambiado la plantilla."
    )


def _partir_fila(linea: list[dict], ref: dict[str, float]) -> dict:
    """Reparte una fila en texto, cantidad, precio, descuento y valor.

    La cola numérica se interpreta por posición relativa, no por columnas fijas: el
    importe es lo que cae en la columna Valor, el precio unitario es el último número
    antes de ella, y lo que quede por delante es la cantidad (con su unidad, si trae).
    """
    texto, cola, valor, descuento = [], [], None, None
    for palabra in linea:
        x, t = palabra["x0"], palabra["text"]
        if x < ref["texto"]:
            texto.append(t)
        elif x >= ref["valor"]:
            valor = t if valor is None else f"{valor}{t}"
        elif x >= ref["descuento"]:
            descuento = t
        else:
            cola.append(t)

    precio = None
    for i in range(len(cola) - 1, -1, -1):
        if _numero(cola[i]) is not None:
            precio = cola[i]
            cola = cola[:i]
            break
    return {
        "texto": " ".join(texto).strip(),
        "cantidad": " ".join(cola).strip(),
        "precio": precio,
        "descuento": descuento,
        "valor": valor,
    }


# Un código de producto trae al menos un dígito; eso lo distingue de la primera
# palabra de una descripción ("COMISION", "Diesel"), que si no se llevaría el puesto.
ES_CODIGO = re.compile(r"^(?=.*\d)[A-Za-z0-9][\w./\-]*$")


def _separar_codigo(texto: str) -> tuple[str | None, str]:
    partes = texto.split()
    while partes and partes[0] in ("-", "–", "—"):
        partes.pop(0)
    if len(partes) > 1 and ES_CODIGO.match(partes[0]):
        return partes[0], " ".join(partes[1:])
    return None, " ".join(partes)


def extraer_detalle(pdf_bytes: bytes) -> DetalleDTE:
    """Lee un PDF de DTE del SII y devuelve sus ítems y totales.

    Lanza DTEPdfError si el PDF no tiene la plantilla esperada; nunca devuelve ítems
    inventados ni parciales sin avisar (ver `DetalleDTE.cuadra`).
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            raise DTEPdfError("El PDF no tiene páginas.")
        palabras: list[dict] = []
        for pagina in pdf.pages:
            palabras.extend(pagina.extract_words())

    lineas = _agrupar_en_lineas(palabras)
    ref, fila_encabezado = _referencias(lineas)

    detalle = DetalleDTE()
    for linea in lineas[fila_encabezado + 1:]:
        texto_linea = " ".join(w["text"] for w in linea)

        if FIN_ITEMS.search(texto_linea):
            _leer_totales(texto_linea, detalle.totales)
            continue

        campos = _partir_fila(linea, ref)
        valor = _numero(campos["valor"])

        if valor is None:
            # Sin importe: continuación de la descripción del ítem anterior. Se evita
            # repetirla cuando el SII imprime el nombre del ítem y abajo su detalle,
            # que a veces empieza con el mismo texto.
            resto = campos["texto"]
            if resto and detalle.items and resto not in detalle.items[-1].desc:
                detalle.items[-1].desc = f"{detalle.items[-1].desc} {resto}".strip()
            continue

        codigo, descripcion = _separar_codigo(campos["texto"])
        if not descripcion and not codigo:
            # Filas sueltas de la plantilla (por ejemplo un "$ 0" aislado): no son ítems.
            continue
        if not descripcion:
            descripcion, codigo = codigo, None

        detalle.items.append(
            ItemDTE(
                desc=descripcion,
                cant=_numero(campos["cantidad"]),
                precio=_numero(campos["precio"]),
                subtotal=valor,
                codigo=codigo,
            )
        )

    _verificar(detalle)
    return detalle


def _leer_totales(texto: str, totales: dict[str, float]) -> None:
    for etiqueta, clave in (
        (r"MONTO\s*NETO", "neto"),
        (r"MONTO\s*EXENTO", "exento"),
        (r"I\.V\.A\.[^$]*", "iva"),
        (r"IMPUESTO\s*ADICIONAL", "impuesto_adicional"),
        (r"TOTAL", "total"),
    ):
        m = re.search(etiqueta + r"\$?\s*([\d.,\-]+)", texto, re.IGNORECASE)
        if m:
            valor = _numero(m.group(1))
            if valor is not None:
                totales.setdefault(clave, valor)


def _verificar(detalle: DetalleDTE) -> None:
    """Contrasta la suma de los ítems con los totales impresos en el documento.

    No corrige nada: solo deja constancia de si cuadra, para que quien consuma esto
    sepa si puede confiar en el desglose o le conviene revisarlo a mano.
    """
    if not detalle.items:
        detalle.cuadra = False
        detalle.observacion = "El PDF no tiene filas de ítems reconocibles."
        return

    suma = sum(i.subtotal or 0 for i in detalle.items)
    base = detalle.totales.get("neto", 0) + detalle.totales.get("exento", 0)
    if not base:
        base = detalle.totales.get("total", 0)
    if base and abs(suma - base) > max(2.0, base * 0.01):
        detalle.cuadra = False
        detalle.observacion = (
            f"La suma de los ítems ({suma:,.0f}) no coincide con el neto+exento del "
            f"documento ({base:,.0f}). Puede haber descuentos globales u otros cobros "
            "que el desglose no refleja."
        )
