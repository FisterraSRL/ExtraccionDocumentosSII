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

    # Finnegans
    finnegans_api_url: str | None = os.getenv("FINNEGANS_API_URL")
    finnegans_api_key: str | None = os.getenv("FINNEGANS_API_KEY")
    finnegans_env: str = os.getenv("FINNEGANS_ENV", "sandbox")

    # App
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./sii_finnegans.db")

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
