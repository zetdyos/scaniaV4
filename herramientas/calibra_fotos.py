"""Mide Qwen2.5-VL (:8088) en la cantidad sobre la foto con el conjunto de fotos_verdad.py (index.json + verdad.json).
Cada variante manda el recorte tal como lo haría producción; se guarda cada respuesta cruda (respuestas_<variante>.json)
para no repetir llamadas. Uso: calibra_fotos.py dir variante [variante...]
Variantes: prod (PHOTO_QTY_PROMPT y esquema de producción)."""
import base64, json, sys, time
from pathlib import Path
sys.path.insert(0, r"C:\Users\LERIT\scaniaV4")
import facturas_json as f2

D = Path(sys.argv[1])
index = json.loads((D / "index.json").read_text(encoding="utf-8"))
truth = {int(k): v for k, v in json.loads((D / "verdad.json").read_text(encoding="utf-8")).items()}

VARIANTS = {  # (prompt, esquema, zona del recorte: None = foto entera; (x, y) = esquina inferior derecha desde x, y relativos)
    "prod": (f2.PHOTO_QTY_PROMPT, f2.PHOTO_QTY_SCHEMA, None),
    "esquina50": (f2.PHOTO_QTY_PROMPT, f2.PHOTO_QTY_SCHEMA, (0.5, 0.5)),
    "esquina40": (f2.PHOTO_QTY_PROMPT, f2.PHOTO_QTY_SCHEMA, (0.6, 0.6)),
    # prompt de OCR del cookbook oficial de Qwen2.5-VL (texto libre): cantidad = renglón que es SOLO un entero 1-999
    "ocr": ("Read all the text in the image.", None, None),
    "ocr_esquina50": ("Read all the text in the image.", None, (0.5, 0.5)),
    "ocr_esquina40": ("Read all the text in the image.", None, (0.6, 0.6)),
}


def qty_of(raw, schema):
    import re
    if schema:
        return json.loads(raw).get("quantity")
    ints = [int(l.strip()) for l in raw.splitlines() if re.fullmatch(r"\s*\d{1,3}\s*", l) and 1 <= int(l) <= 999]
    return ints[-1] if len(set(ints)) == 1 else (1 if not ints else None)


def image(i, zone):
    if zone is None:
        return (D / f"{i:03d}.png").read_bytes()
    import fitz
    e = index[i]
    r = fitz.Rect(*e["rect"])
    z = fitz.Rect(r.x0 + zone[0] * r.width, r.y0 + zone[1] * r.height, r.x1, r.y1)
    area = (z.width / 72) * (z.height / 72)
    dpi = int(min(max(200, (1024 * 28 * 28 / area) ** 0.5), (4096 * 28 * 28 / area) ** 0.5))
    with fitz.open(e["pdf"]) as d:
        return d[e["page"]].get_pixmap(dpi=dpi, clip=z, alpha=False).tobytes("png")


def ask(i, prompt, schema, zone):
    img = base64.b64encode(image(i, zone)).decode("ascii")
    rj = f2.post_json(f"{f2.VL_SERVER}/v1/chat/completions", {
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                                                 {"type": "text", "text": prompt}]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "cache_prompt": False, "max_tokens": 20 if schema else 300,
        **({"response_format": {"type": "json_schema", "json_schema": {"name": "q", "schema": schema}}} if schema else {})}, 120)
    return rj["choices"][0]["message"]["content"]


for v in sys.argv[2:]:
    prompt, schema, zone = VARIANTS[v]
    cache_f = D / f"respuestas_{v}.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    t0 = time.time()
    for i in truth:
        if str(i) not in cache:
            cache[str(i)] = ask(i, prompt, schema, zone)
    cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    ok, fp, fn, wrong = 0, [], [], []
    for i, t in truth.items():
        q = qty_of(cache[str(i)], schema)
        if q == t:
            ok += 1
        elif t == 1:
            fp.append((i, q))
        elif q == 1:
            fn.append((i, t))
        else:
            wrong.append((i, t, q))
    print(f"{v}: {ok}/{len(truth)} bien ({time.time() - t0:.0f}s) | número sin haberlo (falso +): {fp} | "
          f"número no visto: {fn} | número distinto: {wrong}")
