"""Fase 2 del commit (HEAD) frente a la del árbol de trabajo, sobre la MISMA lectura (ocr.md del último trabajo de
cada PDF) y las MISMAS respuestas del 27B (caché compartida). Imprime cada diferencia. Uso: regresion_head.py pdf,pdf"""
import importlib.util, json, sys, time
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); S = Path(__file__).parent
sys.path.insert(0, str(REPO))
import facturas_json as new
import ocr_pipeline as p1
# El módulo de saltos del COMMIT (git show HEAD:saltos.py > herramientas/saltos_head.py): si no se carga antes, el
# facturas_json de HEAD importaría en silencio el saltos.py del árbol de trabajo.
_saltos_tree = sys.modules.pop("saltos", None)
if (S / "saltos_head.py").exists():
    _ss = importlib.util.spec_from_file_location("saltos", S / "saltos_head.py")
    _sm = importlib.util.module_from_spec(_ss); sys.modules["saltos"] = _sm; _ss.loader.exec_module(_sm)
spec = importlib.util.spec_from_file_location("facturas_json_head", S / "facturas_json_head.py")
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
if _saltos_tree is not None:
    sys.modules["saltos"] = _saltos_tree


class Keep(dict):
    def clear(self):
        pass


shared = Keep(); new._LLM_CACHE = shared
# Con el mismo prompt y esquema se comparten las respuestas del 27B (misma lectura para los dos); si cambian, cada
# versión pregunta con los suyos (si no, la nueva reutilizaría respuestas pedidas con el prompt viejo).
same_prompt = (old.USER_PROMPT, old.SYSTEM_PROMPT, old.LINES_SCHEMA) == (new.USER_PROMPT, new.SYSTEM_PROMPT, new.LINES_SCHEMA)
old._LLM_CACHE = shared if same_prompt else Keep()
print("prompt/esquema del 27B", "iguales: respuestas compartidas" if same_prompt else "DISTINTOS: cada versión llama al 27B", flush=True)
latest = {}
for d in sorted((REPO / "demo_jobs").iterdir()):
    m = d / "meta.json"
    if (d / "result.json").exists() and m.exists() and (d / "ocr.md").exists():
        latest[json.loads(m.read_text(encoding="utf-8"))["pdf"]] = d
page_md = {}
p1.page_markdown = lambda num, full_text, regs: (page_md[num], set(), set())
p1.check_shipments = lambda regions: ([], [], None)
logs = []
tally = {"iguales": 0, "cambian": 0}
for pdf in sys.argv[1].split(","):
    d = latest.get(pdf)
    if not d:
        print(pdf, "no está", flush=True); continue
    full = (d / "ocr.md").read_text(encoding="utf-8")
    page_md.clear(); page_md.update({n: t.rstrip().rstrip("-").rstrip() for n, t in new.page_texts(full).items()})
    n = max(page_md) if page_md else 0
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": full,
           "full_text": {i: page_md.get(i + 1, "") for i in range(n)}, "region_pages": [[] for _ in range(n)]}
    dict.clear(shared); dict.clear(old._LLM_CACHE)
    t0 = time.time()
    with new.modelos_lock(lambda m: None):
        a = old.build_all(res, log=lambda m: None)
        logs.clear()
        b = new.build_all(res, log=logs.append)
    diffs = []
    for o, w in zip(a, b):
        if (o["motivo_revision"], o["mismatch"]) != (w["motivo_revision"], w["mismatch"]):
            diffs.append(f"   #{w['id']}: {o['motivo_revision'] or 'ok'} -> {w['motivo_revision'] or 'ok'} | {w['mismatch'][:220]}")
        g = lambda i: [(l["cantidad"], l["importe_linea"], l["descripcion"][:40]) for l in i["lineas"] if l["line_kind"] == "goods"]
        if g(o) != g(w):
            diffs.append(f"   #{w['id']} líneas: -{[x for x in g(o) if x not in g(w)]} +{[x for x in g(w) if x not in g(o)]}")
    if len(a) != len(b):
        diffs.append(f"   facturas {len(a)} -> {len(b)}")
    tally["cambian" if diffs else "iguales"] += 1
    print(f"{pdf} {time.time() - t0:.0f}s: {[i['motivo_revision'] or 'ok' for i in b]}" + ("  CAMBIA" if diffs else "  IGUAL"), flush=True)
    for x in diffs: print(x, flush=True)
    for m in logs:
        if "precio por unidad" in m or "cuadra exactamente" in m: print("   log:", m, flush=True)
print("FIN", tally, flush=True)
