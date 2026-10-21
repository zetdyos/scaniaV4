"""Guarda las cajas de PP-OCRv6 de cada página en ppocr.json del trabajo (una vez; las herramientas de medición las
leen de ahí sin volver a llamar al servicio). Uso: cache_ppocr.py lista.txt   (una ruta de PDF de demo_jobs por línea)
Solo PP-OCRv6 (det+rec, no generativo, ~1 s/página); no usa el 27B ni el VL. Correr con WTCP parado."""
import json, sys, time
from pathlib import Path
import fitz
sys.path.insert(0, r"C:\Users\LERIT\scaniaV4")
import facturas_json as f2

t0, n = time.time(), 0
for p in [l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]:
    out = Path(p).parent / "ppocr.json"
    if out.exists():
        continue
    try:
        with fitz.open(p) as d:
            k = len(d)
        pages = {}
        for i in range(k):
            toks, w, h = f2.ppocr_tokens(p, i)
            pages[str(i)] = {"tokens": toks, "w": w, "h": h}
        out.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        n += k
        print(f"{Path(p).name}: {k} pág ({time.time() - t0:.0f}s, {n} págs)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(Path(p).name, "ERROR", e, flush=True)
    f2._PPOCR_CACHE.clear()
print("FIN", n, "páginas", f"{time.time() - t0:.0f}s", flush=True)
