"""Repite la fase 2 de un trabajo con las respuestas guardadas de los modelos (modelos.json: 27B, Qwen2.5-VL de página y
de foto, PP-OCRv6 y regiones de la fase 1). Sin GPU y exacto para cualquier cambio del código que no altere lo que se
pregunta a los modelos (si se pregunta algo nuevo, esa llamada falla y se dice). Solo trabajos desde 2026-10-21 08:43.
Uso: repetir_trabajo.py JOB_ID [JOB_ID...]"""
import json, sys
from decimal import Decimal
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); sys.path.insert(0, str(REPO))
import facturas_json as f2
import ocr_pipeline as p1

RAW = {}


def fake_27b(markdown, server, timeout, log):
    r = RAW["27b"].get(f2.md_key(markdown))
    if r is None:
        return None, "sin respuesta guardada del 27B para este texto"
    return json.loads(r["content"], parse_float=Decimal), ("model output limit reached" if r.get("finish_reason") == "length" else "")


def fake_ppocr(pdf, page_idx, timeout=120):
    pg = RAW["ppocr"].get(str(page_idx))
    if pg is None:
        raise RuntimeError("sin PP-OCRv6 guardado")
    return [tuple(t) for t in pg["tokens"]], pg["w"], pg["h"]


def fake_photo(pdf, page_idx, rect, server, timeout):
    q = RAW["vl_foto"].get(f"{page_idx}:{rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f}")
    q = json.loads(q).get("quantity") if q else None
    return q if isinstance(q, int) and 1 <= q <= 999 else None


real_vl = f2.lines_with_vl


def fake_vl(pdf, pages, server, timeout, log):
    saved = f2.post_json
    answers = [RAW["vl_pagina"].get(str(i)) for i in pages]
    it = iter(answers)
    f2.post_json = lambda *a, **k: {"choices": [{"message": {"content": next(it) or '{"items": []}'}}]}
    try:
        return real_vl(pdf, pages, server, timeout, log)
    finally:
        f2.post_json = saved


f2.lines_with_llm, f2.ppocr_tokens, f2.photo_quantity, f2.lines_with_vl = fake_27b, fake_ppocr, fake_photo, fake_vl
p1.server_ok = lambda url: True

for job in sys.argv[1:]:
    d = REPO / "demo_jobs" / job
    RAW.clear(); RAW.update(json.loads((d / "modelos.json").read_text(encoding="utf-8")))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    md = (d / "ocr.md").read_text(encoding="utf-8")
    pages = f2.page_texts(md)
    n = max(pages) if pages else 0
    full = {i: pages[i + 1].split("\n", 2)[2].split("\n### Tablas detectadas")[0].split("\n### Verificación de importes")[0].strip()
            for i in range(n)}
    res = {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": md, "full_text": full,
           "region_pages": RAW.get("fase1_regiones") or [[] for _ in range(n)]}
    saved = json.loads((d / "result.json").read_text(encoding="utf-8"))
    logs = []
    new = f2.build_all(res, log=logs.append)
    print(f"{meta['pdf']} ({job}): guardado {[i['motivo_revision'] or 'ok' for i in saved]} -> ahora {[i['motivo_revision'] or 'ok' for i in new]}")
    for i in new:
        print(f"  #{i['id']} p{i['ocr']['paginas']} total {i['total']} {i['motivo_revision'] or 'ok'} {i['mismatch'][:160]}")
    for m in logs:
        if "sin respuesta guardada" in m or "salto" in m or "realineado" in m or "quitada" in m:
            print("   log:", m[:200])
