"""Conjunto de calibración de la cantidad sobre la foto (Qwen2.5-VL): recorta, con el localizador de PRODUCCIÓN
(facturas_json.item_photos y la geometría guardada en ppocr.json), la foto de cada artículo de los PDF indicados. Guarda
cada recorte tal como se le manda al VL, un índice y hojas de contacto numeradas para fijar la verdad a mano en
verdad.json ({"i": cantidad}; 1 = sin número pintado). Sin modelos. Uso: fotos_verdad.py dir_salida lista.txt"""
import base64, json, sys
from pathlib import Path
import fitz
from PIL import Image, ImageDraw
sys.path.insert(0, r"C:\Users\LERIT\scaniaV4")
import facturas_json as f2

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
index = []
for p in [l.strip() for l in Path(sys.argv[2]).read_text().splitlines() if l.strip()]:
    fj = Path(p).parent / "ppocr.json"
    if not fj.exists():
        continue
    pages = json.loads(fj.read_text(encoding="utf-8"))
    f2.ppocr_tokens = lambda pdf, i, timeout=120: ([tuple(t) for t in pages[str(i)]["tokens"]], pages[str(i)]["w"], pages[str(i)]["h"])
    for k in range(len(pages)):
        for price, rect in f2.item_photos(p, k):
            i = len(index)
            area = (rect.width / 72) * (rect.height / 72)
            dpi = int(min(max(200, (1024 * 28 * 28 / area) ** 0.5), (4096 * 28 * 28 / area) ** 0.5))  # como photo_quantity
            with fitz.open(p) as d:
                d[k].get_pixmap(dpi=dpi, clip=rect, alpha=False).save(out / f"{i:03d}.png")
            index.append({"i": i, "pdf": p, "page": k, "price": price, "rect": [rect.x0, rect.y0, rect.x1, rect.y1]})
(out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
# hojas de contacto de 20 recortes (5 x 4), cada uno con su número
for s in range(0, len(index), 20):
    tiles = []
    for e in index[s:s + 20]:
        im = Image.open(out / f"{e['i']:03d}.png").convert("RGB")
        im.thumbnail((240, 240))
        t = Image.new("RGB", (250, 270), "white"); t.paste(im, (5, 25))
        ImageDraw.Draw(t).text((5, 5), f"#{e['i']} {Path(e['pdf']).name} p{e['page'] + 1} ${e['price']}", fill="red")
        tiles.append(t)
    sheet = Image.new("RGB", (250 * 5, 270 * ((len(tiles) + 4) // 5)), "gray")
    for n, t in enumerate(tiles):
        sheet.paste(t, (250 * (n % 5), 270 * (n // 5)))
    sheet.save(out / f"hoja_{s // 20:02d}.png")
print(len(index), "recortes")
