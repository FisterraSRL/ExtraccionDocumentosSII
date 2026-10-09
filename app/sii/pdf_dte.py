"""Extrae el detalle de ítems de la representación impresa (PDF) de un DTE recibido.

Por qué esto existe: el Registro de Compras y Ventas del SII entrega solo cabeceras, sin
detalle de ítems (ver `SIIClient.get_dte_xml()`). Pero el Portal de Facturación
Electrónica del SII, en "Historial de DTE y respuesta a documentos recibidos", sí ofrece
la representación impresa en PDF de cada documento recibido, y **ese PDF lo genera el
propio SII** a partir del XML que tiene guardado. Eso es lo que lo hace parseable de
forma razonablemente confiable: el formato es el mismo para todos los emisores, no el
diseño de cada empresa. Se verificó contra documentos de un banco, una empresa de telecomunicaciones,
un comercio, una estación de servicio y una consultora: misma plantilla en todos.

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

from app.descuentos import DescuentoNoConciliado, analizar_descuentos

# Palabras que marcan el fin de la tabla de ítems y el comienzo de los totales.
FIN_ITEMS = re.compile(
    r"MONTO\s*(NETO|EXENTO)|TOTAL\$|I\.V\.A\.|IMPUESTO\s*ADICIONAL|"
    r"Timbre\s*Electr|Verifique\s*documento|Otros\s*cobros|Descuento\s*Global",
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
    descuento_pct: float | None = None
    # Importe validado contra el subtotal y la cabecera; no se inventa a partir de
    # una mera diferencia entre precio y subtotal.
    descuento_monto: float | None = None
    # Qué se dedujo en vez de leerse del PDF ("cantidad", "precio" o "cantidad y precio"),
    # para no hacer pasar por dato lo que en realidad es una inferencia.
    derivado: str | None = None
    # cantidad x precio (menos descuento) coincide con el subtotal impreso.
    cuadra: bool = True

    def como_dict(self) -> dict:
        return {
            "desc": self.desc,
            "cant": self.cant,
            "precio": self.precio,
            "subtotal": self.subtotal,
            "codigo": self.codigo,
            "descuento_pct": self.descuento_pct,
            "descuento_monto": self.descuento_monto,
            "derivado": self.derivado,
            "cuadra": self.cuadra,
        }


@dataclass
class DetalleDTE:
    items: list[ItemDTE] = field(default_factory=list)
    totales: dict[str, float] = field(default_factory=dict)
    descuentos_globales: list[dict] = field(default_factory=list)
    # False cuando la suma de los ítems no cuadra con los totales del propio documento.
    cuadra: bool = True
    observacion: str | None = None


def _numero(texto: str) -> float | None:
    """'58.815,13' → 58815.13. Formato chileno: punto de miles, coma decimal.

    Rechaza cualquier cosa que tenga letras. Antes se limpiaba a golpe de expresión
    regular y una unidad de medida como "M3" se convertía en el número 3, que después
    se tomaba como la cantidad del ítem; la cantidad real quedaba descartada. "UN" y
    "Lt" no daban problema porque no tienen dígitos, pero "M3" y "M2" sí.
    """
    if not texto:
        return None
    limpio = texto.strip().replace("$", "").replace(" ", "")
    if not re.fullmatch(r"-?[\d.,]+", limpio):
        return None
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def _porcentaje(texto: str | None) -> float | None:
    if not texto:
        return None
    limpio = texto.strip().replace("%", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", limpio):
        return None
    # En la columna %Desc. el punto es decimal ("10.00" = 10 %), mientras que
    # en los importes monetarios del PDF el punto separa miles.
    return float(limpio.replace(",", "."))


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

    # La cola se lee de derecha a izquierda: el último número es el precio unitario y
    # el anterior es la cantidad, salteando la unidad si la hay ("1 UN", "38,17 Lt").
    #
    # Antes se tomaba "todo lo que quedaba" como cantidad y se lo unía en un solo número.
    # Eso rompía cuando un pedazo del código o de la descripción se pasaba de la frontera:
    # "140" + "2" terminaba siendo una cantidad de 1402 en vez de 2. Ahora lo que sobra a
    # la izquierda vuelve al texto, que es de donde salió.
    precio = cantidad = None
    i = len(cola) - 1
    while i >= 0 and _numero(cola[i]) is None:
        i -= 1                      # basura a la derecha del precio (raro, pero barato)
    if i >= 0:
        precio = cola[i]
        i -= 1
        while i >= 0 and _numero(cola[i]) is None:
            i -= 1                  # la unidad de medida
        if i >= 0:
            cantidad = cola[i]
            i -= 1
    sobrante = cola[: i + 1]

    return {
        "texto": " ".join(texto + sobrante).strip(),
        "cantidad": cantidad,
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
            _leer_totales(texto_linea, detalle.totales, detalle.descuentos_globales)
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
            _armar_item(
                desc=descripcion,
                codigo=codigo,
                cant=_numero(campos["cantidad"]),
                precio=_numero(campos["precio"]),
                descuento=_porcentaje(campos["descuento"]),
                subtotal=valor,
            )
        )

    _verificar(detalle)
    return detalle


def _armar_item(
    desc: str,
    codigo: str | None,
    cant: float | None,
    precio: float | None,
    descuento: float | None,
    subtotal: float | None,
) -> ItemDTE:
    """Completa cantidad y precio unitario y controla que cuadren con el subtotal.

    Hay documentos que no imprimen una de las dos columnas —una póliza de seguro suele
    traer solo el monto— y ahí el dato se deduce del subtotal, que sí está siempre. Lo
    deducido queda marcado en `derivado`: es una inferencia razonable, no algo leído del
    documento, y quien lo consuma tiene que poder distinguirlo.

    El control es `cantidad x precio (menos descuento) = subtotal`. Se tolera un peso o
    un 1%, porque el SII imprime el subtotal redondeado a pesos mientras que el precio
    unitario puede traer decimales.
    """
    factor = 1 - (descuento / 100) if descuento else 1
    derivado = None

    if subtotal is not None and factor:
        if cant is None and precio is None:
            # Una sola línea de importe: se lee como una unidad a ese precio.
            cant, precio = 1.0, subtotal / factor
            derivado = "cantidad y precio"
        elif cant is None and precio:
            cant = subtotal / (precio * factor)
            derivado = "cantidad"
        elif precio is None and cant:
            precio = subtotal / (cant * factor)
            derivado = "precio"

    cuadra = True
    if cant is not None and precio is not None and subtotal is not None:
        esperado = cant * precio * factor
        cuadra = abs(esperado - subtotal) <= max(1.0, abs(subtotal) * 0.01)

    return ItemDTE(
        desc=desc,
        cant=cant,
        precio=precio,
        subtotal=subtotal,
        codigo=codigo,
        descuento_pct=descuento,
        derivado=derivado,
        cuadra=cuadra,
    )


def _leer_totales(
    texto: str, totales: dict[str, float], descuentos_globales: list[dict] | None = None,
) -> None:
    for etiqueta, clave in (
        (r"Descuento\s*Global", "descuento_global"),
        (r"MONTO\s*NETO", "neto"),
        (r"MONTO\s*EXENTO", "exento"),
        (r"I\.V\.A\.[^$]*", "iva"),
        (r"IMPUESTO\s*ADICIONAL", "impuesto_adicional"),
        (r"TOTAL", "total"),
    ):
        for m in re.finditer(etiqueta + r"\s*\$?\s*([\d.,\-]+)", texto, re.IGNORECASE):
            valor = _numero(m.group(1))
            if valor is not None:
                if clave == "descuento_global":
                    if valor == 0:
                        continue
                    totales[clave] = totales.get(clave, 0) + valor
                    if descuentos_globales is not None:
                        descuentos_globales.append({"importe": valor})
                else:
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

    base = detalle.totales.get("neto", 0) + detalle.totales.get("exento", 0)
    if not base:
        base = detalle.totales.get("total", 0)
    try:
        conciliacion = analizar_descuentos(
            [item.como_dict() for item in detalle.items], base,
            detalle.descuentos_globales or detalle.totales.get("descuento_global", 0),
            "PDF del SII",
        )
    except DescuentoNoConciliado as exc:
        detalle.cuadra = False
        detalle.observacion = str(exc)
    else:
        for descuento in conciliacion.descuentos:
            if descuento.origen == "item" and descuento.indice is not None:
                item = detalle.items[descuento.indice]
                item.descuento_monto = float(descuento.importe)
                item.cuadra = True
        detalle.cuadra = True
        detalle.observacion = None
