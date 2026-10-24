"""Clasificación de descripciones de producto para WTCP: `POST /api/classify` (contrato §8, pedido por WTCP el 2026-10-22;
implementado el 2026-10-23 a pedido del usuario).

SCANIA no clasifica: es una pasarela hacia el clasificador HS6 + Schedule B (servicio aparte, `CLASIFICADOR_URL`, por
defecto http://localhost:8092; esquema en su /docs y /redoc), que usa el mismo Qwen3.6-27B (:8083) que la fase 2.

El contrato pide «una llamada, una respuesta» en menos de 60 s, pero el clasificador tarda de 20 s a varios minutos por
línea. El contrato ya prevé la salida: «un `ref` que falta en la respuesta queda sin código y se vuelve a pedir en el
próximo lote». Por eso:
- cada descripción (normalizada: espacios y mayúsculas) se clasifica UNA vez y su resultado queda en caché
  (`clasif_jobs/cache.json`); una descripción ya clasificada se responde al momento;
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
        with _LOCK:
            if k in _leer(CACHE_FILE):
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
                cache[k] = dict(resultado(it), ts=time.time())
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
    with _LOCK:
        cache = _leer(CACHE_FILE)
    if any(k not in cache for k in claves) and not salud():
        raise ClasificadorError(503, "classifier unavailable")  # §8.3: WTCP reintenta más tarde
    with _LOCK:
        cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
        nuevas = [k for k in claves if k not in cache and k not in _EN_COLA]
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
        if all(k in cache for k in claves) or time.time() >= fin:
            break
        time.sleep(min(2.0, max(0.0, fin - time.time())))
    salida, version = [], ""
    for ref, d in pares:
        c = cache.get(clave(d))
        if c is None:
            continue  # todavía en la cola o en el clasificador: se omite y WTCP la vuelve a pedir (§8.2)
        version = version or c.get("version_catalogo") or ""
        salida.append({"ref": ref, "estado": c["estado"], "sugerencias": c["sugerencias"]})
    if not version:
        try:
            version = (_pedir("GET", "/api/health", timeout=5).get("versions") or {}).get("scheduleb") or ""
        except ClasificadorError:
            version = ""
    return {"version_catalogo": version, "items": salida}
