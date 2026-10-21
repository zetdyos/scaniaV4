"""Calibración del detector de céntimos en superíndice («$55⁹⁹», que GLM y PP-OCRv6 transcriben «$5599» sin punto).
GLM lee; PP-OCRv6 solo da la caja; el PDF da los píxeles. Por cada caja de PP-OCRv6 con forma «$NNN» (marca de moneda y
cifras sin separador) se recorta la caja del PDF a 600 dpi, se separa cada cifra por sus columnas de tinta y se mide
alto y base de las dos últimas frente a las anteriores. Sin modelos generativos (no toca el 27B ni el VL).
Uso: calibra_superindice.py dir_salida [pdf,pdf]   (sin lista: documentos con «$NNN» en el OCR + Amazon app)
Escribe dir_salida/index.json (medidas) y un PNG por caja (recorte con margen, para fijar la verdad a mano)."""
import json, re, sys
from pathlib import Path
import fitz
import numpy as np

REPO = Path(r"C:\Users\LERIT\scaniaV4")
sys.path.insert(0, str(REPO))
import facturas_json as f2

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
TOKEN_RE = re.compile(r"^(?:US)?\$\s?\d{2,6}$")   # forma exacta del disparador: «$5599», «$ 299»
DPI = 600


def glyphs(pdf, page, box):
    """Glifos (x0, x1, arriba, abajo) en píxeles a DPI dentro de la caja de PP-OCRv6 (a PPOCR_DPI)."""
    x0, y0, x1, y1 = [v * 72 / f2.PPOCR_DPI for v in box]
    with fitz.open(pdf) as doc:
        pix = doc[page].get_pixmap(dpi=DPI, clip=fitz.Rect(x0 - 2, y0 - 4, x1 + 2, y1 + 4), alpha=False, colorspace=fitz.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 128
    cols, out, x = a.any(axis=0), [], 0
    while x < len(cols):
        if cols[x]:
            s = x
            while x < len(cols) and cols[x]:
                x += 1
            rows = np.where(a[:, s:x].any(axis=1))[0]
            if x - s > 2 and len(rows):
                out.append((s, x, int(rows.min()), int(rows.max())))
        x += 1
    return out


def measure(text, gl):
    """Alto relativo y elevación de las dos últimas cifras. None si el número de glifos no es el de caracteres
    (cifras pegadas o manchas): entonces no se puede afirmar nada (revisión)."""
    chars = text.replace(" ", "")
    if len(gl) != len(chars):
        return None
    digits = [g for g, c in zip(gl, chars) if c.isdigit()]
    if len(digits) < 3:
        return None
    main, sup = digits[:-2], digits[-2:]
    mh = float(np.median([b - t + 1 for _, _, t, b in main])); mb = float(np.median([b for *_, b in main]))
    sh = float(np.median([b - t + 1 for _, _, t, b in sup])); sb = float(np.median([b for *_, b in sup]))
    return {"alto_rel": round(sh / mh, 3), "elevacion_rel": round((mb - sb) / mh, 3)}


latest = {}
for d in sorted((REPO / "demo_jobs").iterdir()):
    m = d / "meta.json"
    if m.exists() and (d / "result.json").exists() and (d / "ocr.md").exists():
        latest[json.loads(m.read_text(encoding="utf-8"))["pdf"]] = d
if len(sys.argv) > 2:
    pdfs = sys.argv[2].split(",")
else:
    pdfs = [p for p, d in latest.items()
            if re.search(r"\$\s?\d{2,6}(?![\d.,])", (d / "ocr.md").read_text(encoding="utf-8"))
            or re.search(r"Order\s*#\s*\d{3}-\d{7}-\d{7}", (d / "ocr.md").read_text(encoding="utf-8"))]
index = []
for n, pdf in enumerate(pdfs):
    d = latest.get(pdf)
    if not d or not (d / pdf).exists():
        continue
    with fitz.open(d / pdf) as doc:
        pages = len(doc)
    for pg in range(pages):
        try:
            toks, _, _ = f2.ppocr_tokens(d / pdf, pg)
        except Exception as e:  # noqa: BLE001
            print(pdf, pg + 1, "PP-OCRv6 falló:", e, flush=True); continue
        for (x0, y0, x1, y1, t) in toks:
            if not TOKEN_RE.match(t):
                continue
            gl = glyphs(d / pdf, pg, (x0, y0, x1, y1))
            k = len(index)
            r = fitz.Rect(*(v * 72 / f2.PPOCR_DPI for v in (x0 - 260, y0 - 60, x1 + 60, y1 + 60)))
            with fitz.open(d / pdf) as doc:
                doc[pg].get_pixmap(dpi=150, clip=r & doc[pg].rect).save(OUT / f"{k:03d}.png")
            e = {"i": k, "pdf": pdf, "page": pg, "texto": t, "caja": [x0, y0, x1, y1], "glifos": len(gl),
                 "medida": measure(t, gl)}
            index.append(e)
            print(json.dumps(e, ensure_ascii=False), flush=True)
    if n % 20 == 0:
        print(f"... {n + 1}/{len(pdfs)} documentos", flush=True)
(OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
print("FIN", len(index), "cajas", flush=True)
