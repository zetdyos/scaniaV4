"""Clasificación de descripciones de producto para WTCP: `POST /api/classify` (contrato §8, pedido por WTCP el 2026-10-22;
implementado el 2026-10-23 a pedido del usuario).

SCANIA no clasifica: es una pasarela hacia el clasificador HS6 + Schedule B (servicio aparte, `CLASIFICADOR_URL`, por
defecto http://localhost:8092; esquema en su /docs y /redoc), que usa el mismo Qwen3.6-27B (:8083) que la fase 2.

El contrato pide «una llamada, una respuesta» en menos de 60 s, pero el clasificador tarda de 20 s a varios minutos por
línea. El contrato ya prevé la salida: «un `ref` que falta en la respuesta queda sin código y se vuelve a pedir en el
próximo lote». Por eso:
- cada descripción (normalizada: espacios y mayúsculas) se clasifica UNA vez y su resultado queda en caché
  (`clasif_jobs/cache.json`); una descripción ya clasificada se responde al momento;
- las nuevas se encolan en el clasificador (`POST /api/batch`, `clasif_jobs/pendientes.json`) y se espera hasta
  `ESPERA` segundos; las que terminan en ese plazo van en la respuesta, las demás se omiten (WTCP las vuelve a pedir);
- una línea que el clasificador no pudo procesar (error) no se guarda: se reintenta en la próxima llamada, y tras
  `MAX_INTENTOS` errores queda `no_clasificado`.

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


def _encolar(nuevas, pend, cliente):
    """Encola en el clasificador las descripciones (clave -> texto) que no están ni en caché ni pendientes."""
    if not nuevas:
        return
    claves = list(nuevas)
    r = _pedir("POST", "/api/batch", json={"items": [{"id": str(i), "line": nuevas[k]} for i, k in enumerate(claves)],
                                           "client_ref": f"SCANIA {cliente}", "mode": "brief"})
    now = time.time()
    for i, k in enumerate(claves):
        prev = pend.get(k, {})
        pend[k] = {"batch": r["batch_id"], "item": str(i), "desde": now, "intentos": prev.get("intentos", 0)}


def _actualizar(claves, cache, pend):
    """Consulta los lotes pendientes de estas claves; guarda en caché lo terminado. Devuelve True si cambió algo."""
    cambios = False
    por_lote = {}
    for k in claves:
        if k in pend and k not in cache:
            por_lote.setdefault(pend[k]["batch"], []).append(k)
    for bid, ks in por_lote.items():
        try:
            b = _pedir("GET", f"/api/batch/{bid}", params={"include": "summary", "limit": 0})
        except ClasificadorError as e:
            if e.status == 404:  # lote perdido (clasificador reiniciado sin su base): se vuelve a encolar
                for k in ks:
                    pend.pop(k, None)
                cambios = True
                continue
            raise
        items = {str(it.get("id")): it for it in b.get("items") or []}
        for k in ks:
            it = items.get(pend[k]["item"])
            st = (it or {}).get("status")
            if st == "done":
                cache[k] = dict(resultado(it), ts=time.time())
                pend.pop(k)
                cambios = True
            elif it is None or st in ("error", "cancelled") or b.get("status") in ("cancelled", "failed"):
                intentos = pend[k].get("intentos", 0) + 1
                pend.pop(k)
                if intentos >= MAX_INTENTOS:
                    cache[k] = {"estado": "no_clasificado", "sugerencias": [], "version_catalogo": "",
                                "motivo": (it or {}).get("error") or "classifier error", "ts": time.time()}
                else:
                    pend[k] = {"batch": "", "item": "", "desde": 0, "intentos": intentos, "reintentar": True}
                cambios = True
    return cambios


def clasificar(items, cliente, espera=None):
    """§8: items [{ref, descripcion, …}] -> {version_catalogo, items:[{ref, estado, sugerencias}]} con las líneas ya
    clasificadas; las demás quedan encoladas y se omiten (WTCP las vuelve a pedir). ValueError si la entrada no vale."""
    pares = _validar(items)
    espera = ESPERA if espera is None else espera
    fin = time.time() + espera
    claves = {clave(d): d for _, d in pares}
    with _LOCK:
        cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
        nuevas = {k: d for k, d in claves.items() if k not in cache and (k not in pend or pend[k].get("reintentar"))}
        _encolar(nuevas, pend, cliente)
        _guardar(PEND_FILE, pend)
    while True:
        with _LOCK:
            cache, pend = _leer(CACHE_FILE), _leer(PEND_FILE)
            if _actualizar(claves, cache, pend):
                _guardar(CACHE_FILE, cache)
                _guardar(PEND_FILE, pend)
            reintentar = {k: claves[k] for k in claves if k in pend and pend[k].get("reintentar")}
            if reintentar:
                _encolar(reintentar, pend, cliente)
                _guardar(PEND_FILE, pend)
        if all(k in cache for k in claves) or time.time() >= fin:
            break
        time.sleep(min(3.0, max(0.0, fin - time.time())))
    salida, version = [], ""
    for ref, d in pares:
        c = cache.get(clave(d))
        if c is None:
            continue  # todavía en el clasificador: se omite y WTCP la vuelve a pedir (§8.2)
        version = version or c.get("version_catalogo") or ""
        salida.append({"ref": ref, "estado": c["estado"], "sugerencias": c["sugerencias"]})
    if not version:
        try:
            version = (_pedir("GET", "/api/health", timeout=5).get("versions") or {}).get("scheduleb") or ""
        except ClasificadorError:
            version = ""
    return {"version_catalogo": version, "items": salida}
