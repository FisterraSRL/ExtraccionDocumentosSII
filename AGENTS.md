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
  **el login automatizado al SII** (`login_with_browser()`, confirmado end-to-end contra
  producción — ver la sección del login más abajo), el backend completo (FastAPI +
  SQLAlchemy + endpoints REST), y el portal web ("Bandeja SII") servido por el propio
  backend, con la identidad de marca de Fisterra, ya conectado a la API real (no hay
  datos de ejemplo).
- **No funciona todavía (a propósito, con errores explícitos, no simulado):**
  extraer documentos reales del SII (`get_rcv`/`get_bhe`/`get_dte_xml` están sin
  implementar — falta mapear la navegación real del portal del SII) y enviar a
  Finnegans (`FinnegansClient.send_document` sin implementar — falta la documentación
  de su API, todavía no la pasó el usuario).
- **Si venís de una versión anterior de este documento:** la "restricción de entorno
  por proxy TLS interceptor" que se daba por cierta **quedó descartada**. No era la
  causa del fallo de login. Ver la sección "✅ Login al SII: RESUELTO" más abajo antes
  de tomar cualquier decisión de infraestructura basada en aquello.

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

## ✅ Login al SII: RESUELTO (16-sep-2026) — leer antes de tocar `sii/client.py`

`SIIClient.login_with_browser()` **funciona**, confirmado end-to-end contra el SII de
producción: inicia sesión, y desde esa sesión se llega al Registro de Compras y Ventas
(`https://www4.sii.cl/consdcvinternetui/#/index`), que lista las empresas a las que el
RUT tiene acceso.

**Corrección importante de una conclusión anterior de este documento.** Durante varias
sesiones se sostuvo que el login fallaba por un *proxy TLS interceptor* en el sandbox de
la nube. **Esa hipótesis era incorrecta.** El mismo síntoma (redirect a `www.sii.cl`)
aparecía en la PC de Windows del usuario, con salida directa a internet. Las causas
reales eran dos, ambas del lado del cliente:

1. **El `confirm()` de JavaScript.** La página `zeusr.sii.cl/AUT2000/InicioAutenticacion/
   IngresoCertificado.html` muestra un `confirm()` antes de auto-enviar el formulario de
   autenticación, y su rama `else` es literalmente `location.replace('http://www.sii.cl')`.
   Playwright **descarta los diálogos por defecto**, así que el `confirm()` devolvía
   `false` y el navegador se iba solo a la home pública. Ese redirect, que se venía
   leyendo como "el SII rechazó el certificado", era en realidad el flujo de cancelación.
   Solución: `page.on("dialog", lambda d: d.accept())`.

2. **El certificado nunca se presentaba.** El código usaba
   `--auto-select-certificate-for-urls`, que **no es un switch de línea de comandos de
   Chromium** — es una política de empresa (registro de Windows). Chromium lo ignoraba
   en silencio. Solución: la opción `client_certificates` de Playwright (>=1.46).

   Detalle adicional: el `.pfx` de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza
   ("Unsupported TLS certificate"), así que `pfxPath` falla. Hay que pasarle los PEM que
   `_load_pkcs12()` ya produce (`certPath`/`keyPath`).

Datos concretos del flujo, por si hay que volver a depurarlo:

```
misiir.sii.cl/cgi_misii/siihome.cgi
  → zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoRutClave.html
  → [click "Ingresar con Certificado Digital"]
  → zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoCertificado.html   (confirm() + POST)
  → herculesr.sii.cl/cgi_AUT2000/CAutInicio.cgi                        (TLS mutuo)
```

`herculesr.sii.cl` es el host que hace la autenticación TLS mutua. Se verificó aparte,
con `requests`, que acepta el handshake con este certificado y responde 200.

**Consecuencias para la infraestructura.** El certificado ya **no** necesita estar en el
almacén del sistema operativo: se lee del `.pfx` en disco. Eso vuelve el login portable
(Linux, contenedor, cloud) y elimina el parámetro `nss_home`. Y el requisito de "salida a
internet sin proxy interceptor" queda **sin fundamento confirmado** — se derivaba de la
hipótesis descartada. Habrá que reprobarlo en el entorno de destino, pero ya no es una
restricción conocida a la hora de elegir hosting.

## Empresas representadas

El certificado de Alonso Álvarez representa a **55 contribuyentes**. El RCV los lista en
su selector y en el servicio `getDcvEmpresasAutorizadas`, pero **solo por RUT**: el campo
`razonSocONombreEmp` viene `null` para todos. No hay ninguna pantalla del portal que dé
la lista con nombres.

El único lugar donde aparece la razón social de una empresa representada es el modal
`verDTE` de un documento suyo ("Razón Social Receptor"). Por eso el nombre se resuelve
de dos formas, y ninguna es obligatoria para que el sistema funcione:

- **Automática:** `get_rcv()` aprovecha que está en la pantalla de detalle y lee la razón
  social del receptor (`SIIClient.get_nombre_empresa()`). Solo funciona si la empresa
  tiene al menos un documento en el período.
- **Manual:** el usuario le pone el nombre con el que la conoce desde el portal
  (`PATCH /api/empresas/{rut}`). Un nombre puesto a mano **siempre** manda sobre el que
  se lea del SII.

`POST /api/empresas/refrescar` trae la lista desde el SII. Las empresas que el SII deja
de listar se marcan `autorizada=False` en vez de borrarse, para no perder sus documentos.

**Sin verificar todavía contra el SII** (quedó bloqueado por límite de frecuencia de
logins mientras se desarrollaba esto, ver sección siguiente): `get_empresas()`,
`get_nombre_empresa()` y la sincronización de varias empresas en una pasada
(`POST /api/sync?empresa=todas`). El modelo, la API y el portal sí están verificados.

## ⚠️ El SII limita la frecuencia de logins

Observado el 16-sep-2026, después de unas 15 autenticaciones en menos de una hora
durante el mapeo del RCV: el login empezó a terminar en
`https://www.sii.cl/servicios_online/1943-1945.html` (una página de ayuda) en vez de
iniciar sesión, de forma consistente, con el mismo certificado y el mismo flujo que
venían funcionando. Se descartó que el mecanismo hubiera cambiado: la página
`IngresoCertificado.html` seguía teniendo el `confirm()` y el formulario a
`herculesr.sii.cl`, y el handshake TLS mutuo con ese host seguía respondiendo 200.

No está confirmado con el SII, pero la explicación que encaja es una limitación por
frecuencia. `login_with_browser()` detecta ese redirect y lo reporta con un mensaje
específico en vez del error genérico.

**Consecuencias de diseño, a tener en cuenta:**

- Reusar una sesión abierta para varias consultas. `get_rcv()` ya acepta `session=`
  justamente para eso, y `POST /api/sync` hace un solo login para todos los períodos.
- No poner reintentos automáticos de login: empeoran el bloqueo.
- Si aparece durante el desarrollo, esperar un rato antes de volver a probar.

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
- **Nivel de detalle:** se había decidido que hacía falta el XML completo del DTE
  (detalle de ítems) y que la cabecera del RCV no alcanzaba. **Esa decisión quedó
  bloqueada por una restricción del SII** (16-sep-2026): el portal no expone el detalle
  de ítems de los documentos recibidos por ningún camino — se verificaron todas las
  vistas del RCV, el registro de aceptación/reclamo y el export CSV. El XML completo se
  intercambia directamente entre emisor y receptor ("Intercambio de información", que no
  está habilitado para este certificado). Ver el docstring de `get_dte_xml()` en
  `app/sii/client.py` para el detalle y las cuatro opciones posibles. **Hay que decidir
  esto con el dueño del proyecto antes de seguir.**
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

1. ~~Confirmar el login automatizado por navegador~~ **HECHO** (16-sep-2026).
   `login_with_browser()` autentica y lanza `SIIAuthenticationError` si no lo logra.
   Script de prueba manual en `scripts/probar_login_navegador.py`.
2. ~~Mapear la navegación del RCV~~ **HECHO** (16-sep-2026): `get_rcv()` implementado
   y verificado contra producción. Falta lo mismo para **BHE**, que es un módulo
   separado y sigue sin mapear. Y falta resolver el bloqueo del detalle de ítems
   (ver "Nivel de detalle" arriba). Lo que sigue del punto original: Punto de partida ya confirmado: con la sesión iniciada,
   `https://www4.sii.cl/consdcvinternetui/#/index` abre el Registro de Compras y Ventas
   y presenta un selector con las empresas a las que el RUT tiene acceso (hay 8; la del
   proyecto es 10439188-5) — falta mapear desde ahí los períodos, el detalle y el XML. E
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
