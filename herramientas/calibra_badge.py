"""Calibración de Qwen2.5-VL-7B (:8088) para LO ÚNICO que necesitamos de él: la cantidad impresa sobre la foto del artículo.
Entrada: recortes de la foto (badge_crops.py) + verdad fijada a mano (verdad.json: {"i": cantidad}; 1 = sin número).
Variantes documentadas:
  prompt  ocr = "Read all the text in the image." (prompt de OCR del cookbook de Qwen2.5-VL) -> enteros sueltos;
          q   = pregunta cerrada con esquema JSON {"quantity": int}.
  resol.  r0  = recorte a 200 dpi (tal cual);  r1 = recorte re-renderizado desde el PDF a >= 1024 fichas de imagen
          (aviso de llama.cpp clip.cpp / issue 16842: Qwen-VL necesita >= 1024 fichas; tope 4096).
Uso: calibra_badge.py dir_recortes"""
import base64, json, re, sys
from pathlib import Path
import fitz

REPO = Path(r"C:\Users\LERIT\scaniaV4")
sys.path.insert(0, str(REPO))
import facturas_json as f2

S = f2.VL_SERVER
D = Path(sys.argv[1])
index = json.loads((D / "index.json").read_text(encoding="utf-8"))
truth = {int(k): v for k, v in json.loads((D / "verdad.json").read_text(encoding="utf-8")).items()}
Q = ("This is the photo of one purchased item from an order page. If a small number badge printed on the photo shows "
     "how many units were bought, return that number as quantity. If there is no such badge, return 1.")
QS = {"type": "object", "properties": {"quantity": {"type": "integer"}}, "required": ["quantity"]}


def png(e, min_tokens):
    if not min_tokens:
        return base64.b64encode(Path(e["png"]).read_bytes()).decode()
    doc = fitz.open(e["pdf"]); pg = doc[e["page"]]
    rect = fitz.Rect(*e["rect_pt"])
    area = (rect.width / 72) * (rect.height / 72)
    dpi = int(max(200, (min_tokens * 28 * 28 / area) ** 0.5))
    dpi = min(dpi, int((4096 * 28 * 28 / area) ** 0.5))
    return base64.b64encode(pg.get_pixmap(dpi=dpi, clip=rect, alpha=False).tobytes("png")).decode()


def ask(img, prompt, schema=None):
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                                                     {"type": "text", "text": prompt}]}],
            "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "max_tokens": 200}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "q", "schema": schema}}
    return f2.post_json(f"{S}/v1/chat/completions", body, 120)["choices"][0]["message"]["content"]


def from_ocr(txt):
    ints = [int(x) for x in re.findall(r"(?<![\w.,$])\d{1,3}(?![\w.,%])", txt)]
    return ints[0] if len(ints) == 1 else (1 if not ints else None)  # varios números: ambiguo


V = {"ocr_r0": ("ocr", 0), "ocr_r1": ("ocr", 1024), "q_r0": ("q", 0), "q_r1": ("q", 1024)}
ok = {v: 0 for v in V}; bad = {v: [] for v in V}; amb = {v: 0 for v in V}
pp_ok = 0
with f2.modelos_lock(lambda m: None):
    for e in index:
        if e["i"] not in truth:
            continue
        t = truth[e["i"]]
        pp = int(e["pp_ints"][0]) if len(e["pp_ints"]) == 1 else 1
        pp_ok += pp == t
        line = f"#{e['i']} {e['doc']} ${e['price']} verdad={t} pp={e['pp_ints']}"
        for v, (kind, mt) in V.items():
            img = png(e, mt)
            if kind == "ocr":
                raw = ask(img, "Read all the text in the image.")
                got = from_ocr(raw)
            else:
                raw = ask(img, Q, QS)
                got = json.loads(raw).get("quantity")
            if got is None:
                amb[v] += 1
            elif got == t:
                ok[v] += 1
            else:
                bad[v].append(e["i"])
            line += f" | {v}={got}" + (f" «{raw[:40]!r}»" if kind == "ocr" and got != t else "")
        print(line, flush=True)
n = len(truth)
print(f"\nTOTAL {n} fotos (con cantidad >1: {sum(1 for v in truth.values() if v > 1)}). PP-OCRv6 solo: {pp_ok}")
for v in V:
    print(f"  {v}: aciertos {ok[v]}  errores {len(bad[v])} {bad[v]}  ambiguos {amb[v]}")
