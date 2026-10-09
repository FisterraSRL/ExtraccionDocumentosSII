"""Versión del portal. Única fuente de verdad: el header, la API y los docs la leen de acá.

La numeración **no es semver**. Es un contador de tres dígitos que avanza de a uno y
arrastra al llegar a 10, por pedido del dueño del proyecto:

    1.0.0 → 1.0.1 → … → 1.0.9 → 1.1.0 → … → 1.9.9 → 2.0.0

Se incrementa **una vez por cada publicación a GitHub**, no por cada cambio ni por cada
commit. Para hacerlo: `python scripts/subir_version.py`, que aplica el arrastre y evita
el error fácil de escribir "1.0.10".
"""
from __future__ import annotations

__version__ = "1.0.5"


def siguiente(version: str) -> str:
    """'1.0.9' → '1.1.0'. Cada posición es un dígito: al pasar de 9 arrastra.

    Lanza ValueError si la versión no tiene la forma esperada o si ya se agotó
    (9.9.9 no tiene siguiente), en vez de devolver algo raro en silencio.
    """
    partes = version.split(".")
    if len(partes) != 3 or not all(p.isdigit() and len(p) == 1 for p in partes):
        raise ValueError(
            f"Versión inesperada: {version!r}. Se espera 'M.m.p' con un dígito por posición."
        )
    mayor, menor, parche = (int(p) for p in partes)

    parche += 1
    if parche > 9:
        parche, menor = 0, menor + 1
    if menor > 9:
        menor, mayor = 0, mayor + 1
    if mayor > 9:
        raise ValueError(
            "La numeración llegó a 9.9.9 y no puede seguir con un dígito por posición. "
            "Hay que decidir con el dueño del proyecto cómo continuar."
        )
    return f"{mayor}.{menor}.{parche}"
