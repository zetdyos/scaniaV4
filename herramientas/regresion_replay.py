"""Fase 2 del commit (HEAD) frente al árbol de trabajo SIN modelos: la respuesta del 27B de cada página se reconstruye
desde el result.json guardado (líneas con su line_kind, descripción, cantidad, importe y precio unitario). Sirve para
cambios que actúan DESPUÉS del 27B (cabecera, cuadre, división). Aproximada: no conserva el total del propio 27B, el
signo negativo de los cargos ni las líneas que el código descartó; el rescate VL y los recortes de foto se desactivan
(dan lo mismo en las dos versiones). Uso: regresion_replay.py [pdf,pdf]   (sin lista: todos los trabajos vía 27B)"""
import importlib.util, json, sys
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); S = Path(__file__).parent
sys.path.insert(0, str(REPO))
import facturas_json as new
import ocr_pipeline as p1
spec = importlib.util.spec_from_file_location("facturas_json_head", S / "facturas_json_head.py")
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)

latest = {}
for d in sorted((REPO / "demo_jobs").iterdir()):
    m = d / "meta.json"
    if (d / "result.json").exists() and m.exists() and (d / "ocr.md").exists():
        latest[json.loads(m.read_text(encoding="utf-8"))["pdf"]] = d
page_md, replies, totals = {}, {}, {}
p1.page_markdown = lambda num, full_text, regs: (page_md[num], set(), set())
p1.check_shipments = lambda regions: ([], [], None)


def num(s):
    try:
        return float(str(s).replace(",", "")) if str(s).strip() else 0
    except ValueError:
        return 0


def fake_llm(markdown, server, timeout, log):
    page = next((n for n, t in page_md.items() if t == markdown), None)
    t = totals.get(page, "")
    return ({"lineas": replies.get(page, []), "total": t, "total_valor": num(t), "total_is_printed": bool(t)}, "")


for mod in (new, old):
    mod.lines_with_llm = fake_llm
    mod.lines_with_vl = lambda *a, **k: ([], "")
    if hasattr(mod, "photo_quantities"):
        mod.photo_quantities = lambda *a, **k: None
    mod.ppocr_tokens = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin PP-OCRv6 en la reconstrucción"))
    mod.post_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin modelos en la reconstrucción"))

docs = sys.argv[1].split(",") if len(sys.argv) > 1 else list(latest)
tally = {"docs": 0, "iguales": 0, "cambian": 0, "omitidos": 0}
ok_old = ok_new = n_inv = 0
for pdf in docs:
    d = latest.get(pdf)
    if not d:
        continue
    saved = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if not saved or any(i["ocr"].get("origen_lineas") != "llm" for i in saved):
        tally["omitidos"] += 1; continue
    full = (d / "ocr.md").read_text(encoding="utf-8")
    page_md.clear(); page_md.update({n: t.rstrip().rstrip("-").rstrip() for n, t in new.page_texts(full).items()})
    replies.clear(); totals.clear()
    for f in saved:
        if f.get("total_is_printed") and f.get("total") and f["ocr"].get("paginas"):
            totals[f["ocr"]["paginas"][-1]] = f["total"]  # el total impreso guardado, en la última página de la factura
        for l in f["lineas"]:
            o = l.get("ocr") or {}
            if o.get("origen") not in (None, "qwen27b"):
                continue
            replies.setdefault(o.get("pagina"), []).append({
                "descripcion": l["descripcion"], "cantidad": str(l["cantidad"]), "cantidad_valor": num(l["cantidad"]),
                "importe_linea": l["importe_linea"] or "", "importe_valor": num(l["importe_linea"]),
                "divisa": l.get("divisa") or "USD", "precio_unitario": o.get("precio_unitario") or "",
                "precio_unitario_valor": num(o.get("precio_unitario")), "vendedor": o.get("vendedor", ""),
                "seguimiento": o.get("seguimiento", ""), "estado_envio": o.get("estado_envio", ""),
                "line_kind": l["line_kind"]})
    n = max(page_md) if page_md else 0
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": full,
           "full_text": {i: page_md.get(i + 1, "") for i in range(n)}, "region_pages": [[] for _ in range(n)]}
    try:
        a = old.build_all(res, log=lambda m: None)
        b = new.build_all(res, log=lambda m: None)
    except Exception as e:  # noqa: BLE001
        print(pdf, "ERROR", e, flush=True); continue
    tally["docs"] += 1
    # La reconstrucción vale solo si la versión del commit da lo mismo que lo guardado (mismas facturas, mismo
    # estado, mismos artículos); si no, el documento no se cuenta (artefacto de la reconstrucción).
    g = lambda i: sorted((str(l["cantidad"]), l["importe_linea"]) for l in i["lineas"] if l["line_kind"] == "goods")
    if [(i["motivo_revision"], g(i)) for i in a] != [(i["motivo_revision"], g(i)) for i in saved]:
        tally["no_reproduce"] = tally.get("no_reproduce", 0) + 1
        continue
    diffs = []
    for o, w in zip(a, b):
        n_inv += 1; ok_old += o["motivo_revision"] == ""; ok_new += w["motivo_revision"] == ""
        if (o["motivo_revision"], o["mismatch"]) != (w["motivo_revision"], w["mismatch"]):
            diffs.append(f"   #{w['id']}: {o['motivo_revision'] or 'ok'} -> {w['motivo_revision'] or 'ok'} | {w['mismatch'][:200]}")
        h = lambda i: (i["total"], i["ocr"].get("envio"), i["ocr"].get("impuestos"), i["ocr"].get("descuentos"))
        if h(o) != h(w):
            diffs.append(f"   #{w['id']} cabecera: {h(o)} -> {h(w)}")
    if len(a) != len(b):
        diffs.append(f"   facturas {len(a)} -> {len(b)}")
    tally["cambian" if diffs else "iguales"] += 1
    if diffs or len(sys.argv) > 2:
        print(f"{pdf}: antes {[i['motivo_revision'] or 'ok' for i in a]} ahora {[i['motivo_revision'] or 'ok' for i in b]} | {b[0]['mismatch'][:150]}", flush=True)
        for x in diffs: print(x, flush=True)
print(f"FIN {tally} | sin revisión (reconstrucción): antes {ok_old}/{n_inv}, ahora {ok_new}/{n_inv}", flush=True)
