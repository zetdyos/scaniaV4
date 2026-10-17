"""
Demo interno (FastAPI) para evaluar visualmente el OCR de facturas.

- GET  /                      interfaz: subir PDF, barra de progreso, tarjetas de facturas a la
                              izquierda y el PDF a la derecha, JSON y resumen OCR desplegables.
- POST /api/jobs              sube un PDF y lo encola (un trabajo a la vez: los llama-server
                              remotos tienen un solo slot).
- GET  /api/jobs              lista de trabajos (los resultados persisten en demo_jobs/).
- GET  /api/jobs/{id}         estado, progreso y registro; con ?full=1 incluye el JSON y el OCR.
- GET  /api/jobs/{id}/pdf     el PDF subido (para el visor).
- GET  /api/jobs/{id}/result.json  descarga del JSON de facturas.
- WS   /ws/jobs/{id}         progreso en tiempo real (JSON por mensaje; cierra al terminar).
- GET  /api/health           estado de los tres llama-server del host de modelos.
- GET  /docs  y  /redoc      documentación OpenAPI (Swagger UI y ReDoc).

Sin autenticación: uso interno. Puerto por defecto 8091 (variable DEMO_PORT). Arranque:
    .venv/bin/python demo_api.py                                   # 0.0.0.0:8091
    .venv/bin/uvicorn demo_api:app --host 0.0.0.0 --port 8091      # equivalente
OCR_MODELS_HOST (opcional, por defecto 192.168.1.194) indica dónde están los llama-server.
"""

import asyncio
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import markdown as mdlib
import requests
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import facturas_json as f2
import ocr_pipeline as p1

HERE = Path(__file__).resolve().parent
JOBS_DIR = HERE / "demo_jobs"
JOBS_DIR.mkdir(exist_ok=True)

TAGS = [
    {"name": "trabajos", "description": "Subida de PDF y consulta de resultados. Un trabajo a la vez: los llama-server tienen un solo slot."},
    {"name": "archivos", "description": "PDF original y JSON de facturas de un trabajo."},
    {"name": "sistema", "description": "Salud de los servidores de modelos."},
]
app = FastAPI(
    title="PDF Reder · OCR de facturas",
    version="1.0.0",
    description=(
        "API del demo interno de OCR de facturas.\n\n"
        "**Flujo:** `POST /api/jobs` sube un PDF y devuelve un `id`; el progreso llega por WebSocket en "
        "`/ws/jobs/{id}` (un JSON por mensaje con `state`, `progress`, `step`, `log`; se cierra al terminar) "
        "o consultando `GET /api/jobs/{id}`; con `?full=1` se obtiene el array de facturas y el OCR en HTML.\n\n"
        "**Fases:** 1) OCR con GLM-OCR (página completa + regiones + verificación + rescate con Qwen2.5-VL); "
        "2) división en facturas y JSON estándar (Qwen3.6-27B para líneas cuando no hay bloques de envío).\n\n"
        f"**Host de modelos:** `{p1.MODELS_HOST}` (variable de entorno `OCR_MODELS_HOST`). Sin autenticación: uso interno."
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
    state: str = Field(description="queued | running | done | error")
    progress: int = Field(ge=0, le=100)
    step: str = ""
    created: float
    started: Optional[float] = None
    seconds: Optional[float] = None
    error: Optional[str] = None
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
                job.update(state="error", error="interrumpido al reiniciar el servidor", step="Error")
            jobs[job["id"]] = job


load_jobs()


def set_step(job, pct, step):
    job["progress"] = max(job.get("progress", 0), int(pct))
    job["step"] = step


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
        res = p1.run(str(d / job["pdf"]), log=log)
        out = f2.build_all(res, log=log)
        (d / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "ocr.md").write_text(res["markdown"], encoding="utf-8")
        (d / "ocr.html").write_text(mdlib.markdown(res["markdown"], extensions=["tables"]), encoding="utf-8")
        job.update(state="done", progress=100, step="Listo", n_pages=res["n_pages"], n_invoices=len(out),
                   n_lines=sum(len(i["lineas"]) for i in out), n_review=sum(1 for i in out if i["motivo_revision"]),
                   n_verified=sum(1 for i in out for l in i["lineas"] if l["ocr"]["verificado"]))
    except Exception as e:  # noqa: BLE001
        job.update(state="error", error=f"{type(e).__name__}: {e}", step="Error")
    job["seconds"] = round(time.time() - job["started"], 1)
    save_meta(job)


def public(j):
    return {k: v for k, v in j.items() if k != "log"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return HTML


@app.get("/api/health", response_model=Health, tags=["sistema"], summary="Estado de los servidores de modelos")
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


@app.post("/api/jobs", response_model=JobCreated, status_code=201, tags=["trabajos"], summary="Subir un PDF y encolarlo")
async def create_job(file: UploadFile = File(..., description="PDF a procesar")):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos PDF")
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = JOBS_DIR / job_id
    d.mkdir()
    name = re.sub(r"[^\w.-]", "_", Path(file.filename).name)
    (d / name).write_bytes(await file.read())
    job = {"id": job_id, "pdf": name, "state": "queued", "progress": 0, "step": "En cola", "log": [], "created": time.time()}
    with lock:
        jobs[job_id] = job
    save_meta(job)
    executor.submit(process, job_id)
    return {"id": job_id}


@app.get("/api/jobs", response_model=list[JobStatus], tags=["trabajos"], summary="Listar trabajos (más recientes primero)")
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
         summary="Estado de un trabajo; con full=1 incluye facturas y OCR")
def get_job(job_id: str, full: int = 0):
    return job_status(job_id, bool(full))


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str):
    """Progreso en tiempo real: envía el estado (sin el resultado) cada medio segundo mientras el
    trabajo está en cola o en ejecución, un último mensaje al terminar y cierra."""
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


@app.get("/api/jobs/{job_id}/pdf", response_class=FileResponse, tags=["archivos"], summary="PDF original del trabajo")
def get_pdf(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "trabajo no encontrado")
    return FileResponse(JOBS_DIR / job_id / j["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{j["pdf"]}"'})


@app.get("/api/jobs/{job_id}/result.json", response_class=FileResponse, tags=["archivos"], summary="Descargar el JSON de facturas")
def get_result(job_id: str):
    j = jobs.get(job_id)
    if not j or j["state"] != "done":
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
  <input type="file" id="file" accept="application/pdf">
  <button class="primary" id="btn" onclick="upload()">Procesar PDF</button>
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
  <section id="cards"><p class="muted" style="padding:12px">Subí un PDF para empezar. Los resultados anteriores quedan guardados en la lista de arriba.</p></section>
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
async function upload(){const f=$('file').files[0];if(!f){alert('Elegí un PDF');return}$('btn').disabled=true;const fd=new FormData();fd.append('file',f);const r=await fetch('/api/jobs',{method:'POST',body:fd});$('btn').disabled=false;if(!r.ok){alert(await r.text());return}const {id}=await r.json();load(id);}
function load(id){current=id;stop();$('cards').innerHTML='';$('json').textContent='';$('ocr').innerHTML='';$('summary').innerHTML='';showPdf(id,1);refreshJobs();
 // progreso por WebSocket; si no está disponible, sondeo cada 1,5 s
 try{ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+`/ws/jobs/${id}`);ws.onmessage=e=>onStatus(JSON.parse(e.data));ws.onerror=()=>{stop();poll();timer=setInterval(poll,1500);};ws.onclose=()=>{if(timer===null&&ws)fetch(`/api/jobs/${id}`).then(r=>r.json()).then(onStatus);};}
 catch(e){poll();timer=setInterval(poll,1500);}}
function stop(){if(timer){clearInterval(timer);timer=null}if(ws){try{ws.onclose=null;ws.close()}catch(e){}ws=null}}
async function poll(){const r=await fetch(`/api/jobs/${current}`);if(r.ok)onStatus(await r.json());}
async function onStatus(j){if(j.error&&!j.state){$('step').textContent=j.error;return}$('prog').hidden=false;$('bar').value=j.progress;$('pct').textContent=j.progress+'%';let step=j.step||'';if(j.state==='queued'){step=`En cola (posición ${j.queue_position} de ${j.queue_total})`+(j.running_now?` · procesando ahora: ${j.running_now.pdf} ${j.running_now.progress}% — ${j.running_now.step}`:'');}$('step').textContent=step;$('log').textContent=(j.log||[]).join('\n');
 if(j.state==='done'){stop();const rf=await fetch(`/api/jobs/${current}?full=1`);render(await rf.json());refreshJobs();}
 else if(j.state==='error'){stop();$('step').textContent='Error: '+(j.error||'');$('cards').innerHTML=`<div class="card review"><h2>Error</h2><pre>${esc(j.error)}</pre></div>`;refreshJobs();}}
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
