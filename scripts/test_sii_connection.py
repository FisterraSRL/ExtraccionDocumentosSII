#!/usr/bin/env python3
"""Primer script a correr apenas tengamos el certificado digital.

Qué valida:
1. Que .env tenga SII_RUT, SII_CERT_PATH y SII_CERT_PASSWORD.
2. Que el archivo .pfx cargue con esa contraseña (certificado válido).
3. Que el SII acepte un handshake TLS mutuo con ese certificado.

Qué NO valida todavía (a propósito): si "Intercambio de información" está
habilitado, ni el acceso al RCV/BHE — eso es el paso siguiente, una vez esto
funcione. Correr con:

    python scripts/test_sii_connection.py
"""
import sys

from app.config import settings
from app.sii.client import SIICertificateError, SIIClient


def main() -> int:
    print("1) Verificando variables de entorno...")
    try:
        rut, cert_path, password = settings.require_sii_credentials()
    except RuntimeError as exc:
        print(f"   ✗ {exc}")
        return 1
    print(f"   ✓ RUT={rut}, certificado en {cert_path}")

    print("2) Cargando el certificado (.pfx)...")
    client = SIIClient(rut, cert_path, password)
    try:
        client._load_pkcs12()
    except SIICertificateError as exc:
        print(f"   ✗ {exc}")
        return 1
    print("   ✓ Certificado y clave privada cargados correctamente.")

    print("3) Probando conexión con el SII (handshake TLS con el certificado)...")
    resultados = client.test_connection()
    client.close()

    hubo_ok = False
    for r in resultados:
        marca = "✓" if r.ok else "✗"
        print(f"   {marca} {r.host} → {r.detalle}")
        hubo_ok = hubo_ok or r.ok

    print()
    if hubo_ok:
        print("Al menos un host aceptó el certificado. Siguiente paso: con esto confirmado,")
        print("definimos si vamos por el servicio web de Intercambio o por automatizar el")
        print("portal para RCV/BHE — avisame el resultado exacto para decidirlo.")
        return 0
    else:
        print("Ningún host aceptó el certificado. Antes de seguir, confirmemos: ¿el")
        print("certificado está vigente? ¿es el tipo correcto (persona/empresa)? ¿la")
        print("contraseña es la del .pfx y no la clave tributaria?")
        return 1


if __name__ == "__main__":
    sys.exit(main())
