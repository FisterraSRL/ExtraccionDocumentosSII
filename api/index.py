"""Punto de entrada para Vercel.

Vercel ejecuta cada archivo de `api/` como una función serverless. `vercel.json` manda
todas las rutas acá, así que esta función sirve tanto el portal (`/`) como la API.

Importante: **la sincronización con el SII no corre acá**. Necesita lanzar un Chromium
real con el certificado digital y mantener una sesión, y una función serverless no tiene
navegador, ni disco persistente, ni tiempo de ejecución suficiente. El endpoint de
sincronización lo detecta (variable `VERCEL`) y responde 501 explicándolo. El sync corre
en la máquina que tiene el certificado y escribe en la misma base Postgres que lee esto.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

__all__ = ["app"]
