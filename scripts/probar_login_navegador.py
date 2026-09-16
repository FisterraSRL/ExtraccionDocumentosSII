"""Prueba manual: loguea al SII usando el certificado ya instalado en Windows,
via un Chromium real controlado por Playwright.

Correr con: python scripts\\probar_login_navegador.py

Con headless=False el navegador se abre visible para poder confirmar con los
ojos si el login funcionó (deberías ver el nombre/RUT del contribuyente).
"""
from app.config import settings
from app.sii.client import SIIClient

rut, cert_path, password = settings.require_sii_credentials()
client = SIIClient(rut, cert_path, password)

page, context, browser, playwright = client.login_with_browser(headless=False)

input("Revisa el navegador que se abrio. Presiona Enter aca para cerrar...")

context.close()
browser.close()
playwright.stop()
