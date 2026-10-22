"""Mediciones en sombra sobre los envíos de WTCP (petición del usuario 2026-10-21: «haz todas las mediciones sobre la
marcha»). No cambia nada de lo que recibe WTCP: corre en otro proceso, tras cada trabajo terminado, y guarda lo suyo en
demo_jobs/JOB/sombra.json. Dos mediciones:
  - prompt v3: la fase 2 con el prompt nuevo (descripción no SKU, artículos pendientes no son mercancía, cantidad =
    unidades compradas, importe tras promociones, pagos nunca descuento, total = valor del pedido) frente al resultado
    de producción;
  - imagen: el 27B lee cada factura CON la imagen de la página (como el segundo intento de producción, pero siempre):
    sus cantidades frente a las de producción, para saber si inventa cantidades > 1 (fiabilidad, 32487).
Fase 1, PP-OCRv6 y el VL salen de modelos.json; el 27B se llama de verdad. Cada llamada a un modelo toma
facturas_json.modelos_lock (la API también): los trabajos de WTCP se intercalan entre llamadas, nunca a la vez.
Uso: sombra.py [JOB_ID ...]   (sin lista: vigila la API y mide cada trabajo nuevo)"""
import json, sys, time, urllib.request
from decimal import Decimal
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4"); sys.path.insert(0, str(REPO))
import facturas_json as f2
import ocr_pipeline as p1

USER_PROMPT_PROD = f2.USER_PROMPT
USER_PROMPT_V3 = (
    "OCR text of the document (Markdown; HTML tables are regions detected separately):\n\n{doc}\n\n"
    "Return every line of the document that carries an amount: each purchased item AND each amount of "
    "the order summary or totals section (in whatever language it is printed), with: descripcion (the product name or "
    "description as printed; when the document prints both an item code or SKU and a description, the description), "
    "cantidad (number of units bought as printed, or \"1\" when a single unit is implied; not the pack size or other "
    "numbers that are part of the product name), importe_linea (the amount printed for that line, after any promotion "
    "applied to it; when the line shows both an amount before tax and a total that includes tax, use the amount before tax), "
    "divisa (ISO 4217 code; use USD for $), precio_unitario (unit price if printed, else \"\"), vendedor "
    "(seller name after 'Sold by', else \"\"), seguimiento (tracking number printed for that item, else \"\"), "
    "estado_envio (delivery status text printed for that item such as 'Delivered October 8', else \"\"), "
    "line_kind (goods if the line is a product or service being purchased and charged in this document; otherwise the "
    "kind of amount it is: shipping, tax, discount, subtotal_or_total, payment for money paid or still due (amount "
    "charged to a payment method, gift card, store credit or prepaid balance, balance due; a payment is never a "
    "discount), or other_charges for any other charge that is not a purchased item). Items listed as back-ordered or "
    "not yet shipped that are not charged in this document are not returned. Also return total (the value of the "
    "order as printed, before payments; the price of a single item is not the order total) and total_is_printed (true "
    "only if that total is printed in the document). For cantidad, importe_linea, precio_unitario and total also "
    "return the value as a number (cantidad_valor, importe_valor, precio_unitario_valor, total_valor), reading the "
    "decimal and thousands separators the way this document uses them; 0 when the value is not printed.")

API = "http://127.0.0.1:8091/api"
RAW = {}


def get(url, t=5):
    with urllib.request.urlopen(url, timeout=t) as r:
        return json.load(r)


def api_idle():
    try:
        h = get(f"{API}/health")
        return h.get("jobs_running", 1) == 0 and h.get("jobs_queued", 1) == 0
    except Exception:  # noqa: BLE001
        return False


# Cada llamada a un modelo: esperar a que la API no tenga trabajo y tomar el bloqueo de los modelos.
real_post = f2.post_json


def locked_post(*a, **k):
    while not api_idle():
        time.sleep(2)
    with f2.modelos_lock(log=lambda m: None):
        return real_post(*a, **k)


f2.post_json = locked_post


def fake_ppocr(pdf, page_idx, timeout=120):
    pg = RAW.get("ppocr", {}).get(str(page_idx))
    if pg is None:
        raise RuntimeError("sin PP-OCRv6 guardado")
    return [tuple(t) for t in pg["tokens"]], pg["w"], pg["h"]


real_photo = f2.photo_quantity


def fake_photo(pdf, page_idx, rect, server, timeout):
    key = f"{page_idx}:{rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f}"
    answers = [RAW.get("vl_foto", {}).get(key), RAW.get("vl_foto", {}).get(key + ":esquina")]
    if answers[0] is None:
        return real_photo(pdf, page_idx, rect, server, timeout)  # foto que producción no preguntó: VL real
    it = iter(answers)
    saved, img = f2.post_json, f2._vl_image
    f2.post_json = lambda *a, **k: {"choices": [{"message": {"content": next(it) or ""}}]}
    f2._vl_image = lambda *a: ""
    try:
        return real_photo(pdf, page_idx, rect, server, timeout)
    finally:
        f2.post_json, f2._vl_image = saved, img


real_vl = f2.lines_with_vl


def fake_vl(pdf, pages, server, timeout, log):
    if not all(str(i) in RAW.get("vl_pagina", {}) for i in pages):
        return real_vl(pdf, pages, server, timeout, log)
    saved = f2.post_json
    it = iter([RAW["vl_pagina"][str(i)] for i in pages])
    f2.post_json = lambda *a, **k: {"choices": [{"message": {"content": next(it)}}]}
    try:
        return real_vl(pdf, pages, server, timeout, log)
    finally:
        f2.post_json = saved


real_27b = f2._lines_with_llm


def saved_or_real_27b(markdown, server, timeout, log, image=None):
    """Con el prompt de producción, la respuesta guardada si la hay (misma entrada, temperatura 0); si no, el 27B real."""
    if f2.USER_PROMPT == USER_PROMPT_PROD:
        r = RAW.get("27b_imagen" if image else "27b", {}).get(f2.md_key(markdown))
        if r is not None:
            return json.loads(r["content"], parse_float=Decimal), ("model output limit reached" if r.get("finish_reason") == "length" else "")
    return real_27b(markdown, server, timeout, log, image)


f2._lines_with_llm = saved_or_real_27b
f2.ppocr_tokens, f2.photo_quantity, f2.lines_with_vl = fake_ppocr, fake_photo, fake_vl
p1.server_ok = lambda url: True


def res_of(d):
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    md = (d / "ocr.md").read_text(encoding="utf-8")
    pages = f2.page_texts(md)
    n = max(pages) if pages else 0
    full = {i: pages[i + 1].split("\n", 2)[2].split("\n### Tablas detectadas")[0].split("\n### Verificación de importes")[0].strip()
            for i in range(n)}
    return meta, {"pdf": str(d / meta["pdf"]), "n_pages": n, "markdown": md, "full_text": full,
                  "region_pages": RAW.get("fase1_regiones") or [[] for _ in range(n)]}


def summary(invs):
    return [{"ref": i.get("referencia"), "rev": i["motivo_revision"], "total": i["total"], "mismatch": i["mismatch"][:300],
             "goods": [(l["cantidad"], l["importe_linea"], l["descripcion"][:60]) for l in i["lineas"] if l["line_kind"] == "goods"]}
            for i in invs]


def measure(job):
    d = REPO / "demo_jobs" / job
    if not (d / "modelos.json").exists() or not (d / "result.json").exists():
        return None
    RAW.clear(); RAW.update(json.loads((d / "modelos.json").read_text(encoding="utf-8")))
    meta, res = res_of(d)
    prod = summary(json.loads((d / "result.json").read_text(encoding="utf-8")))
    out = {"pdf": meta["pdf"], "produccion": prod}
    # 1) imagen siempre: el 27B con la imagen de cada página, prompt de producción, para cada factura
    f2.USER_PROMPT = USER_PROMPT_PROD
    f2._LLM_CACHE.clear()
    orig_build = f2.build_invoice
    img_docs, done = [], set()

    def build_with_image(*a, **k):
        doc, src = orig_build(*a, **k)
        key = tuple(a[2]["pages"]) if len(a) > 2 else None
        if not k.get("img") and not k.get("unit_alt") and k.get("vl") is False and src == "llm" and key not in done:
            done.add(key)
            alt, _ = orig_build(*a, **dict(k, img=True))
            img_docs.append({"paginas": alt["ocr"]["paginas"], "rev": alt["motivo_revision"], "total": alt["total"],
                             "mismatch": alt["mismatch"][:300],
                             "goods": [(l["cantidad"], l["importe_linea"], l["descripcion"][:60]) for l in alt["lineas"] if l["line_kind"] == "goods"]})
        return doc, src
    f2.build_invoice = build_with_image
    try:
        f2.build_all(res, log=lambda m: None)
    finally:
        f2.build_invoice = orig_build
    out["imagen"] = img_docs
    # 2) prompt v3: la fase 2 entera con el prompt nuevo (27B real, texto y, si no cuadra, imagen)
    f2.USER_PROMPT = USER_PROMPT_V3
    f2._LLM_CACHE.clear()
    try:
        out["v3"] = summary(f2.build_all(res, log=lambda m: None))
    finally:
        f2.USER_PROMPT = USER_PROMPT_PROD
    (d / "sombra.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def report(job, out):
    t = time.strftime("%H:%M:%S")
    for k, (a, b) in enumerate(zip(out["produccion"], out["v3"])):
        if a != b:
            print(f"{t} SOMBRA v3 {out['pdf']}#{k}: {a['rev'] or 'ok'} {a['total']} -> {b['rev'] or 'ok'} {b['total']}"
                  f" | prod {a['goods'][:4]} | v3 {b['goods'][:4]} | {b['mismatch'][:150]}", flush=True)
    if len(out["produccion"]) != len(out["v3"]):
        print(f"{t} SOMBRA v3 {out['pdf']}: facturas {len(out['produccion'])} -> {len(out['v3'])}", flush=True)
    prod_q = [(q, d) for inv in out["produccion"] for q, _, d in inv["goods"]]
    for im in out["imagen"]:
        mas = [(q, a, d[:40]) for q, a, d in im["goods"] if str(q).strip() not in ("", "1")]
        if mas:
            print(f"{t} SOMBRA imagen {out['pdf']} págs {im['paginas']}: cantidades > 1 del 27B con imagen {mas} "
                  f"(producción: {[(q, d[:30]) for q, d in prod_q if str(q).strip() not in ('', '1')]}); "
                  f"{im['rev'] or 'ok'} {im['total']}", flush=True)
    print(f"{t} SOMBRA LISTO {out['pdf']} ({job})", flush=True)


if __name__ == "__main__":
    if sys.argv[1:]:
        for job in sys.argv[1:]:
            o = measure(job)
            if o:
                report(job, o)
        sys.exit(0)
    seen = {j["id"] for j in get(f"{API}/jobs") if j["state"] in ("done", "error")}
    print(time.strftime("%H:%M:%S"), f"sombra vigilando ({len(seen)} trabajos anteriores no se miden)", flush=True)
    while True:
        try:
            for j in sorted(get(f"{API}/jobs"), key=lambda j: j["id"]):
                if j["state"] == "done" and j["id"] not in seen:
                    seen.add(j["id"])
                    try:
                        o = measure(j["id"])
                        if o:
                            report(j["id"], o)
                    except Exception as e:  # noqa: BLE001
                        print(time.strftime("%H:%M:%S"), f"SOMBRA ERROR {j['pdf']}: {type(e).__name__}: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(time.strftime("%H:%M:%S"), f"SOMBRA API: {e}", flush=True)
        time.sleep(10)
