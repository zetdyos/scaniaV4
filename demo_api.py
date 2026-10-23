"""
API de OCR de facturas (FastAPI) y demo para evaluarlo visualmente. Contrato con el cliente WTCP:
C:/Share/CONTRATO-WTCP.md (carpeta compartida con WTCP).

- GET  /                      interfaz (solo desde 127.0.0.1): subir archivo, barra de progreso,
                              tarjetas de facturas a la izquierda y el PDF a la derecha.
- POST /api/jobs              sube un PDF o una imagen (PNG, JPG/JPEG, BMP) y lo encola (un
                              trabajo a la vez: los llama-server remotos tienen un solo slot).
- GET  /api/jobs              lista de trabajos (los resultados persisten en demo_jobs/).
- GET  /api/jobs/{id}         estado, progreso y registro; con ?full=1 incluye el JSON y el OCR.
- GET  /api/jobs/{id}/pdf     el PDF subido (para el visor).
- GET  /api/jobs/{id}/result.json  descarga del JSON de facturas.
- WS   /ws/jobs/{id}         progreso en tiempo real (JSON por mensaje; cierra al terminar).
- GET  /api/health           estado de los tres llama-server del host de modelos.
- GET  /docs  y  /redoc      documentación OpenAPI (Swagger UI y ReDoc).

Acceso: todas las rutas /api/... y el WebSocket exigen un API-Token en la cabecera X-API-Key
(401 si falta o no es válido). Los tokens viven en api_keys.json ({"nombre": "token", ...}, fuera
de git; otra ruta con SCANIA_API_KEYS_FILE). Desde 127.0.0.1 no se pide token: es la propia demo.
/docs, /redoc y /openapi.json son públicos (solo esquemas, sin datos).

Puerto por defecto 8091 (variable DEMO_PORT). Arranque:
    .venv/bin/python demo_api.py                                   # 0.0.0.0:8091
    .venv/bin/uvicorn demo_api:app --host 0.0.0.0 --port 8091      # equivalente
OCR_MODELS_HOST (opcional, por defecto 192.168.1.194) indica dónde están los llama-server.
"""

import asyncio
import hmac
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, Optional

import fitz  # PyMuPDF
import markdown as mdlib
import requests
from fastapi import FastAPI, File, HTTPException, Request, Security, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import facturas_json as f2
import ocr_pipeline as p1

HERE = Path(__file__).resolve().parent
JOBS_DIR = HERE / "demo_jobs"
JOBS_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------------- acceso (API-Token)

API_KEYS_FILE = Path(os.environ.get("SCANIA_API_KEYS_FILE") or HERE / "api_keys.json")
API_KEYS: dict[str, str] = json.loads(API_KEYS_FILE.read_text(encoding="utf-8")) if API_KEYS_FILE.exists() else {}
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API-Token del cliente")


def is_local(host: Optional[str]) -> bool:
    return host in LOCAL_HOSTS


def key_name(key: Optional[str]) -> Optional[str]:
    """Nombre del token (p. ej. 'wtcp-produccion') o None. Comparación en tiempo constante."""
    if not key:
        return None
    return next((name for name, value in API_KEYS.items() if hmac.compare_digest(key, value)), None)


def require_key(request: Request, key: Optional[str] = Security(api_key_header)) -> str:
    """Dependencia de todas las rutas /api: devuelve quién llama ('local' para la demo)."""
    if is_local(request.client.host if request.client else None):
        return "local"
    name = key_name(key)
    if not name:
        raise HTTPException(401, "missing or invalid API token (X-API-Key header)")
    return name


# ----------------------------------------------------------------------------- entrada

INPUT_TYPES = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".bmp": "bmp"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
IMAGE_PAGE_WIDTH_PT = 612  # 8.5 in: la fase 1 está ajustada para escaneos Letter (fit_dpi)


def image_to_pdf(data: bytes, filetype: str) -> bytes:
    """Imagen -> PDF de una página de 8.5 in de ancho con la misma proporción. Así la fase 1 la
    trata como un escaneo Letter: fit_dpi elige el dpi para no pasar de las 4096 fichas de imagen
    de llama.cpp. Convertida tal cual, una foto sin metadatos de DPI mediría pixeles = puntos y el
    piso de 72 dpi de fit_dpi la dejaría muy por encima del límite (reescalado bilineal)."""
    with fitz.open(stream=data, filetype=filetype) as img:
        w, h = img[0].rect.width, img[0].rect.height
    out = fitz.open()
    page = out.new_page(width=IMAGE_PAGE_WIDTH_PT, height=IMAGE_PAGE_WIDTH_PT * h / w)
    page.insert_image(page.rect, stream=data)
    pdf = out.tobytes()
    out.close()
    return pdf


TAGS = [
    {"name": "trabajos", "description": "Subida de archivos y consulta de resultados. Un trabajo a la vez: los llama-server tienen un solo slot."},
    {"name": "archivos", "description": "PDF procesado y JSON de facturas de un trabajo."},
    {"name": "sistema", "description": "Salud de los servidores de modelos."},
]
app = FastAPI(
    title="PDF Reder · OCR de facturas",
    version="1.0.0",
    description=(
        "API de OCR de facturas. Contrato con WTCP: `CONTRATO-WTCP.md` de la carpeta compartida.\n\n"
        "**Acceso:** todas las rutas `/api/...` y el WebSocket exigen el API-Token en la cabecera `X-API-Key` "
        "(botón *Authorize*); sin token o con uno inválido responden `401`.\n\n"
        "**Flujo:** `POST /api/jobs` sube un PDF o una imagen (PNG, JPG/JPEG, BMP) y devuelve un `id`; el progreso llega por WebSocket en "
        "`/ws/jobs/{id}` (un JSON por mensaje con `state`, `progress`, `step`, `log`; se cierra al terminar) "
        "o consultando `GET /api/jobs/{id}`; con `?full=1` se obtiene el array de facturas y el OCR en HTML.\n\n"
        "**Fases:** 1) OCR con GLM-OCR (página completa + regiones + verificación + rescate con Qwen2.5-VL); "
        "2) división en facturas y JSON estándar (Qwen3.6-27B para líneas cuando no hay bloques de envío).\n\n"
        f"**Host de modelos:** `{p1.MODELS_HOST}` (variable de entorno `OCR_MODELS_HOST`)."
    ),
    openapi_tags=TAGS, docs_url="/docs", redoc_url="/redoc",
)


class RunningNow(BaseModel):
    pdf: str
    progress: int
    step: str = ""


class JobStatus(BaseModel):
    id: str
    pdf: str
    state: Literal["queued", "running", "done", "error"]
    progress: int = Field(ge=0, le=100)
    step: str = ""
    created: float
    started: Optional[float] = None
    seconds: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[Literal["UNREADABLE", "INTERNAL"]] = Field(
        None, description="solo con state=error. UNREADABLE: el problema es el archivo. INTERNAL: el problema es del servidor (reintentar)")
    n_pages: Optional[int] = None
    n_invoices: Optional[int] = None
    n_lines: Optional[int] = None
    n_review: Optional[int] = Field(None, description="facturas con motivo_revision")
    n_verified: Optional[int] = Field(None, description="líneas verificadas aritméticamente")
    queue_position: Optional[int] = None
    queue_total: Optional[int] = None
    running_now: Optional[RunningNow] = None


class JobDetail(JobStatus):
    log: list[str] = []
    result: Optional[list[dict[str, Any]]] = Field(None, description="array de facturas (solo con ?full=1 y state=done)")
    ocr_html: Optional[str] = Field(None, description="resumen OCR de la fase 1 en HTML (solo con ?full=1)")


class JobCreated(BaseModel):
    id: str


class ModelHealth(BaseModel):
    name: str
    url: str
    ok: bool
    model: Optional[str] = None


class Health(BaseModel):
    models_host: str
    models: list[ModelHealth]
    jobs_running: int
    jobs_queued: int
executor = ThreadPoolExecutor(max_workers=1)
jobs: dict = {}
lock = threading.Lock()


def save_meta(job):
    (JOBS_DIR / job["id"] / "meta.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


def load_jobs():
    for d in sorted(JOBS_DIR.iterdir()):
        m = d / "meta.json"
        if m.exists():
            job = json.loads(m.read_text(encoding="utf-8"))
            if job.get("state") in ("queued", "running"):
                job.update(state="error", error="interrupted by a server restart", step="Error")
            if job["state"] == "error" and not job.get("error_code"):
                job["error_code"] = "INTERNAL"  # trabajos anteriores a error_code o interrumpidos: se reintentan
            jobs[job["id"]] = job


load_jobs()


def set_step(job, pct, step):
    job["progress"] = min(99, max(job.get("progress", 0), int(pct)))  # 100 solo al terminar (el corte por pedidos puede dar más facturas que las detectadas)
    job["step"] = step


def fingerprints_of(job_id):
    """Huellas de pedido de un trabajo terminado (f2.order_fingerprints), guardadas en huellas.json la primera vez."""
    d = JOBS_DIR / job_id
    h = d / "huellas.json"
    if h.exists():
        return json.loads(h.read_text(encoding="utf-8"))
    if not ((d / "result.json").exists() and (d / "ocr.md").exists()):
        return []
    fps = f2.order_fingerprints(json.loads((d / "result.json").read_text(encoding="utf-8")),
                                (d / "ocr.md").read_text(encoding="utf-8"))
    h.write_text(json.dumps(fps, ensure_ascii=False), encoding="utf-8")
    return fps


def with_refs(job_id, fps):
    """Huellas guardadas antes de 2026-10-22 no traen la referencia: se completa desde result.json (misma factura por id)."""
    if all("ref" in f for f in fps):
        return fps
    try:
        refs = {i["id"]: (i.get("referencia") or "").upper()
                for i in json.loads((JOBS_DIR / job_id / "result.json").read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError):
        refs = {}
    return [dict(f, ref=f.get("ref", refs.get(f["id"], ""))) for f in fps]


def mark_cross_document_duplicates(job, out, markdown, log):
    """El mismo pedido en OTRO documento ya procesado (mismo total, mismos artículos y un identificador impreso
    en común): se declararía dos veces. Va a revisión, con el documento anterior en el motivo. El reenvío del
    mismo archivo (mismo nombre) no cuenta. Se compara con el último trabajo terminado de cada documento."""
    mine = f2.order_fingerprints(out, markdown)
    latest = {}
    for j in list(jobs.values()):
        if j["id"] != job["id"] and j.get("state") == "done" and j.get("pdf") != job["pdf"]:
            latest[j["pdf"]] = j["id"]
    for pdf, jid in latest.items():
        try:
            theirs = with_refs(jid, fingerprints_of(jid))
        except (OSError, ValueError, KeyError):
            continue
        for a, doc in zip(mine, out):
            b = next((b for b in theirs if f2.same_order(a, b)), None)
            if b is not None:
                log(f"  factura {doc['id']}: el mismo pedido ya vino en {pdf} (factura {b['id']})")
                f2.mark_review(doc, f"same order as invoice {b['id']} of document {pdf} (identical items and total; "
                                    "possible duplicate declaration)")
                continue
            # Misma referencia impresa y algún artículo en común aunque cambien total o artículos (versión ampliada o
            # recortada del mismo papel): la mercancía en común se declararía dos veces.
            b = next((b for b in theirs if f2.partial_same_order(a, b)), None)
            if b is not None:
                log(f"  factura {doc['id']}: misma referencia {a['ref']} y artículos en común con {pdf} (factura {b['id']})")
                f2.mark_review(doc, f"same order number {a['ref']} as invoice {b['id']} of document {pdf}, with item(s) "
                                    "in common (possible partial duplicate declaration)")
    return mine


def process(job_id):
    job = jobs[job_id]
    d = JOBS_DIR / job_id
    job.update(state="running", started=time.time())
    set_step(job, 2, "Iniciando")

    def log(m):
        job["log"].append(m)
        mm = re.search(r"página completa (\d+)/(\d+)", m)
        if mm:
            set_step(job, 5 + 55 * int(mm.group(1)) / int(mm.group(2)), f"OCR de página {mm.group(1)} de {mm.group(2)} (GLM-OCR, página completa)")
        elif m.startswith("Lectura B"):
            set_step(job, 62, "Detección de regiones y tablas (PP-DocLayoutV3 + Table Recognition)")
        elif m.startswith("Lectura C"):
            set_step(job, 74, "Rescate de precios con Qwen2.5-VL")
        elif "factura(s) detectada(s)" in m:
            job["n_inv"] = int(re.search(r"(\d+) factura", m).group(1))
            set_step(job, 80, f"{job['n_inv']} factura(s) detectada(s)")
        elif m.strip().startswith("-- factura"):
            k = int(re.search(r"-- factura (\d+)", m).group(1))
            n = max(1, job.get("n_inv", 1))
            set_step(job, 80 + 18 * k / n, f"Estructurando factura {k + 1} de {n}")

    try:
        with f2.modelos_lock(log, on_wait=lambda: set_step(job, 2, "Esperando a los modelos (otro proceso los está usando)")):
            res = p1.run(str(d / job["pdf"]), log=log)
            out = f2.build_all(res, log=log)
        fps = mark_cross_document_duplicates(job, out, res["markdown"], log)
        (d / "huellas.json").write_text(json.dumps(fps, ensure_ascii=False), encoding="utf-8")
        (d / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        # Respuestas crudas de los modelos y regiones de la fase 1: para medir cambios y calibrar sin volver a llamarlos.
        (d / "modelos.json").write_text(json.dumps(dict(f2.MODEL_RAW, fase1_regiones=res.get("region_pages")),
                                                   ensure_ascii=False, default=str), encoding="utf-8")
        (d / "ocr.md").write_text(res["markdown"], encoding="utf-8")
        (d / "ocr.html").write_text(mdlib.markdown(res["markdown"], extensions=["tables"]), encoding="utf-8")
        job.update(state="done", progress=100, step="Listo", n_pages=res["n_pages"], n_invoices=len(out),
                   n_lines=sum(len(i["lineas"]) for i in out), n_review=sum(1 for i in out if i["motivo_revision"]),
                   n_verified=sum(1 for i in out for l in i["lineas"] if l["ocr"]["verificado"]))
    except fitz.FileDataError as e:  # el archivo no se puede leer (la subida ya lo valida; por si acaso)
        job.update(state="error", error=f"unreadable file: {e}", error_code="UNREADABLE", step="Error")
    except Exception as e:  # noqa: BLE001  modelos caídos, red, errores internos: el cliente reintenta
        job.update(state="error", error=f"{type(e).__name__}: {e}", error_code="INTERNAL", step="Error")
    job["seconds"] = round(time.time() - job["started"], 1)
    save_meta(job)


def public(j):
    return {k: v for k, v in j.items() if k != "log"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    if not is_local(request.client.host if request.client else None):
        raise HTTPException(404, "Not Found")  # la demo solo se abre desde la propia máquina
    return HTML


AUTH = [Security(require_key)]


@app.get("/api/health", response_model=Health, tags=["sistema"], summary="Estado de los servidores de modelos", dependencies=AUTH)
def health():
    out = []
    for name, url in (("GLM-OCR", p1.DEFAULT_SERVER), ("Qwen3.6-27B", f2.QWEN27_SERVER), ("Qwen2.5-VL", f2.VL_SERVER)):
        ok, model = False, None
        try:
            ok = requests.get(f"{url}/health", timeout=3).status_code == 200
            if ok:
                model = Path(requests.get(f"{url}/props", timeout=3).json().get("model_path", "")).name or None
        except requests.RequestException:
            pass
        out.append(ModelHealth(name=name, url=url, ok=ok, model=model))
    return Health(models_host=p1.MODELS_HOST, models=out,
                  jobs_running=sum(1 for j in jobs.values() if j["state"] == "running"),
                  jobs_queued=sum(1 for j in jobs.values() if j["state"] == "queued"))


@app.post("/api/jobs", response_model=JobCreated, status_code=201, tags=["trabajos"], summary="Subir un archivo y encolarlo",
          responses={400: {"description": "archivo no aceptado"}, 413: {"description": "archivo demasiado grande"}})
async def create_job(file: UploadFile = File(..., description="PDF, PNG, JPG/JPEG o BMP (nombre en ASCII)"),
                     client: str = Security(require_key)):
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in INPUT_TYPES:
        raise HTTPException(400, "unsupported file type: send a PDF, PNG, JPG/JPEG or BMP file")
    if not filename.isascii():
        raise HTTPException(400, "the file name must be ASCII")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    # Validar ANTES de crear el trabajo: PyMuPDF abre de forma perezosa y un archivo dañado puede
    # fallar recién al cargar la página, así que se carga la primera (y la imagen se convierte aquí).
    try:
        with fitz.open(stream=data, filetype=INPUT_TYPES[ext]) as doc:
            n_pages = doc.page_count
            if n_pages:
                doc.load_page(0)
        pdf_bytes = image_to_pdf(data, INPUT_TYPES[ext]) if ext != ".pdf" and n_pages else None
    except Exception:  # noqa: BLE001  PyMuPDF lanza distintos tipos según el formato
        raise HTTPException(400, f"the file cannot be read as {ext[1:].upper()}") from None
    if n_pages == 0:
        raise HTTPException(400, "the file has no pages")
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = JOBS_DIR / job_id
    d.mkdir()
    name = re.sub(r"[^\w.-]", "_", filename)
    (d / name).write_bytes(data)
    pdf = name
    if pdf_bytes is not None:  # la fase 1 recibe siempre un PDF
        pdf = Path(name).stem + ".pdf"
        (d / pdf).write_bytes(pdf_bytes)
    job = {"id": job_id, "pdf": pdf, "original": name, "client": client, "state": "queued", "progress": 0,
           "step": "En cola", "log": [f"subido por {client}: {name}"], "created": time.time()}
    with lock:
        jobs[job_id] = job
    save_meta(job)
    executor.submit(process, job_id)
    return {"id": job_id}


@app.get("/api/jobs", response_model=list[JobStatus], tags=["trabajos"], summary="Listar trabajos (más recientes primero)",
         dependencies=AUTH)
def list_jobs():
    return [public(j) for j in sorted(jobs.values(), key=lambda j: j["created"], reverse=True)]


def job_status(job_id: str, full: bool = False) -> dict:
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "trabajo no encontrado")
    data = dict(j)
    data["log"] = j.get("log", [])[-300:]
    # Posición en la cola y qué se está procesando ahora (un trabajo a la vez).
    queued = [x["id"] for x in sorted(jobs.values(), key=lambda x: x["created"]) if x["state"] == "queued"]
    if j["state"] == "queued":
        data["queue_position"], data["queue_total"] = queued.index(job_id) + 1, len(queued)
    running = next((x for x in jobs.values() if x["state"] == "running" and x["id"] != job_id), None)
    data["running_now"] = {"pdf": running["pdf"], "progress": running["progress"], "step": running.get("step", "")} if running else None
    if full and j["state"] == "done":
        d = JOBS_DIR / job_id
        data["result"] = json.loads((d / "result.json").read_text(encoding="utf-8"))
        data["ocr_html"] = (d / "ocr.html").read_text(encoding="utf-8")
    return data


@app.get("/api/jobs/{job_id}", response_model=JobDetail, response_model_exclude_none=True, tags=["trabajos"],
         summary="Estado de un trabajo; con full=1 incluye facturas y OCR", dependencies=AUTH)
def get_job(job_id: str, full: int = 0):
    return job_status(job_id, bool(full))


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str):
    """Progreso en tiempo real: envía el estado (sin el resultado) cada medio segundo mientras el
    trabajo está en cola o en ejecución, un último mensaje al terminar y cierra. Exige X-API-Key
    salvo desde 127.0.0.1 (sin token: cierra con el código 1008, violación de política)."""
    if not is_local(ws.client.host if ws.client else None) and not key_name(ws.headers.get("x-api-key")):
        await ws.close(code=1008, reason="missing or invalid API token")
        return
    await ws.accept()
    if job_id not in jobs:
        await ws.send_json({"error": "trabajo no encontrado"})
        await ws.close()
        return
    last = None
    try:
        while True:
            st = job_status(job_id)
            snap = (st["state"], st["progress"], st["step"], len(st["log"]), st.get("queue_position"), (st.get("running_now") or {}).get("progress"))
            if snap != last:
                await ws.send_json(st)
                last = snap
            if st["state"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
    await ws.close()


@app.get("/api/jobs/{job_id}/pdf", response_class=FileResponse, tags=["archivos"], dependencies=AUTH,
         summary="PDF procesado del trabajo (una imagen subida se sirve convertida a PDF)")
def get_pdf(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "trabajo no encontrado")
    return FileResponse(JOBS_DIR / job_id / j["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{j["pdf"]}"'})


@app.get("/api/jobs/{job_id}/result.json", response_class=FileResponse, tags=["archivos"], summary="Descargar el JSON de facturas",
         dependencies=AUTH)
def get_result(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "trabajo no encontrado")
    if j["state"] != "done":
        raise HTTPException(404, "resultado no disponible")
    return FileResponse(JOBS_DIR / job_id / "result.json", media_type="application/json",
                        filename=Path(j["pdf"]).stem + ".facturas.json")


HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo OCR de facturas</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1f2328;--muted:#6b7280;--line:#e5e7eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--accent:#2563eb}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0 12px 0 0}
button,select{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button:disabled{opacity:.5;cursor:default}
#prog{display:flex;align-items:center;gap:10px;padding:8px 16px;background:#fff;border-bottom:1px solid var(--line)}
progress{width:320px;height:12px}.muted{color:var(--muted)}
details{background:#fff;border:1px solid var(--line);border-radius:8px;margin:10px 16px}details>summary{cursor:pointer;padding:9px 12px;font-weight:600}
details .body{padding:0 12px 12px;max-height:60vh;overflow:auto}pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px}
.ocr table{border-collapse:collapse;margin:6px 0}.ocr td,.ocr th{border:1px solid var(--line);padding:3px 6px;vertical-align:top;font-size:12px}
main{display:grid;grid-template-columns:minmax(420px,1fr) minmax(380px,1fr);gap:12px;padding:0 16px 24px}
@media(max-width:1000px){main{grid-template-columns:1fr}#viewer{position:static;height:70vh}}
#viewer{position:sticky;top:120px;height:calc(100vh - 140px);border:1px solid var(--line);border-radius:8px;background:#fff;overflow:hidden}
#viewer iframe{width:100%;height:100%;border:0}#viewer .bar{display:flex;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--line);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:8px;margin-bottom:12px;padding:12px}
.card.review{border-left-color:var(--warn)}.card h2{font-size:15px;margin:0 0 6px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;background:#eef2ff;color:#3730a3}
.ev{display:inline-block;min-width:14px;text-align:center;border-radius:3px;font-size:10px;font-weight:700;margin-left:3px;padding:0 3px;cursor:help}
.ev-leido{background:#dcfce7;color:var(--ok)}.ev-aritmetica{background:#dbeafe;color:#1d4ed8}.ev-calculado{background:#f3f4f6;color:#374151}.ev-asumido{background:#fef3c7;color:var(--warn)}.ev-modelo{background:#fee2e2;color:var(--bad)}
.chip.ok{background:#dcfce7;color:var(--ok)}.chip.warn{background:#fef3c7;color:var(--warn)}.chip.bad{background:#fee2e2;color:var(--bad)}.chip.src{background:#f3f4f6;color:#374151}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px 12px;font-size:12px;margin:6px 0}.grid b{color:var(--muted);font-weight:500}
.issues{margin:6px 0;padding:6px 10px;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;font-size:12px}
table.lines{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}table.lines th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:4px}
table.lines td{border-bottom:1px solid #f1f5f9;padding:4px;vertical-align:top}table.lines tr.warn td{background:#fff7ed}.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
a.pg{color:var(--accent);text-decoration:none;cursor:pointer}.small{font-size:11px;color:var(--muted)}
#summary{display:flex;flex-wrap:wrap;gap:8px;padding:8px 16px}#summary .chip{font-size:12px;padding:3px 10px}
</style></head><body>
<header>
  <h1>Demo OCR de facturas</h1>
  <input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg,.bmp">
  <button class="primary" id="btn" onclick="upload()">Procesar</button>
  <span class="muted">o abrir anterior:</span>
  <select id="jobs" onchange="if(this.value)load(this.value)"><option value="">— trabajos anteriores —</option></select>
  <span style="flex:1"></span>
  <span id="health" class="small"></span>
  <a class="small" href="/docs" target="_blank">Swagger</a> <a class="small" href="/redoc" target="_blank">ReDoc</a>
</header>
<div id="prog" hidden><progress id="bar" max="100" value="0"></progress><span id="step" class="muted"></span><span id="pct" class="muted"></span></div>
<div id="summary"></div>
<details id="dJson"><summary>JSON generado <span class="small" id="jsonInfo"></span> <button onclick="event.preventDefault();copyJson()">Copiar</button> <a id="dl" href="#" download><button onclick="event.preventDefault();window.location=document.getElementById('dl').href">Descargar</button></a></summary><div class="body"><pre id="json"></pre></div></details>
<details id="dOcr"><summary>Resumen OCR (fase 1: texto de página completa, tablas y verificación)</summary><div class="body ocr" id="ocr"></div></details>
<details id="dLog"><summary>Registro del proceso</summary><div class="body"><pre id="log"></pre></div></details>
<main>
  <section id="cards"><p class="muted" style="padding:12px">Subí un PDF o una imagen (PNG, JPG, BMP) para empezar. Los resultados anteriores quedan guardados en la lista de arriba.</p></section>
  <aside id="viewer"><div class="bar"><span id="pdfName" class="muted">PDF</span><span style="flex:1"></span><span class="small">Evidencia: <span class="ev ev-leido">●</span> leído · <span class="ev ev-aritmetica">≈</span> por aritmética · <span class="ev ev-calculado">=</span> calculado · <span class="ev ev-asumido">1</span> asumido · <span class="ev ev-modelo">?</span> sin respaldo · clic en «pág. N» salta a la página</span></div><iframe id="pdf" title="PDF"></iframe></aside>
</main>
<script>
let current=null, timer=null, ws=null;
const EV={leido:['●','leído en el OCR'],aritmetica:['≈','inferido por aritmética (cuadra con el total impreso)'],calculado:['=','calculado: cantidad × precio'],asumido:['1','cantidad asumida (no impresa)'],modelo:['?','sin respaldo en el OCR']};
function ev(e,k){const v=(e||{})[k];if(!v)return '';const [s,t]=EV[v]||['?',v];return `<span class="ev ev-${v}" title="${t}">${s}</span>`;}
async function health(){try{const h=await (await fetch('/api/health')).json();$('health').innerHTML='modelos @'+esc(h.models_host)+': '+h.models.map(m=>`<span class="chip ${m.ok?'ok':'bad'}" title="${esc(m.url)} ${esc(m.model||'')}">${esc(m.name)}</span>`).join(' ');}catch(e){}}
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refreshJobs(){const r=await fetch('/api/jobs');const js=await r.json();const sel=$('jobs');sel.innerHTML='<option value="">— trabajos anteriores —</option>'+js.map(j=>`<option value="${j.id}" ${current===j.id?'selected':''}>${esc(j.pdf)} · ${j.state}${j.n_invoices!=null?' · '+j.n_invoices+' fact.':''} · ${new Date(j.created*1000).toLocaleString()}</option>`).join('');}
async function upload(){const f=$('file').files[0];if(!f){alert('Elegí un PDF o una imagen');return}$('btn').disabled=true;const fd=new FormData();fd.append('file',f);const r=await fetch('/api/jobs',{method:'POST',body:fd});$('btn').disabled=false;if(!r.ok){alert(await r.text());return}const {id}=await r.json();load(id);}
function load(id){current=id;stop();$('cards').innerHTML='';$('json').textContent='';$('ocr').innerHTML='';$('summary').innerHTML='';showPdf(id,1);refreshJobs();
 // progreso por WebSocket; si no está disponible, sondeo cada 1,5 s
 try{ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+`/ws/jobs/${id}`);ws.onmessage=e=>onStatus(JSON.parse(e.data));ws.onerror=()=>{stop();poll();timer=setInterval(poll,1500);};ws.onclose=()=>{if(timer===null&&ws)fetch(`/api/jobs/${id}`).then(r=>r.json()).then(onStatus);};}
 catch(e){poll();timer=setInterval(poll,1500);}}
function stop(){if(timer){clearInterval(timer);timer=null}if(ws){try{ws.onclose=null;ws.close()}catch(e){}ws=null}}
async function poll(){const r=await fetch(`/api/jobs/${current}`);if(r.ok)onStatus(await r.json());}
async function onStatus(j){if(j.error&&!j.state){$('step').textContent=j.error;return}$('prog').hidden=false;$('bar').value=j.progress;$('pct').textContent=j.progress+'%';let step=j.step||'';if(j.state==='queued'){step=`En cola (posición ${j.queue_position} de ${j.queue_total})`+(j.running_now?` · procesando ahora: ${j.running_now.pdf} ${j.running_now.progress}% — ${j.running_now.step}`:'');}$('step').textContent=step;$('log').textContent=(j.log||[]).join('\n');
 if(j.state==='done'){stop();const rf=await fetch(`/api/jobs/${current}?full=1`);render(await rf.json());refreshJobs();}
 else if(j.state==='error'){stop();$('pdfName').textContent=j.pdf||'';$('step').textContent='Error: '+(j.error||'');$('cards').innerHTML=`<div class="card review"><h2>Error</h2><pre>${esc(j.error)}</pre></div>`;refreshJobs();}}
function showPdf(id,page){$('pdf').src=`/api/jobs/${id}/pdf#page=${page||1}&zoom=page-width`;}
function goto(page){showPdf(current,page);}
function copyJson(){navigator.clipboard.writeText($('json').textContent);}
function money(v){return v?('$'+v):'—';}
function render(j){const res=j.result||[];$('pdfName').textContent=j.pdf;$('json').textContent=JSON.stringify(res,null,2);$('jsonInfo').textContent=`(${res.length} factura(s), ${JSON.stringify(res).length.toLocaleString()} bytes)`;$('dl').href=`/api/jobs/${j.id}/result.json`;$('ocr').innerHTML=j.ocr_html||'';
 const nl=res.reduce((a,i)=>a+i.lineas.length,0), nv=res.reduce((a,i)=>a+i.lineas.filter(l=>l.ocr&&l.ocr.verificado).length,0), nr=res.filter(i=>i.motivo_revision).length;
 $('summary').innerHTML=`<span class="chip">${j.n_pages} página(s)</span><span class="chip">${res.length} factura(s)</span><span class="chip">${nl} línea(s)</span><span class="chip ok">${nv} verificadas</span><span class="chip ${nr?'warn':'ok'}">${nr} a revisar</span><span class="chip src">${j.seconds}s</span>`;
 $('cards').innerHTML=res.map(card).join('')||'<p class="muted">Sin facturas detectadas</p>';}
function card(inv){const o=inv.ocr||{};const pages=o.paginas||[];const rev=!!inv.motivo_revision;
 const lines=inv.lineas.map(l=>{const lo=l.ocr||{};return `<tr class="${lo.verificado?'':'warn'}"><td>${l.seq}</td><td class="num">${esc(l.cantidad)}${ev(lo.evidencia,'cantidad')}</td><td>${esc(l.descripcion)}${ev(lo.evidencia,'descripcion')}${lo.condicion?` <span class="small">· ${esc(lo.condicion)}</span>`:''}</td><td class="num">${esc(lo.precio_unitario||'')}${ev(lo.evidencia,'precio_unitario')}</td><td class="num"><b>${esc(l.importe_linea)}</b> ${esc(l.divisa)}${ev(lo.evidencia,'importe_linea')}</td><td>${esc(lo.vendedor||'')}</td><td>${esc(lo.estado_envio||'')}</td><td class="small">${esc(lo.seguimiento||'')}</td><td><span class="chip src">${esc(lo.origen||'')}</span></td><td>${lo.verificado?'<span class="chip ok">✓</span>':'<span class="chip warn">?</span>'}</td><td>${lo.pagina?`<a class="pg" onclick="goto(${lo.pagina})">pág. ${lo.pagina}</a>`:''}</td></tr>`}).join('');
 const desc=(o.descuentos||[]).map(d=>`${esc(d.concepto)} ${esc(d.importe)}`).join('; ');
 const env=(o.envios||[]).length?`${o.envios.filter(e=>e.cuadra).length}/${o.envios.length} cuadran`:'—';
 const eb=o.envio_bloque;const blk=eb?`<span class="chip" title="bloque de envío dentro del pedido">envío ${eb.n}/${eb.de} · ${esc(eb.estado)}</span>`:'';
 const ped=o.total_pedido?`<div><b>Total del pedido</b> ${money(o.total_pedido)} <span class="small">(${eb?eb.de+' envíos':'pedido'})</span></div>`:'';
 return `<div class="card ${rev?'review':''}"><h2>Factura ${inv.id} <span class="chip src">${esc(o.proveedor||'?')}</span> <span>${esc(inv.referencia||'sin referencia')}</span> ${blk}<span class="chip ${inv.estado==='DONE'?'ok':'bad'}">${esc(inv.estado)}</span>${rev?`<span class="chip warn">${esc(inv.motivo_revision)}</span>`:''}<span class="chip src">líneas: ${esc(o.origen_lineas||'')}</span><span style="flex:1"></span><a class="pg" onclick="goto(${pages[0]||1})">pág. ${pages[0]||'?'}${pages[1]&&pages[1]!==pages[0]?'–'+pages[1]:''}</a></h2>
 <div class="grid"><div><b>Fecha</b> ${esc(o.fecha_pedido||'—')}</div><div><b>Total</b> <b>${money(inv.total)}</b> ${inv.total_is_printed?'<span class="small">impreso</span>':'<span class="small">calculado</span>'}</div><div><b>Subtotal artículos</b> ${money(o.subtotal_articulos)}</div><div><b>Envío</b> ${money(o.envio)}</div><div><b>Impuestos</b> ${money(o.impuestos)}</div><div><b>Antes de impuestos</b> ${money(o.total_antes_impuestos)}</div><div><b>Pago</b> ${esc(o.metodo_pago||'—')}</div>${ped||`<div><b>Envíos</b> ${env}</div>`}<div style="grid-column:1/-1"><b>Destinatario</b> ${esc(o.destinatario||'—')} <span class="small">${esc(o.direccion_envio||'')}</span></div>${desc?`<div style="grid-column:1/-1"><b>Descuentos</b> ${desc}</div>`:''}</div>
 ${inv.mismatch?`<div class="issues">⚠ ${esc(inv.mismatch)}</div>`:''}
 <table class="lines"><thead><tr><th>#</th><th>Cant.</th><th>Descripción</th><th>P. unit.</th><th>Importe</th><th>Vendedor</th><th>Estado envío</th><th>Seguimiento</th><th>Origen</th><th>✓</th><th>Pág.</th></tr></thead><tbody>${lines}</tbody></table></div>`;}
refreshJobs();health();setInterval(health,30000);
</script></body></html>"""


DEFAULT_PORT = int(os.environ.get("DEMO_PORT", "8091"))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("demo_api:app", host="0.0.0.0", port=DEFAULT_PORT)
