"""Clasificación de descripciones de producto para WTCP: `POST /api/classify` (contrato §8) y correcciones de HS6
(`POST /api/classify/feedback`).

En clasificación SCANIA es SOLO UN INTERMEDIARIO (usuario, 2026-10-27: «NO caché en cuanto a clasificación, eres solo un
proxy»): no guarda respuestas, no decide si una respuesta vieja vale, no clasifica. Toda petición de WTCP va al
clasificador HS6 + Schedule B (servicio aparte, `CLASIFICADOR_URL`, por defecto http://localhost:8092; esquema en su /docs
y /redoc), que usa el mismo Qwen3.6-27B (:8083) que la fase 2.

El contrato pide «una llamada, una respuesta» en menos de 60 s y el clasificador tarda de 20 s a minutos por línea. El
contrato ya prevé la salida (§8.2): «un `ref` que falta en la respuesta queda sin código y se vuelve a pedir en el próximo
lote». Por eso:
- cada descripción pedida va a la COLA ÚNICA de demo_api (un trabajador para análisis y clasificaciones, los análisis
  primero: nunca dos peticiones a la vez contra el 27B, pedido del usuario 2026-10-24); la tarea `procesar` toma el
  bloqueo de los modelos y manda UNA línea al clasificador (`POST /api/lines`);
- SCANIA solo anota qué línea del clasificador corresponde a cada descripción mientras está EN CURSO
  (`clasif_jobs/en_curso.json`, por cliente: lo que pidió un WTCP se entrega solo a ese WTCP; al clasificador va solo la
  descripción, nunca quién la pidió): si WTCP la vuelve a pedir antes de que termine, no se manda otra vez; el resultado lo
  guarda el clasificador y SCANIA lo lee de allí (`GET /api/lines/{id}`);
- `clasificar` espera hasta `ESPERA` s; lo terminado se entrega y se OLVIDA (la próxima petición de esa descripción es
  una clasificación nueva); lo que no terminó se omite y WTCP lo vuelve a pedir;
- clasificador caído -> 503 (§8.3); una línea que el clasificador no pudo procesar se manda de nuevo en la próxima
  petición, y tras `MAX_INTENTOS` errores se entrega como `no_clasificado`.

Estados (§8.2): `sugerencia_segura` si el clasificador supera su umbral para el código de 10 dígitos (`advance10` y sin
`needs_review` ni `requires_confirmation`: WTCP lo pone sin revisión); `dudosa` si hay código pero no supera el umbral;
`no_clasificado` si no decidió un código de 10 dígitos. La sugerencia es siempre la del clasificador, con su `score10`."""
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CLASIF_DIR = HERE / "clasif_jobs"
EN_CURSO_FILE = CLASIF_DIR / "en_curso.json"
CLASIFICADOR_URL = (os.environ.get("CLASIFICADOR_URL") or "http://localhost:8092").rstrip("/")
MAX_ITEMS = 200
MAX_REF = 50
MAX_LARGO = 2000
ESPERA = float(os.environ.get("CLASIF_ESPERA", "40"))  # WTCP espera hasta 60 s por llamada (§8.3)
MAX_INTENTOS = 3
TIEMPO_MAX_LINEA = float(os.environ.get("CLASIF_TIEMPO_MAX", "1200"))  # 20 min: el clasificador puede atender antes su interfaz
OLVIDAR_DIAS = 7  # una línea en curso que WTCP no vuelve a pedir en una semana se olvida
_LOCK = threading.Lock()
_EN_COLA: set = set()  # claves con una tarea viva en la cola única (en memoria: tras un reinicio se vuelven a encolar)


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


def _salud():
    """/api/health del clasificador si responde y sus servidores de modelos están arriba; None si no."""
    try:
        h = _pedir("GET", "/api/health", timeout=5)
    except ClasificadorError:
        return None
    return h if h.get("embedding_server") and h.get("generation_server") else None


def salud():
    return _salud() is not None


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
    version = (it.get("meta") or {}).get("scheduleb") or ""
    if len(code10) != 10:
        return {"estado": "no_clasificado", "sugerencias": [], "version_catalogo": version}
    segura = bool(it.get("advance10")) and not it.get("needs_review") and not it.get("requires_confirmation")
    score = it.get("score10")
    return {"estado": "sugerencia_segura" if segura else "dudosa",
            "sugerencias": [{"codigo": code10, "descripcion": it.get("code10_desc") or "",
                             "confianza": float(score) if score is not None else None,
                             "canal": "clasificador", "calibrado": False}],
            "version_catalogo": version}


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


def clave_cliente(cliente, descripcion):
    """Clave de una descripción EN CURSO: por cliente (cada WTCP recibe solo lo que pidió; usuario, 2026-10-28)."""
    return f"{cliente}|{clave(descripcion)}"


def procesar(k, texto, modelos_lock):
    """Tarea de la cola única: manda UNA descripción al clasificador con el bloqueo de los modelos tomado (también espera
    al CLI) y espera a que la termine, para no tener dos líneas a la vez contra el 27B. No guarda el resultado: lo guarda
    el clasificador y `clasificar` lo lee por el número de línea. Clasificador caído: la línea queda sin enviar y se
    vuelve a encolar en la próxima petición."""
    try:
        with modelos_lock():
            with _LOCK:
                en = _leer(EN_CURSO_FILE)
                if k not in en or en[k].get("line_id"):
                    return  # ya entregada u otra tarea ya la mandó
            # Solo la descripción: el clasificador no sabe de clientes (datos mínimos entre servicios).
            r = _pedir("POST", "/api/lines", json={"line": texto, "mode": "brief"})
            with _LOCK:
                en = _leer(EN_CURSO_FILE)
                if k in en:
                    en[k]["line_id"] = r["line_id"]
                    _guardar(EN_CURSO_FILE, en)
            fin = time.time() + TIEMPO_MAX_LINEA
            while time.time() < fin:
                it = _pedir("GET", f"/api/lines/{r['line_id']}", params={"include": "summary"})
                if it.get("status") in ("done", "error", "cancelled"):
                    break
                time.sleep(2)
    except ClasificadorError:
        pass  # sin line_id: se vuelve a encolar en la próxima petición de WTCP
    finally:
        _EN_COLA.discard(k)


def clasificar(items, cliente, encolar, espera=None):
    """§8: items [{ref, descripcion, …}] -> {version_catalogo, items:[{ref, estado, sugerencias}]} con las líneas que el
    clasificador terminó. Lo que no terminó en `espera` s se omite y WTCP lo vuelve a pedir (§8.2). ValueError si la
    entrada no vale; ClasificadorError(503) si el clasificador no está disponible."""
    pares = _validar(items)
    espera = ESPERA if espera is None else espera
    fin = time.time() + espera
    claves = {clave_cliente(cliente, d): d for _, d in pares}
    h = _salud()
    if h is None:
        raise ClasificadorError(503, "classifier unavailable")  # §8.3: WTCP reintenta más tarde
    encolar_ya = []
    with _LOCK:
        en = _leer(EN_CURSO_FILE)
        viejo = time.time() - OLVIDAR_DIAS * 86400
        for k in [k for k, v in en.items() if v.get("desde", 0) < viejo and k not in claves]:
            en.pop(k)
        for k, d in claves.items():
            if k not in en and clave(d) in en and "|" not in clave(d):
                en[k] = en.pop(clave(d))  # en curso desde antes de las claves por cliente (un solo WTCP): pasa a quien la pide
            if k not in en:
                en[k] = {"texto": d, "line_id": None, "desde": time.time(), "errores": 0}
            if not en[k].get("line_id") and k not in _EN_COLA:
                _EN_COLA.add(k)
                encolar_ya.append(k)
        _guardar(EN_CURSO_FILE, en)
    for k in encolar_ya:
        encolar(k, claves[k])
    listos = {}
    while True:
        with _LOCK:
            en = _leer(EN_CURSO_FILE)
        for k in claves:
            lid = (en.get(k) or {}).get("line_id")
            if k in listos or not lid:
                continue
            try:
                it = _pedir("GET", f"/api/lines/{lid}", params={"include": "summary"})
            except ClasificadorError as e:
                if e.status != 404:
                    continue
                it = {"status": "error", "error": "line lost by the classifier"}
            if it.get("status") == "done":
                listos[k] = resultado(it)
            elif it.get("status") in ("error", "cancelled"):
                with _LOCK:
                    en = _leer(EN_CURSO_FILE)
                    if k in en:
                        en[k]["errores"] = en[k].get("errores", 0) + 1
                        en[k]["line_id"] = None  # se vuelve a mandar en la próxima petición
                        if en[k]["errores"] >= MAX_INTENTOS:
                            listos[k] = {"estado": "no_clasificado", "sugerencias": [], "version_catalogo": ""}
                        _guardar(EN_CURSO_FILE, en)
        if len(listos) == len(claves) or time.time() >= fin:
            break
        time.sleep(min(3.0, max(0.0, fin - time.time())))
    with _LOCK:  # lo entregado se olvida: la próxima petición de esa descripción es una clasificación nueva
        en = _leer(EN_CURSO_FILE)
        for k in listos:
            en.pop(k, None)
        _guardar(EN_CURSO_FILE, en)
    salida = [{"ref": ref, "estado": listos[k]["estado"], "sugerencias": listos[k]["sugerencias"]}
              for ref, d in pares if (k := clave_cliente(cliente, d)) in listos]
    version = next((v["version_catalogo"] for v in listos.values() if v.get("version_catalogo")), "") \
        or (h.get("versions") or {}).get("scheduleb") or ""
    return {"version_catalogo": version, "items": salida}


# ----------------------------------------------------------------------------- correcciones de HS6 (POST /api/classify/feedback)
# SCANIA es intermediario: reenvía {line, hs6} al clasificador (POST /api/validate) y nada más. No guarda quién corrigió
# ni mide nada: eso es de WTCP y del clasificador (usuario, 2026-10-24). Cada corrección queda en disco
# (`correcciones.json`) hasta que el clasificador la acepta: no se pierde aunque el clasificador esté caído.
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
    """Registra las correcciones y pone su reenvío al clasificador en la cola única. ValueError si la entrada no vale."""
    filas = _validar_correcciones(items)
    with _LOCK:
        corr = _leer(CORR_FILE)
        for _ref, d, hs6 in filas:
            corr[f"{clave(d)}|{hs6}"] = {"line": d, "hs6": hs6}
        _guardar(CORR_FILE, corr)
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
