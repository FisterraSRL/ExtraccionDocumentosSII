# Contexto del proyecto para agentes de código (Codex, etc.)

Este archivo resume todo lo que un agente nuevo necesita para seguir trabajando en este
repo sin haber visto la conversación original. Fue generado por Claude tras varias
sesiones de trabajo con el dueño del proyecto (Fisterra SRL). Léelo completo antes de
tocar código — hay una restricción de entorno (más abajo) que hace perder mucho tiempo
si no se conoce de antemano.

## Qué es esto

Sistema que se conecta al portal del SII (Servicio de Impuestos Internos, Chile),
extrae los documentos de compra recibidos, y los envía al ERP Finnegans vía API. Lo
usa el equipo de administración de Fisterra SRL para no tener que copiar a mano cada
factura/boleta/nota de crédito del SII a Finnegans.

## Estado actual (resumen ejecutivo)

- **Funciona hoy:** carga del certificado digital (.pfx), test de conexión TLS al SII,
  el backend completo (FastAPI + SQLAlchemy + endpoints REST), y el portal web
  ("Bandeja SII") servido por el propio backend, con la identidad de marca de Fisterra,
  ya conectado a la API real (no hay datos de ejemplo).
- **No funciona todavía (a propósito, con errores explícitos, no simulado):**
  extraer documentos reales del SII (`get_rcv`/`get_bhe`/`get_dte_xml` están sin
  implementar — falta mapear la navegación real del portal del SII) y enviar a
  Finnegans (`FinnegansClient.send_document` sin implementar — falta la documentación
  de su API, todavía no la pasó el usuario).
- **Restricción de entorno crítica, leer antes de perder tiempo debuggeando:** ver
  la sección "⚠️ Restricción de entorno" más abajo. Resumen: el login por certificado
  al SII **no puede probarse desde un entorno con proxy de salida que intercepta TLS**
  (típico de sandboxes de agentes en la nube). Hay que correr esto en una máquina con
  salida directa a internet — hoy, la PC de Windows del usuario.

## Stack técnico

- Python 3.11+, FastAPI, SQLAlchemy (SQLite en dev, pensado para Postgres en prod),
  Pydantic v2, `python-dotenv`.
- `cryptography` para leer el certificado `.pfx` (PKCS12) y sacar la clave privada +
  certificado en PEM.
- `requests` para el test de conexión TLS simple.
- **Playwright** (`playwright>=1.47`) para automatizar un Chromium real que hace el
  login al SII con el certificado del almacén del sistema operativo — es el único
  camino confirmado viable (ver hallazgo técnico abajo).
- Frontend: HTML/CSS/JS vanilla en un solo archivo (`app/static/index.html`), sin
  build step, servido directamente por FastAPI. Sigue el sistema de diseño de marca de
  Fisterra (ver `references/fisterra-brand.md` si existe, o pedirle los tokens al
  usuario — colores, tipografía Montserrat, radios, degradés).

## Estructura del repo

```
app/
  main.py            → app FastAPI. Monta el router de documentos y sirve el portal
                       en GET "/" (FileResponse de app/static/index.html) — mismo
                       origen que la API, evita problemas de CORS/mixed-content.
  config.py          → Settings: lee SII_RUT, SII_CERT_PATH, SII_CERT_PASSWORD,
                       FINNEGANS_API_URL, FINNEGANS_API_KEY, FINNEGANS_ENV,
                       DATABASE_URL desde .env. require_sii_credentials() valida y
                       tira RuntimeError con mensaje accionable si falta algo.
  db.py              → engine/SessionLocal de SQLAlchemy, Base, get_db(), init_db().
  models.py          → Documento (tabla), TipoDocumento y EstadoDocumento (enums),
                       y los schemas Pydantic de la API (DocumentoOut, SyncResult,
                       EnviarResult, ItemSchema).
  sii/
    client.py         → SIIClient: _load_pkcs12() (carga real, funciona),
                       test_connection() (funciona), login_with_browser() (Playwright,
                       confirmado conceptualmente pero pendiente de correr una prueba
                       real end-to-end fuera del sandbox — ver TODO), get_rcv()/
                       get_bhe()/get_dte_xml() (NotImplementedError a propósito).
  finnegans/
    client.py         → FinnegansClient: valida config al instanciar, send_document()
                       tira NotImplementedError con la lista de lo que falta definir
                       (endpoint, mapeo de proveedores por RUT, mapeo de ítems, etc.)
  routers/
    documents.py       → GET /api/documents (filtros estado/tipo/q), POST /api/sync
                       (dispara SIIClient.get_rcv/get_bhe — hoy 501 o 503), POST
                       /api/documents/{id}/send (dispara FinnegansClient.send_document
                       — hoy 501 o 503, y persiste el resultado en el Documento).
  static/
    index.html         → El portal real ("Bandeja SII"). Vanilla JS, fetch contra
                       /api/*. Sin dependencias externas salvo Google Fonts
                       (Montserrat). Ver sección "Portal" abajo para detalles de UX.
scripts/
  test_sii_connection.py → Diagnóstico CLI: valida .env, carga el certificado, prueba
                       la conexión TLS a los hosts del SII. Correr primero siempre.
secrets/
  certificado.pfx     → EL CERTIFICADO REAL DE PRODUCCIÓN (ver sección Seguridad).
                       Gitignored. Puede no existir en este checkout — pedírselo al
                       usuario si falta.
.env                  → Credenciales reales (gitignored). Ver .env.example para las
                       claves que necesita. NUNCA leer su contenido en voz alta ni
                       pegarlo en logs/commits/mensajes — ver Seguridad.
.env.example          → Plantilla de las variables de entorno necesarias.
README.md             → Overview del proyecto + cómo correrlo.
SETUP_WINDOWS.md       → Guía paso a paso para correr todo (backend + portal) en la
                       PC de Windows del usuario, que es donde hoy vive el desarrollo.
requirements.txt       → Dependencias Python.
```

## ⚠️ Restricción de entorno: proxy TLS interceptor

Esto costó varias horas de debugging, documentado acá para que no se repita:

**El login por certificado digital al SII NO puede completarse desde un entorno cuya
salida a internet pasa por un proxy que intercepta y re-firma TLS** (MITM transparente,
común en sandboxes de agentes en la nube para poder filtrar por dominio permitido).

Se probó de tres formas distintas, con el certificado real:
1. Navegador real del usuario en su propia PC de Windows (certificado importado al
   almacén nativo del SO) → **funcionó**, login confirmado.
2. Script Python (`requests`, mutual TLS) desde un sandbox con proxy interceptor →
   falló con un error genérico del SII y redirect a `http://www.sii.cl`.
3. Chromium real vía Playwright desde el mismo sandbox, con el certificado importado a
   una base NSS y `--auto-select-certificate-for-urls` → **mismo error que el script
   simple**, mismo redirect.

Que un navegador real con el certificado bien puesto falle exactamente igual que un
script simple descarta que sea detección de bot. La explicación real: con un proxy que
intercepta TLS, la conexión mutua TLS se completa entre el proxy y el SII — el
certificado del cliente nunca sale realmente hacia el SII real, sin importar qué
herramienta de automatización se use desde ese entorno.

**Conclusión práctica:** si estás corriendo como agente dentro de un sandbox en la nube
con proxy de salida obligatorio (probá `curl -v` a `https://www.sii.cl` y fijate si hay
un `HTTPS_PROXY` seteado, o si el certificado que presenta el servidor en el handshake
no es el real de sii.cl), **no vas a poder validar el login al SII desde ahí**, por
más que el código esté perfecto. No es un bug a arreglar en el código. Las alternativas
son: (a) pedirle al usuario que corra la prueba en su propia máquina y te pase el
resultado, o (b) si tenés acceso a una shell real en la máquina del usuario (sin proxy
interceptor), correr ahí directamente.

Esto **no debería aplicar** a un servidor de producción real bien elegido (que salga
directo a internet) — es un requisito a validar al elegir dónde hostear en la nube más
adelante (ver Pendientes).

## Certificado y credenciales — SEGURIDAD

`secrets/certificado.pfx` es un **certificado digital de producción real**, no un
certificado de prueba. Titular: Alonso Enrique Álvarez Tejeda, RUT 10.439.188-5, empresa
Alvarez Asociados SpA (cliente de Fisterra), emitido por E-CERTCHILE, vigente hasta
18-jun-2027.

Reglas no negociables:
- Nunca commitear `secrets/`, `*.pfx`, `*.p12`, `.env` (ya están en `.gitignore` — no
  tocar esas líneas).
- Nunca imprimir el contenido de `.env` ni la contraseña del certificado en salidas de
  terminal que puedan quedar logueadas, en mensajes al usuario, ni en este tipo de
  documento de contexto.
- Si el certificado o el `.env` no están presentes en tu checkout, pedírselos al
  usuario por un canal directo (no los reconstruyas ni los inventes).
- Cuando esto pase a producción en la nube, el certificado y las credenciales deben
  vivir en un gestor de secretos real, no en un `.env` plano en el servidor — todavía
  sin definir proveedor.

## El portal ("Bandeja SII")

`app/static/index.html`, servido en `GET /`. Diseño con identidad de marca Fisterra
(Montserrat, paleta con rojo `#F52125` de acento, navy `#0A2F43`, superficies grises
nunca blancas puras, "titular pareado" — línea en negrita roja + línea regular en
navy). Reemplazó a un prototipo con datos de ejemplo que antes vivía como artifact en
claude.ai — ese artifact quedó solo como referencia visual, ya no es lo que se usa.

Funcionalidad implementada:
- Trae documentos reales de `GET /api/documents` al cargar (no dispara sync solo).
- Botón "Sincronizar con SII" llama a `POST /api/sync` (hoy devuelve error real, no
  simulado, porque `get_rcv`/`get_bhe` no están implementados).
- Selección de documentos pendientes (individual o en lote) y envío manual a Finnegans
  vía `POST /api/documents/{id}/send` — **nunca automático**, requisito explícito del
  usuario.
- KPIs (pendientes/enviados/con error/total), tabs por estado, filtro por tipo de
  documento, búsqueda por proveedor/RUT/folio, detalle expandible por fila (ítems del
  XML para documentos del RCV, campos de retención para BHE), modal de error con
  reintento.
- Indicador de conexión al backend (verde/rojo) junto al título.
- Bug ya corregido y a no reintroducir: el modal de error y las filas de detalle usan
  el atributo HTML `hidden` — el CSS necesita `[hidden]{display:none !important;}`
  explícito, si no una clase con `display:` en el mismo elemento gana la cascada y
  el modal queda visible aunque tenga `hidden`.

Por qué se sirve desde el propio backend y no como artifact hablándole a `localhost`:
un artifact vive en `https://claude.ai`; un navegador moderno bloquea que una página
https le hable a `http://localhost` ("mixed content"). Sirviendo el HTML desde el mismo
FastAPI, todo queda en el mismo origen y no hay ese problema — además funciona hoy,
local, sin depender de tener un backend en la nube.

## Cómo correrlo

Ver `SETUP_WINDOWS.md` para la guía completa paso a paso (Python, venv, `pip install -r
requirements.txt`, `playwright install chromium`, completar `.env`, `uvicorn
app.main:app --reload`). Resumen:

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium   # no hace falta si ya hay Chromium preinstalado en el entorno
cp .env.example .env          # completar con los datos reales, nunca commitear
python scripts/test_sii_connection.py   # valida certificado + conexión TLS
uvicorn app.main:app --reload
```

Portal en `http://localhost:8000/`, API en `http://localhost:8000/api/...`, docs
automáticas en `http://localhost:8000/docs`.

## Decisiones de producto ya tomadas (no volver a preguntar)

- **Alcance de documentos:** TODOS los tipos — facturas afectas (33) y exentas (34),
  notas de crédito (61) y débito (56), guías de despacho (52), boletas (39), y boletas
  de honorarios electrónicas (BHE, sistema separado del RCV en el SII).
- **Nivel de detalle:** hace falta el XML completo del DTE (detalle de ítems), el
  resumen del RCV (solo cabecera) no alcanza.
- **Modo de ejecución:** bajo demanda (el usuario abre el portal y/o aprieta
  "Sincronizar"), no desatendido ni programado por cron.
- **Envío a Finnegans:** siempre manual, el usuario elige qué documentos enviar
  (individual o en lote con confirmación) — nunca automático.
- **Manejo de errores:** sin notificaciones proactivas (nada de mail/Slack). El
  feedback es en el portal, al momento de sincronizar/enviar, con el error real.
- **Infraestructura objetivo (más adelante):** en la nube, pero con salida directa a
  internet sin proxy interceptor obligatorio (ver restricción de entorno arriba). Hoy:
  todo corre local en la PC de Windows del usuario.

## Pendiente / próximos pasos, en orden

1. **Confirmar el login automatizado por navegador** (`SIIClient.login_with_browser()`)
   corriendo en la PC de Windows del usuario (o cualquier entorno sin proxy
   interceptor) — hay un script de prueba en `scripts/probar_login_navegador.py` (si
   no existe en tu checkout, es porque se armó fuera de este repo; recrearlo es
   trivial: instanciar `SIIClient`, llamar `login_with_browser(headless=False)`, y
   verificar visualmente que quedó logueado).
2. **Mapear la navegación real** del RCV y del módulo de BHE dentro de "Mi SII" una
   vez logueado (URLs exactas, cómo se ve/exporta el XML de cada documento) e
   implementar `get_rcv()`, `get_bhe()`, `get_dte_xml()` en `app/sii/client.py`, más la
   persistencia en `POST /api/sync` (hoy tiene un TODO explícito: "persistir rcv + bhe
   como Documento, evitando duplicados por (tipo, folio, proveedor_rut)").
3. **Conseguir del usuario la documentación/credenciales de la API de Finnegans** y
   completar `FinnegansClient.send_document()`: endpoint de comprobantes de compra,
   cómo identifica proveedores por RUT (qué hacer si no existen), mapeo de ítems del
   XML a productos/conceptos, plan de cuentas/centro de costo por defecto, reglas para
   evitar duplicados, si se puede editar el documento antes de enviarlo.
4. Definir el período histórico desde el cual cargar documentos en la primera
   sincronización.
5. Terminar de subir el código al repo remoto (ver estado del repo abajo).
6. Elegir proveedor cloud para producción (con el requisito de salida directa sin
   proxy interceptor) y un gestor de secretos real para el certificado/credenciales.
7. Definir usuarios/roles con acceso al portal.

## Estado del repositorio Git

Remoto: `https://github.com/FisterraSRL/ExtraccionDocumentosSII.git` — al 16-sep-2026
estaba **completamente vacío** (sin ningún commit subido todavía). El historial local
tenía 3 commits (scaffolding inicial, `login_with_browser()` + hallazgo del proxy,
portal real conectado a la API). Si estás viendo esto en un checkout que ya tiene ese
historial y está sincronizado con el remoto, ignorá este párrafo — probablemente ya se
resolvió. Si no, confirmá el estado con `git log` y `git remote -v` antes de asumir nada.

## Documento de referencia más detallado

Además de este archivo, existe `requisitos-portal-sii-finnegans.md` en el proyecto de
Claude asociado (fuera de este repo de código) con el log completo de decisiones,
capturas de pantalla y el detalle turno por turno de cómo se llegó a cada conclusión.
Si tenés acceso a integraciones de Claude/Anthropic y necesitás más contexto histórico
que el que cabe acá, preguntale al usuario por ese documento.
