"""Uso: replay_cmp.py MODDIR OUT.json  -> repite la fase 2 de los trabajos con modelos.json usando facturas_json de MODDIR.
Fotos sin respuesta guardada: llamada REAL al VL (con permiso del usuario, WTCP detenido)."""
import sys, json, importlib.util
from decimal import Decimal
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); sys.path.insert(0, str(REPO))
moddir, outp = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("facturas_json", str(Path(moddir) / "facturas_json.py"))
f2 = importlib.util.module_from_spec(spec); sys.modules["facturas_json"] = f2; spec.loader.exec_module(f2)
import ocr_pipeline as p1
RAW = {}
NEW_PHOTO = []
def fake_27b(markdown, server, timeout, log, image=None):
    r = RAW.get("27b_imagen" if image else "27b", {}).get(f2.md_key(markdown))
    if r is None:
        return None, "sin respuesta guardada del 27B para este texto"
    return json.loads(r["content"], parse_float=Decimal), ("model output limit reached" if r.get("finish_reason") == "length" else "")
real_ppocr = f2.ppocr_tokens
PP_CACHE = {}
def api_idle():
    import urllib.request
    try:
        h = json.load(urllib.request.urlopen("http://127.0.0.1:8091/api/health", timeout=5))
        return h["jobs_running"] == 0 and h["jobs_queued"] == 0
    except Exception:
        return False
def fake_ppocr(pdf, page_idx, timeout=120):
    pg = RAW["ppocr"].get(str(page_idx))
    if pg is None:  # página que producción no pidió: PP-OCRv6 real, entre trabajos de WTCP y con el bloqueo
        key = (pdf, page_idx)
        if key not in PP_CACHE:
            import time
            while not api_idle():
                time.sleep(2)
            with f2.modelos_lock(log=lambda m: None):
                PP_CACHE[key] = real_ppocr(pdf, page_idx, timeout)
        return PP_CACHE[key]
    return [tuple(t) for t in pg["tokens"]], pg["w"], pg["h"]
real_photo = f2.photo_quantity
def fake_photo(pdf, page_idx, rect, server, timeout):
    key = f"{page_idx}:{rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f}"
    answers = [RAW["vl_foto"].get(key), RAW["vl_foto"].get(key + ":esquina")]
    if answers[0] is None:
        import time
        while not api_idle():
            time.sleep(2)
        with f2.modelos_lock(log=lambda m: None):
            q = real_photo(pdf, page_idx, rect, "http://192.168.1.194:8088", 300)  # GPU real
        NEW_PHOTO.append((pdf, key, q))
        return q
    it = iter(answers)
    saved, img = f2.post_json, f2._vl_image
    f2.post_json = lambda *a, **k: {"choices": [{"message": {"content": next(it) or ""}}]}
    f2._vl_image = lambda *a: ""
    try:
        return real_photo(pdf, page_idx, rect, server, timeout)
    finally:
        f2.post_json, f2._vl_image = saved, img
real_vl = f2.lines_with_vl
def fake_vl(pdf, pages, server, timeout, log):
    saved = f2.post_json
    it = iter([RAW["vl_pagina"].get(str(i)) for i in pages])
    f2.post_json = lambda *a, **k: {"choices": [{"message": {"content": next(it) or '{"items": []}'}}]}
    try:
        return real_vl(pdf, pages, server, timeout, log)
    finally:
        f2.post_json = saved
f2.lines_with_llm, f2.ppocr_tokens, f2.photo_quantity, f2.lines_with_vl = fake_27b, fake_ppocr, fake_photo, fake_vl
p1.server_ok = lambda url: True
out = {}
jobs = sorted(d.name for d in (REPO / "demo_jobs").iterdir() if d.name >= "20261021-0843" and (d / "modelos.json").exists() and (d / "result.json").exists())
for job in jobs:
    d = REPO / "demo_jobs" / job
    RAW.clear(); RAW.update(json.loads((d / "modelos.json").read_text(encoding="utf-8")))
    f2.server_has_vision = lambda server: bool(RAW.get("27b_imagen"))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    md = (d / "ocr.md").read_text(encoding="utf-8")
    pages = f2.page_texts(md); n = max(pages) if pages else 0
    full = {i: pages[i + 1].split("\n", 2)[2].split("\n### Tablas detectadas")[0].split("\n### Verificación de importes")[0].strip() for i in range(n)}
    res = {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": md, "full_text": full, "region_pages": RAW.get("fase1_regiones") or [[] for _ in range(n)]}
    logs = []
    try:
        new = f2.build_all(res, log=logs.append)
        out[job] = {"pdf": meta["pdf"], "inv": [{"ref": i.get("referencia"), "rev": i["motivo_revision"], "total": i["total"], "mm": i["mismatch"][:200],
                    "lines": [(l["cantidad"], l["importe_linea"], l["descripcion"][:40]) for l in i["lineas"] if l["line_kind"] == "goods"]} for i in new],
                    "missing27b": sum("sin respuesta guardada" in m for m in logs)}
    except Exception as e:
        out[job] = {"pdf": meta["pdf"], "error": repr(e)}
    print(job, meta["pdf"], "ok" if "inv" in out[job] else out[job]["error"], flush=True)
out["_new_photo"] = NEW_PHOTO
Path(outp).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
