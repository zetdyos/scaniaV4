"""Fase 4: la Claude API en la LECTURA de facturas (contrato §9, autorización de WTCP del 2026-10-24; propuesta de SCANIA
en el contrato, «Propuesta de SCANIA del 2026-10-24»). En la clasificación SCANIA es solo un intermediario: aquí no.

Solo para las facturas que la fase 2 deja en revisión (motivo_revision distinto de "" y de DUPLICADO: un duplicado bien
detectado ya es la respuesta correcta). La Claude API recibe las páginas de esa factura (imagen) y el texto de GLM-OCR, y
propone líneas y cabecera. El modelo propone, el código comprueba (regla 4 de CLAUDE.md, «no inventar valores»):
  - cada importe (líneas, precio unitario usado, envío, impuestos, descuentos, otros cargos, subtotal, total) está
    impreso: lo leyó GLM-OCR o PP-OCRv6 en esas páginas (los mismos lectores de cifras que la fase 2);
  - cada PALABRA de la descripción la leyó GLM-OCR o PP-OCRv6 (los dos son lectores: no generan texto), con una letra de
    tolerancia en palabras de 5+ letras (error de lectura de un OCR); la fase 2 solo
    acepta GLM porque allí la descripción la escribe el 27B a partir de ese texto; aquí la escribe Claude mirando el papel
    y un lector la confirma (33733: GLM perdió «3 Ton Scissor Trolley Jack», PP-OCRv6 la leyó);
  - cantidad: entera ≥ 1; si es > 1, cantidad × precio unitario impreso = importe, o la cantidad está impresa junto
    a la descripción;
  - artículos + envío + impuestos + otros cargos − descuentos = total impreso (y = subtotal impreso si lo hay), al céntimo;
  - el modelo dice que la factura está completa.
Si todo se cumple, la factura sale SIN revisión con esas líneas (`ocr.origen_lineas = "claude"`; lo que había antes en
`ocr.fase4`). Si no, sigue en revisión y el `mismatch` añade la explicación del modelo, marcada como no verificada.

Configuración en `scania_config.json` -> `fase4` (se lee en cada factura, sin reiniciar): `activa` (true/false: el
interruptor; con false no se llama nunca, haya o no clave), `modelo` (por defecto claude-sonnet-5-5, el probado el
2026-10-23) y `precios_usd_por_millon` (precio de lista de la Claude API). La clave va en ANTHROPIC_API_KEY o en
anthropic_key.txt (fuera de git); activa sin clave = apagada, con aviso en el registro. Para pruebas, la variable
FASE4=cli usa el CLI de Claude Code en lugar de la API.

Coste: el de la Claude API, no el nuestro. Con la API, las fichas de entrada y salida que ella devuelve (`usage`) x el
precio de lista del modelo; con el CLI de pruebas, estimado con las mismas reglas de la API (imagen: ceil(ancho/28) x
ceil(alto/28) fichas, máximo 4784 por imagen; texto ~ caracteres/4; salida: las fichas que informa el CLI). Va al
registro del trabajo por factura y por documento, y a modelos.json (MODEL_RAW["claude"][].coste_api_usd)."""
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz
import requests

import divisas
import facturas_json as f2
import ocr_pipeline as p1

HERE = Path(__file__).resolve().parent
KEY_FILE = HERE / "anthropic_key.txt"
API_URL = "https://api.anthropic.com/v1/messages"
CONFIG_FILE = HERE / "scania_config.json"
MAX_FICHAS_IMAGEN = 4784  # tope de fichas por imagen de la API (alta resolución)
MAX_LADO = 2400  # px del lado mayor de cada página (alta resolución de la API: hasta 2576)
TIMEOUT = 300


def _clave():
    k = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
    return k


def config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("fase4") or {}
    except (OSError, ValueError):
        return {}


def modelo():
    return config().get("modelo") or "claude-sonnet-5-5"


def modo():
    """'api', 'cli' (solo pruebas, FASE4=cli) u 'off'. El interruptor es scania_config.json -> fase4.activa."""
    if (os.environ.get("FASE4") or "").strip().lower() == "cli":
        return "cli"
    if not config().get("activa"):
        return "off"
    return "api" if _clave() else "off"


def coste_api(fichas_entrada, fichas_salida, mod=None):
    """USD que cobra la Claude API por esas fichas, con el precio de lista de scania_config.json (None si no hay precio)."""
    pr = (config().get("precios_usd_por_millon") or {}).get(mod or modelo())
    if not pr:
        return None
    return round((fichas_entrada * pr["entrada"] + fichas_salida * pr["salida"]) / 1e6, 4)


def fichas_imagen(png):
    """Fichas de una imagen según la API: ceil(ancho/28) x ceil(alto/28), con el tope de la API."""
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")  # cabecera IHDR del PNG
    return min(-(-w // 28) * -(-h // 28), MAX_FICHAS_IMAGEN)


PROMPT = """You are reading ONE purchase invoice (or order summary) from scanned pages. The page images are the truth;
the OCR text below is a helper and can be wrong (missing columns, shifted numbers).

Rules:
- Copy descriptions EXACTLY as printed (same words, same order); do not translate or summarize.
- Amounts exactly as printed (digits only, dot decimal, no currency sign), e.g. "1234.50".
- Handwritten numbers and notes are NOT part of the invoice: ignore them. Items crossed out BY HAND are not declared on
  this sheet: leave them out.
- An item whose printed list price is struck through next to a lower price: use the lower (paid) price as the line
  amount and do not list that per-item saving as a discount.
- importe = the line total of the item (quantity x unit price when both are printed).
- Discounts/coupons/gift cards subtracted INSIDE the order summary before the printed total are discounts. Payments
  ("Paid", "Balance Due", card charges, "Amount Due") are NOT discounts and are not charges.
- subtotal = the printed subtotal of the ITEMS ONLY; "" if the only printed subtotal also includes shipping, tax or
  other charges.
- impuestos: one entry per printed tax amount (e.g. county tax and state tax separately), exactly as printed.
- completa = false if the item list is not complete on these pages (a page is missing, items cut off, illegible
  amounts) so that the printed totals cannot be reproduced from the items.
- explicacion: ONE short English sentence for a human reviewer saying what is wrong or missing on the paper (e.g. "page 1
  of 3 is missing: 101.98 of items are not in the file"), or "" if the invoice is complete and adds up.

Answer ONLY with this JSON (no text before or after):
{"lineas": [{"descripcion": "", "cantidad": 1, "precio_unitario": "", "importe": ""}],
 "subtotal": "", "envio": "", "impuestos": [{"concepto": "", "importe": ""}], "descuentos": [{"concepto": "", "importe": ""}],
 "otros_cargos": [{"concepto": "", "importe": ""}], "total": "", "completa": true, "explicacion": ""}
Use "" for values that are not printed.

OCR text of these pages:
"""


# ----------------------------------------------------------------------------- llamada al modelo

def _paginas_png(pdf, paginas):
    out = []
    with fitz.open(pdf) as doc:
        for i in range(paginas[0] - 1, paginas[1]):
            pg = doc[i]
            dpi = min(200, int(MAX_LADO / max(pg.rect.width, pg.rect.height) * 72))
            out.append(pg.get_pixmap(dpi=dpi, alpha=False).tobytes("png"))
    return out


def _llamar_api(pngs, texto):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(p).decode()}}
               for p in pngs]
    content.append({"type": "text", "text": PROMPT + texto})
    r = requests.post(API_URL, timeout=TIMEOUT, headers={"x-api-key": _clave(), "anthropic-version": "2023-06-01",
                                                          "content-type": "application/json"},
                      json={"model": modelo(), "max_tokens": 8000, "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    ent = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return ("".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"),
            {"fichas_entrada": ent, "fichas_salida": u.get("output_tokens", 0), "estimado": False})


def _llamar_cli(pngs, texto):
    with tempfile.TemporaryDirectory(prefix="fase4_") as tmp:
        for k, p in enumerate(pngs, 1):
            (Path(tmp) / f"pagina{k}.png").write_bytes(p)
        prompt = (f"Read the page images pagina1.png..pagina{len(pngs)}.png in this folder with the Read tool, then follow "
                  "these instructions.\n\n" + PROMPT + texto)
        r = subprocess.run(["claude", "-p", "--model", modelo(), "--allowedTools", "Read", "--output-format", "json"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=TIMEOUT,
                           shell=os.name == "nt")
        d = json.loads(r.stdout)
        salida = sum(v.get("outputTokens", 0) for k, v in (d.get("modelUsage") or {}).items() if "haiku" not in k)
        # lo que cobraría la API por la MISMA petición (no lo que gasta el CLI, que añade su propio contexto)
        ent = sum(fichas_imagen(p) for p in pngs) + len(PROMPT + texto) // 4
        return d.get("result", ""), {"fichas_entrada": ent, "fichas_salida": salida, "estimado": True}


def _respuesta(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    return json.loads(m.group(0)) if m else None


# ----------------------------------------------------------------------------- comprobación

def _cifra(v):
    """'1,234.50' / '$12' / 12.5 -> Decimal con céntimos, o None si no hay cifra."""
    t = re.sub(r"[^\d.\-]", "", str(v if v is not None else "").replace(",", ""))
    if not t or t in ("-", "."):
        return None
    try:
        return abs(Decimal(t)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def comprobar(p, leido):
    """Lista de motivos por los que la propuesta NO se acepta (vacía = aceptada) y las líneas normalizadas."""
    impresos, sq_text = leido.importes, leido.sq
    fallos, lineas = [], []
    if not p.get("completa"):
        fallos.append("the model says the item list is not complete")
    for l in p.get("lineas") or []:
        desc = str(l.get("descripcion") or "").strip()
        amt, unit = _cifra(l.get("importe")), _cifra(l.get("precio_unitario"))
        try:
            qty = int(Decimal(str(l.get("cantidad") or 1)))
        except (InvalidOperation, ValueError):
            qty = 0
        key = p1.squash(desc)[:20]
        if not desc or not leido.descripcion(desc):
            fallos.append(f"description not in the OCR text: «{desc[:40]}»")
        if amt is None or f"{amt}" not in impresos:
            fallos.append(f"amount {amt} of «{desc[:30]}» is not printed")
        if qty < 1:
            fallos.append(f"quantity of «{desc[:30]}» is not a whole number ≥ 1")
        elif qty > 1 and not ((unit is not None and f"{unit}" in impresos and amt is not None and qty * unit == amt)
                              or f2.qty_printed(qty, sq_text, key)):
            fallos.append(f"quantity {qty} of «{desc[:30]}» is not backed by a printed unit price or quantity")
        lineas.append({"desc": desc, "qty": max(qty, 1), "amount": f"{amt}" if amt is not None else "",
                       "unit": f"{unit}" if unit is not None and f"{unit}" in impresos else ""})
    if not lineas:
        fallos.append("no item lines")
    cab = {}
    for k in ("subtotal", "envio", "total"):
        v = _cifra(p.get(k))
        cab[k] = v
        if v is not None and v != 0 and f"{v}" not in impresos:
            fallos.append(f"{k} {v} is not printed")
    listas = {}
    imp = p.get("impuestos")
    if not isinstance(imp, list):  # una sola cifra en vez de la lista pedida
        imp = [{"concepto": "tax", "importe": imp}] if _cifra(imp) is not None else []
    p = dict(p, impuestos=imp)
    for k in ("impuestos", "descuentos", "otros_cargos"):
        listas[k] = []
        for c in p.get(k) or []:
            v = _cifra(c.get("importe"))
            if v is None or v == 0:
                continue
            if f"{v}" not in impresos:
                fallos.append(f"{k[:-1]} {v} is not printed")
            listas[k].append({"concepto": str(c.get("concepto") or "").strip(), "importe": f"{v}"})
    if cab["total"] is None or cab["total"] == 0:
        fallos.append("no printed total")
    if all(l["amount"] for l in lineas) and cab["total"]:
        suma = sum(Decimal(l["amount"]) for l in lineas)
        # el subtotal impreso puede ser el de artículos o el de artículos + envío + otros cargos antes de impuestos
        # (33726: fila sin etiqueta 17.50 + 28.32 = 45.82): vale cualquiera de los dos; Claude lo llama subtotal aunque el
        # prompt pida solo artículos
        antes_imp = suma + (cab["envio"] or 0) + sum(Decimal(c["importe"]) for c in listas["otros_cargos"])
        if cab["subtotal"] and cab["subtotal"] not in (suma, antes_imp):
            fallos.append(f"items {suma} do not match the printed subtotal {cab['subtotal']}")
        cuenta = (suma + (cab["envio"] or 0) + sum(Decimal(c["importe"]) for c in listas["impuestos"])
                  + sum(Decimal(c["importe"]) for c in listas["otros_cargos"])
                  - sum(Decimal(c["importe"]) for c in listas["descuentos"]))
        if cuenta != cab["total"]:
            fallos.append(f"items + charges - discounts = {cuenta}, printed total {cab['total']}")
    return fallos, lineas, cab, listas


# ----------------------------------------------------------------------------- aplicar a una factura

def _texto_factura(res, paginas):
    return divisas.invoice_text(res["markdown"], paginas) or res["markdown"]


class Leido:
    """Lo que leyeron GLM-OCR y PP-OCRv6 en las páginas de la factura: importes, texto aplastado y palabras. Los dos son
    lectores (no generan texto): una cifra o una descripción que propone Claude cuenta como impresa si la leyó alguno."""

    def __init__(self, res, paginas, texto, log):
        self.importes = f2.doc_amounts(texto) | f2.exact_cents(texto)
        self.sq = _sq_text(texto)
        textos = [texto]
        try:
            pp = f2.ppocr_text(res["pdf"], list(range(paginas[0] - 1, paginas[1])), log)
            self.importes |= f2.doc_amounts(pp) | f2.exact_cents(pp)
            self.sq += "|" + _sq_text(pp)
            textos.append(pp)
        except Exception:  # noqa: BLE001  sin PP-OCRv6 cuenta solo lo que leyó GLM
            pass
        import html
        self.palabras = {w for t in textos for w in _palabras(html.unescape(re.sub(r"<[^>]+>", " ", t)))}

    def descripcion(self, desc):
        """Cada palabra de la descripción la leyó un OCR (en el orden que sea: el código de pieza y el nombre pueden ir
        en renglones distintos, 33701), con UNA letra de tolerancia en palabras de 5+ letras (PP-OCRv6 «Tralley» por
        «Trolley», 33733)."""
        ws = _palabras(desc)
        if not ws:
            return False
        for w in ws:
            if w in self.palabras or w in self.sq:
                continue
            if len(w) >= 5 and any(len(o) == len(w) and sum(a != b for a, b in zip(o, w)) <= 1 for o in self.palabras):
                continue
            return False
        return True


def _palabras(t):
    return [w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) >= 2]


def _sq_text(texto):
    import html
    t = html.unescape(re.sub(r"<[^>]+>", " ", texto))
    return (p1.squash(texto) + "|" + p1.squash(t) + "|"
            + p1.squash(re.sub(r"\(?-?\s*(?:US)?\$\s*\d[\d,]*(?:\.\d{2})?\)?", " ", t)))


def resolver(doc, res, log, llamar=None):
    """Intenta resolver UNA factura en revisión. Devuelve True si la dejó sin revisión."""
    paginas = (doc.get("ocr") or {}).get("paginas") or [1, res.get("n_pages") or 1]
    texto = _texto_factura(res, paginas)
    llamar = llamar or (_llamar_api if modo() == "api" else _llamar_cli)
    t0 = time.time()
    try:
        crudo, uso = llamar(_paginas_png(res["pdf"], paginas), texto)
        p = _respuesta(crudo)
    except Exception as e:  # noqa: BLE001  sin red, sin saldo, CLI ausente: la factura sigue como estaba
        log(f"  fase 4: factura {doc['id']}: Claude no respondió ({type(e).__name__}: {str(e)[:120]})")
        return False
    coste = coste_api(uso.get("fichas_entrada", 0), uso.get("fichas_salida", 0))
    f2.MODEL_RAW.setdefault("claude", []).append({"factura": doc["id"], "paginas": paginas, "modelo": modelo(),
                                                  "respuesta": crudo, "uso": uso, "coste_api_usd": coste,
                                                  "segundos": round(time.time() - t0, 1)})
    log(f"  fase 4: factura {doc['id']}: Claude API {modelo()}, {uso.get('fichas_entrada', 0)} fichas de entrada + "
        f"{uso.get('fichas_salida', 0)} de salida = {coste if coste is not None else '?'} USD"
        + (" (estimado)" if uso.get("estimado") else ""))
    if not isinstance(p, dict):
        log(f"  fase 4: factura {doc['id']}: respuesta sin JSON")
        return False
    leido = Leido(res, paginas, texto, log)
    fallos, lineas, cab, listas = comprobar(p, leido)
    explicacion = str(p.get("explicacion") or "").strip()
    if fallos:
        log(f"  fase 4: factura {doc['id']} sigue en revisión: " + "; ".join(fallos[:4]))
        if explicacion:
            note = f"AI reading (unverified): {explicacion}"
            doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"], note]))
            doc["ocr"]["incidencias"].append(note)
        return False
    divisa = next((l.get("divisa") for l in doc["lineas"] if l.get("divisa")), "") or "USD"
    o = doc["ocr"]
    o["fase4"] = {"modelo": modelo(), "antes": {"motivo_revision": doc["motivo_revision"], "mismatch": doc["mismatch"],
                                             "total": doc["total"], "lineas": len(doc["lineas"])}}
    nuevas = []
    for i, l in enumerate(lineas):
        ev = {"descripcion": "leido", "importe_linea": "leido", "precio_unitario": "leido" if l["unit"] else "",
              "cantidad": "leido" if f2.qty_printed(l["qty"], leido.sq, p1.squash(l["desc"])[:20])
              else "aritmetica" if l["qty"] > 1 else "asumido"}
        ocr = {"precio_unitario": l["unit"] or f"{Decimal(l['amount']) / l['qty']:.2f}", "cantidad_impresa": ev["cantidad"] == "leido",
               "vendedor": "", "seguimiento": "", "estado_envio": "", "condicion": "", "pagina": paginas[0],
               "origen": "claude", "verificado": True, "evidencia": ev}
        line = f2.goods_line(i, {"desc": l["desc"], "qty": l["qty"], "amount": l["amount"]}, divisa, ocr)
        line["estimacion"] = None
        nuevas.append(line)
    doc["lineas"] = nuevas
    o["subtotal_articulos"] = f"{sum(Decimal(l['amount']) for l in lineas)}"
    o["envio"] = f"{cab['envio']}" if cab["envio"] is not None else ""
    o["impuestos"] = f"{sum(Decimal(c['importe']) for c in listas['impuestos'])}" if listas["impuestos"] else ""
    o["descuentos"], o["other_charges"] = listas["descuentos"], listas["otros_cargos"]
    o["origen_lineas"] = "claude"
    o["incidencias"] = []
    doc.update(motivo_revision="", mismatch="", total=f"{cab['total']}", total_is_printed=True)
    log(f"  fase 4: factura {doc['id']} resuelta por Claude ({len(nuevas)} líneas, total {doc['total']}): sin revisión")
    return True


def aplicar(out, res, log, llamar=None):
    """Recorre las facturas del documento y pasa por la Claude API las que están en revisión (no DUPLICADO)."""
    if llamar is None and modo() == "off":
        if config().get("activa") and not _clave():
            log("Fase 4 activa en scania_config.json pero sin clave de la Claude API: no se usa")
        return 0
    candidatas = [d for d in out if d.get("motivo_revision") and d["motivo_revision"] != "DUPLICADO"]
    if not candidatas:
        return 0
    log(f"Fase 4 (Claude, {modo() if llamar is None else 'prueba'}): {len(candidatas)} factura(s) en revisión")
    antes = len(f2.MODEL_RAW.get("claude", []))
    n = sum(1 for d in candidatas if resolver(d, res, log, llamar))
    costes = [c.get("coste_api_usd") or 0 for c in f2.MODEL_RAW.get("claude", [])[antes:]]
    log(f"Fase 4: {n} de {len(candidatas)} resuelta(s); coste Claude API del documento {sum(costes):.4f} USD")
    return n
