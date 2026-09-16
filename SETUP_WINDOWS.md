# Cómo correr esto en tu PC con Windows

Guía para correr el proyecto completo (backend + portal + login al SII) en tu PC.

**Nota (16-sep-2026):** una versión anterior de esta guía decía que este era el único entorno posible, por un supuesto proxy que interceptaba el TLS. Esa explicación quedó descartada — el login fallaba por otro motivo, ya corregido (ver `AGENTS.md`). El certificado se lee del `.pfx` en disco, no del almacén de Windows, así que esto también corre en Linux o en un contenedor.

## 1. Requisitos previos

- **Python 3.11 o superior** instalado. Verificá con `python --version` en una consola (PowerShell o CMD). Si no lo tenés: [python.org/downloads](https://www.python.org/downloads/) — al instalar, marcá la casilla "Add python.exe to PATH".
- **Git** (para poder hacer `git push` del repo). Si no lo tenés: [git-scm.com](https://git-scm.com/download/win).
- **No** hace falta importar el certificado en Windows ni en el navegador: el código lee el `.pfx` directamente desde la ruta que indiques en `SII_CERT_PATH`.

## 2. Clonar el repo (si todavía no lo tenés local)

```powershell
git clone https://github.com/FisterraSRL/ExtraccionDocumentosSII.git
cd ExtraccionDocumentosSII
```

Si ya lo tenés clonado, solo entrá a esa carpeta.

## 3. Crear el entorno virtual e instalar dependencias

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Instalar el navegador de Playwright

Esto es un paso nuevo (no estaba en el `README.md` original) porque agregamos automatización de navegador para el login al SII. El sandbox de Claude tiene un Chromium preinstalado, pero tu PC no, así que hace falta bajarlo una vez:

```powershell
playwright install chromium
```

## 5. Configurar las credenciales

```powershell
copy .env.example .env
```

Editá `.env` con un editor de texto y completá:

```
SII_RUT=10.439.188-5
SII_CERT_PATH=secrets/certificado.pfx
SII_CERT_PASSWORD=<la contraseña real del .pfx>
FINNEGANS_API_URL=
FINNEGANS_API_KEY=
FINNEGANS_ENV=sandbox
DATABASE_URL=sqlite:///./sii_finnegans.db
```

Copiá el `.pfx` real a `secrets/certificado.pfx` (esa carpeta ya está excluida en `.gitignore`, así que nunca se sube a GitHub). **Nunca compartas ni commitees el `.env` ni el `.pfx`.**

## 6. Probar la conexión al SII

```powershell
python scripts\test_sii_connection.py
```

Esto valida que el certificado cargue y que el SII acepte el handshake TLS. Como estás fuera del proxy del sandbox, este paso debería funcionar directamente.

## 7. Probar el login por navegador (el paso que en el sandbox no pudo confirmarse)

Como tu certificado ya está en el almacén de Windows, **no hace falta** el parámetro `nss_home` — eso es solo para Linux. Un script mínimo para probarlo:

```python
# scripts/probar_login_navegador.py
from app.config import settings
from app.sii.client import SIIClient

rut, cert_path, password = settings.require_sii_credentials()
client = SIIClient(rut, cert_path, password)

page, context, browser, playwright = client.login_with_browser(headless=False)  # headless=False para ver el navegador

input("Revisá el navegador que se abrió. Presioná Enter acá para cerrar...")

context.close()
browser.close()
playwright.stop()
```

Con `headless=False` el navegador se abre visible en tu pantalla, así podés confirmar con los ojos si quedó logueado (deberías ver el nombre/RUT del contribuyente, "Alvarez Asociados SpA" o similar). Guardalo como `scripts/probar_login_navegador.py` y corré:

```powershell
python scripts\probar_login_navegador.py
```

## 8. Levantar el backend y el portal

```powershell
uvicorn app.main:app --reload
```

Ahora esto sirve dos cosas a la vez, en el mismo puerto:

- **El portal (Bandeja SII):** abrí `http://localhost:8000/` en tu navegador. Es el mismo diseño con la identidad de Fisterra que ya viste en el prototipo, pero ahora conectado a datos reales — lee y escribe contra la API de abajo, no contra datos de ejemplo.
- **La API:** `http://localhost:8000/api/...` (documentación automática en `http://localhost:8000/docs`).

Al abrir el portal vacío (sin haber sincronizado todavía) vas a ver la bandeja en cero, con un aviso de "Conectado a tu backend local" en verde si el servidor está corriendo, o en rojo si no lo está. El botón "Sincronizar con SII" va a devolver un error visible en el portal hasta que terminemos de implementar `get_rcv()`/`get_bhe()` (paso pendiente, ver abajo) — es esperado, no es un bug: el portal ya está mostrando honestamente que esa pieza falta, tal como pediste (nada de datos simulados).

## Qué falta después de esto

- Confirmar que el login por navegador funciona en tu PC (paso 7) — con eso cerramos la parte de acceso al SII.
- Mapear la navegación real dentro de Mi SII para RCV y BHE (URLs exactas, cómo se ve/exporta el XML de cada documento) e implementar `get_rcv()`, `get_bhe()`, `get_dte_xml()` en `app/sii/client.py` — es lo que hace falta para que "Sincronizar con SII" traiga documentos reales.
- Documentación/credenciales de la API de Finnegans para implementar el envío real (`FinnegansClient.send_document()`) — es lo que hace falta para que "Enviar" funcione en vez de mostrar el error de configuración.
- Más adelante: elegir dónde hostear esto en producción — el único requisito ya identificado es que la salida a internet sea directa, sin un proxy interceptor obligatorio de por medio (lo que sí tiene el sandbox de Claude en la nube).
