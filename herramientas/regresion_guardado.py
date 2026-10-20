"""Fase 2 NUEVA (árbol de trabajo) sobre la lectura guardada (ocr.md) de cada trabajo, comparada con su result.json
(producido por el código desplegado). Uso: regresion_guardado.py DESDE(yyyymmdd-hhmm) [pdf,pdf extra]"""
import json, sys, time
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); sys.path.insert(0, str(REPO))
import facturas_json as f2
import ocr_pipeline as p1
desde = sys.argv[1]; extra = sys.argv[2].split(",") if len(sys.argv) > 2 else []
latest = {}
for d in sorted((REPO / "demo_jobs").iterdir()):
    m = d / "meta.json"
    if (d / "result.json").exists() and m.exists() and (d / "ocr.md").exists():
        latest[json.loads(m.read_text(encoding="utf-8"))["pdf"]] = d
docs = [p for p, d in latest.items() if d.name >= desde] + [p for p in extra if p in latest]
page_md = {}
p1.page_markdown = lambda num, full_text, regs: (page_md[num], set(), set())
p1.check_shipments = lambda regions: ([], [], None)
tally = {"docs": 0, "iguales": 0, "cambian": 0, "omitidos": 0}
old_ok = new_ok = n_inv = 0
for pdf in dict.fromkeys(docs):
    d = latest[pdf]
    saved = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if any(i["ocr"].get("origen_lineas") == "envios" for i in saved):
        tally["omitidos"] += 1; continue
    full = (d / "ocr.md").read_text(encoding="utf-8")
    page_md.clear(); page_md.update({n: t.rstrip().rstrip("-").rstrip() for n, t in f2.page_texts(full).items()})
    n = max(page_md) if page_md else 0
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": full,
           "full_text": {i: page_md.get(i + 1, "") for i in range(n)}, "region_pages": [[] for _ in range(n)]}
    logs = []; t0 = time.time()
    with f2.modelos_lock(lambda m: None):
        new = f2.build_all(res, log=logs.append)
    tally["docs"] += 1
    diffs = []
    g = lambda i: [(l["cantidad"], l["importe_linea"], l["descripcion"][:35]) for l in i["lineas"] if l["line_kind"] == "goods"]
    for o, w in zip(saved, new):
        dup = "same order as" in o["mismatch"] or "same order as" in w["mismatch"]
        if not dup:
            n_inv += 1; old_ok += o["motivo_revision"] == ""; new_ok += w["motivo_revision"] == ""
        if (o["motivo_revision"], o["mismatch"][:120]) != (w["motivo_revision"], w["mismatch"][:120]):
            diffs.append(f"   #{w['id']}: {o['motivo_revision'] or 'ok'} -> {w['motivo_revision'] or 'ok'} | {w['mismatch'][:200]}")
        if g(o) != g(w):
            a, b = g(o), g(w)
            diffs.append(f"   #{w['id']} líneas: -{[x for x in a if x not in b][:6]} +{[x for x in b if x not in a][:6]}")
    if len(saved) != len(new):
        diffs.append(f"   facturas {len(saved)} -> {len(new)}")
    tally["cambian" if diffs else "iguales"] += 1
    print(f"{pdf} {time.time() - t0:.0f}s: {[i['motivo_revision'] or 'ok' for i in new]}" + ("  CAMBIA" if diffs else "  IGUAL"), flush=True)
    for x in diffs: print(x, flush=True)
    for m in logs:
        if "precio por unidad" in m or "cuadra exactamente" in m: print("   log:", m, flush=True)
print(f"FIN {tally} | sin revisión: antes {old_ok}/{n_inv}, ahora {new_ok}/{n_inv}", flush=True)
