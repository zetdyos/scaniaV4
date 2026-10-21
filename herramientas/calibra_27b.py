"""Calibración del 27B: corre la fase 2 ACTUAL con el 27B real sobre una muestra de trabajos (lectura de la fase 1
desde ocr.md, como regresion_replay) con una variante de lo que se le pregunta, y guarda por documento la respuesta
cruda del 27B y el resultado (dir/variante/JOB.json). Reanudable: salta los documentos ya hechos. Sin VL (el rescate
visual y las fotos se apagan: se mide solo el 27B). PP-OCRv6 desde ppocr.json si está guardado.
Uso: calibra_27b.py muestra.json dir variante [variante...]
Variantes:
  base   como en producción.
  v1     sin la nota «### Verificación de importes» de la fase 1 en el texto que recibe el 27B.
  v2     v1 + en el prompt, importe_linea = importe de la línea ANTES de impuestos (si hay columna sin y con impuesto)."""
import json, sys, time
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); sys.path.insert(0, str(REPO))
import facturas_json as f2
import ocr_pipeline as p1

sample = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
jobs = list(sample["err"]) + list(sample["ctrl"])
OUT = Path(sys.argv[2])

BASE_PROMPT = f2.USER_PROMPT
V2_SENTENCE = ("importe_linea (the amount printed for that line; when the line shows both an amount before tax and a "
               "total that includes tax, use the amount before tax)")
assert "importe_linea (the amount printed for that line)" in BASE_PROMPT
real_llm = f2.lines_with_llm
cut = lambda md: md.split("\n### Verificación de importes")[0].rstrip()

f2.lines_with_vl = lambda *a, **k: ([], "")
f2.photo_quantities = lambda *a, **k: []
page_md, ppocr = {}, {}
p1.page_markdown = lambda num, full_text, regs: (page_md[num], set(), set())
p1.check_shipments = lambda regions: ([], [], None)


def cached_ppocr(pdf, page_idx, timeout=120):
    if str(page_idx) not in ppocr:
        raise RuntimeError("sin PP-OCRv6 guardado")
    pg = ppocr[str(page_idx)]
    return [tuple(t) for t in pg["tokens"]], pg["w"], pg["h"]


f2.ppocr_tokens = cached_ppocr

for variant in sys.argv[3:]:
    f2.USER_PROMPT = BASE_PROMPT.replace("importe_linea (the amount printed for that line)", V2_SENTENCE) if variant == "v2" else BASE_PROMPT
    f2.lines_with_llm = (lambda md, *a, **k: real_llm(cut(md), *a, **k)) if variant in ("v1", "v2") else real_llm
    d_out = OUT / variant; d_out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    for n, job in enumerate(jobs):
        o = d_out / f"{job}.json"
        if o.exists():
            continue
        d = REPO / "demo_jobs" / job
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        full = (d / "ocr.md").read_text(encoding="utf-8")
        page_md.clear(); page_md.update({k: t.rstrip().rstrip("-").rstrip() for k, t in f2.page_texts(full).items()})
        ppocr.clear()
        if (d / "ppocr.json").exists():
            ppocr.update(json.loads((d / "ppocr.json").read_text(encoding="utf-8")))
        npg = max(page_md) if page_md else 0
        res = {"pdf": str(d / meta["pdf"]), "n_pages": npg, "markdown": full,
               "full_text": {i: page_md.get(i + 1, "") for i in range(npg)}, "region_pages": [[] for _ in range(npg)]}
        logs = []
        try:
            with f2.modelos_lock(lambda m: None):
                out = f2.build_all(res, log=logs.append)
        except Exception as e:  # noqa: BLE001
            print(f"{variant} {meta['pdf']} ERROR {e}", flush=True)
            continue
        o.write_text(json.dumps({"pdf": meta["pdf"], "result": out, "raw27b": f2.MODEL_RAW["27b"]}, ensure_ascii=False),
                     encoding="utf-8")
        print(f"{variant} {n + 1}/{len(jobs)} {meta['pdf']} {[i['motivo_revision'] or 'ok' for i in out]} "
              f"({time.time() - t0:.0f}s)", flush=True)
    print(f"FIN {variant} {time.time() - t0:.0f}s", flush=True)
