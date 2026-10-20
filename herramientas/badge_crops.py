"""Recorta la FOTO de cada artículo de Amazon (bloque «Sold by» alineado a la izquierda + precio suelto debajo),
con las coordenadas de PP-OCRv6. Guarda recortes PNG, un índice JSON y una hoja de contacto para fijar la verdad a mano.
Uso: badge_crops.py salida_dir pdf[:paginas] ..."""
import io, json, re, sys
from pathlib import Path
import fitz, numpy as np
from PIL import Image, ImageDraw
from paddleocr import PaddleOCR

DPI = 200
PRICE = re.compile(r"^\$\d{1,3}(?:,\d{3})*\.\d{2}$")
ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="cpu")
out = Path(sys.argv[1]); out.mkdir(exist_ok=True)
index = []
for arg in sys.argv[2:]:
    pdf, _, pages = arg.partition("::")
    doc = fitz.open(pdf)
    name = Path(pdf).name
    for k in ([int(p) for p in pages.split(",")] if pages else range(len(doc))):
        pix = doc[k].get_pixmap(dpi=DPI, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        r = ocr.predict(np.array(img))[0]
        toks = [(int(b[0]), int(b[1]), int(b[2]), int(b[3]), t.strip()) for t, b in zip(r["rec_texts"], r["rec_boxes"]) if t.strip()]
        sold = [t for t in toks if t[4].lower().startswith("sold by")]
        for x0, y0, x1, y1, t in toks:
            if not PRICE.match(t):
                continue
            s = [u for u in sold if abs(u[0] - x0) <= 20 and 0 < y0 - u[1] < 260]
            if not s:
                continue
            su = max(s, key=lambda u: u[1])
            top = su[1]
            for _ in range(4):  # subir por renglones alineados (descripción en varias líneas), sin pasar el artículo anterior
                up = [u for u in toks if abs(u[0] - x0) <= 20 and 0 < top - u[3] < 30]
                if not up or any(PRICE.match(u[4]) or u[4].lower().startswith(("sold by", "return", "buy it")) for u in up):
                    break
                top = min(u[1] for u in up)
            h = (y1 + 45) - (top - 15)
            box = (max(0, int(x0 - 1.15 * h)), max(0, top - 15), x0 - 8, min(img.height, y1 + 45))
            crop = img.crop(box)
            f = out / f"{name[:-4]}_p{k + 1}_{len(index):03d}.png"
            crop.save(f)
            ints = [u[4] for u in toks if box[0] <= u[0] and u[2] <= box[2] + 4 and box[1] <= u[1] and u[3] <= box[3] and re.fullmatch(r"\d{1,3}", u[4])]
            index.append({"i": len(index), "pdf": pdf, "doc": name, "page": k, "price": t[1:], "png": str(f),
                          "rect_pt": [v * 72 / DPI for v in box], "pp_ints": ints})
        print(f"{name} p{k + 1}: {sum(1 for e in index if e['doc'] == name and e['page'] == k)} fotos", flush=True)
(out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
# hoja de contacto: 6 columnas, celda 260x220 con etiqueta
cw, ch, cols = 260, 230, 6
sheets = [index[i:i + 36] for i in range(0, len(index), 36)]
for si, part in enumerate(sheets):
    rows = (len(part) + cols - 1) // cols
    sheet = Image.new("RGB", (cw * cols, ch * rows), "white")
    d = ImageDraw.Draw(sheet)
    for j, e in enumerate(part):
        im = Image.open(e["png"]); im.thumbnail((cw - 10, ch - 30))
        x, y = (j % cols) * cw, (j // cols) * ch
        sheet.paste(im, (x + 5, y + 25))
        d.text((x + 5, y + 5), f"#{e['i']} {e['doc'][:12]} ${e['price']}", fill="red")
    sheet.save(out / f"hoja_{si}.png")
print("total", len(index))
