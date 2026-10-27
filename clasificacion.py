"""Clasificación de descripciones de producto para WTCP: `POST /api/classify` (contrato §8, pedido por WTCP el 2026-10-22;
implementado el 2026-10-23 a pedido del usuario).

SCANIA no clasifica: es una pasarela hacia el clasificador HS6 + Schedule B (servicio aparte, `CLASIFICADOR_URL`, por
defecto http://localhost:8092; esquema en su /docs y /redoc), que usa el mismo Qwen3.6-27B (:8083) que la fase 2.

El contrato pide «una llamada, una respuesta» en menos de 60 s, pero el clasificador tarda de 20 s a varios minutos por
línea. El contrato ya prevé la salida: «un `ref` que falta en la respuesta queda sin código y se vuelve a pedir en el
próximo lote». Por eso:
- cada descripción (normalizada: espacios y mayúsculas) se clasifica UNA vez por versión del clasificador y su
  resultado queda en caché (`clasif_jobs/cache.json`, con la versión en `vc`); una descripción ya clasificada con la
  versión actual se responde al momento; si el clasificador cambió de versión, se le vuelve a preguntar;
- las nuevas van a la COLA ÚNICA de demo_api (un trabajador para análisis y clasificaciones, los análisis primero;
  pedido del usuario 2026-10-24: nunca dos peticiones a la vez contra el 27B): cada una es una tarea `procesar` que
  toma el bloqueo de los modelos y manda UNA línea al clasificador (`POST /api/lines`); `clasificar` espera hasta
  `ESPERA` segundos y lo que no terminó se omite (WTCP lo vuelve a pedir; pendientes en `clasif_jobs/pendientes.json`);
- clasificador caído -> 503 (§8.3) sin contar intentos; una línea que el clasificador no pudo procesar (error o más de
  `TIEMPO_MAX_LINEA`) se reintenta en la próxima llamada, y tras `MAX_INTENTOS` queda `no_clasificado`.

Estados (§8.2): `sugerencia_segura` si el código de 10 dígitos supera el umbral del clasificador (`advance10` y sin
`needs_review`: WTCP lo pone sin revisión); `dudosa` si hay código pero no supera el umbral o el clasificador pide
confirmación; `no_clasificado` si no decidió un código de 10 dígitos (bloqueado, fuera de candidatos…). Nunca se
inventa un código: la sugerencia es siempre la del clasificador, con su `score10` como `confianza`."""
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CLASIF_DIR = HERE / "clasif_jobs"
CACHE_FILE = CLASIF_DIR / "cache.json"
PEND_FILE = CLASIF_DIR / "pendientes.json"
CLASIFICADOR_URL = (os.environ.get("CLASIFICADOR_URL") or "http://localhost:8092").rstrip("/")
MAX_ITEMS = 200
MAX_REF = 50
MAX_LARGO = 2000
ESPERA = float(os.environ.get("CLASIF_ESPERA", "40"))  # WTCP espera hasta 60 s por llamada (§8.3)
MAX_INTENTOS = 3
_LOCK = threading.Lock()


class ClasificadorError(Exception):
    """El clasificador no responde o rechazó la petición: `status` es el código HTTP que se devuelve a WTCP."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _pedir(metodo, ruta, **kw):
    try:
        r = requests.request(metodo, CLASIFICADOR_URL + ruta, timeout=kw.pop("timeout", 30), **kw)
    except requests.RequestException as e:
        raise ClasificadorError(503, f"classifier unavailable ({type(e).__name__})") from e
    if r.status_code == 404:
        raise ClasificadorError(404, "not found in the classifier")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        raise ClasificadorError(400 if r.status_code < 500 else 503, f"classifier rejected the request: {detail}")
    return r.json()


def salud():
    """True si el clasificador responde y sus servidores de modelos están arriba."""
    try:
        h = _pedir("GET", "/api/health", timeout=5)
    except ClasificadorError:
        return False
    return bool(h.get("embedding_server") and h.get("generation_server"))


# Versión del clasificador: cada respuesta guardada lleva la versión con la que se obtuvo (`vc`) y solo se sirve si el
# clasificador sigue en esa versión. Si cambia (2026-10-26: «revision_2»; WTCP recibió 1490 no_clasificado viejos al
# reclasificar), la próxima petición de esa descripción vuelve al clasificador. Firma = catálogos + revisión de su
# /api/health (`versions`); si el clasificador no responde se usa la última conocida, y si nunca se conoció se sirve la
# caché tal cual (no se bloquea a WTCP por no saber la versión).
CLAVES_VERSION = ("hs", "hts", "scheduleb", "rulings", "revision")
_VER = {"v": None, "t": 0.0}


def version_clasificador(fresca=False):
    """Firma de la versión actual del clasificador (60 s de caché), o la última conocida si no responde, o None."""
    if not fresca and _VER["v"] and time.time() - _VER["t"] < 60:
        return _VER["v"]
    try:
        vs = _pedir("GET", "/api/health", timeout=5).get("versions") or {}
    except ClasificadorError:
        return _VER["v"]
    _VER.update(v=firma(vs), t=time.time())
    return _VER["v"]


def firma(versions):
    return json.dumps({k: versions.get(k) for k in CLAVES_VERSION if versions.get(k)}, sort_keys=True, ensure_ascii=False)


def vigente(entrada, ver):
    """La respuesta guardada vale para la versión `ver` del clasificador (con `ver` desconocida, vale)."""
    return bool(entrada) and (ver is None or entrada.get("vc") == ver)


def clave(descripcion):
    return " ".join(descripcion.split()).lower()


def _leer(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _guardar(p, data):
    CLASIF_DIR.mkdir(exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def resultado(it):
    """Línea terminada del clasificador -> {estado, sugerencias, version_catalogo} (§8.2)."""
    code10 = re.sub(r"\D", "", it.get("code10") or it.get("code10_fmt") or "")
    meta = it.get("meta") or {}
    version = meta.get("scheduleb") or ""
    if len(code10) != 10:
        return {"estado": "no_clasificado", "sugerencias": [], "version_catalogo": version,
                "motivo": it.get("review_reason") or it.get("decision_status") or ""}
    segura = bool(it.get("advance10")) and not it.get("needs_review") and not it.get("requires_confirmation")
    score = it.get("score10")
    return {"estado": "sugerencia_segura" if segura else "dudosa",
            "sugerencias": [{"codigo": code10, "descripcion": it.get("code10_desc") or "",
                             "confianza": float(score) if score is not None else None,
                             "canal": "clasificador", "calibrado": False}],
            "version_catalogo": version, "motivo": "" if segura else (it.get("review_reason") or "")}


def _validar(items):
    if not items:
        raise ValueError("items is empty")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"more than {MAX_ITEMS} items")
    out = []
    for i, it in enumerate(items):
        ref = it.get("ref")
        d = it.get("descripcion")
        if not isinstance(ref, str) or not ref or len(ref) > MAX_REF:
            raise ValueError(f"items[{i}].ref must be text of 1 to {MAX_REF} characters")
        if not isinstance(d, str) or not d.strip():
            raise ValueError(f"items[{i}].descripcion is empty")
        if len(d) > MAX_LARGO:
            raise ValueError(f"items[{i}].descripcion is longer than {MAX_LARGO} characters")
        out.append((ref, d.strip()))
    return out


_EN_COLA: set = set()  # claves con una tarea viva en la cola única de demo_api (en memoria: tras un reinicio se re-encolan)
TIEMPO_MAX_LINEA = float(os.environ.get("CLASIF_TIEMPO_MAX", "1200"))  # 20 min: el clasificador puede atender antes su interfaz


def procesar(k, texto, cliente, modelos_lock):
    """Tarea de la cola única (un trabajador para análisis y clasificaciones): clasifica UNA descripción con el bloqueo
    de los modelos tomado (también espera al CLI) y guarda el resultado en la caché. Un error o un plazo vencido suma un
    intento: la próxima llamada de WTCP la vuelve a encolar; tras MAX_INTENTOS queda no_clasificado."""
    try:
        ver = version_clasificador()
        with _LOCK:
            if vigente(_leer(CACHE_FILE).get(k), ver):
                return
        with modelos_lock():
            r = _pedir("POST", "/api/lines", json={"line": texto, "client_ref": f"SCANIA {cliente}", "mode": "brief"})
            url = r.get("status_url") or f"{CLASIFICADOR_URL}/api/lines/{r['line_id']}"
            ruta = url[len(CLASIFICADOR_URL):] if url.startswith(CLASIFICADOR_URL) else f"/api/lines/{r['line_id']}"
            fin = time.time() + TIEMPO_MAX_LINEA
            it = {}
            while time.time() < fin:
                it = _pedir("GET", ruta, params={"include": "summary"})
                if it.get("status") in ("done", "error", "cancelled"):
                    break
                time.sleep(2)
        with _LOCK:
            cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
            if it.get("status") == "done":
                cache[k] = dict(resultado(it), ts=time.time(), vc=version_clasificador(fresca=True))
                pend.pop(k, None)
            else:
                _fallo(k, pend, cache, it.get("error") or f"classifier status {it.get('status') or 'timeout'}")
            _guardar(CACHE_FILE, cache)
            _guardar(PEND_FILE, pend)
    except ClasificadorError as e:
        if e.status == 503:  # clasificador caído: no es culpa de la línea, se reintenta en la próxima llamada sin contar
            return
        with _LOCK:
            cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
            _fallo(k, pend, cache, e.detail)
            _guardar(CACHE_FILE, cache)
            _guardar(PEND_FILE, pend)
    finally:
        _EN_COLA.discard(k)


def _fallo(k, pend, cache, motivo):
    p = pend.setdefault(k, {})
    p["intentos"] = p.get("intentos", 0) + 1
    p["ultimo_error"] = motivo
    if p["intentos"] >= MAX_INTENTOS:
        cache[k] = {"estado": "no_clasificado", "sugerencias": [], "version_catalogo": "", "motivo": motivo, "ts": time.time()}
        pend.pop(k, None)


def clasificar(items, cliente, encolar, espera=None):
    """§8: items [{ref, descripcion, …}] -> {version_catalogo, items:[{ref, estado, sugerencias}]} con las líneas ya
    clasificadas. Las nuevas se ponen en la cola única (`encolar(clave, texto, cliente)`, prioridad por debajo de los
    análisis) y se espera hasta `espera` s; las que no terminan se omiten y WTCP las vuelve a pedir (§8.2).
    ValueError si la entrada no vale."""
    pares = _validar(items)
    espera = ESPERA if espera is None else espera
    fin = time.time() + espera
    claves = {clave(d): d for _, d in pares}
    ver = version_clasificador()

    def lista(c):  # respuesta guardada y de la versión actual del clasificador
        return vigente(c, ver)

    with _LOCK:
        cache = _leer(CACHE_FILE)
    if any(not lista(cache.get(k)) for k in claves) and not salud():
        raise ClasificadorError(503, "classifier unavailable")  # §8.3: WTCP reintenta más tarde
    with _LOCK:
        cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
        nuevas = [k for k in claves if not lista(cache.get(k)) and k not in _EN_COLA]
        for k in nuevas:
            _EN_COLA.add(k)
            pend.setdefault(k, {})["texto"] = claves[k]
        if nuevas:
            _guardar(PEND_FILE, pend)
    for k in nuevas:
        encolar(k, claves[k], cliente)
    while True:
        with _LOCK:
            cache = _leer(CACHE_FILE)
        if all(lista(cache.get(k)) for k in claves) or time.time() >= fin:
            break
        time.sleep(min(2.0, max(0.0, fin - time.time())))
    salida, version = [], ""
    for ref, d in pares:
        c = cache.get(clave(d))
        if not lista(c):
            continue  # todavía en la cola o en el clasificador: se omite y WTCP la vuelve a pedir (§8.2)
        version = version or c.get("version_catalogo") or ""
        salida.append({"ref": ref, "estado": c["estado"], "sugerencias": c["sugerencias"]})
    if not version:
        try:
            version = (_pedir("GET", "/api/health", timeout=5).get("versions") or {}).get("scheduleb") or ""
        except ClasificadorError:
            version = ""
    return {"version_catalogo": version, "items": salida}


# ----------------------------------------------------------------------------- correcciones de HS6 (POST /api/classify/feedback)
# SCANIA es intermediario: reenvía {line, hs6} al clasificador (POST /api/validate) y deja de responder desde su caché esa
# descripción y las que no salieron como sugerencia_segura, para que la próxima petición vuelva al clasificador. No
# guarda quién corrigió ni mide nada: eso es de WTCP y del clasificador (usuario, 2026-10-24). Cada corrección queda en
# disco (`correcciones.json`) hasta que el clasificador la acepta: no se pierde aunque el clasificador esté caído.
CORR_FILE = CLASIF_DIR / "correcciones.json"
HS6_RE = re.compile(r"^\d{6}$")
_CORR_EN_COLA = [False]


def _validar_correcciones(items):
    if not items:
        raise ValueError("items is empty")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"more than {MAX_ITEMS} items")
    out = []
    for i, it in enumerate(items):
        ref, d = it.get("ref"), it.get("descripcion")
        hs6 = re.sub(r"[.\s]", "", str(it.get("hs6") or ""))
        if not isinstance(ref, str) or not ref or len(ref) > MAX_REF:
            raise ValueError(f"items[{i}].ref must be text of 1 to {MAX_REF} characters")
        if not isinstance(d, str) or not d.strip():
            raise ValueError(f"items[{i}].descripcion is empty")
        if not HS6_RE.match(hs6):
            raise ValueError(f"items[{i}].hs6 must have 6 digits")
        out.append((ref, d.strip(), hs6))
    return out


def corregir(items, encolar):
    """Registra las correcciones, limpia la caché y pone el reenvío en la cola única. ValueError si la entrada no vale."""
    filas = _validar_correcciones(items)
    with _LOCK:
        corr, cache = _leer(CORR_FILE), _leer(CACHE_FILE)
        for _ref, d, hs6 in filas:
            corr[f"{clave(d)}|{hs6}"] = {"line": d, "hs6": hs6}
            cache.pop(clave(d), None)
        for k in [k for k, v in cache.items() if v.get("estado") != "sugerencia_segura"]:
            cache.pop(k)  # la próxima petición vuelve al clasificador, que ya conoce la corrección
        _guardar(CORR_FILE, corr)
        _guardar(CACHE_FILE, cache)
    pendientes_correcciones(encolar)
    return {"items": [{"ref": ref, "estado": "registrado"} for ref, _d, _h in filas]}


def pendientes_correcciones(encolar):
    """Pone en la cola única el reenvío de las correcciones pendientes, si hay y no está ya en cola."""
    with _LOCK:
        hay = bool(_leer(CORR_FILE))
        if not hay or _CORR_EN_COLA[0]:
            return
        _CORR_EN_COLA[0] = True
    encolar()


def enviar_correcciones(modelos_lock):
    """Tarea de la cola única: manda al clasificador las correcciones pendientes. Si el clasificador no responde, se
    quedan para el próximo intento; si las rechaza (400: HS6 que no existe), se descartan con un aviso en el registro."""
    try:
        with _LOCK:
            corr = _leer(CORR_FILE)
        hechas = []
        with modelos_lock():
            for k, c in corr.items():
                try:
                    _pedir("POST", "/api/validate", json={"line": c["line"], "hs6": c["hs6"]})
                    hechas.append(k)
                except ClasificadorError as e:
                    if e.status == 503:
                        break  # caído: el resto se reintenta más tarde
                    print(f"clasificación: corrección rechazada por el clasificador ({c['hs6']} «{c['line'][:40]}»): {e.detail}", flush=True)
                    hechas.append(k)
        with _LOCK:
            corr = _leer(CORR_FILE)
            for k in hechas:
                corr.pop(k, None)
            _guardar(CORR_FILE, corr)
    finally:
        _CORR_EN_COLA[0] = False
