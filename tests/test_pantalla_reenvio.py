"""La bandeja exige una confirmación antes de volver a habilitar un enviado."""
import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def test_habilitacion_visible_sin_envio_automatico():
    documento = {
        "id": 1, "empresa_rut": "00000000-0", "tipo": "33",
        "tipo_nombre": "Factura Electrónica", "folio": 123456,
        "proveedor_rut": "11111111-1", "proveedor_nombre": "Proveedor de prueba",
        "fecha": "2025-01-02", "total": 1000, "neto": 840, "iva": 160,
        "exento": 0, "items": [], "estado": "enviado",
        "fecha_envio": "2025-01-03T12:00:00", "finnegans_id": "COMPROBANTE_PRUEBA",
    }
    solicitudes_reenvio = []
    solicitudes_envio = []

    def responder(ruta):
        path = urlparse(ruta.request.url).path
        if path == "/api/sesion":
            datos = {"autenticado": True, "configurada": True, "sin_login": True}
        elif path == "/api/version":
            datos = {"version": "1.0.4"}
        elif path == "/api/configuracion/sii":
            datos = {"nombre_activo": "Certificado de prueba"}
        elif path == "/api/empresas":
            datos = [{"rut": "00000000-0", "nombre_mostrado": "Empresa de prueba", "documentos": 1}]
        elif path == "/api/sync":
            datos = {"mensaje": "Sincronización simulada", "documentos_nuevos": 0}
        elif path == "/api/documents":
            datos = [documento]
        elif path == "/api/documents/1/habilitar-reenvio":
            solicitudes_reenvio.append(ruta.request.post_data_json)
            documento.update(estado="pendiente", finnegans_id=None, fecha_envio=None)
            datos = documento
        elif path in ("/api/documents/send", "/api/documents/1/send"):
            solicitudes_envio.append(path)
            datos = {}
        else:
            datos = {}
        ruta.fulfill(status=200, content_type="application/json", body=json.dumps(datos))

    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch(headless=True)
        pagina = navegador.new_page()
        pagina.route("**/api/**", responder)
        pagina.goto("http://127.0.0.1:8000/")
        pagina.get_by_label("Empresa a consultar").select_option("00000000-0")
        pagina.get_by_role("button", name="Sincronizar con SII").click()
        pagina.get_by_role("button", name="Habilitar reenvío").click()
        confirmar = pagina.get_by_role("button", name="Dejar pendiente para reenviar")
        assert confirmar.is_disabled()
        assert "COMPROBANTE_PRUEBA" in pagina.locator("#reenvioMeta").inner_text()
        pagina.get_by_label("Confirmo que el comprobante ya fue eliminado en Finnegans.").check()
        confirmar.click()
        pagina.get_by_text("Documento pendiente. Seleccionalo cuando quieras volver a enviarlo.").wait_for()
        assert solicitudes_reenvio == [{
            "eliminado_en_finnegans": True, "finnegans_id": "COMPROBANTE_PRUEBA",
        }]
        assert not solicitudes_envio
        assert pagina.locator('input[data-action="select"][data-id="1"]').is_visible()
        assert not pagina.locator('input[data-action="select"][data-id="1"]').is_checked()
        navegador.close()


if __name__ == "__main__":
    test_habilitacion_visible_sin_envio_automatico()
    print("Pantalla de reenvío: OK")
