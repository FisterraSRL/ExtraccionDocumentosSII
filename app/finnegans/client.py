"""Cliente de la API de Finnegans.

Stub a propósito: falta la documentación real del endpoint (URL base, forma de
autenticación, forma exacta del payload para un comprobante de compra con detalle
de ítems, y si BHE va por el mismo endpoint que una factura con IVA o por uno
distinto dado que es retención de honorarios). Ver requisitos-portal-sii-finnegans.md,
sección "Integración Finnegans", para la lista completa de lo que falta confirmar.

La forma pública (`send_document`) ya está pensada para no tener que tocar el resto
del código cuando se complete la implementación real: recibe un `Documento` del
modelo y devuelve un resultado tipado, así el router que lo llama no cambia.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.models import Documento


class FinnegansConfigError(RuntimeError):
    """Falta configuración de Finnegans (URL, API key) para poder enviar documentos."""


@dataclass
class FinnegansSendResult:
    ok: bool
    finnegans_id: str | None
    error_detalle: str | None


class FinnegansClient:
    def __init__(self) -> None:
        if not settings.finnegans_api_url or not settings.finnegans_api_key:
            raise FinnegansConfigError(
                "Faltan FINNEGANS_API_URL y/o FINNEGANS_API_KEY en .env. "
                "Además falta confirmar con la documentación real de Finnegans: "
                "el endpoint de comprobantes de compra, cómo identifican proveedores "
                "por RUT, y el mapeo de ítems/plan de cuentas."
            )
        self.base_url = settings.finnegans_api_url
        self.api_key = settings.finnegans_api_key

    def send_document(self, documento: Documento) -> FinnegansSendResult:
        """Envía un documento (factura/NC/ND/guía/boleta/BHE) a Finnegans.

        TODO una vez tengamos la documentación de la API:
        - Confirmar el endpoint y el verbo HTTP.
        - Mapear proveedor_rut a como Finnegans identifica proveedores (¿falla si no existe?).
        - Mapear cada item (desc/cant/precio/subtotal) al formato de línea de detalle de Finnegans.
        - Definir si BHE usa este mismo endpoint o uno de retenciones/honorarios distinto.
        - Manejar duplicados (¿Finnegans rechaza un folio ya cargado, o hay que chequear antes?).
        """
        raise NotImplementedError(
            "Pendiente de la documentación de la API de Finnegans — ver "
            "requisitos-portal-sii-finnegans.md, sección 'Integración Finnegans'."
        )
