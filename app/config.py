"""Configuración de la app, cargada desde variables de entorno (.env en desarrollo).

Nada de esto debe tener valores por defecto sensibles: si falta un dato requerido
para una operación real (certificado, credenciales de Finnegans), el código que
lo necesita debe fallar con un mensaje claro en vez de asumir algo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # SII
    sii_rut: str | None = os.getenv("SII_RUT")
    sii_cert_path: str | None = os.getenv("SII_CERT_PATH")
    sii_cert_password: str | None = os.getenv("SII_CERT_PASSWORD")
    sii_perfil: str | None = os.getenv("SII_PERFIL")

    # Finnegans (Teamplace). Client_ID y Client_Secret viven en cada perfil de
    # certificado SII; ninguna llamada al ERP usa credenciales globales de .env.
    finnegans_api_url: str = os.getenv("FINNEGANS_API_URL", "https://api.finneg.com/api")
    # Selección automática conservadora; una segunda opción cercana la bloquea igualmente.
    finnegans_match_umbral: str = os.getenv("FINNEGANS_MATCH_UMBRAL", "0.86")
    finnegans_env: str = os.getenv("FINNEGANS_ENV", "sandbox")
    # Parámetros de imputación. No tienen un valor por defecto sensato: dependen de cómo
    # esté configurado el ERP de cada empresa, y adivinarlos crearía asientos mal
    # imputados. El cliente falla con un mensaje claro si faltan.
    finnegans_workflow: str | None = os.getenv("FINNEGANS_WORKFLOW")
    # Producto del maestro con el que se cargan las líneas. Los ítems que leemos del PDF
    # son texto libre del emisor y no tienen código, así que van todos contra un producto
    # genérico de gastos y la descripción real queda en la línea.
    finnegans_producto: str | None = os.getenv("FINNEGANS_PRODUCTO")
    # Producto gravado al 19 % para una línea negativa de descuento global. No se
    # deduce del producto de los ítems: en Finnegans su imputación es distinta.
    finnegans_producto_descuento_afecto: str | None = os.getenv("FINNEGANS_PRODUCTO_DESCUENTO_AFECTO")
    # Un descuento exento necesita un producto con imputación exenta propia; no se
    # reutiliza el gravado porque eso cambiaría la clasificación tributaria.
    finnegans_producto_descuento_exento: str | None = os.getenv("FINNEGANS_PRODUCTO_DESCUENTO_EXENTO")
    # Elección del usuario: compensar cargos públicos y ajustes exentos identificados
    # en el PDF con una línea negativa del producto Gastos Comunes.
    finnegans_producto_ajuste_exento: str = os.getenv("FINNEGANS_PRODUCTO_AJUSTE_EXENTO", "AACZ-2048")
    # Reservado para cuando termine la etapa de pruebas. Hoy todas las líneas se
    # imputan a DIMCTC/5 al 100 % en construir_payload(), sin consultar la cuenta.
    finnegans_centro_costo_predeterminado: str = os.getenv("FINNEGANS_CENTRO_COSTO_PREDETERMINADO", "5")
    # "PES" y no "CLP": aunque el catálogo tiene CLP, la instancia usa PES como moneda
    # local (así viene en el documento real y en MonedaPrincipalCodigo de las empresas).
    finnegans_moneda: str = os.getenv("FINNEGANS_MONEDA", "PES")
    finnegans_condicion_pago: str = os.getenv("FINNEGANS_CONDICION_PAGO", "30D")
    # Conceptos del desglose impositivo, tal como aparecen en el documento real.
    finnegans_concepto_iva: str = os.getenv("FINNEGANS_CONCEPTO_IVA", "COMPRA_IVA_19")
    finnegans_concepto_exento: str = os.getenv("FINNEGANS_CONCEPTO_EXENTO", "ivacomexe")

    # App
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./sii_finnegans.db")
    # Cuándo se guarda el PDF de cada documento. Pesan ~170 KB y no comprimen, así que
    # bajarlos todos engorda la base rápido (1.000 documentos ~ 170 MB).
    #
    #   demanda        (por defecto) No se bajan al sincronizar. Cuando alguien abre uno,
    #                  se trae del SII en el momento y queda guardado para la próxima.
    #                  Solo ocupan espacio los documentos que a alguien le interesaron.
    #   sincronizacion Se bajan todos durante la sincronización. Aprovecha que el PDF ya
    #                  se descarga para leer los ítems, pero ocupa mucho más.
    #   nunca          No se guardan. Se traen del SII cada vez que se los abre.
    #
    # Ojo: "demanda" y "nunca" necesitan poder hablar con el SII en el momento, así que
    # solo sirven donde corre la sincronización. En un despliegue serverless hay que usar
    # "sincronizacion" o los documentos no tendrán PDF que mostrar.
    pdf_modo: str = os.getenv("PDF_MODO", "demanda").strip().lower()

    @property
    def guardar_pdf_al_sincronizar(self) -> bool:
        return self.pdf_modo == "sincronizacion"

    @property
    def guardar_pdf_al_verlo(self) -> bool:
        return self.pdf_modo in ("demanda", "sincronizacion")

    def require_sii_credentials(self) -> tuple[str, Path, str]:
        """Valida que haya credenciales del SII configuradas y devuelve (rut, cert_path, password).

        Lanza RuntimeError con un mensaje accionable si falta algo — pensado para
        que scripts/test_sii_connection.py y el cliente del SII fallen rápido y claro,
        en vez de arrastrar un None hasta un error críptico más abajo.
        """
        faltantes = []
        if not self.sii_rut:
            faltantes.append("SII_RUT")
        if not self.sii_cert_path:
            faltantes.append("SII_CERT_PATH")
        if not self.sii_cert_password:
            faltantes.append("SII_CERT_PASSWORD")
        if faltantes:
            raise RuntimeError(
                "Faltan variables de entorno para conectar al SII: "
                + ", ".join(faltantes)
                + ". Completá .env (ver .env.example)."
            )

        cert_path = Path(self.sii_cert_path)  # type: ignore[arg-type]
        if not cert_path.is_file():
            raise RuntimeError(
                f"SII_CERT_PATH apunta a '{cert_path}', que no existe. "
                "Colocá el archivo .pfx ahí (por ejemplo en secrets/, ya ignorado por git)."
            )

        return self.sii_rut, cert_path, self.sii_cert_password  # type: ignore[return-value]


settings = Settings()
