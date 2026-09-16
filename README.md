# SII → Finnegans

Sistema que extrae documentos de compra recibidos desde el SII (Chile) — Registro de Compras y Ventas (RCV) y Boletas de Honorarios Electrónicas (BHE) — y permite revisarlos y enviarlos a Finnegans vía API.

Contexto completo del proyecto (decisiones, alcance, pendientes) en el documento de requisitos del proyecto de Claude: `requisitos-portal-sii-finnegans.md`.

## Estado actual

Scaffolding inicial. Todavía no hay conexión real al SII ni a Finnegans — falta:

1. Certificado digital (.pfx) de la empresa + contraseña, y confirmar si está habilitado para "Intercambio de información" en el SII.
2. Documentación y credenciales concretas del endpoint de Finnegans para registrar comprobantes de compra.

## Stack

- **Backend:** Python 3.11+ / FastAPI
- **Base de datos:** SQLite en desarrollo (`sii_finnegans.db`), pensado para migrar a Postgres en producción sin cambiar el modelo.
- **SII:** `requests` para autenticación (mutual TLS con el certificado digital) y `zeep` para los servicios web SOAP del SII cuando corresponda. `cryptography` para leer el `.pfx`.
- **Frontend:** el prototipo interactivo ya publicado (Bandeja SII) consume esta API vía `fetch`; migrar su lógica de datos de ejemplo a llamadas reales es el paso siguiente una vez el backend responda.

## Estructura

```
app/
  main.py            → app FastAPI, monta los routers
  config.py          → carga configuración desde variables de entorno (.env)
  db.py              → engine y sesión de SQLAlchemy (SQLite en dev)
  models.py          → modelo Documento (tabla) + esquemas Pydantic de la API
  sii/
    client.py         → SIIClient: autenticación con certificado digital y extracción de RCV/BHE/XML (pendiente de validar con certificado real)
  finnegans/
    client.py         → FinnegansClient: stub pendiente de la documentación de su API
  routers/
    documents.py       → endpoints /api/documents, /api/sync, /api/documents/{id}/send, /api/documents/{id}/retry
scripts/
  test_sii_connection.py → primer script a correr apenas tengamos el certificado: valida que el .pfx cargue y que el SII acepte la autenticación por certificado.
```

## Cómo correrlo en desarrollo

```bash
python -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # completar con los datos reales, nunca commitear .env
uvicorn app.main:app --reload
```

La API queda en `http://localhost:8000`. Docs automáticas de FastAPI en `http://localhost:8000/docs`.

## Seguridad

- El certificado `.pfx` y su contraseña **nunca se commitean**. Van fuera del repo, referenciados por ruta y contraseña en variables de entorno (ver `.env.example` y `.gitignore`).
- Mismo criterio para las credenciales de Finnegans.
- Cuando esto pase a producción en la nube, el certificado y las credenciales deben vivir en un gestor de secretos (no en variables de entorno planas del servidor) — pendiente de definir proveedor cloud.

## Próximo paso concreto

1. Colocar el certificado `.pfx` en una ruta fuera del repo (por ejemplo `secrets/certificado.pfx`, ya excluida en `.gitignore`) y completar `.env` con `SII_RUT`, `SII_CERT_PATH` y `SII_CERT_PASSWORD`.
2. Correr `python scripts/test_sii_connection.py` — valida que el certificado cargue y que el SII responda a la autenticación. A partir de ese resultado definimos si el camino es el servicio web de Intercambio o automatización del portal.
