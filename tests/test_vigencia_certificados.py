"""El vencimiento se detecta localmente, antes de abrir Playwright o cambiar de perfil."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID

from app import configuracion_sii
from app.sii.client import SIIClient, SIICertificateError


def _pfx(vencido: bool) -> bytes:
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ahora = datetime.now(timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Prueba")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Prueba")]))
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - timedelta(days=30))
        .not_valid_after(ahora - timedelta(days=1) if vencido else ahora + timedelta(days=30))
        .sign(clave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"prueba", clave, certificado, None, BestAvailableEncryption(b"clave-prueba")
    )


def test_certificado_vencido_se_muestra_y_no_se_activa():
    with TemporaryDirectory() as temporal:
        ruta = Path(temporal) / "certificado.pfx"
        ruta.write_bytes(_pfx(vencido=True))
        datos = {"activo": "otro", "perfiles": {
            "vencido": {"nombre": "Prueba", "rut": "00000000-0", "archivo": ruta.name,
                         "password": "clave-prueba"}
        }}
        with patch.object(configuracion_sii, "DIRECTORIO_SECRETOS", Path(temporal)), patch.object(
            configuracion_sii, "_leer_perfiles", return_value=datos
        ), patch.object(configuracion_sii, "_activar") as activar:
            assert configuracion_sii.estado()["perfiles"][0]["certificado_vencido"] is True
            assert configuracion_sii.detalle("vencido")["certificado_vencido"] is True
            with TestCase().assertRaisesRegex(configuracion_sii.ConfiguracionSIIError, "venció"):
                configuracion_sii.seleccionar("vencido")
            activar.assert_not_called()


def test_certificado_nuevo_vencido_no_se_guarda():
    with TestCase().assertRaisesRegex(configuracion_sii.ConfiguracionSIIError, "venció"):
        configuracion_sii._validar("00000000-0", "clave-prueba", _pfx(vencido=True))


def test_cliente_sii_no_inicia_login_con_certificado_vencido():
    with TemporaryDirectory() as temporal:
        ruta = Path(temporal) / "certificado.pfx"
        ruta.write_bytes(_pfx(vencido=True))
        cliente = SIIClient("00000000-0", ruta, "clave-prueba")
        with TestCase().assertRaisesRegex(SIICertificateError, "vencido"):
            cliente._load_pkcs12()
        assert cliente._cert_pem_file is None
        assert cliente._key_pem_file is None


def test_certificado_vigente_puede_seleccionarse():
    with TemporaryDirectory() as temporal:
        ruta = Path(temporal) / "certificado.pfx"
        ruta.write_bytes(_pfx(vencido=False))
        datos = {"activo": "otro", "perfiles": {
            "vigente": {"nombre": "Prueba", "rut": "00000000-0", "archivo": ruta.name,
                         "password": "clave-prueba"}
        }}
        with patch.object(configuracion_sii, "DIRECTORIO_SECRETOS", Path(temporal)), patch.object(
            configuracion_sii, "_leer_perfiles", return_value=datos
        ), patch.object(configuracion_sii, "_activar", return_value={"activo": "vigente"}) as activar:
            assert configuracion_sii.seleccionar("vigente")["activo"] == "vigente"
            activar.assert_called_once()


if __name__ == "__main__":
    test_certificado_vencido_se_muestra_y_no_se_activa()
    test_certificado_nuevo_vencido_no_se_guarda()
    test_cliente_sii_no_inicia_login_con_certificado_vencido()
    test_certificado_vigente_puede_seleccionarse()
    print("Vigencia de certificados: OK")
