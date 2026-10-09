# Contexto del proyecto para agentes de IA

Todo lo que hace falta para seguir trabajando en este repo sin haber visto las
conversaciones anteriores. Leelo entero antes de tocar código: hay varias trampas que
cuestan horas si se descubren solas, y están todas anotadas acá abajo.

Última revisión: **09-oct-2026**. Si encontrás algo que ya no es cierto, corregilo en
vez de dejarlo: este archivo solo sirve si se puede confiar en él.

---

## 1. Seguridad — leer primero, no es negociable

- **Nunca commitear** `secrets/`, `*.pfx`, `*.p12`, `.env`. Ya están en `.gitignore`:
  no toques esas líneas.
- **Nunca imprimir** el contenido de `.env`, la contraseña del certificado ni las
  credenciales de Finnegans en la terminal, en mensajes al usuario, en commits o en
  este archivo. Quedan en logs y transcripciones.
- `secrets/certificado.pfx` es un **certificado digital de producción real**, no de
  prueba. Emitido por E-CERTCHILE, vigente hasta 18-jun-2027. Si no está en tu
  checkout, pedíselo al usuario: no lo reconstruyas ni lo inventes.
- `secrets/sii_sesion.json` guarda **cookies de sesión vivas del SII**. Dan acceso a la
  cuenta. Tratalo igual que al certificado.
- **El repositorio de GitHub es público.** No agregues a ningún archivo versionado
  nombres de personas, RUT de personas físicas, razones sociales de clientes ni nada
  parecido. Una versión anterior de este archivo traía el nombre y el RUT del titular
  del certificado y llegó a publicarse; se quitaron del texto actual, pero **siguen en
  el historial de git** y solo desaparecen reescribiéndolo.
- **Enviar a Finnegans escribe en un ERP.** No dispares un envío por tu cuenta: es una
  operación que el usuario confirma, documento por documento o lote por lote.

---

## 2. Qué es esto

Extrae del portal del SII (Servicio de Impuestos Internos, Chile) los documentos de
compra que reciben las empresas representadas por un certificado digital, y los manda
al ERP **Finnegans (Teamplace)** por API. Lo usa el equipo de administración de
Fisterra SRL para no copiar a mano factura por factura.

No hay API del SII: **todo lo que se lee del SII se obtiene navegando su portal con un
navegador real y el certificado digital.** La API de Finnegans es solo para la otra
punta, el envío. Es un pedido explícito del dueño del proyecto y no hay que cambiarlo.

---

## 3. Estado actual

**Anda, verificado contra producción:**

| Pieza | Dónde |
|---|---|
| Login al SII con certificado | `SIIClient.login_with_browser()` |
| Reuso de sesión y auto-freno de logins | `SIIClient.abrir_sesion()` |
| Extracción del RCV por empresa y período | `SIIClient.get_rcv()` |
| Empresas representadas, con razón social | `SIIClient.get_empresas_con_nombre()` |
| Grilla de recibidos del Portal FE | `SIIClient.listar_recibidos_portal()` |
| PDF del documento y desglose de ítems | `get_pdf_documento()` + `app/sii/pdf_dte.py` |
| Portal web con login, selector de empresa y sincronización | `app/static/index.html` |
| Armado del payload de Finnegans | `FinnegansClient.construir_payload()` |
| Envío de la selección desde el portal | `POST /api/documents/send` |

**No está hecho:**

- `get_bhe()` — boletas de honorarios. Módulo aparte del SII, sin mapear.
- `get_dte_xml()` — **hallazgo: el XML del DTE no se puede obtener del portal.** Se
  recorrieron todas las vistas del RCV con la sesión iniciada y ninguna lo entrega. El
  detalle de ítems sale del PDF, no del XML. No vuelvas a intentar este camino sin un
  dato nuevo.

**El primer documento entró en Finnegans el 8-oct-2026.** El folio de combustible
usado en las pruebas quedó marcado como enviado y un GET posterior confirmó que
Finnegans lo guardó con el importe de control correcto y tres líneas de producto. La
cuenta de compra del producto elegido exige distribuir el 100 % en la dimensión
Centros de Costo (`DIMCTC`); la cuenta, el producto y el proveedor no tenían
distribución predeterminada. Durante el período de pruebas, por decisión del
usuario, **todas las líneas de `Productos` usan `DIMCTC`, centro 5 al 100 %**,
incluidos descuentos y ajustes. Esto prevalece temporalmente sobre las
distribuciones manuales guardadas. La consulta de `CuentaDimension` se conserva
para detectar si un producto además requiere Bien de Uso (`DIMBU`). Solo esas líneas,
incluidos sus descuentos y ajustes, agregan `DIMBU`, bien `EPRUEB-001` al 100 %.
Si no se pueden leer las dimensiones de la cuenta, la vista previa y el envío se
bloquean en vez de omitir una dimensión requerida.
`DimensionDistribucion` va dentro de cada producto; con `tipoCalculo: "2"` recibe
`distribucionItems` con `codigo` y `porcentaje` que sumen 100. Fuente:
https://bc.finneg.com/t/como-llamar-un-metodo-post-de-una-transaccion/2907
Un intento anterior, el 23-sep-2026, fue rechazado y se perdió su mensaje (ver §12).

**Números al 07-oct-2026** (base local, para dimensionar): 2.736 documentos,
55 empresas, 1.367 documentos con desglose de ítems, 142 con alguna observación
sobre ese desglose y 2.337 descripciones únicas en el maestro. Versión `1.0.4`.

---

## 4. Stack y estructura

Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic v2. SQLite en local, Postgres
(`psycopg`) en la nube. Frontend: HTML/CSS/JS vanilla en un archivo, sin build.

```
app/
  main.py          FastAPI. Monta el router, sirve el portal en GET "/",
                   login/logout/version/sesion, y configura el logging (§12).
  config.py        Settings desde .env. require_sii_credentials() falla con
                   mensaje accionable si falta algo.
  db.py            engine/SessionLocal/Base/get_db/init_db. Reescribe las URL
                   postgres:// y postgresql:// al driver psycopg.
  auth.py          Login del portal: cookie firmada con HMAC, 12 h (§10).
  version.py       Única fuente de verdad de la versión (§11).
  models.py        Documento y Empresa (tablas), TIPOS_DOCUMENTO (catálogo de
                   45 tipos del SII), EstadoDocumento, y los schemas Pydantic.
  sii/
    client.py      Todo el acceso al SII. Ver §5, §6, §7.
    pdf_dte.py     Parser por coordenadas del PDF del SII. Ver §7.
  finnegans/
    client.py      Cliente de la API de Finnegans. Ver §8.
    catalogo.py    Token por perfil y lectura de producto/list.
  productos.py     Catálogo local, búsqueda y matching de ítems.
  routers/
    documents.py   Todos los /api/* de datos. Exigen sesión.
    productos.py   Sincronización local y asociaciones por ítem.
  static/
    index.html     El portal ("Bandeja SII"). Ver §9.
scripts/
  test_sii_connection.py   Valida .env + certificado + TLS. Correr primero.
  probar_login_navegador.py Prueba manual del login.
  recalcular_items.py      Rehace el control de ítems sin bajar PDF (§7).
  reabrir_envios.py        Deja pendientes documentos ya "enviados" (§8).
  copiar_a_postgres.py     Lleva la SQLite local a Postgres (§10).
  subir_version.py         Sube la versión con arrastre (§11).
api/index.py       Punto de entrada de Vercel.
vercel.json        Manda todas las rutas a esa función.
requirements.txt        Portal + base. Liviano: es lo único que instala Vercel.
requirements-sync.txt   Lo anterior + cryptography, requests, playwright, pdfplumber.
Excel datos/Reporte.xlsx  Catálogo de subtipos de transacción de Finnegans, que
                   pasó el dueño del proyecto. Sin datos de clientes.
```

### Endpoints

```
GET    /                             el portal
GET    /health, /api/version, /api/sesion      públicos
POST   /api/login, /api/logout

GET    /api/documents                filtros: empresa, estado, tipo, q
GET    /api/documents/{id}/pdf       representación impresa del SII
GET    /api/documents/{id}/finnegans el JSON que se enviaría, sin enviarlo
POST   /api/documents/{id}/send      envía uno
POST   /api/documents/send           envía la selección  {"ids": [...], "perfil_esperado": "..."}
POST   /api/documents/{id}/habilitar-reenvio  deja pendiente tras confirmar baja en Finnegans
GET    /api/empresas
PATCH  /api/empresas/{rut}           nombre manual
POST   /api/empresas/refrescar
POST   /api/empresas/asegurar       consulta SII solo si el perfil no tiene lista guardada
POST   /api/sync                     empresa, periodo, meses, con_items,
                                     reprocesar_items, descargar_pdf
GET    /api/productos/estado         cantidad local del perfil activo
POST   /api/productos/sincronizar    trae producto/list de Finnegans
GET    /api/productos?q=...          búsqueda local por nombre o código
POST   /api/documents/{id}/productos/preparar   sugiere y asocia coincidencias claras
PUT    /api/documents/{id}/items/{indice}/producto  elección o limpieza manual
GET    /api/documents/{id}/centros-costo          distribución guardada por ítem
PUT    /api/documents/{id}/items/{indice}/centros-costo  códigos y porcentajes
```

### Cómo correrlo

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -r requirements-sync.txt
playwright install chromium
cp .env.example .env        # completar; nunca commitear
python scripts/test_sii_connection.py
uvicorn app.main:app --reload
```

Portal en `http://localhost:8000/`, docs en `/docs`. Guía detallada en
`SETUP_WINDOWS.md`.

---

## 5. El login al SII

`login_with_browser()` funciona. Costó entenderlo, y durante varias sesiones se culpó a
un *proxy TLS interceptor* que **no tenía nada que ver**. Las dos causas reales eran del
lado del cliente:

1. **El `confirm()` de JavaScript.** `IngresoCertificado.html` muestra un `confirm()`
   antes de auto-enviar el formulario, y su rama `else` es
   `location.replace('http://www.sii.cl')`. Playwright **descarta los diálogos por
   defecto**, así que devolvía `false` y el navegador se iba solo a la home pública. Ese
   redirect se venía leyendo como "el SII rechazó el certificado" y era el flujo de
   cancelación. Solución: `page.on("dialog", lambda d: d.accept())`.

2. **El certificado nunca se presentaba.** El código usaba
   `--auto-select-certificate-for-urls`, que **no es un switch de Chromium** — es una
   política de empresa del registro de Windows. Chromium lo ignoraba en silencio.
   Solución: la opción `client_certificates` de Playwright (>=1.46).

   El `.pfx` de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza, así que `pfxPath`
   falla: hay que pasarle los **PEM** que `_load_pkcs12()` ya produce
   (`certPath` / `keyPath`), contra el origen `https://herculesr.sii.cl`.

Antes de activar un perfil o iniciar un nuevo login, se comprueba localmente la vigencia
del certificado contenido en el `.pfx`. Un certificado vencido no se activa ni se sube
como nuevo; la pantalla muestra la fecha de vencimiento. Esta comprobación no contacta
al SII y evita gastar intentos de autenticación con un certificado rechazado por TLS.

El flujo, por si hay que volver a depurarlo:

```
misiir.sii.cl/cgi_misii/siihome.cgi
  → zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoRutClave.html
  → [click "Ingresar con Certificado Digital"]
  → zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoCertificado.html   (confirm() + POST)
  → herculesr.sii.cl/cgi_AUT2000/CAutInicio.cgi                        (TLS mutuo)
```

El certificado **no** necesita estar en el almacén del sistema operativo: se lee del
`.pfx` en disco. El login es portable (Linux, contenedor, nube).

---

## 6. ⚠️ El SII limita la frecuencia de logins

Observado tras unas 15 autenticaciones en menos de una hora: el login empieza a terminar
en `https://www.sii.cl/servicios_online/1943-1945.html` de forma consistente, con el
mismo certificado y el mismo flujo que venían funcionando. No está confirmado con el
SII, pero es lo que encaja.

La estrategia **no es evadir el límite sino necesitar muy pocos logins**:

- **Usá siempre `abrir_sesion()`.** Guarda las cookies en `secrets/sii_sesion.json` y en
  el siguiente uso abre el navegador con ellas y comprueba con una sola petición si la
  sesión sigue viva. Reusar tarda ~8 s contra ~40 s de un login completo, y una
  sincronización entera puede correr sin autenticarse ni una vez.
- **No llames a `login_with_browser()` desde código nuevo**: es la primitiva que fuerza
  una autenticación.
- Auto-freno en `_revisar_si_puedo_loguear()`: mínimo `MIN_SEGUNDOS_ENTRE_LOGINS` (120 s)
  entre logins, y `ESPERA_TRAS_BLOQUEO_SEGUNDOS` (30 min) si el SII bloqueó. Los valores
  son conservadores a propósito: no conocemos el umbral real. Cuando el freno actúa la
  API responde **429**, no 502 — no es un fallo del SII, somos nosotros.
- **Nunca reintentes un login en automático**: alarga el bloqueo.
- `_sesion_viva()` comprueba **dos** cosas: que Mi SII reconozca la sesión y que el
  módulo del RCV inicialice. Con solo la primera pasaban sesiones que después fallaban.

Al desarrollar: evitá sincronizaciones de prueba innecesarias. Cada una es tráfico real
contra el SII.

---

## 7. Lo que se lee del SII

### RCV — Registro de Compras y Ventas

`https://www4.sii.cl/consdcvinternetui/#/index`. Es una SPA de Angular; los servicios
útiles son `getResumen`, `getDetalleCompra` y `getDatosInicio`. Entrega **cabeceras**:
emisor, folio, fecha, neto, IVA, exento, total, y el documento referenciado en las notas
de crédito y débito. No entrega ítems.

**Trampas de navegación, todas descubiertas rompiendo la sincronización:**

- **No uses `page.reload()`.** Rompe la SPA: `getDatosInicio` empieza a responder 500.
  Si ya estás en la URL del RCV, andá a `about:blank` y después navegá de nuevo.
- Un `goto` que solo cambia el hash **no recarga**. De ahí el `about:blank`.
- Antes de `select_option`, esperá el `option` concreto
  (`select[name=rut] option[value="..."]`), no el `select`. Si no, salta un timeout de
  "visible and enabled" en cuanto el combo tarda.
- Envolvé el submit en `page.expect_response(... "getResumen" ...)`: es la única señal
  confiable de que la consulta terminó.

### Portal de Facturación Electrónica — de acá salen los PDF y los nombres

Es un contexto distinto del RCV, con su propia selección de empresa.

```
https://www1.sii.cl/cgi-bin/Portal001/mipeSelEmpresa.cgi      selección de empresa
https://www1.sii.cl/cgi-bin/Portal001/mipeAdminDocsRcp.cgi    grilla de recibidos
    ?RUT_EMI=&FOLIO=&RZN_SOC=&FEC_DESDE=&FEC_HASTA=&TPO_DOC=&ESTADO=&ORDEN=&NUM_PAG=1
https://www1.sii.cl/cgi-bin/Portal001/mipeShowPdf.cgi?CODIGO=  el PDF
```

- Las fechas van en **AAAA-MM-DD**. Con AAAAMMDD la grilla devuelve cero filas **sin
  avisar**.
- Pagina de a 100 con `NUM_PAG`.
- Hay que seleccionar la empresa en este portal antes de consultar.
- **No todas las empresas están**: solo las que registraron al titular como usuario de
  este portal (30 de las 55 del RCV). Para el resto no hay desglose posible, y el portal
  lo dice en vez de dejar filas sin explicación.
- **No todos los documentos están**: la grilla y el RCV son listas distintas.

### Nombres de las empresas

El RCV lista las 55 empresas representadas **solo por RUT**: `razonSocONombreEmp` viene
`null` para todas. La razón social sale del Portal FE
(`get_empresas_con_nombre()`), que cubre 30. Para el resto está el nombre manual
(`PATCH /api/empresas/{rut}`), que **siempre manda** sobre el automático. Las empresas
que el SII deja de listar se marcan `autorizada=False`, no se borran.
`empresas_perfil` guarda qué empresas puede consultar cada certificado y si aparecen
en el Portal FE; `empresas_perfil_estado` distingue una consulta sin empresas de un
perfil todavía no consultado. Al pulsar **Usar este certificado**, la bandeja usa la
lista guardada de ese perfil o la obtiene del SII una sola vez si falta. Un cambio de
perfil limpia la selección y los documentos visibles antes de cargarla. Los perfiles
ya consultados nunca heredan la lista de otro certificado. Se reutiliza la sesión SII
existente: no se fuerza un login en cada cambio de perfil. `POST /api/empresas/refrescar`
sigue disponible para forzar una consulta nueva.

### Ítems: se leen del PDF

Lo genera el propio SII a partir del XML que guarda, así que la plantilla es la misma
para todos los emisores. `app/sii/pdf_dte.py` lo lee por coordenadas (repartir en
columnas fijas no funciona; ver su docstring).

- **Control por ítem: cantidad × precio = subtotal**, con tolerancia de un peso o 1%
  porque el SII redondea. Si el PDF no imprime una de las dos columnas, el dato se deduce
  del subtotal y queda marcado en `derivado`: no se hace pasar por leído lo que es una
  inferencia.
- `_numero()` **rechaza cualquier token con letras**. Sin eso la unidad "M3" se leía como
  cantidad 3.
- Los tokens sobrantes de la cola numérica vuelven a la descripción; no se concatenan
  (pegaba "140" y "2" en 1402).
- **Lo que no cuadra no siempre es un error de parseo.** Hay emisores que aplican
  descuento por monto, y la representación impresa del SII solo tiene columna para
  descuento por porcentaje: muestra el precio de lista y el valor ya rebajado sin mostrar
  la rebaja. El subtotal es el importe válido. Quedan con `cuadra: false` y el portal los
  resalta.
- Hay emisores que usan líneas de ítem para metadatos ("RUTCOBRANZA XXXXXXXX" con valor
  0). No se filtran: están en el documento.
- En bastantes PDF los acentos salen como "?". Es del PDF de origen, no del parseo.
- `scripts/recalcular_items.py` rehace el control sobre lo ya guardado **sin bajar
  ningún PDF**. `POST /api/sync?reprocesar_items=true` relee los PDF de los que sí lo
  necesitan.
- `app/descuentos.py` concilia los ítems con la base del documento para todos los
  tipos compatibles. Acepta descuentos por porcentaje impreso, por monto respaldado
  por subtotal y cabecera, y uno o más descuentos globales impresos. Nunca infiere
  un descuento solo del saldo. Los descuentos por ítem confirmados se guardan en
  `descuento_monto`; los globales se guardan agregados en
  `descuentos_globales_documentos` y por línea en `descuentos_globales_lineas`.
  El envío también recalcula el análisis desde los datos originales, por lo que
  funciona con documentos históricos sin reconsultar el SII ni duplicar líneas.
  Si el exceso de los ítems coincide exactamente con cargos por servicio público
  o ajustes para facilitar el pago en efectivo identificados en el PDF, los
  clasifica como exentos y genera una compensación exenta por el mismo importe.
  Otros excesos siguen bloqueados; no se compensan por saldo sin evidencia.

### El PDF: cuándo se baja y cuándo se guarda

Pesa ~170 KB y no comprime: mil documentos son ~170 MB. Lo decide `PDF_MODO`:

| `PDF_MODO` | Al sincronizar | Al abrirlo |
|---|---|---|
| `demanda` (por defecto) | no lo baja | lo trae del SII y lo **deja guardado** |
| `sincronizacion` | lo baja y guarda | ya está |
| `nunca` | no lo baja | lo trae y no lo guarda |

`demanda` es el default porque solo ocupa espacio lo que a alguien le interesó abrir.
Medido: la primera apertura ~10 s, la segunda 0,07 s.

**`demanda` y `nunca` necesitan hablar con el SII en el momento**, así que en serverless
hay que usar `sincronizacion` o no habrá PDF que mostrar (el endpoint responde 501
explicándolo). En la API, `tiene_pdf` es "ya está guardado" y `pdf_disponible` es "hay
PDF, esté guardado o haya que ir a buscarlo".

---

## 8. Finnegans (Teamplace)

`POST /facturaCompra`. Son **compras recibidas**: `/facturaVenta` pide `Cliente` y no es
nuestro caso. El dueño del proyecto dijo al principio "factura de venta" y lo corrigió
cuando se le señaló.

**Autenticación:** cada cliente Finnegans recibe el `Client_ID` y `Client_Secret`
del certificado SII activo. Pide el token con `POST /oauth/token`, el ID en el cuerpo
y el secreto en el encabezado; acepta token en texto plano o JSON. Lo usa como
`Authorization: Bearer`. Nunca leer credenciales globales de `.env` ni incluirlas en
URLs, logs o mensajes de error.

**Mapeos comprobados contra la instancia real:**

- **Proveedor**: el código de proveedor **es el RUT con puntos** (`XX.XXX.XXX-X`).
  Nosotros lo guardamos sin puntos → `formatear_rut()`.
- **Empresa**: `empresaChile/{codigo}` expone el RUT en `NumeroIdentificacion`, lo que
  permite cruzarla automáticamente. Hay RUT con varios registros; se toma el activo de
  código más bajo y se deja constancia en el log. `empresaChile/list` no trae el RUT:
  en la instancia actual lista 208 empresas, por lo que sus detalles se consultan con
  concurrencia acotada y el mapa se reutiliza diez minutos por perfil y credenciales.
  La primera vista previa puede tardar alrededor de 40 segundos; no implica un envío.
- **Tipo de documento**: lo identifica `TransaccionSubtipoCodigo` (FC, FCEX, NCCPRA…),
  ver `SUBTIPO_POR_TIPO_SII`. `TransaccionTipoCodigo` es siempre `OPER`.
- **Moneda**: `PES`, no `CLP`. El catálogo tiene las dos; la instancia usa `PES`.
- `ComprobanteTipoImpositivoCodigo` va en `null` y `CAE` vacío: son campos de AFIP
  argentino que en Chile no se completan.
- `Cotizaciones` **no puede ir vacío**: lleva al menos la moneda local en 1.

**Dos cosas que la documentación no deja ver y un documento real sí:**

- **`Conceptos` es el desglose impositivo** (IVA y base gravada), **no** las líneas de
  gasto. Leyendo solo la documentación se mapea mal.
- **Las líneas van en `Productos`**, con un código del maestro.
- Cuando el PDF de combustible detalla `IE Base` e `IE Variable`, el impuesto
  específico se calcula como cantidad × (base + variable), se redondea al peso y se
  envía como **una línea de producto exenta aparte**: `Cantidad: 1`, `Precio` e
  `ImporteExento` por el mismo importe. El ítem original de combustible conserva
  cantidad y precio y no lleva ese impuesto en su propio `ImporteExento`.
  El `exento` informado por el SII no se modifica.
- En `Productos`, **conservar `Cantidad` y `Precio` originales del PDF**. El control
  monetario usa `Cantidad × Precio` **sin redondear** cada línea. En la factura de
  combustible que falló, esa multiplicación difiere del neto SII solo por una
  fracción de peso: se agrega una línea de ajuste gravada. `ImporteExento` **no se
  suma** al producto: clasifica qué parte de `Cantidad × Precio` es exenta. El
  ejemplo oficial de `facturaCompra` muestra 10 × 1500 = 15000, con 10000 en
  `ImporteExento` y 5000 de base gravada. Una diferencia positiva entre total SII
  y neto + IVA + exento se agrega como línea de producto exenta separada, nunca
  como `ImporteExento` del ítem original. Una diferencia negativa sin descuento
  comprobado se bloquea;
  no se alteran cantidad, precio o IVA. Los documentos con neto y exento sin
  identificación de la afectación por ítem siguen requiriendo revisión. Solo se
  usa una línea genérica cuando no existe desglose de ítems. La vista previa
  informa el total de líneas exentas adicionales por cabecera HTTP, sin agregar
  campos al JSON de Finnegans; el modal lo destaca para el primer documento.
  Fuente del ejemplo: https://bc-dev.finneg.com/t/como-realizar-una-integracion-a-traves-de-la-api-de-factura-de-compra/3458
  Finnegans explica que un impuesto interno incorporado al precio también debe
  ir en `ImporteExento` para excluirlo de IVA y retenciones:
  https://bc.finneg.com/t/como-registrar-una-factura-de-compra-que-contiene-impuestos-internos/5030
  La bandeja guarda por ítem el producto elegido, asociado al perfil activo. La
  previsualización y el envío deben usar ese código, no volver al genérico de
  `.env`. Cuando hay líneas exentas adicionales, `ConceptoImporteGravado` del
  concepto exento debe concordar con la suma de `Productos[].ImporteExento`.
  `GET /api/documents/{id}/finnegans` responde `Cache-Control: no-store` y el
  frontend pide `cache: "no-store"`: un JSON anterior no debe parecer el actual
  al revisar un error o confirmar un envío.
- `Conceptos[].ImporteEditable` debe ir en `true` para que Finnegans use el IVA y
  la base gravada enviados. Su documentación indica que con `false` los recalcula,
  lo que puede causar el error de importe total contra importe de control. Fuente:
  https://bc-dev.finneg.com/t/como-realizar-una-integracion-a-traves-de-la-api-de-factura-de-compra/3458
  La propia base de conocimiento describe ese error por diferencias decimales de
  IVA y sugiere desactivar el control en la configuración del tipo de documento:
  https://bc.finneg.com/t/api-facturacompras-mensaje-error-el-importe-total-no-coincide-con-el-importe-de-control/4375
  No cambiar esa configuración del ERP desde esta aplicación. El folio de prueba
  sí quedó registrado tras corregir importes y asignar su Centro de Costo.
- Los descuentos comprobados se envían como **líneas separadas con `Cantidad: -1`**,
  conservando cantidad y precio originales. Los descuentos por ítem usan el mismo
  `ProductoCodigo` y Centro de Costo del ítem de origen, y la descripción identifica
  el descuento negativo. Los descuentos globales usan
  `FINNEGANS_PRODUCTO_DESCUENTO_AFECTO` o `_EXENTO` según corresponda; los exentos
  llevan `ImporteExento` negativo. Si falta el producto global correspondiente o la
  diferencia no queda justificada por subtotales, porcentaje o descuento global
  impreso, la vista previa y el envío se bloquean con los importes comparados. Solo
  se agrega un redondeo pequeño cuando los subtotales del PDF corroboran la base.
  Los documentos mixtos sin afectación por ítem siguen bloqueados. Finnegans
  recomienda productos específicos con tag DESCUENTO y cantidad negativa para Chile:
  https://bc.finneg.com/t/factura-electronica-configuracion-inicial/2558
- Para cargos por servicio público y ajustes de efectivo que exceden la base del
  SII, se conserva cada ítem original con `ImporteExento` igual a su importe y se
  agrega una línea exenta negativa con el producto Gastos Comunes
  (`FINNEGANS_PRODUCTO_AJUSTE_EXENTO`). Así el neto exento, la base gravada,
  el IVA y el total quedan iguales a la cabecera. La compensación solo se aplica
  si la suma exacta de esos cargos explica toda la diferencia; no se persiste
  en `Documento.items` y no se duplica al volver a previsualizar.

**`Vencimientos` no se manda.** El documento de ejemplo lo trae porque el ERP lo generó
al grabarlo, pero armarlo desde acá significaría elegir la cuenta contable de
proveedores, y esa imputación no nos corresponde. Decisión del dueño del proyecto.

### ⚠️ Hay emisores que imprimen los ítems con IVA incluido

En el 8% de los documentos con desglose (104 de 1.287) **los ítems suman `neto × 1,19`**.
Mandarlos como base gravada inflaría la factura: el documento usado en la prueba habría quedado
registrado en $33.296 en vez de $27.980.

Cuando todos los subtotales del PDF suman exactamente el total del SII y coinciden
`neto + IVA = total`, la conciliación reconoce precios con IVA incluido. Primero
representa los descuentos por ítem comprobados; después agrega por cada ítem una
línea negativa con el mismo producto para separar el IVA ya incluido en el precio.
Distribuye el IVA según los subtotales y exige que la suma de las líneas resultantes
sea exactamente el neto del RCV antes de sumar el concepto de IVA. Conserva las
cantidades y precios originales. Si falta alguna de esas evidencias o persiste una
diferencia, bloquea la vista previa y el envío. El desglose del PDF es informativo;
la base imponible del RCV sigue siendo el dato contable.

### Parámetros de imputación

Van en `.env` y **no tienen valor por defecto**: elegirlos mal deja asientos mal
imputados. Si faltan, `construir_payload()` falla con un mensaje claro.

| Variable | Valor hoy | Estado |
|---|---|---|
| `FINNEGANS_WORKFLOW` | `CENTRALIZA` | "Compras - CENTRALIZA", activo. **Sin confirmar por el dueño.** |
| `FINNEGANS_PRODUCTO` | `GTOSGRAL` | "Gastos Generales", genérico. **Sin confirmar.** |
| `FINNEGANS_PRODUCTO_DESCUENTO_AFECTO` | Según `.env` | Producto de descuento con imputación afecta; obligatorio solo si se detecta un descuento afecto. |
| `FINNEGANS_PRODUCTO_DESCUENTO_EXENTO` | Vacío | Producto de descuento con imputación exenta; obligatorio solo si se detecta un descuento exento. |
| `FINNEGANS_PRODUCTO_AJUSTE_EXENTO` | `AACZ-2048` | Gastos Comunes para compensar cargos y ajustes exentos comprobados. |
| `FINNEGANS_CENTRO_COSTO_PREDETERMINADO` | `5` | Temporalmente inactivo: durante las pruebas todas las líneas usan `DIMCTC`, centro 5 al 100 %. |
| `FINNEGANS_CONDICION_PAGO` | `30D` | Del documento de ejemplo. |
| `FINNEGANS_MONEDA` | `PES` | |
| `FINNEGANS_CONCEPTO_IVA` / `_EXENTO` | `COMPRA_IVA_19` / `ivacomexe` | |

Durante el período de pruebas, **la vista previa y el envío usan `EmpresaCodigo:
"PRUEBA39"` para todos los documentos**, por decisión explícita del usuario hasta que
indique cambiarla. La constante está en `app/finnegans/client.py`. Las credenciales
de API siguen siendo las del certificado activo; no se consulta el RUT del documento
para elegir la empresa destino mientras rige esta decisión. Todas las líneas del
JSON usan además `DIMCTC` con centro 5 al 100 %, aunque la cuenta no lo exija.
Cuando la cuenta de compra de un producto incluye `DIMBU`, sus líneas agregan
Bien de Uso `EPRUEB-001` al 100 % junto al centro de costo. La lectura de cuentas
usa las credenciales Finnegans del certificado activo.
El antiguo
`FINNEGANS_EMPRESA_CODIGO` de `.env` no se usa. Los envíos de prueba se reabren con
`scripts/reabrir_envios.py` tras eliminar el comprobante desde Finnegans; el script
no lo borra en el ERP.

Catálogos útiles, todos de solo lectura:
`empresaChile`, `proveedor/list`, `producto/list`, `condicionPago/list`, `moneda/list`,
`WorkflowEntidadAPI/list`. Los de `workflow/list`, `circuito/list` y
`transaccionSubtipo/list` responden 501: no existen.

**Catálogo de productos (primera versión):** OAuth respondió un token de texto plano y
`producto/list` devolvió una lista JSON completa, sin paginación visible. La lista trae
`Codigo`, `Nombre`, `UnidadIDCompra`, `Unidad`, `NombreRubro` y `NombreFamilia`; no trae
un ID distinto de `Codigo` ni el campo `Activo` (el detalle individual sí lo tiene).
La copia local se guarda por perfil en `productos_finnegans`. El transporte usa POST
OAuth con secreto en header, cachea el token por perfil y renueva tras un 401.
El filtro de fecha documentado oficialmente se llama `desde`, no `updatedSince`; la
función lo admite, pero la sincronización de la UI todavía hace carga completa.
Las asociaciones van en `asociaciones_items` por perfil, documento e índice, separadas
de `Documento.items` porque una sincronización del SII puede reemplazar ese JSON.
Una coincidencia automática requiere umbral `FINNEGANS_MATCH_UMBRAL` (default 0,86)
y distancia suficiente frente a la segunda opción. Una limpieza manual deja el ítem
pendiente y evita que se vuelva a asignar automáticamente.
Las elecciones manuales existentes son también la memoria: al abrir otro documento,
`preparar()` consulta las del mismo perfil y proveedor, verifica que el ítem siga
coincidiendo con la firma guardada y compara descripción normalizada y código SII.
Una única elección consistente se reutiliza incluso si corrige una sugerencia previa;
decisiones contradictorias o una limpieza manual impiden seleccionar automáticamente.
El historial solo contiene decisiones humanas vigentes: no aprende de sus propias
selecciones automáticas. Las alternativas recordadas aparecen primero en el buscador.

**Maestro de equivalencias:** `GET /equivalencias` abre una página local para revisar
descripciones únicas del SII. El buscador y la sugerencia están en la misma fila de
cada descripción; las selecciones se conservan al filtrar o cambiar de página y se
guardan juntas con el botón al pie de la grilla. `POST /api/equivalencias/lote`
valida todas las decisiones y las confirma en una sola transacción: un producto
inválido no deja guardado un lote parcial. Incluye el perfil esperado para impedir
que un cambio de certificado en otra pestaña aplique decisiones al perfil equivocado.
`app/equivalencias.py` normaliza sin borrar palabras que
pueden distinguir presentaciones (por ejemplo, BOLSA), y registra cada aparición con
clave documento/índice. Por eso una resincronización no infla el conteo. El primer
`GET /api/equivalencias` incorpora ítems históricos de la base; la sincronización
registra los nuevos al terminar cada empresa. `productos_sii` guarda original,
normalizado y frecuencia; `apariciones_productos_sii` une los ítems históricos;
`equivalencias_productos` guarda la decisión por perfil y referencia el catálogo
local `productos_finnegans`, sin duplicarlo. Las sugerencias son informativas y
requieren confirmación: las descripciones ya registradas en el maestro no reciben
matching automático sin una equivalencia, aunque la memoria manual previa del mismo
proveedor sigue aplicando. Una equivalencia confirmada se aplica también al cerrar
la sincronización de cada empresa, sin esperar a que se abra el detalle. Manda sobre
el matching y la memoria en documentos pendientes; una decisión manual de un ítem manda sobre el
maestro para ese ítem. Al cambiar el maestro no se reescriben documentos enviados.
`PUT /api/equivalencias/{id}` permite confirmar, dejar sin equivalencia, deshabilitar
o devolver a pendiente. Los endpoints del maestro, igual que los de productos, exigen
sesión y loopback. `producto/list` no expone ID separado: el selector busca por código
y nombre, y el código es la identidad que usa la API de Finnegans.

### Lo que falta resolver con el dueño del proyecto

1. Confirmar `WorkflowCodigo` y `ProductoCodigo` (¿uno genérico para todo, o mapeo por
   rubro?).
2. **66 proveedores** de 190 documentos no existen en Finnegans. Hay que crearlos allá.
3. Ejemplos pedidos y no entregados: una **nota de crédito** y una **factura exenta**
   exportadas del ERP, para validar esos mapeos como se hizo con la factura.
4. Cuál de los registros de empresa duplicados es la correcta.

---

## 9. El portal ("Bandeja SII")

`app/static/index.html`, servido en `GET /`. Vanilla JS, sin build, sin dependencias
salvo Google Fonts. Identidad de marca Fisterra: Montserrat, rojo `#F52125` de acento,
navy `#0A2F43`, superficies grises nunca blancas puras, "titular pareado" (línea en
negrita roja + línea regular en navy).

**El flujo es: primero la empresa, después consultar sus datos guardados o sincronizar.**
Al volver a la bandeja o cambiar de empresa, se recuperan desde la base los documentos
de esa empresa si ya se habían sincronizado; no se vuelve a consultar el SII. Las
asociaciones de productos se recuperan al abrir cada documento y el catálogo de
Finnegans sigue asociado al certificado activo, no a la empresa. Si la empresa aún no
tiene una sincronización guardada, los KPI muestran "–", no "0": un cero afirmaría
que no tiene documentos, cuando todavía no se consultó.

Funcionalidad: KPI, tabs por estado, filtro por tipo, búsqueda por proveedor/RUT/folio,
detalle expandible con los ítems, insignia de desglose (`sí` / `parcial` / `no`), visor
de PDF embebido, modal de error con el JSON del documento, y el envío a Finnegans.

**El envío.** Un solo control, al pie de la bandeja, sobre la **selección**. Antes de
mandar nada se pide `GET /api/documents/{id}/finnegans` del primero y se muestra en la
confirmación: si falta un parámetro de imputación o el proveedor no existe, se ve ahí y
el botón queda bloqueado, en vez de enterarse a mitad del lote. Se puede marcar lo que
falló (así se reintenta); lo ya enviado no.

**Nunca automático.** Requisito explícito del dueño del proyecto.

**Reenvío de un documento enviado.** La bandeja ofrece "Habilitar reenvío" solo en
documentos enviados. La persona debe eliminar primero el comprobante en Finnegans y
confirmarlo en el diálogo; la aplicación no lo borra ni puede comprobar esa baja.
El endpoint exige esa confirmación y la referencia vigente, conserva el identificador
y la fecha anteriores en `historial_reenvios`, limpia los campos del envío activo y
deja el documento pendiente. Después hay que seleccionarlo y confirmar un nuevo envío
como cualquier otro documento. No reabrir enviados automáticamente ni marcarlos al
sincronizar.

Trampas del frontend:

- El modal y las filas de detalle usan el atributo `hidden`. El CSS necesita
  `[hidden]{display:none !important;}` explícito: si no, una clase con `display:` en el
  mismo elemento gana la cascada.
- Los modales llevan `margin:auto` además de `align-items:center`. Con solo lo segundo,
  un modal más alto que la ventana se recorta por arriba y deja los botones fuera de
  alcance.
- Al editar el HTML, **el navegador cachea**. Si ves comportamiento viejo, forzá la
  recarga antes de salir a buscar el bug en otro lado.
- No atribuyas todo fallo de `fetch` a la red: un error de JavaScript cae en el mismo
  `catch` y se reportaba como "el backend no responde", mandando a diagnosticar el lugar
  equivocado.

---

## 10. Datos, despliegue y acceso

### Modelo

`Documento` tiene `UniqueConstraint(empresa_rut, tipo, folio, proveedor_rut)`: la
empresa entra en la clave porque dos empresas distintas pueden recibir el mismo tipo y
folio del mismo proveedor y no son el mismo documento.

- `tipo` es un **String con el código del SII**, no un enum cerrado: el SII puede agregar
  tipos y uno nuevo tiene que entrar igual, mostrándose como "Tipo N", en vez de reventar
  la sincronización. `TIPOS_DOCUMENTO` es solo para el nombre legible.
- `EstadoDocumento` sí es enum, y se persiste **por valor** (`values_callable`), no por
  nombre. Sin eso `?tipo=33` devolvía 500.
- **`Documento.items` usa `JSON(none_as_null=True)`.** Sin eso SQLAlchemy guarda Python
  `None` como el texto JSON `'null'` en vez de NULL de SQL, y el filtro `items IS NULL`
  con el que la sincronización busca qué enriquecer deja de encontrarlos. Apareció al
  copiar a otra base: 185 documentos quedaban invisibles.
- La sesión usa `autoflush=False`: hay que hacer **`db.flush()`** antes de seleccionar
  documentos recién creados, o el enriquecimiento no los ve.
- La sincronización actualiza los documentos existentes y **nunca pisa su estado de
  envío** a Finnegans.

### Despliegue: dos mitades

El sistema **no puede correr entero en Vercel**. La sincronización necesita un Chromium
real con el certificado, sesión en disco y minutos de ejecución. Una función serverless
no tiene nada de eso. No es un problema de configuración.

| | Dónde | Qué hace |
|---|---|---|
| Portal + API de lectura | Vercel | Muestra la bandeja, lee de Postgres |
| Sincronización | Máquina con el certificado | Habla con el SII, escribe en el mismo Postgres |

Consecuencias en el código:

- `requirements.txt` es el set liviano y es lo único que instala Vercel. El stack del SII
  vive en `requirements-sync.txt`.
- **`app/routers/documents.py` no importa `app.sii.client` arriba**: lo hace dentro de las
  funciones (`_sii()`). Si volvés a poner el import a nivel de módulo, el despliegue deja
  de arrancar.
- `POST /api/sync` y `POST /api/empresas/refrescar` detectan el entorno (variable
  `VERCEL`) y responden **501** con la explicación, en vez de un ImportError.
- `scripts/copiar_a_postgres.py` lleva los datos de la SQLite local al Postgres sin volver
  a sincronizar contra el SII (que además presiona el límite de logins).

Pendiente del lado del usuario: crear el Postgres en Vercel y cargar las variables de
entorno.

### Acceso al portal

`app/auth.py`: usuario y contraseña por variables de entorno (`PORTAL_USUARIO`,
`PORTAL_PASSWORD`) y cookie firmada con HMAC sobre `SECRET_KEY`, 12 horas. No hay tabla
de usuarios: es un portal interno.

- Si faltan esas variables el portal **no se abre**, en vez de quedar accesible.
- Todo el router de datos exige sesión. Públicos: `/api/version` y `/api/sesion`.
- `PORTAL_COOKIE_INSEGURA=1` **solo** en desarrollo sobre `http://localhost`.
- `GET /configuracion` permite configurar el certificado SII **solo desde loopback**
  y con sesión del portal. Valida el `.pfx/.p12` y su contraseña localmente, guarda el
  archivo en `secrets/` y actualiza las variables SII de `.env` al activar un perfil,
  sin instalarlo en el almacén del navegador. No habilitar esta ruta fuera de la
  máquina que sincroniza.
- La configuración admite varios perfiles de certificado. Cada perfil conserva un
  `.pfx`, contraseña y opcionalmente `Client_ID` / `Client_Secret` en
  `secrets/sii_certificados.json`, ignorado por Git. La lista y el detalle solo devuelven
  indicadores de campos configurados. El botón "Mostrar" permite consultar la contraseña
  del certificado, `Client_ID` o `Client_Secret` solo tras pulsarlo; al ocultar o cambiar
  de pestaña se borra de la vista. El token nunca se muestra. El acceso está protegido
  por sesión y loopback; nunca registrar respuestas con secretos. Catálogo, validación
  de centros de costo, vista previa y envíos usan las credenciales del mismo perfil.
  La vista previa fija ese perfil en la confirmación; si cambia antes del envío, el
  backend devuelve 409. `SIIClient` separa cookies y freno de login por `SII_PERFIL`;
  no reutilizar sesiones entre perfiles.
- `PORTAL_SIN_LOGIN=1` saltea el login. Para desarrollo: reautenticarse cada 12 horas
  no protege nada cuando el portal escucha en la propia máquina. `sesion_automatica()`
  exige **las tres** condiciones y cada una tapa un agujero distinto: la variable
  activada, que el pedido venga de loopback (se mira `request.client.host`, el socket,
  no cabeceras que el cliente escribe) y que no estemos en serverless. Dejarla prendida
  por error no abre el portal publicado. No la aflojes para "probar desde el celular":
  la bandeja muestra documentos tributarios de clientes. Además, `requiere_sesion()`
  rechaza `Origin` de otro host y `Sec-Fetch-Site: cross-site`: una web ajena abierta
  en el mismo navegador no debe poder leer Client_Secret ni llamar al envío local.

---

## 11. Versión

Vive en **`app/version.py`**, única fuente de verdad: de ahí la toman FastAPI,
`GET /api/version` y el encabezado del portal. No la escribas a mano en el HTML.

**No es semver.** Es un contador de tres dígitos que avanza de a uno y arrastra al llegar
a 10, por pedido del dueño del proyecto:

```
1.0.0 → 1.0.1 → … → 1.0.9 → 1.1.0 → … → 1.9.9 → 2.0.0
```

Después de `1.0.9` viene `1.1.0`, **no** `1.0.10`. Por eso existe
`scripts/subir_version.py`, que aplica el arrastre solo.

**Se incrementa una vez por cada publicación a GitHub**, no por cada cambio ni por cada
commit:

```bash
python scripts/subir_version.py   # 1.0.1 → 1.0.2
git add -A && git commit && git push
```

Remoto: `https://github.com/FisterraSRL/ExtraccionDocumentosSII.git`. Es **público**.

---

## 12. Trampas conocidas y errores ya cometidos

Los técnicos están en su sección; estos son los que no encajan en ninguna:

- **uvicorn no configura los loggers de la aplicación.** Los deja en WARNING sin handler,
  así que nada de lo que registra la app sale por ningún lado. Se descubrió cuando un
  envío a Finnegans falló y el motivo, que sí se registraba, no quedó en ninguna parte.
  `app/main.py` ahora hace `logging.basicConfig()`. No lo saques.

- **El usuario puede estar usando el portal mientras trabajás.** Pasó: mientras se
  editaba código, el dueño apretó "Enviar" y el intento falló. Después, para probar el
  modal de error, se marcó **ese mismo documento** con un error de prueba y **se
  sobrescribió el mensaje real**, que no estaba en ningún log. La regla: antes de
  modificar cualquier fila de la base, mirá qué hay ahí; y para probar, usá un documento
  que no tenga nada que ver con lo que el usuario está mirando.

- **No confundas "lo que se mandaría ahora" con "lo que se mandó".** El JSON que muestra
  el modal de error se rearma en el momento con la configuración actual. Es lo útil para
  decidir si reintentar, pero no es un registro histórico.

- **Sincronizar es caro.** Cada prueba es tráfico real contra el SII y acerca el bloqueo
  por frecuencia (§6). Verificá con la base que ya tenés siempre que puedas.

---

## 13. Decisiones ya tomadas — no volver a preguntar

- **Alcance:** todos los tipos de documento del RCV, más BHE cuando se implemente.
- **Detalle de ítems:** hace falta; sale del PDF del Portal FE, no del XML.
- **Modo de ejecución:** bajo demanda. El usuario abre el portal y aprieta "Sincronizar".
  Nada de cron ni desatendido.
- **Envío a Finnegans:** siempre manual, sobre la selección, nunca automático.
- **Manejo de errores:** sin notificaciones proactivas (nada de mail ni Slack). El
  feedback es en el portal, con el error real.
- **Fuente de datos:** el SII se lee con el certificado digital navegando su portal. La
  API de Finnegans es solo para el envío. No cambies esto.

### Convenciones de código

- Todo en **español**, incluidos nombres de funciones y variables del código nuevo que
  siga el estilo del que está alrededor.
- Los comentarios explican **por qué**, no qué. El código ya dice qué hace. Si algo se
  hace de una forma rara, el comentario tiene que decir qué pasó cuando se hizo de la
  forma obvia.
- Mensajes de error **accionables**: qué falta y qué hacer, no un stack trace.
- Nada de valores por defecto inventados donde una elección equivocada tenga
  consecuencias (imputación contable, por ejemplo). Preferí fallar con un mensaje claro.
- Commits en español, en presente, sin tildes (por la codificación de la terminal), con
  un cuerpo que explique el porqué.

---

## 14. Pendientes, en orden

1. Incorporar en la bandeja una edición visible de Centros de Costo por ítem. El
   backend ya guarda repartos por documento e ítem, pero hoy se completó por API.
2. Confirmar con el dueño `WorkflowCodigo` y `ProductoCodigo`.
3. Crear en Finnegans los 66 proveedores que faltan.
4. Pedir los ejemplos de nota de crédito y factura exenta, y validar esos mapeos.
5. Terminar el despliegue en Vercel: Postgres, variables de entorno,
   `scripts/copiar_a_postgres.py`.
6. Implementar `get_bhe()`.
7. Definir el período histórico desde el cual cargar en la primera sincronización.
8. Gestor de secretos real para el certificado y las credenciales cuando esto viva en la
   nube. Hoy es un `.env` plano.
9. Cosmético: el indicador de conexión del portal se queda en "Conectando con el
    backend…".
10. El `README.md` quedó congelado en la etapa de scaffolding y hoy describe mal el
    proyecto (dice que no hay conexión ni al SII ni a Finnegans, y nombra librerías que
    ya no se usan). Está publicado.
