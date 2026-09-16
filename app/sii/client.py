"""Cliente de conexión al SII (Chile).

ACTUALIZACIÓN (16-sep-2026) — el login automatizado FUNCIONA, confirmado end-to-end
contra el SII real. Corrige dos conclusiones anteriores de este archivo:

1. "Intercambio de información" (servicio web SOAP) sigue descartado para este
   certificado. El camino es automatizar un navegador — eso no cambió.

2. **El proxy interceptor NO era la causa del fallo de login.** Esa hipótesis quedó
   descartada. Las causas reales eran dos, ambas del lado del cliente:

   a. La página `IngresoCertificado.html` del SII muestra un `confirm()` de
      JavaScript antes de enviar el formulario de autenticación. Su rama `else` es
      `location.replace('http://www.sii.cl')`. Playwright **descarta los diálogos
      automáticamente**, así que el `confirm()` devolvía `false` y el navegador se
      iba solo a la home pública del SII. Ese era el "redirect a www.sii.cl" que se
      venía interpretando como rechazo del certificado. Hay que registrar un handler
      `page.on("dialog", ...)` que lo acepte.

   b. El certificado hay que presentarlo con la opción `client_certificates` de
      Playwright, no con `--auto-select-certificate-for-urls`: ese no es un switch de
      línea de comandos de Chromium (es una política de empresa, se configura por
      registro en Windows), así que Chromium lo ignoraba en silencio.

   Además, el `.pfx` de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza por
   obsoleto, así que `pfxPath` falla con "Unsupported TLS certificate". Por eso se le
   pasan los PEM que ya produce `_load_pkcs12()` (`certPath`/`keyPath`).

Consecuencias prácticas, importantes para decidir el hosting:

- Ya no hace falta que el certificado esté en el almacén del sistema operativo: el
  `.pfx` se lee del disco. Esto vuelve el login **portable** (Linux, contenedor, cloud)
  y elimina el parámetro `nss_home` que tenía este método.
- El requisito de "salida a internet sin proxy interceptor" queda **sin confirmar**:
  se estableció a partir de la hipótesis que acabamos de descartar. Habrá que
  reprobarlo en el entorno de destino, pero ya no es una restricción conocida.

El endpoint que hace la autenticación TLS mutua es `herculesr.sii.cl` (confirmado:
acepta el handshake con este certificado y responde 200).
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import requests
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

# Hosts públicos conocidos del SII para autenticación con certificado digital.
# No confirmado todavía cuál responde mejor a un cliente no-browser — test_connection
# los prueba a ambos y reporta qué pasó con cada uno.
# Origen que hace la autenticación TLS mutua — al que hay que presentarle el
# certificado. Confirmado probando el handshake directo con requests.
SII_CERT_AUTH_ORIGIN = "https://herculesr.sii.cl"
SII_MISII_HOME = "https://misiir.sii.cl/cgi_misii/siihome.cgi"

# Registro de Compras y Ventas. La UI es una SPA Angular que habla con estos servicios
# JSON; llamarlos directo (con las cookies de la sesión del navegador) es mucho más
# estable que scrapear el DOM. Mapeado capturando el tráfico real de la UI el 16-sep-2026.
RCV_UI = "https://www4.sii.cl/consdcvinternetui/"
RCV_SERVICE = RCV_UI + "services/data/facadeService"
RCV_NAMESPACE = "cl.sii.sdi.lob.diii.consdcv.data.api.interfaces.FacadeService"

# Portal de Facturación Electrónica (el "sistema de facturación gratuito"). Es un módulo
# distinto del RCV, con su propia selección de empresa, y es el único lugar del SII donde
# están los PDF de los documentos recibidos — o sea, el detalle de ítems. Además lista
# las empresas representadas CON su razón social, cosa que el RCV no hace.
PORTAL_FE_EMPRESAS = "https://www1.sii.cl/cgi-bin/Portal001/mipeSelEmpresa.cgi"
PORTAL_FE_RECIBIDOS = (
    "https://www1.sii.cl/cgi-bin/Portal001/mipeAdminDocsRcp.cgi"
    "?RUT_EMI={rut_emisor}&FOLIO={folio}&RZN_SOC=&FEC_DESDE={desde}&FEC_HASTA={hasta}"
    "&TPO_DOC=&ESTADO=&ORDEN=&NUM_PAG=1"
)
PORTAL_FE_PDF = "https://www1.sii.cl/cgi-bin/Portal001/mipeShowPdf.cgi?CODIGO={codigo}"

# Estado de la sesión, en la misma carpeta que el certificado porque son secretos del
# mismo orden: las cookies de sesión dan acceso a la cuenta mientras estén vivas.
# `secrets/` ya está en .gitignore.
DIR_ESTADO = Path("secrets")
ARCHIVO_COOKIES = DIR_ESTADO / "sii_sesion.json"
ARCHIVO_ESTADO = DIR_ESTADO / "sii_estado.json"

# El SII limita la cantidad de autenticaciones (ver AGENTS.md). No sabemos el umbral
# exacto, así que el diseño no intenta adivinarlo: lo que hace es necesitar muy pocos
# logins (reusando la sesión) y frenarse solo si igual se acerca a un ritmo alto.
MIN_SEGUNDOS_ENTRE_LOGINS = 120
# Cuando el SII ya nos bloqueó, insistir empeora las cosas: se espera antes de reintentar.
ESPERA_TRAS_BLOQUEO_SEGUNDOS = 30 * 60

SII_AUTH_HOSTS = [
    "https://zeusr.sii.cl",   # portal de autenticación (login humano, certificado o clave)
    "https://palena.sii.cl",  # servicios web de DTE en producción (histórico, a confirmar vigencia)
]


class SIIBloqueadoError(RuntimeError):
    """No se intenta autenticar: el SII bloqueó hace poco, o sería demasiado seguido."""


class SIIPortalFEError(RuntimeError):
    """Algo falló en el Portal de Facturación Electrónica (empresa no habilitada, PDF ausente)."""


class SIIRCVError(RuntimeError):
    """Un servicio del Registro de Compras y Ventas respondió con error."""


class SIIAuthenticationError(RuntimeError):
    """El navegador terminó el flujo de login pero la sesión no quedó iniciada."""


class SIICertificateError(RuntimeError):
    """El .pfx no se pudo leer con la contraseña dada, o el archivo no es un certificado válido."""


@dataclass
class ConnectionTestResult:
    host: str
    ok: bool
    status_code: int | None
    detalle: str


class SIIClient:
    def __init__(self, rut: str, cert_path: Path | str, cert_password: str):
        self.rut = rut
        self.cert_path = Path(cert_path)
        self._cert_password = cert_password
        self._conversation_id = uuid.uuid4().hex[:13].upper()
        # Lo completa get_rcv() si logra leerlo; es dato de presentación, no crítico.
        self.nombre_empresa: str | None = None
        self._cert_pem_file: Path | None = None
        self._key_pem_file: Path | None = None

    # ---------- Certificado ----------

    def _load_pkcs12(self) -> None:
        """Lee el .pfx y deja la clave privada + certificado como archivos PEM temporales,
        que es el formato que espera `requests` para autenticación TLS mutua.

        Esto es real y corre hoy — no depende de ninguna decisión pendiente del SII.
        """
        try:
            data = self.cert_path.read_bytes()
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                data, self._cert_password.encode("utf-8")
            )
        except FileNotFoundError as exc:
            raise SIICertificateError(f"No se encontró el certificado en '{self.cert_path}'.") from exc
        except Exception as exc:  # la librería lanza distintos tipos según qué falle
            raise SIICertificateError(
                "No se pudo leer el certificado. Motivo más probable: la contraseña "
                f"es incorrecta o el archivo no es un .pfx/.p12 válido. Detalle: {exc}"
            ) from exc

        if private_key is None or certificate is None:
            raise SIICertificateError(
                "El archivo cargó pero no contiene clave privada y certificado juntos "
                "(esperado en un .pfx exportado desde el navegador o la autoridad certificadora)."
            )

        key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        cert_pem = certificate.public_bytes(Encoding.PEM)

        # Archivos temporales porque `requests` pide rutas de archivo para cert=(cert, key),
        # no bytes en memoria. Se borran al cerrar el cliente (ver close()).
        key_f = tempfile.NamedTemporaryFile(suffix=".key.pem", delete=False)
        key_f.write(key_pem)
        key_f.close()
        cert_f = tempfile.NamedTemporaryFile(suffix=".cert.pem", delete=False)
        cert_f.write(cert_pem)
        cert_f.close()

        self._key_pem_file = Path(key_f.name)
        self._cert_pem_file = Path(cert_f.name)

    def close(self) -> None:
        for f in (self._cert_pem_file, self._key_pem_file):
            if f and f.exists():
                f.unlink()

    def __enter__(self) -> "SIIClient":
        self._load_pkcs12()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- Primer paso real: ¿el SII acepta este certificado? ----------

    def test_connection(self, timeout: float = 15.0) -> list[ConnectionTestResult]:
        """Intenta un handshake TLS mutuo (con el certificado cargado) contra los hosts
        conocidos del SII y reporta qué respondió cada uno.

        Esto NO confirma todavía que "Intercambio de información" esté habilitado ni
        cuál es el endpoint correcto para RCV/BHE — solo confirma que el certificado
        es válido y que el SII lo acepta a nivel de conexión. El resultado de esto
        es lo que decide si seguimos por el Camino A o el Camino B de arriba.
        """
        if not self._cert_pem_file:
            self._load_pkcs12()

        resultados = []
        for host in SII_AUTH_HOSTS:
            try:
                resp = requests.get(
                    host,
                    cert=(str(self._cert_pem_file), str(self._key_pem_file)),
                    timeout=timeout,
                )
                resultados.append(
                    ConnectionTestResult(
                        host=host, ok=resp.ok, status_code=resp.status_code,
                        detalle=f"Respondió {resp.status_code}.",
                    )
                )
            except requests.exceptions.SSLError as exc:
                resultados.append(
                    ConnectionTestResult(
                        host=host, ok=False, status_code=None,
                        detalle=f"El servidor rechazó el certificado en el handshake TLS: {exc}",
                    )
                )
            except requests.exceptions.RequestException as exc:
                resultados.append(
                    ConnectionTestResult(host=host, ok=False, status_code=None, detalle=str(exc))
                )
        return resultados

    # ---------- Camino confirmado: navegador real con el certificado instalado ----------

    # ---------- Reuso de sesión: la defensa contra el bloqueo del SII ----------

    @staticmethod
    def _leer_estado() -> dict:
        try:
            return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _escribir_estado(estado: dict) -> None:
        try:
            DIR_ESTADO.mkdir(parents=True, exist_ok=True)
            ARCHIVO_ESTADO.write_text(json.dumps(estado), encoding="utf-8")
        except Exception:
            # Que no se pueda guardar el estado no debe impedir trabajar; solo se pierde
            # la protección contra logins seguidos.
            pass

    def _revisar_si_puedo_loguear(self) -> None:
        """Frena antes de pedirle al SII una autenticación que probablemente rechace."""
        estado = self._leer_estado()
        ahora = time.time()

        bloqueo = estado.get("ultimo_bloqueo", 0)
        restante = ESPERA_TRAS_BLOQUEO_SEGUNDOS - (ahora - bloqueo)
        if bloqueo and restante > 0:
            raise SIIBloqueadoError(
                f"El SII bloqueó la autenticación hace {int((ahora - bloqueo) / 60)} minutos. "
                f"No se reintenta hasta dentro de {int(restante / 60)} minutos: insistir "
                "alarga el bloqueo. Los documentos ya sincronizados se siguen viendo."
            )

        ultimo = estado.get("ultimo_login", 0)
        espera = MIN_SEGUNDOS_ENTRE_LOGINS - (ahora - ultimo)
        if ultimo and espera > 0:
            raise SIIBloqueadoError(
                f"Hubo un login al SII hace {int(ahora - ultimo)} segundos. Se esperan "
                f"{int(espera)} segundos más antes de pedir otro, para no gatillar el "
                "límite de frecuencia del SII."
            )

    def _sesion_viva(self, page) -> bool:
        """¿Las cookies guardadas siguen sirviendo? Una sola petición, sin autenticar."""
        try:
            page.goto(SII_MISII_HOME, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            texto = page.inner_text("body")
        except Exception:
            return False
        rut_plano = self.rut.replace(".", "").replace("-", "")
        return "Cerrar Sesión" in texto or rut_plano[:8] in texto.replace(".", "").replace("-", "")

    def abrir_sesion(self, headless: bool = True, forzar_login: bool = False):
        """Devuelve una sesión autenticada, **reusando** la anterior si sigue viva.

        Esta es la forma de entrar al SII que debe usar el resto del código, no
        `login_with_browser()` directo. El SII limita la cantidad de autenticaciones y
        bloquea por un rato cuando se pasa (ver AGENTS.md), así que cada login que se
        evita es la mejor protección disponible:

        1. Si hay cookies guardadas de una sesión anterior, se abre el navegador con
           ellas y se comprueba con **una** petición si siguen sirviendo. Si sirven, se
           devuelve la sesión sin autenticar.
        2. Recién si no sirven se hace el login con certificado, y las cookies nuevas
           quedan guardadas para la próxima.

        Devuelve la misma tupla que `login_with_browser()`.
        """
        from playwright.sync_api import sync_playwright  # import diferido: dependencia pesada

        if not forzar_login and ARCHIVO_COOKIES.is_file():
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=headless)
            try:
                context = browser.new_context(storage_state=str(ARCHIVO_COOKIES))
                page = context.new_page()
                page.on("dialog", lambda dialogo: dialogo.accept())
                if self._sesion_viva(page):
                    return page, context, browser, playwright
                context.close()
            except Exception:
                pass
            browser.close()
            playwright.stop()

        self._revisar_si_puedo_loguear()
        try:
            session = self.login_with_browser(headless=headless)
        except SIIAuthenticationError as exc:
            if "servicios_online/1943" in str(exc):
                estado = self._leer_estado()
                estado["ultimo_bloqueo"] = time.time()
                self._escribir_estado(estado)
            raise

        estado = self._leer_estado()
        estado["ultimo_login"] = time.time()
        estado.pop("ultimo_bloqueo", None)
        self._escribir_estado(estado)
        self._guardar_cookies(session[1])
        return session

    @staticmethod
    def _guardar_cookies(context) -> None:
        try:
            DIR_ESTADO.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(ARCHIVO_COOKIES))
        except Exception:
            pass

    def login_with_browser(self, headless: bool = True, timeout: float = 45000):
        """Loguea al SII con un Chromium real (Playwright) usando el certificado digital.

        Confirmado funcionando end-to-end contra el SII de producción. El flujo real es:

            misiir/siihome.cgi
              → zeusr/IngresoRutClave.html          (pantalla de RUT+clave)
              → [click "Ingresar con Certificado Digital"]
              → zeusr/IngresoCertificado.html       (muestra un confirm() y auto-envía
                                                     un form por POST)
              → herculesr/cgi_AUT2000/CAutInicio.cgi (TLS mutuo: acá va el certificado)

        Dos detalles sin los cuales esto falla en silencio, devolviendo la home pública
        del SII en vez de un error (ver el docstring del módulo para el detalle):

        - Hay que **aceptar el confirm()**; Playwright descarta los diálogos por defecto
          y la rama `else` del SII redirige a www.sii.cl.
        - Hay que presentar el certificado con `client_certificates`, en PEM (el .pfx
          de E-CERTCHILE usa un algoritmo que OpenSSL 3 rechaza).

        Si el RUT está autorizado para representar a otros contribuyentes, el SII
        intercala una pantalla "ESCOJA COMO DESEA INGRESAR". Este método elige
        "Continuar" (trámites propios), que es el caso de uso del proyecto.

        Devuelve (page, context, browser, playwright) con la sesión ya iniciada, o
        lanza SIIAuthenticationError si no lo logró — nunca devuelve una página sin
        autenticar haciéndola pasar por buena.
        """
        from playwright.sync_api import sync_playwright  # import diferido: dependencia pesada

        if not self._cert_pem_file:
            self._load_pkcs12()

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            client_certificates=[
                {
                    "origin": SII_CERT_AUTH_ORIGIN,
                    "certPath": str(self._cert_pem_file),
                    "keyPath": str(self._key_pem_file),
                }
            ]
        )
        page = context.new_page()
        page.on("dialog", lambda dialogo: dialogo.accept())

        try:
            page.goto(SII_MISII_HOME, wait_until="domcontentloaded", timeout=timeout)
            page.click("text=Ingresar con Certificado Digital", timeout=timeout / 2)
            page.wait_for_load_state("networkidle", timeout=timeout)

            # Pantalla de representación: seguir como el propio contribuyente.
            # Por rol y texto exacto: un `text=Continuar` genérico engancha un
            # contenedor oculto del menú y el click se queda esperando visibilidad.
            continuar = page.get_by_role("link", name="Continuar", exact=True)
            if continuar.count() > 0:
                continuar.first.click()
                page.wait_for_load_state("networkidle", timeout=timeout)

            texto = page.inner_text("body")
            rut_plano = self.rut.replace(".", "").replace("-", "")
            autenticado = "Cerrar Sesión" in texto or rut_plano[:8] in texto.replace(".", "").replace("-", "")
            if not autenticado:
                # El SII parece limitar la frecuencia de autenticaciones: tras una docena
                # de logins en menos de una hora empezó a mandar a esta página de ayuda,
                # de forma consistente, con el mismo certificado y el mismo flujo que
                # venían funcionando (el handshake TLS con herculesr seguía bien). No está
                # confirmado con el SII, pero es la explicación que encaja.
                if "servicios_online/1943" in page.url:
                    raise SIIAuthenticationError(
                        "El SII redirigió a su página de ayuda de autenticación en vez de "
                        f"iniciar sesión ({page.url}). Lo más probable es que esté limitando "
                        "la frecuencia de logins con este certificado: pasa después de "
                        "varias autenticaciones seguidas y se resuelve solo esperando. "
                        "Conviene reusar una sesión abierta en vez de loguear por cada "
                        "consulta. Si persiste después de un rato, revisar la vigencia del "
                        "certificado en el SII."
                    )
                raise SIIAuthenticationError(
                    "El flujo de login terminó sin sesión iniciada. "
                    f"URL final: {page.url}. Primeros 300 caracteres de la página: "
                    f"{texto[:300]!r}"
                )
        except Exception:
            context.close()
            browser.close()
            playwright.stop()
            raise

        return page, context, browser, playwright

    # ---------- Pendiente de validar con el resultado de login_with_browser() ----------

    # ---------- Portal de Facturación Electrónica: PDF y razón social ----------

    def get_empresas_con_nombre(self, session=None) -> list[dict]:
        """Empresas representadas, **con razón social**, desde el Portal de Facturación.

        El RCV solo entrega RUT (su campo de razón social viene null), así que esta es
        la única fuente de nombres del SII. Ojo: no es la misma lista — acá aparecen las
        empresas que registraron al titular como usuario del portal de facturación, que
        pueden ser menos que las que lo autorizaron a consultar el RCV.

        Devuelve [{"rut": "77185459-1", "nombre": "CENTRALIZA SPA"}, ...].
        """
        propia = session is None
        if propia:
            session = self.abrir_sesion(headless=True)
        page, context, browser, playwright = session
        try:
            page.goto(PORTAL_FE_EMPRESAS, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            opciones = page.eval_on_selector_all(
                "select[name=RUT_EMP] option",
                "els => els.map(e => ({rut: e.value, txt: (e.textContent||'').trim()}))",
            )
            empresas = []
            for o in opciones:
                rut = (o["rut"] or "").strip()
                if not rut:
                    continue
                # El texto es "RAZON SOCIAL 77025379-9": el RUT va al final.
                nombre = re.sub(r"\s*" + re.escape(rut) + r"\s*$", "", o["txt"]).strip()
                empresas.append({"rut": rut, "nombre": nombre or None})
            return empresas
        finally:
            if propia:
                context.close()
                browser.close()
                playwright.stop()

    def _entrar_portal_fe(self, page, rut_empresa: str) -> None:
        """Selecciona la empresa en el Portal de Facturación (requisito de ese módulo)."""
        page.goto(PORTAL_FE_EMPRESAS, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        disponibles = page.eval_on_selector_all(
            "select[name=RUT_EMP] option", "els => els.map(e => e.value).filter(Boolean)"
        )
        if rut_empresa not in disponibles:
            raise SIIPortalFEError(
                f"La empresa {rut_empresa} no está en el Portal de Facturación Electrónica. "
                "Ahí solo aparecen las empresas que registraron a este usuario en el portal "
                "de facturación, que son menos que las del RCV. Sin eso no hay PDF."
            )
        page.select_option("select[name=RUT_EMP]", rut_empresa)
        page.click("input[type=submit], button[type=submit]")
        page.wait_for_load_state("domcontentloaded", timeout=45000)
        page.wait_for_timeout(800)

    def listar_recibidos_portal(
        self,
        rut_empresa: str,
        desde: str = "",
        hasta: str = "",
        session=None,
        max_paginas: int = 10,
    ) -> list[dict]:
        """Grilla "Ver documentos recibidos" del Portal de Facturación Electrónica.

        Cada fila trae un CODIGO interno del SII, que es lo que identifica al documento
        para pedir su PDF. Las fechas van como **AAAA-MM-DD** (probado: el formato
        AAAAMMDD devuelve cero filas en silencio) o vacías para traer todo.

        La grilla pagina de a 100 filas; se recorren hasta `max_paginas` para no quedar
        atrapado si el SII devuelve siempre la misma página.

        Devuelve [{"codigo", "rut_emisor", "razon_social", "documento", "folio",
                   "fecha", "monto", "estado"}, ...].
        """
        propia = session is None
        if propia:
            session = self.abrir_sesion(headless=True)
        page, context, browser, playwright = session
        try:
            self._entrar_portal_fe(page, rut_empresa)
            salida: list[dict] = []
            vistos: set[str] = set()
            base = PORTAL_FE_RECIBIDOS.format(
                rut_emisor="", folio="", desde=desde, hasta=hasta
            )
            for pagina in range(1, max_paginas + 1):
                page.goto(
                    base.replace("NUM_PAG=1", f"NUM_PAG={pagina}"),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                # La grilla la pinta un DataTable después de cargar: sin esta espera a
                # veces se lee la página con cero filas y parece que no hay documentos.
                try:
                    page.wait_for_selector("a[href*='mipeGesDocRcp.cgi']", timeout=15000)
                except Exception:
                    break
                page.wait_for_timeout(500)

                filas = page.eval_on_selector_all(
                    "a[href*='mipeGesDocRcp.cgi']",
                    """els => els.map(a => {
                        const tr = a.closest('tr');
                        const celdas = tr ? Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim()) : [];
                        return {href: a.getAttribute('href'), celdas: celdas};
                    })""",
                )
                nuevos = 0
                for f in filas:
                    m = re.search(r"CODIGO=(\d+)", f["href"] or "")
                    c = f["celdas"]
                    if not m or len(c) < 8 or m.group(1) in vistos:
                        continue
                    vistos.add(m.group(1))
                    nuevos += 1
                    salida.append({
                        "codigo": m.group(1),
                        "rut_emisor": c[1],
                        "razon_social": c[2],
                        "documento": c[3],
                        "folio": c[4],
                        "fecha": c[5],
                        "monto": c[6],
                        "estado": c[7],
                    })
                # Última página: el SII repite la anterior en vez de devolver vacío.
                if nuevos == 0 or len(filas) < 100:
                    break
            return salida
        finally:
            if propia:
                context.close()
                browser.close()
                playwright.stop()

    def get_pdf_documento(self, codigo: str, session) -> bytes:
        """Baja la representación impresa (PDF) de un documento recibido.

        `codigo` sale de `listar_recibidos_portal()`. Requiere una sesión que ya haya
        entrado al Portal de Facturación con la empresa correspondiente.
        """
        page, context, browser, playwright = session
        resp = context.request.get(PORTAL_FE_PDF.format(codigo=codigo), timeout=60000)
        tipo = resp.headers.get("content-type", "")
        if resp.status != 200 or "pdf" not in tipo.lower():
            raise SIIPortalFEError(
                f"El SII no devolvió un PDF para el documento {codigo}: "
                f"HTTP {resp.status}, content-type {tipo!r}."
            )
        return resp.body()

    def get_items_documento(self, codigo: str, session):
        """PDF → detalle de ítems. Devuelve un `DetalleDTE` (ver app/sii/pdf_dte.py)."""
        from app.sii.pdf_dte import extraer_detalle

        return extraer_detalle(self.get_pdf_documento(codigo, session))

    def get_empresas(self, session=None) -> list[str]:
        """Lista los RUT de las empresas que este certificado puede consultar en el RCV.

        Sale del propio selector del RCV, que es la fuente de verdad de lo que el SII
        deja consultar hoy. El servicio `getDcvEmpresasAutorizadas` devuelve lo mismo
        pero con `razonSocONombreEmp` en null para todas, así que **no hay nombres**:
        el nombre se resuelve aparte (ver `get_nombre_empresa()` y el modelo Empresa).
        """
        propia = session is None
        if propia:
            session = self.abrir_sesion(headless=True)
        page, context, browser, playwright = session
        try:
            if not page.url.startswith(RCV_UI):
                page.goto(RCV_UI + "#/index", wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1500)
            ruts = page.eval_on_selector_all(
                "select[name=rut] option",
                "els => els.map(e => e.value).filter(v => v && v.trim())",
            )
            return [r.strip() for r in ruts]
        finally:
            if propia:
                context.close()
                browser.close()
                playwright.stop()

    def get_nombre_empresa(self, page) -> str | None:
        """Razón social de la empresa consultada, leída del detalle de un documento.

        Es el único lugar del portal del SII donde aparece: el modal `verDTE` muestra
        "Razón Social Receptor", y el receptor es justamente la empresa consultada. Hay
        que estar en la pantalla de detalle de un tipo de documento, con al menos una
        fila. Devuelve None si no se puede leer — es información para mostrar, nunca
        motivo para fallar una sincronización.
        """
        try:
            enlace = page.locator("[ng-click^='verDTE']")
            if enlace.count() == 0:
                return None
            enlace.first.click()
            page.wait_for_timeout(2500)
            texto = page.inner_text("body")
            m = re.search(r"Razón Social Receptor\s*:?\s*(.+)", texto)
            nombre = m.group(1).strip() if m else None
            cerrar = page.locator("button:has-text('Cerrar')")
            if cerrar.count():
                cerrar.first.click()
                page.wait_for_timeout(500)
            return nombre or None
        except Exception:
            return None

    @staticmethod
    def _partir_rut(rut: str) -> tuple[str, str]:
        """'10.439.188-5' → ('10439188', '5')."""
        limpio = rut.replace(".", "").replace(" ", "").upper()
        cuerpo, _, dv = limpio.partition("-")
        if not cuerpo or not dv:
            raise ValueError(f"RUT con formato inesperado: {rut!r} (se esperaba 12345678-9)")
        return cuerpo, dv

    @staticmethod
    def _documento_desde_rcv(fila: dict, tipo: int) -> dict:
        """Normaliza una fila de getDetalleCompra a la forma que usa el modelo Documento."""
        fecha = fila.get("detFchDoc")  # el SII la manda como DD/MM/AAAA
        return {
            "tipo": str(tipo),
            "folio": fila.get("detNroDoc"),
            "proveedor_rut": f"{fila.get('detRutDoc')}-{fila.get('detDvDoc')}",
            "proveedor_nombre": (fila.get("detRznSoc") or "").strip(),
            "fecha": datetime.strptime(fecha, "%d/%m/%Y").date() if fecha else None,
            "neto": fila.get("detMntNeto"),
            "iva": fila.get("detMntIVA"),
            "exento": fila.get("detMntExe"),
            "total": fila.get("detMntTotal"),
            # Referencia al documento que corrige, en notas de crédito/débito.
            "tipo_doc_ref": fila.get("detTipoDocRef") or None,
            "folio_doc_ref": fila.get("detFolioDocRef"),
            "fecha_recepcion_sii": fila.get("detFecRecepcion"),
        }

    @staticmethod
    def _rcv_consultar(page, rut_objetivo: str, mes: str, anho: str) -> dict:
        """Completa el formulario del RCV y devuelve el JSON de getResumen."""
        page.goto(RCV_UI + "#/index", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        with page.expect_response(lambda r: "getResumen" in r.url, timeout=60000) as esperado:
            page.select_option("select[name=rut]", rut_objetivo)
            page.select_option("#periodoMes", mes)
            page.select_option("select[ng-model=periodoAnho]", anho)
            page.click("button[type=submit]")
        resumen = esperado.value.json()
        estado = resumen.get("respEstado") or {}
        if estado.get("codRespuesta") not in (0, None):
            raise SIIRCVError(
                f"El RCV rechazó la consulta de {anho}-{mes} para {rut_objetivo}: "
                f"código {estado.get('codRespuesta')}, {estado.get('msgeRespuesta')}"
            )
        page.wait_for_load_state("networkidle", timeout=60000)
        return resumen

    def get_rcv(self, periodo: str, rut_empresa: str | None = None, session=None) -> list[dict]:
        """Trae los documentos de COMPRA del Registro de Compras y Ventas para un período.

        `periodo` va como "AAAA-MM". `rut_empresa` permite consultar una de las empresas
        que el certificado representa; por defecto usa el RUT del titular (`SII_RUT`).

        **Cómo funciona y por qué así.** El RCV es una SPA Angular que habla con servicios
        JSON (`getResumen` para los totales por tipo de documento, `getDetalleCompra` para
        las cabeceras de cada documento). Lo ideal sería llamar esos servicios directo,
        pero **no funciona**: reproduciéndolos con las mismas cookies, el mismo cuerpo y
        los mismos headers, el backend responde `codRespuesta: 99, "El token no es
        valido"`. Se probó llamándolos desde el contexto HTTP de Playwright y con un
        `fetch` dentro de la propia página; ambos fallan igual, mientras que las llamadas
        que dispara la SPA funcionan. El backend ata los datos al ciclo de vida de la
        aplicación de alguna forma que no quedó identificada. **No re-intentar la vía
        API-pura sin un hallazgo nuevo.**

        Así que se maneja la UI (seleccionar empresa y período, apretar "Consultar") pero
        se leen las **respuestas JSON** que esa interacción dispara, no el DOM. Es estable
        frente a cambios de maquetado y entrega los datos ya tipados.

        Devuelve cabeceras, no el detalle de ítems: eso vive en el XML del DTE, que el RCV
        no expone. Ver `get_dte_xml()`.
        """
        ptributario = periodo.replace("-", "")
        if len(ptributario) != 6 or not ptributario.isdigit():
            raise ValueError(f"Período inválido: {periodo!r}. Se espera 'AAAA-MM' (ej: '2026-08').")
        anho, mes = ptributario[:4], ptributario[4:]
        rut_objetivo = (rut_empresa or self.rut).replace(".", "").upper()

        propia = session is None
        if propia:
            session = self.abrir_sesion(headless=True)
        page, context, browser, playwright = session

        detalles: list[dict] = []

        def al_responder(resp):
            if "getDetalleCompra" in resp.url:
                try:
                    detalles.append(resp.json())
                except Exception:
                    pass

        page.on("response", al_responder)
        try:
            resumen = self._rcv_consultar(page, rut_objetivo, mes, anho)
            tipos = [
                (f.get("rsmnTipoDocInteger"), f.get("dcvNombreTipoDoc"), f.get("rsmnTotDoc"))
                for f in (resumen.get("data") or [])
                if f.get("rsmnTipoDocInteger") is not None
            ]

            documentos: list[dict] = []
            for indice, (tipo, nombre, total) in enumerate(tipos):
                # Una consulta fresca por tipo: al volver del detalle la SPA descarta los
                # resultados, así que reusar la misma pantalla no es confiable.
                if indice > 0:
                    self._rcv_consultar(page, rut_objetivo, mes, anho)

                enlace = page.locator(f"a[href='#detalle/{tipo}']")
                if enlace.count() == 0:
                    raise SIIRCVError(
                        f"El período {periodo} tiene {total} documentos de tipo {tipo} "
                        f"({nombre}) pero el RCV no ofrece enlace al detalle — "
                        "probablemente supera su límite de documentos por consulta."
                    )
                detalles.clear()
                with page.expect_response(lambda r: "getDetalleCompra" in r.url, timeout=60000):
                    enlace.first.click()
                page.wait_for_load_state("networkidle", timeout=60000)

                for payload in detalles:
                    documentos.extend(
                        self._documento_desde_rcv(d, tipo) for d in (payload.get("data") or [])
                    )

                # Aprovechar que estamos en el detalle para leer la razón social de la
                # empresa, que no se puede obtener de ninguna otra pantalla.
                if self.nombre_empresa is None and documentos:
                    self.nombre_empresa = self.get_nombre_empresa(page)

            return documentos
        finally:
            page.remove_listener("response", al_responder)
            if propia:
                context.close()
                browser.close()
                playwright.stop()

    def get_bhe(self, periodo: str) -> list[dict]:
        """Trae las boletas de honorarios electrónicas recibidas en un período (YYYY-MM).
        Módulo separado del RCV en el SII — ver DESIGN-SYSTEM/requisitos del proyecto."""
        raise NotImplementedError("Pendiente, mismo motivo que get_rcv().")

    def get_dte_xml(self, rut_emisor: str, tipo: str, folio: int) -> str:
        """Trae el XML completo de un documento (para el detalle de ítems).

        **Hallazgo (16-sep-2026): esto no parece obtenible desde el portal del SII.**
        Se recorrieron, con la sesión autenticada, todas las vistas del documento que
        el RCV ofrece, y ninguna entrega el detalle de ítems ni el XML:

        - `getDetalleCompra` (la tabla del RCV): cabeceras — emisor, folio, fecha, montos.
        - `verDTE(...)` (el enlace del folio, modal "Detalle Documento Electrónico"):
          emisor, receptor, tipo, folio, fechas, IVA, monto total, RUT firmante,
          identificador de envío, documentos referenciados y reparos. **Sin ítems.**
        - `modTipoCompra(...)` (`complementoscvui/#/detalleDocumento`): sirve para
          cambiar el tipo de compra; muestra montos, no ítems.
        - `registrorechazodtej6ui` (Registro de Aceptación o Reclamo): cabecera y
          eventos de acuse. Sin ítems, y solo cubre tipos 33, 34 y liquidación-factura.
        - "Exportar Csv" del detalle: las mismas columnas de la tabla.

        Esto es coherente con cómo funciona la facturación electrónica en Chile: el XML
        completo con el detalle de ítems lo intercambian **emisor y receptor directamente**
        (eso es justamente el "Intercambio de información"); el SII conserva en el RCV solo
        los datos de cabecera. Y el Intercambio no está habilitado para este certificado.

        Caminos posibles, a decidir con el dueño del proyecto (ver AGENTS.md, "Pendiente"):

        1. Habilitar "Intercambio de información" en el SII para este certificado.
        2. Leer los XML desde la casilla de intercambio del contribuyente (los emisores
           envían el DTE por correo); es el mecanismo estándar y no depende del portal.
        3. Aceptar trabajar a nivel de cabecera, si a Finnegans le alcanza con el total
           por documento sin desglose de ítems.
        4. Contratar un proveedor DTE que ya reciba y almacene los XML.

        Mientras no se resuelva eso, esto falla explícito en vez de devolver datos a medias.
        """
        raise NotImplementedError(
            "El detalle de ítems no está disponible en el portal del SII: el RCV solo "
            "expone cabeceras (se verificaron getDetalleCompra, verDTE, modTipoCompra, "
            "el registro de aceptación/reclamo y el export CSV). El XML completo se "
            "obtiene por intercambio entre emisor y receptor, no desde el portal. "
            "Ver el docstring de este método para las opciones."
        )
