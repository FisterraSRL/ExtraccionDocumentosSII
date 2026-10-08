#!/usr/bin/env python3
"""Sube la versión del portal en uno, con el arrastre de dígitos del proyecto.

Correr **una vez por publicación a GitHub**, antes de commitear:

    python scripts/subir_version.py

Existe para que el arrastre no se haga a mano: lo natural sería escribir "1.0.10"
después de 1.0.9, y acá la regla es 1.0.9 → 1.1.0. Ver app/version.py.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO = RAIZ / "app" / "version.py"

sys.path.insert(0, str(RAIZ))
from app.version import __version__, siguiente  # noqa: E402


def main() -> int:
    try:
        nueva = siguiente(__version__)
    except ValueError as exc:
        print(f"No se pudo subir la versión: {exc}")
        return 1

    texto = io.open(ARCHIVO, encoding="utf-8").read()
    actualizado, cambios = re.subn(
        r'^__version__ = "[^"]+"$',
        f'__version__ = "{nueva}"',
        texto,
        count=1,
        flags=re.MULTILINE,
    )
    if cambios != 1:
        print(f"No se encontró la línea __version__ en {ARCHIVO}; nada que cambiar.")
        return 1

    io.open(ARCHIVO, "w", encoding="utf-8", newline="").write(actualizado)
    # La terminal Windows puede usar cp1252 y fallar al imprimir la flecha Unicode.
    print(f"{__version__} -> {nueva}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
