"""
Fase 2: estandariza la salida del OCR (fase 1, ocr_pipeline.run) en JSON con el esquema acordado
(contrato con WTCP: docs/CONTRATO-WTCP.md). No clasifica: los campos arancelarios quedan con su forma
y sin valor para la fase de clasificación posterior: codigo, hs6, estado de la línea y
confianza_modelo = "", candidatos = [], confianza / canal / calibrado = null, version_catalogo = "".

Unidad de salida: la FACTURA. Un PDF puede contener varias facturas seguidas. Por cada PDF se
escribe <salida>/<nombre>.json con un array de facturas. Qué es una factura:
  - Amazon formato de escritorio ("Details for Order"): CADA BLOQUE DE ENVÍO ("Not Yet Shipped",
    "Shipped on ...", con su "Total for This Shipment") es una factura. Los datos comunes del
    pedido (número, fecha, pago, dirección, total del pedido) se repiten en cada una.
  - Amazon app móvil (un pedido por página, un solo total) y Temu: una factura por pedido.
El inicio de una factura se detecta por marcadores en el texto de la página:
  - Amazon: "Details for Order #NNN-NNNNNNN-NNNNNNN" o "Amazon.com order number: ...";
  - Temu:   "Order ID: PO-...".
Las páginas sin marcador se agregan a la factura en curso (pedidos de varias páginas). El campo
`id` es secuencial dentro del PDF (0, 1, 2, ...). Se añade `referencia` (número de pedido leído),
que NO está en el esquema original: es la clave natural para cruzar con otros sistemas.

De dónde salen las líneas de cada factura:
  - Si sus páginas tienen bloques de envío tipo Amazon ("N of: ... $precio", verificados con
    cantidad x precio = total del envío), las líneas salen de ahí sin modelo de lenguaje.
  - Si no, el Markdown de CADA PÁGINA se pasa por separado a Qwen3.6-27B (llama-server, puerto 8083 del
    host de modelos) con esquema JSON impuesto por gramática, pensamiento desactivado
    (chat_template_kwargs.enable_thinking=false) y temperatura 0. max_tokens = n_ctx (/props) - fichas
    del prompt (/tokenize): la salida no crece con el número de páginas. Si una respuesta se corta
    (finish_reason "length") o no es JSON, esa página queda para revisión (LECTURA), nunca un error del
    trabajo. Cada importe devuelto se contrasta con los que leyó el OCR.
  - Si la suma de líneas no reproduce el total impreso, las páginas de la factura van como imagen
    a Qwen2.5-VL (puerto 8088) con el patrón de extracción clave-valor del cookbook de Qwen. Esas
    líneas solo se aceptan si cantidad x precio reproduce el total impreso.

Campos adicionales (objeto `ocr`, anidado para no alterar las claves del esquema):
  - factura.ocr: proveedor, fuente_pdf, paginas, fecha_pedido, subtotal_articulos, envio,
    impuestos, total_antes_impuestos, descuentos[] ({concepto, importe}, importe en positivo),
    other_charges[] (cargos que no encajan en las categorías anteriores; misma forma). En la vía del
    27B, envio / impuestos / descuentos / other_charges que las etiquetas no encuentran se toman de
    las líneas que el 27B clasificó así, solo si su importe está literal en el OCR,
    metodo_pago, destinatario, direccion_envio, origen_lineas, envios[] (bloques de envío
    verificados), incidencias[] (las mismas notas que mismatch, en inglés).
  - linea.ocr: precio_unitario, cantidad_impresa (false si se asumió 1), vendedor, seguimiento,
    estado_envio, condicion, pagina, origen, verificado (cuadró aritméticamente) y evidencia.
  - linea.ocr.evidencia: origen de CADA valor: "leido" (literal en el texto OCR), "aritmetica"
    (cantidad > 1 no impresa; sin incidencia si la sostiene la propia línea: precio e importe literales
    y cantidad x precio = importe; con incidencia si solo la sostiene el cuadre con el total),
    "calculado" (cantidad x precio), "asumido" (cantidad 1 no impresa; sin incidencia si cuadra)
    o "modelo" (sin respaldo: la línea no se da por verificada).
  Reglas duras para lo que devuelve un modelo: un artículo del VL sin descripción en el OCR se
  descarta; las líneas del VL solo se aceptan si todos sus precios unitarios están literalmente en
  el OCR y cantidad x precio reproduce el subtotal impreso. La cantidad es lo único que puede
  sostenerse solo por aritmética (el "x2" casi nunca se lee) y queda marcada como tal.

Convenciones del esquema:
  - importe_linea: importe total de la línea (cantidad x precio unitario cuando el documento
    imprime precio unitario). divisa: ISO 4217 (tres letras); "$" se registra como "USD"; lo que
    devuelva un modelo fuera de ese formato se descarta.
  - estado "DONE" al terminar. motivo_revision (lo decide la lectura, por prioridad):
    "ESTRUCTURA" si la factura no tiene marcador de pedido ni líneas (no parece una factura de
    mercadería), "LECTURA" si hubo incidencias de lectura, "" si no. "CLASIFICACION" queda
    reservado para la fase de clasificación. mismatch: discrepancias o notas de rescate, en
    inglés (lo muestra WTCP tal cual a quien revisa), o "".
  - total_is_printed: true si el total se leyó impreso; false si se sumó de las líneas.
  - result_version: versión de este formato (1).

Uso:
    .venv/bin/python facturas_json.py doc1.pdf doc2.pdf -o salida/ [--md-dir salida_md]
"""

import argparse
import base64
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
import requests

import ocr_pipeline as p1

RESULT_VERSION = 1
QWEN27_SERVER = f"http://{p1.MODELS_HOST}:8083"
VL_SERVER = p1.QWEN_SERVER  # Qwen2.5-VL-7B, puerto 8088

# Marcadores de inicio de factura: (tipo, regex con grupo 1 = referencia). Tolerantes a espacios
# perdidos por el OCR.
INVOICE_START = [
    ("amazon", re.compile(r"Details\s*for\s*Order\s*#?\s*(\d{3}-\d{7}-\d{7})", re.I)),
    ("amazon", re.compile(r"Amazon\.com\s*order\s*number:?\s*(\d{3}-\d{7}-\d{7})", re.I)),
    ("amazon", re.compile(r"Order\s*#\s*:?\s*(\d{3}-\d{7}-\d{7})", re.I)),
    ("temu", re.compile(r"Order\s*ID:?\s*(PO-\d{3}-\d{8,})", re.I)),
]
# Importes: 1,234.56 | 26,99 (coma decimal, capturas de la app) | 17,- (sin céntimos)
AMOUNT_TXT = r"-?\s*\d[\d.,]*(?:[.,]\d{2}|,-)"
ORDER_TOTAL_RE = re.compile(r"(?:Order|Grand)\s*Total:?\s*(?:USD|\$)?\s*(" + AMOUNT_TXT + ")", re.I)
ITEMS_SUBTOTAL_RE = re.compile(r"Item\(?s?\)?\s*Subtotal:?\s*(?:USD|\$)?\s*(" + AMOUNT_TXT + ")", re.I)
# Sin marca de moneda: con separador de miles (2,821.92) o sin él (2821.92).
AMOUNT_RE = re.compile(r"(?:USD|\$)\s*(" + AMOUNT_TXT + r")|(?<![\d.,])(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d{4,}[.,]\d{2})(?![\d.,])")

# Clase de cada línea que devuelve el 27B: el modelo de datos del proyecto, no una lista de palabras. Solo "goods"
# es mercadería (lo que WTCP declara); "other_charges" recoge cualquier cargo que no encaje en las demás;
# "payment" es dinero pagado o pendiente (tarjeta cobrada, saldo), que no es un cargo y no entra en ninguna cuenta.
LINE_KINDS = ["goods", "shipping", "tax", "discount", "subtotal_or_total", "other_charges", "payment"]
LINES_SCHEMA = {
    "type": "object",
    "properties": {
        "lineas": {"type": "array", "items": {
            "type": "object",
            "properties": {"descripcion": {"type": "string"}, "cantidad": {"type": "string"},
                           "importe_linea": {"type": "string"}, "divisa": {"type": "string"},
                           "precio_unitario": {"type": "string"}, "vendedor": {"type": "string"},
                           "seguimiento": {"type": "string"}, "estado_envio": {"type": "string"},
                           "line_kind": {"type": "string", "enum": LINE_KINDS}},
            "required": ["descripcion", "cantidad", "importe_linea", "divisa",
                         "precio_unitario", "vendedor", "seguimiento", "estado_envio", "line_kind"]}},
        "total": {"type": "string"},
        "total_is_printed": {"type": "boolean"},
    },
    "required": ["lineas", "total", "total_is_printed"],
}
SYSTEM_PROMPT = ("You extract purchase line items from the OCR text of invoices and receipts. "
                 "Copy every value exactly as printed in the document. Never invent, estimate or compute values. "
                 "If a value is not visible, return an empty string.")
USER_PROMPT = ("OCR text of the document (Markdown; HTML tables are regions detected separately):\n\n{doc}\n\n"
               "Return every line of the document that carries an amount: each purchased item AND each amount of "
               "the order summary or totals section (in whatever language it is printed), with: descripcion (product name as printed), cantidad (quantity as "
               "printed, or \"1\" when a single unit is implied), importe_linea (the amount printed for that line), "
               "divisa (ISO 4217 code; use USD for $), precio_unitario (unit price if printed, else \"\"), vendedor "
               "(seller name after 'Sold by', else \"\"), seguimiento (tracking number printed for that item, else \"\"), "
               "estado_envio (delivery status text printed for that item such as 'Delivered October 8', else \"\"), "
               "line_kind (goods if the line is a product or service being purchased; otherwise the kind of amount it "
               "is: shipping, tax, discount, subtotal_or_total, payment for money paid or still due (amount charged "
               "to a payment method, balance due), or other_charges for any other charge that is not a purchased "
               "item). Also return total (the final amount payable as printed) and total_is_printed (true only if that total "
               "is printed in the document).")

# Patrón "Visual Information Extraction" del cookbook de Qwen: plantilla JSON con claves vacías.
VL_PROMPT = ('Extract the key-value information in the format:'
             '{"items": [{"quantity": "", "description": "", "unit_price": "", "discounted_unit_price": ""}], '
             '"total": ""}')
VL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"quantity": {"type": "string"}, "description": {"type": "string"},
                           "unit_price": {"type": "string"}, "discounted_unit_price": {"type": "string"}},
            "required": ["quantity", "description", "unit_price", "discounted_unit_price"]}},
        "total": {"type": "string"},
    },
    "required": ["items", "total"],
}


def parse_amount(txt):
    """'1,234.56' -> 1234.56 | '26,99' -> 26.99 | '1.234,56' -> 1234.56 | '17,-' -> 17.0."""
    t = re.sub(r"\s", "", txt)
    neg = t.startswith("-")
    t = t.lstrip("-")
    if t.endswith(",-"):
        t = t[:-2]
    if "," in t and "." in t:
        dec = t[-3] if t[-3] in ".," else None
        t = t.replace("," if dec == "." else ".", "")
        t = t.replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{2}$", t) else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def post_json(url, payload, timeout, attempts=3, log=None):
    """POST con reintentos: los llama-server remotos van por túnel y a veces cortan la conexión."""
    import time as _t
    for k in range(attempts):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if k == attempts - 1:
                raise
            if log:
                log(f"    llamada a {url.split('/v1')[0]} falló ({type(e).__name__}); reintento {k + 1}/{attempts - 1}")
            _t.sleep(2 * (k + 1))


def money(v):
    """Texto con un importe -> '1234.56', o '' si no hay importe."""
    m = re.search(AMOUNT_TXT, str(v or ""))
    if not m:
        return ""
    val = parse_amount(m.group(0))
    return f"{abs(val):.2f}" if val is not None else ""


def doc_amounts(text):
    """Importes presentes en el texto (con o sin marca de moneda), normalizados a '1234.56'."""
    out = set()
    for a, b in AMOUNT_RE.findall(text or ""):
        val = parse_amount(a or b)
        if val is not None:
            out.add(f"{abs(val):.2f}")
    return out


def currency_of(text):
    return "USD" if ("USD" in text or "$" in text) else ""


ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def goods_line(seq, l, divisa, ocr):
    """Línea del esquema. Los campos de clasificación van con su forma y sin valor (ver docstring). line_kind es
    "goods" salvo en las líneas que el 27B clasificó como otra cosa (LINE_KINDS)."""
    return {
        "line_kind": l.get("kind", "goods"), "seq": seq,
        "descripcion": l["desc"], "cantidad": str(l["qty"]), "importe_linea": l["amount"], "divisa": divisa,
        "codigo": "", "hs6": "", "estado": "", "confianza_modelo": "", "candidatos": [],
        "confianza": None, "canal": None, "calibrado": None,
        "ocr": ocr,
    }


def lines_sum(ls):
    return sum(float(l["amount"]) for l in ls) if ls and all(l["amount"] for l in ls) else None


# Descripción que es solo un identificador (código de seguimiento "TBA335177403481", número de
# pedido, etc.): el modelo tomó un código en vez del nombre del producto.
ID_LIKE_RE = re.compile(r"^[A-Z]{0,4}[\d-]{9,}$")


def description_issues(lines):
    bad = [l["desc"] for l in lines if ID_LIKE_RE.match(re.sub(r"\s", "", l["desc"] or "")) or not (l["desc"] or "").strip()]
    return [f"{len(bad)} line(s) with an empty description or one that looks like an identifier: " + ", ".join(f'"{b}"' for b in bad[:4])] if bad else []


# ----------------------------------------------------------------------------- división en facturas

def page_text(res, i):
    regs = res["region_pages"][i] if i < len(res["region_pages"]) else []
    return (res["full_text"].get(i) or "") + "\n" + " ".join(str(r.get("content") or "") for r in regs)


def split_invoices(res):
    """Agrupa las páginas del PDF en facturas según los marcadores de inicio."""
    invoices = []
    for i in range(res["n_pages"]):
        text = page_text(res, i)
        hit = next(((tipo, m.group(1)) for tipo, rx in INVOICE_START for m in [rx.search(text)] if m), None)
        if hit and (not invoices or hit[1] != invoices[-1]["ref"]):
            invoices.append({"tipo": hit[0], "ref": hit[1], "pages": [i]})
        elif invoices:
            invoices[-1]["pages"].append(i)
        else:
            invoices.append({"tipo": "", "ref": "", "pages": [i]})
    return invoices


# ----------------------------------------------------------------------------- líneas desde envíos

ITEM_LINE_RE = re.compile(r"^\s*(\d+)\s*of:\s*(.+)$", re.I)


def description_from_full_text(desc, qty, full_texts):
    """La lectura por regiones a veces pierde espacios ("VONDERSO5Inch"). Si la página completa
    tiene la misma línea "N of: ..." (mismo inicio sin espacios), se toma de ahí la descripción,
    uniendo las líneas de continuación hasta el siguiente campo."""
    key = p1.squash(desc)[:16]
    if not key:
        return desc
    for text in full_texts:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = ITEM_LINE_RE.match(line)
            if not m or int(m.group(1)) != qty or not p1.squash(m.group(2)).startswith(key):
                continue
            parts = [m.group(2)]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or ITEM_LINE_RE.match(nxt) or p1.DESC_STOP.match(nxt.strip()):
                    break
                parts.append(nxt.strip())
            return p1.item_description("of: " + " ".join(parts))
    return desc


def lines_from_shipments(shipments, unverified, full_texts, regions):
    ok_ids = {id(it) for r in shipments if p1.shipment_ok(r) for it in r["items"]}
    items = [it for r in shipments for it in r["items"]] + [it for b in unverified for it in b["items"]]
    items.sort(key=lambda it: (it["page"], it["ri"]))
    metas = desktop_line_metas(regions)
    lines, issues = [], []
    for it in items:
        if it["price"] is None:
            issues.append(f'item without a price: "{it["desc"][:50]}"')
        meta = (metas.get((it["page"], it["ri"])) or [{}])
        meta = meta.pop(0) if meta and meta[0] else {}
        it["_estado"] = meta.get("estado_envio", "")
        lines.append({"desc": description_from_full_text(it["desc"], it["qty"], full_texts), "qty": it["qty"],
                      "amount": f"{it['qty'] * it['price']:.2f}" if it["price"] is not None else "",
                      "unit": f"{it['price']:.2f}" if it["price"] is not None else "", "qty_printed": True,
                      "src": it["src"], "page_rel": it["page"], "verified": id(it) in ok_ids,
                      "extra": dict(meta, determinista=True)})
    for r in shipments:
        if not p1.shipment_ok(r):
            issues.append(f"shipment of {r['total']:.2f} does not add up (sum of items {p1.shipment_sum(r):.2f})")
    return lines, issues


# ----------------------------------------------------------------------------- líneas con el 27B

PROMPT_MARGIN_TOKENS = 256  # plantilla de chat (roles, marcas) que /tokenize no ve en el texto suelto
_N_CTX = {}


def server_n_ctx(server):
    """Contexto del llama-server (entrada + salida), de /props. Se consulta una vez por servidor."""
    if server not in _N_CTX:
        props = requests.get(f"{server}/props", timeout=10).json()
        _N_CTX[server] = int(props.get("default_generation_settings", {}).get("n_ctx") or props.get("n_ctx") or 4096)
    return _N_CTX[server]


def count_tokens(server, text):
    return len(requests.post(f"{server}/tokenize", json={"content": text}, timeout=30).json().get("tokens", []))


def lines_with_llm(markdown, server, timeout, log):
    """Una llamada al 27B con el Markdown de UNA página. Devuelve (datos, motivo_de_fallo).
    max_tokens = n_ctx - prompt (antes 4096 fijo: una factura de 54 artículos se cortaba siempre). Si la
    respuesta se corta o no es JSON devuelve (None, motivo) en vez de lanzar: la factura va a revisión."""
    user = USER_PROMPT.format(doc=markdown)
    budget = server_n_ctx(server) - count_tokens(server, SYSTEM_PROMPT + "\n" + user) - PROMPT_MARGIN_TOKENS
    if budget < 512:
        return None, "page text too long for the model context"
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        "temperature": 0, "top_k": 1, "max_tokens": budget,
        "response_format": {"type": "json_schema", "json_schema": {"name": "factura", "schema": LINES_SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    rj = post_json(f"{server}/v1/chat/completions", payload, timeout, log=log)
    choice = rj["choices"][0]
    content = re.sub(r"<think>.*?</think>", "", choice["message"]["content"] or "", flags=re.S).strip()
    if choice.get("finish_reason") == "length":
        log(f"  27B: respuesta cortada al llegar a {budget} fichas")
        return None, "model output limit reached"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log("  27B: la respuesta no es JSON válido")
        return None, "model output is not valid JSON"
    log(f"  27B devolvió {len(data.get('lineas', []))} línea(s), total «{data.get('total', '')}» (max_tokens {budget})")
    return data, ""


# ----------------------------------------------------------------------------- líneas con Qwen2.5-VL

def lines_with_vl(pdf, pages, server, timeout, log):
    """Lee las páginas indicadas con Qwen2.5-VL y devuelve (líneas, total)."""
    lines, total = [], ""
    doc = fitz.open(pdf)
    try:
        for i in pages:
            page = doc[i]
            dpi = p1.fit_dpi(page, 200, p1.QWEN_MAX_TOKENS)
            img = base64.b64encode(page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")).decode("ascii")
            payload = {
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                    {"type": "text", "text": VL_PROMPT}]}],
                "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "max_tokens": 2048,
                "response_format": {"type": "json_schema", "json_schema": {"name": "items", "schema": VL_SCHEMA}},
            }
            rj = post_json(f"{server}/v1/chat/completions", payload, timeout, log=log)
            data = json.loads(rj["choices"][0]["message"]["content"])
            for it in data.get("items", []):
                q = re.search(r"\d+", str(it.get("quantity", "")))
                qty = int(q.group(0)) if q else 1
                unit = money(it.get("discounted_unit_price")) or money(it.get("unit_price"))
                lines.append({"desc": str(it.get("description", "")).strip(), "qty": qty, "unit": unit,
                              "amount": f"{qty * float(unit):.2f}" if unit else "", "src": "qwen-vl",
                              "page_rel": pages.index(i) + 1})
            if not total and money(data.get("total")):
                total = money(data.get("total"))
            log(f"  Qwen2.5-VL p{i + 1} a {dpi} dpi: {len(data.get('items', []))} artículo(s), total «{data.get('total', '')}»")
    finally:
        doc.close()
    return lines, total


# ----------------------------------------------------------------------------- campos adicionales desde el OCR

AMT_SQ = r"(-?\d[\d.,]*(?:[.,]\d{2}|,-))"  # importe dentro de texto sin espacios
DATE_RE = re.compile(r"Order\s*(?:Placed|placed|time):?\s*([A-Z][a-z]{2,8}\.?\s*\d{1,2},?\s*\d{4})")
PAY_RE = re.compile(r"(Your Bank|Visa|Master\s?Card|Amex|American Express|Discover|PayPal)\D{0,25}?(\d{4})\b", re.I)
ADDR_START_RE = re.compile(r"^\s*(Ship to|Shipping Address:?|Shipping address)\s*$", re.I)
ADDR_STOP_RE = re.compile(r"^\s*(Payment|Shipping Speed|Order Summary|Billing|Item\(s\)|Delivered|Not Yet|Shipped|Estimated|Arriving|Total|Sales)", re.I)
DISCOUNT_RE = re.compile(r"^(.*?(?:discount|coupon|savings).*?)\s*-?\s*(?:USD|\$)\s*-?\s*(\d[\d.,]*(?:[.,]\d{2}))\s*$", re.I)
SELLER_RE = re.compile(r"Sold by(?:\s+and\s+\w+\s+on\s+behalf\s+of)?:?\s*(.+?)\s*(?:\(seller profile\)|Seller Credentials|Business Price|Condition|Supplied by|$)", re.I)
CONDITION_RE = re.compile(r"Condition:?\s*([A-Za-z]+)")


def sq_amount(sq, patterns):
    """Último importe que sigue a una etiqueta, buscando en texto sin espacios y en minúsculas."""
    for pat in patterns:
        ms = re.findall(pat, sq)
        if ms:
            v = ms[-1]
            return "0.00" if v == "free" else money(v)
    return ""


def invoice_fields(md, full_texts, inv, pdf):
    sq = p1.squash(md)
    f = {
        "proveedor": inv["tipo"],
        "fuente_pdf": Path(pdf).name,
        "paginas": [inv["pages"][0] + 1, inv["pages"][-1] + 1],
        "fecha_pedido": "",
        "subtotal_articulos": sq_amount(sq, [r"item\(?s?\)?subtotal:?(?:usd)?\$?" + AMT_SQ, r"item\(s\)total:?\$?" + AMT_SQ]),
        "envio": sq_amount(sq, [r"shipping&\$?" + AMT_SQ + r"handling", r"shipping&handling:?(?:usd)?\$?" + AMT_SQ,
                               r"shipping:?(free)", r"shipping:?\$?" + AMT_SQ]),
        "impuestos": sq_amount(sq, [r"estimatedtaxtobe\$?" + AMT_SQ + r"collected", r"estimatedtaxtobecollected:?(?:usd)?\$?" + AMT_SQ,
                                   r"salestax:?(?:usd)?\$?" + AMT_SQ]),
        "total_antes_impuestos": sq_amount(sq, [r"totalbeforetax:?(?:usd)?\$?" + AMT_SQ]),
        "descuentos": [],
        # cargos que no son artículos, envío, impuestos ni descuentos (categoría abierta, acordada con el usuario);
        # misma forma que descuentos. Las etiquetas no lo rellenan; en la vía del 27B, build_invoice lo toma de sus líneas other_charges.
        "other_charges": [],
        "metodo_pago": "",
        "destinatario": "",
        "direccion_envio": "",
    }
    m = DATE_RE.search(md)
    if m:
        f["fecha_pedido"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = PAY_RE.search(md)
    if m:
        f["metodo_pago"] = f"{m.group(1).strip().title()} ****{m.group(2)}"
    seen = set()
    for line in md.splitlines():
        d = DISCOUNT_RE.match(line.strip())
        if d and not line.lstrip().startswith("<"):
            concepto = re.sub(r"\s+", " ", d.group(1)).strip(" :")
            key = (p1.squash(concepto), d.group(2))
            if key not in seen:
                seen.add(key)
                f["descuentos"].append({"concepto": concepto, "importe": money(d.group(2))})
    for text in full_texts:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if ADDR_START_RE.match(line):
                addr = []
                for nxt in lines[i + 1:i + 7]:
                    if not nxt.strip() or ADDR_STOP_RE.match(nxt):
                        break
                    addr.append(nxt.strip())
                if addr:
                    f["destinatario"] = addr[0].split("+1")[0].split(",")[0].strip()
                    f["direccion_envio"] = ", ".join(addr)
                    break
        if f["direccion_envio"]:
            break
    return f


def desktop_line_metas(regions):
    """Formato de escritorio de Amazon: estado de envío (cabecera del bloque), vendedor y condición
    de cada fila de artículo, en el mismo orden en que la fase 1 produce los ítems."""
    metas, status, current = {}, "", None
    for pi, regs in enumerate(regions):
        for ri, r in enumerate(regs):
            for cells in p1.rows_of(str(r.get("content") or "")):
                first = p1.squash(cells[0])
                txt = re.sub(r"\s+", " ", " ".join(cells))
                if len(cells) == 1 and len(first) < 40 and first.startswith(p1.BLOCK_KEYS):
                    status, current = re.sub(r"\s+", " ", cells[0]).strip(), None
                    continue
                if p1.ITEM_RE.match(cells[0]):
                    current = {"estado_envio": status, "vendedor": "", "condicion": ""}
                    metas.setdefault((pi + 1, ri), []).append(current)
                elif current is None or "totalforthisshipment" in p1.squash(txt) or "shippingaddress" in first:
                    current = None
                    continue
                # la fila del artículo o las filas siguientes ("Sold by...", "Condition: New") completan el meta
                if current is not None:
                    sel, cond = SELLER_RE.search(txt), CONDITION_RE.search(txt)
                    if sel and not current["vendedor"]:
                        current["vendedor"] = sel.group(1).strip()
                    if cond and not current["condicion"]:
                        current["condicion"] = cond.group(1)
    return metas


def qty_printed(qty, sq_text, desc_key=""):
    """La cantidad cuenta como impresa solo si aparece junto a la descripción del artículo: "Nof:"
    inmediatamente delante, o "xN" / "Nx" / "qty:N" en los ~400 caracteres siguientes. Un "x2" en
    otra parte de la página (p. ej. la línea de cupones) no cuenta."""
    if not desc_key:
        return False
    pos = sq_text.find(desc_key)
    if pos < 0:
        return False
    before, after = sq_text[max(0, pos - 12):pos], sq_text[pos:pos + 400]
    m = re.search(r"(\d+)of:$", before)
    if m and int(m.group(1)) == qty:
        return True
    return bool(re.search(rf"(?<!\d)(?:qty:?{qty}|[x×]{qty}|{qty}[x×])(?!\d)", after))


QTY_SRC = {"glm": "leido", "qwen": "leido"}


def line_evidence(l, sq_text, ocr_amounts, arithmetic_ok):
    """Origen de cada valor de la línea (ver docstring del módulo)."""
    src = l.get("src", "")
    if src in ("glm", "qwen"):  # fase 1: filas "N of:" leídas de la tabla; 'qwen' = precio rescatado del recorte
        return {"descripcion": "leido", "cantidad": "leido",
                "precio_unitario": "leido" if src == "glm" else "aritmetica", "importe_linea": "calculado"}
    ev = {}
    key = p1.squash(l.get("desc", ""))[:20]
    ev["descripcion"] = "leido" if key and key in sq_text else "modelo"
    unit, amt = l.get("unit") or "", l.get("amount") or ""
    ev["precio_unitario"] = ("leido" if unit in ocr_amounts else "aritmetica" if arithmetic_ok else "modelo") if unit else ""
    if src == "qwen-vl":
        ev["importe_linea"] = "calculado" if amt else ""
    else:
        ev["importe_linea"] = ("leido" if amt in ocr_amounts else "aritmetica" if arithmetic_ok else "modelo") if amt else ""
    if l.get("qty_printed") or qty_printed(l["qty"], sq_text, key):
        ev["cantidad"] = "leido"
    elif l["qty"] == 1:
        ev["cantidad"] = "asumido"      # una unidad por defecto; el cuadre con el total lo corrobora
    elif qty_backed_by_line(l, ev) or arithmetic_ok:
        ev["cantidad"] = "aritmetica"   # p. ej. "x2" ilegible: solo lo sostiene cantidad x precio = total
    else:
        ev["cantidad"] = "modelo"
    return ev


def qty_backed_by_line(l, ev):
    """Cantidad no impresa con un formato reconocible ("3 EA 3"), pero sostenida por la propia línea:
    precio unitario e importe literales en el OCR y cantidad x precio = importe."""
    unit, amt = l.get("unit") or "", l.get("amount") or ""
    return (bool(unit and amt) and ev.get("precio_unitario") == ev.get("importe_linea") == "leido"
            and abs(l["qty"] * float(unit) - float(amt)) < 0.01)


def evidence_issues(lines):
    out = []
    n_ar = sum(1 for l in lines if l.get("evidence", {}).get("cantidad") == "aritmetica" and not qty_backed_by_line(l, l["evidence"]))
    n_mod = sum(1 for l in lines if "modelo" in l.get("evidence", {}).values())
    if n_ar:
        out.append(f"quantity inferred by arithmetic in {n_ar} line(s)")
    if n_mod:
        out.append(f"{n_mod} line(s) with value(s) not found in the OCR text")
    return out


def line_ocr(l, *, pagina, verificado, sq_text, extra=None):
    """Objeto `ocr` de una línea. Los campos de modelo se descartan si no están en el texto OCR."""
    e = extra or {}
    def seen(v):
        return v if v and p1.squash(v) in sq_text else ""
    return {
        "precio_unitario": l.get("unit") or (f"{float(l['amount']) / l['qty']:.2f}" if l.get("amount") and l.get("qty") else ""),
        "cantidad_impresa": bool(l.get("qty_printed")) or qty_printed(l["qty"], sq_text, p1.squash(l.get("desc", ""))[:20]),
        "vendedor": e.get("vendedor", "") if e.get("determinista") else seen(e.get("vendedor", "")),
        "seguimiento": e.get("seguimiento", "") if e.get("determinista") else seen(re.sub(r"\s", "", e.get("seguimiento", ""))),
        "estado_envio": e.get("estado_envio", "") if e.get("determinista") else seen(e.get("estado_envio", "")),
        "condicion": e.get("condicion", ""),
        "pagina": pagina,
        "origen": l.get("src", ""),
        "verificado": bool(verificado),
        "evidencia": l.get("evidence", {}),
    }


# ----------------------------------------------------------------------------- bloques de envío (escritorio)

def raw_rows(content):
    """Como p1.rows_of pero devolviendo también el HTML crudo de la fila (para ver marcas)."""
    if "<table" in content:
        for row in re.findall(r"<tr[^>]*>.*?</tr>", content, re.S):
            cells = [f2_unescape(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            cells = [c for c in cells if c]
            if cells:
                yield cells, row
    else:
        for line in f2_unescape(content).splitlines():
            if line.strip():
                yield [line.strip()], line


def f2_unescape(c):
    import html as _h
    return _h.unescape(p1.TAG_RE.sub(" ", c)).strip()


def parse_blocks(regions):
    """Bloques de envío del formato de escritorio de Amazon, cada uno con sus artículos e importes.
    Un bloque empieza en una cabecera ("Not Yet Shipped", "Shipped on ...") y termina en
    "Total for This Shipment"; puede continuar en la página siguiente."""
    blocks, cur = [], None
    for pi, regs in enumerate(regions):
        for ri, r in enumerate(regs):
            for cells, raw in raw_rows(str(r.get("content") or "")):
                first, txt = p1.squash(cells[0]), re.sub(r"\s+", " ", " ".join(cells))
                norm = p1.squash(txt)
                if len(cells) == 1 and len(first) < 40 and first.startswith(p1.BLOCK_KEYS):
                    cur = {"estado": re.sub(r"\s+", " ", cells[0]).strip(), "items": [], "subtotal": "", "envio": "",
                           "impuestos": "", "antes_impuestos": "", "total": "", "pages": [pi], "last_item": None}
                    blocks.append(cur)
                    continue
                if cur is None:
                    continue
                if pi not in cur["pages"]:
                    cur["pages"].append(pi)
                m = p1.ITEM_RE.match(cells[0])
                if m:
                    price = p1.first_amount(" ".join(cells[1:]))
                    if price is None:
                        price = p1.first_amount(cells[0].split("of:", 1)[1])
                    item = {"qty": int(m.group(1)), "price": price, "desc": p1.item_description(cells[0]),
                            "text": cells[0][:60], "page_rel": pi + 1, "ri": ri, "vendedor": "", "condicion": "",
                            "src": "qwen" if 'title="recuperado con Qwen2.5-VL"' in raw else "glm"}
                    cur["items"].append(item)
                    cur["last_item"] = item
                if cur["last_item"] is not None and not re.search(r"subtotal|shipping|total|tax", norm):
                    sel, cond = SELLER_RE.search(txt), CONDITION_RE.search(txt)
                    if sel and not cur["last_item"]["vendedor"]:
                        cur["last_item"]["vendedor"] = sel.group(1).strip()
                    if cond and not cur["last_item"]["condicion"]:
                        cur["last_item"]["condicion"] = cond.group(1)
                for key, pats in (("subtotal", [r"item\(?s?\)?subtotal:?(?:usd)?\$?" + AMT_SQ]),
                                  ("envio", [r"shipping&handling:?(?:usd)?\$?" + AMT_SQ]),
                                  ("antes_impuestos", [r"totalbeforetax:?(?:usd)?\$?" + AMT_SQ]),
                                  ("impuestos", [r"salestax:?(?:usd)?\$?" + AMT_SQ, r"estimatedtaxtobecollected:?(?:usd)?\$?" + AMT_SQ])):
                    v = sq_amount(norm, pats)
                    if v:
                        cur[key] = v
                if "totalforthisshipment" in norm:
                    t = p1.first_amount(norm.split("totalforthisshipment", 1)[1])
                    if t is not None:
                        cur["total"] = f"{t:.2f}"
                    cur = None
    return [b for b in blocks if b["items"]]


def build_shipment_invoice(inv_id, res, inv, block, k, n, order_fields, order_total, order_pages, full_texts, divisa):
    """Factura = un bloque de envío del formato de escritorio."""
    issues = []
    lines = []
    for it in block["items"]:
        if it["price"] is None:
            issues.append(f'item without a price: "{it["desc"][:50]}"')
        lines.append({"desc": description_from_full_text(it["desc"], it["qty"], full_texts), "qty": it["qty"],
                      "amount": f"{it['qty'] * it['price']:.2f}" if it["price"] is not None else "",
                      "unit": f"{it['price']:.2f}" if it["price"] is not None else "", "qty_printed": True,
                      "src": it["src"], "page_rel": it["page_rel"],
                      "extra": {"vendedor": it["vendedor"], "condicion": it["condicion"], "estado_envio": block["estado"], "determinista": True}})
    s = lines_sum(lines)
    total, total_printed = block["total"], bool(block["total"])
    if not total:
        issues.append("shipment block without a printed total; total computed as the sum of lines")
        total = f"{s:.2f}" if s is not None else ""
    ok = total_printed and s is not None and abs(s - float(total)) < 0.01
    if total_printed and s is not None and not ok:
        issues.append(f"sum of lines {s:.2f} vs shipment total {total}")
    if any(it["src"] == "qwen" for it in block["items"]):
        issues.append("unit price(s) recovered from the image by the vision model (Qwen2.5-VL)")
    for l in lines:
        l["verified"] = ok
        l["evidence"] = line_evidence(l, "", set(), ok)
    pages_abs = [order_pages[i] for i in block["pages"]]
    fields = dict(order_fields)
    fields.update({
        "paginas": [pages_abs[0] + 1, pages_abs[-1] + 1],
        "subtotal_articulos": block["subtotal"], "envio": block["envio"], "impuestos": block["impuestos"],
        "total_antes_impuestos": block["antes_impuestos"],
        "origen_lineas": "envios",
        "envio_bloque": {"estado": block["estado"], "n": k + 1, "de": n},
        "pedido": inv["ref"], "total_pedido": order_total,
        "envios": [{"estado": block["estado"], "total": total, "suma_lineas": f"{s:.2f}" if s is not None else "", "cuadra": ok, "articulos": len(lines)}],
        "incidencias": list(issues),
    })
    sq_text = p1.squash(" ".join(full_texts))
    return {
        "id": inv_id, "estado": "DONE",
        "motivo_revision": "LECTURA" if issues else "", "mismatch": "; ".join(issues),
        "total": total, "total_is_printed": total_printed,
        "version_catalogo": "", "result_version": RESULT_VERSION,
        "lineas": [goods_line(i, l, divisa, line_ocr(l, pagina=order_pages[l["page_rel"] - 1] + 1, verificado=l["verified"],
                                                    sq_text=sq_text, extra=l["extra"]))
                   for i, l in enumerate(lines)],
        "referencia": inv["ref"],
        "ocr": fields,
    }


# ----------------------------------------------------------------------------- factura

def build_invoice(inv_id, res, inv, use_llm, llm_server, vl_server, timeout, log):
    pages = inv["pages"]
    regions = [res["region_pages"][i] if i < len(res["region_pages"]) else [] for i in pages]
    full_texts = [res["full_text"].get(i) or "" for i in pages]
    md = "\n\n---\n\n".join(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions))
    ocr_amounts = doc_amounts(md)
    sq_text = p1.squash(md)
    divisa = currency_of(md)
    issues = []
    # Con qué deben cuadrar las líneas: el "Item(s) Subtotal" del pedido. El formato de escritorio
    # de Amazon imprime uno por envío y el global al final (caja de pago): se toma el último.
    subs = ITEMS_SUBTOTAL_RE.findall(md)
    items_subtotal = money(subs[-1]) if subs else ""

    shipments, unverified, grand = p1.check_shipments(regions)
    total = f"{grand:.2f}" if grand is not None else ""
    if not total:
        m = ORDER_TOTAL_RE.search(md)
        total = money(m.group(1)) if m else ""
    total_printed = bool(total)
    target = items_subtotal or total  # suma esperada de las líneas (sin envío ni impuestos)
    if 'title="recuperado con Qwen2.5-VL"' in md:
        issues.append("unit price(s) recovered from the image by the vision model (Qwen2.5-VL)")

    read_failed = False  # alguna página no se pudo leer: sin líneas por fallo de lectura, no por no ser factura
    if shipments:
        lines, more = lines_from_shipments(shipments, unverified, full_texts, regions)
        issues += more
        source = "envios"
    elif use_llm:
        # Página por página: la salida de cada llamada está acotada por su página, no por el documento.
        log(f"  sin bloques de envío: extracción de líneas con Qwen3.6-27B, página por página ({len(pages)})")
        lines, model_total = [], None
        for k, (i, regs) in enumerate(zip(pages, regions)):
            data, fail = lines_with_llm(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0], llm_server, timeout, log)
            if data is None:
                read_failed = True
                issues.append(f"item list on page {i + 1} could not be read completely ({fail}); review against the paper")
                continue
            for l in data.get("lineas", []):
                amt = money(l.get("importe_linea"))
                # Solo los artículos: un importe de resumen sin respaldo no se usa (ver "backed"), no es incidencia.
                if amt and amt not in ocr_amounts and l.get("line_kind", "goods") == "goods":
                    issues.append(f'amount {amt} not found in the OCR text ("{l.get("descripcion", "")[:40]}")')
                q = re.search(r"\d+", str(l.get("cantidad", "")))
                lines.append({"desc": l.get("descripcion", "").strip(), "qty": int(q.group(0)) if q else 1,
                              "amount": amt, "unit": money(l.get("precio_unitario")), "src": "qwen27b", "page_rel": k + 1,
                              "kind": l.get("line_kind") if l.get("line_kind") in LINE_KINDS else "goods",
                              "order": len(lines),
                              "extra": {"vendedor": l.get("vendedor", ""), "seguimiento": l.get("seguimiento", ""),
                                        "estado_envio": l.get("estado_envio", "")}})
                d = str(l.get("divisa") or "").strip().upper()
                if ISO_CURRENCY_RE.match(d):
                    divisa = d
            if money(data.get("total")):
                model_total = data  # el total del documento suele estar en la última página que lo trae
        t = money(model_total.get("total")) if model_total else ""
        if t and not total:
            if t in ocr_amounts:
                total, total_printed = t, bool(model_total.get("total_is_printed", True))
            else:
                issues.append(f"total {t} returned by the model not found in the OCR text")
        source = "llm"
    else:
        lines, source = [], "ninguna"
        issues.append("no shipment blocks and model extraction disabled")
    # Las líneas que el 27B clasifica como no-mercadería (envío, impuesto, descuento, subtotal/total, otros cargos)
    # se apartan: todo lo que sigue (sumas, rescate visual, evidencia, ESTRUCTURA) trabaja solo con las "goods".
    # Se devuelven al final con su line_kind (WTCP solo declara las goods).
    others = [l for l in lines if l.get("kind", "goods") != "goods"]
    lines = [l for l in lines if l.get("kind", "goods") == "goods"]
    # Contra qué cuadran las líneas, recalculado con lo que trajo el 27B (antes quedaba vacío si el total
    # solo lo encontraba el modelo). Sin "Item(s) Subtotal" con esa etiqueta: vale una línea que el 27B
    # clasificó subtotal_or_total, cuyo importe está literal en el OCR e iguala la suma de los artículos.
    if source == "llm" and not items_subtotal and lines_sum(lines) is not None:
        s = f"{lines_sum(lines):.2f}"
        if any(l["kind"] == "subtotal_or_total" and l.get("amount") == s for l in others) and s in ocr_amounts:
            items_subtotal = s
    target = items_subtotal or total
    fields = invoice_fields(md, full_texts, inv, res["pdf"])
    if source == "llm":  # cabecera que las etiquetas no encontraron: de las líneas clasificadas por el 27B con importe en el OCR
        # Un mismo resumen impreso en varias páginas no se suma dos veces: importes idénticos cuentan una vez
        # (si eran cargos distintos, la cuenta de cabecera no cierra y la factura va a revisión).
        backed = [l for l in others if l.get("amount") and l["amount"] in ocr_amounts]
        for key, kind in (("envio", "shipping"), ("impuestos", "tax")):
            amts = list(dict.fromkeys(l["amount"] for l in backed if l["kind"] == kind))
            if not fields[key] and amts:
                fields[key] = f"{sum(float(a) for a in amts):.2f}"
        for key, kind in (("descuentos", "discount"), ("other_charges", "other_charges")):
            if not fields[key]:
                seen = dict.fromkeys((p1.squash(l["desc"]), l["amount"]) for l in backed if l["kind"] == kind)
                fields[key] = [{"concepto": next(l["desc"] for l in backed if (p1.squash(l["desc"]), l["amount"]) == k),
                                "importe": k[1]} for k in seen]
        if not fields["subtotal_articulos"]:
            fields["subtotal_articulos"] = items_subtotal
        # Sin subtotal de artículos impreso, la cuenta completa de la cabecera también cuadra la factura:
        # artículos + envío + impuestos + otros cargos − descuentos = total (todos los importes leídos del OCR).
        s = lines_sum(lines)
        if not items_subtotal and total and s is not None:
            extra = (float(fields["envio"] or 0) + float(fields["impuestos"] or 0)
                     + sum(float(c["importe"] or 0) for c in fields["other_charges"])
                     - sum(float(d["importe"] or 0) for d in fields["descuentos"]))
            if abs(s + extra - float(total)) < 0.01:
                target = f"{s:.2f}"

    # Rescate visual: solo en la vía del 27B, cuando la suma no reproduce el total impreso.
    if source == "llm" and total_printed and target and (lines_sum(lines) is None or abs(lines_sum(lines) - float(target)) >= 0.01):
        if p1.server_ok(vl_server):
            log("  la suma no cuadra con el total impreso: lectura visual con Qwen2.5-VL")
            try:
                vl_lines, _ = lines_with_vl(res["pdf"], pages, vl_server, timeout, log)
            except Exception as e:  # noqa: BLE001
                vl_lines = []
                issues.append(f"vision model (Qwen2.5-VL) failed: {e}")
            # Regla dura: sin descripción en el OCR, el artículo se descarta; sin precio literal, no se acepta.
            kept = [v for v in vl_lines if p1.squash(v["desc"])[:20] and p1.squash(v["desc"])[:20] in sq_text]
            if len(kept) < len(vl_lines):
                issues.append(f"vision model (Qwen2.5-VL) returned {len(vl_lines) - len(kept)} item(s) whose description "
                              "is not in the OCR text; discarded")
            literal_price = bool(kept) and all(v.get("unit") and v["unit"] in ocr_amounts for v in kept)
            vl_sum = lines_sum(kept)
            if kept and literal_price and vl_sum is not None and abs(vl_sum - float(target)) < 0.01:
                if len(kept) == len(lines):
                    for v, o in zip(kept, lines):
                        v["extra"] = o.get("extra", {})
                lines, source = kept, "qwen-vl"
                issues = [i for i in issues if not i.startswith("amount ")]
                issues.append("lines read from the image by the vision model (Qwen2.5-VL), unit prices found literally in "
                              "the OCR text; quantity x price matches the printed "
                              + ("items subtotal" if items_subtotal else "total"))
            elif kept and not literal_price:
                issues.append("vision model (Qwen2.5-VL) rejected: unit price(s) not found literally in the OCR text")
            else:
                issues.append("vision model (Qwen2.5-VL) does not add up either (sum " + ("?" if vl_sum is None else f"{vl_sum:.2f}")
                              + f" vs {target})")
        else:
            issues.append(f"vision model (Qwen2.5-VL) not responding at {vl_server}; no visual check")

    issues += description_issues(lines)
    if not total and lines_sum(lines) is not None:
        total, total_printed = f"{lines_sum(lines):.2f}", False
        target = total
    if target and lines_sum(lines) is not None and abs(lines_sum(lines) - float(target)) >= 0.01:
        issues.append(f"sum of lines {lines_sum(lines):.2f} vs " + ("items subtotal" if items_subtotal else "total") + f" {target}")
    # Sin marcador de pedido y sin ninguna línea: no parece una factura de mercadería (albarán,
    # confirmación de envío, recibo de pago...). Lo decide una persona: nunca se descarta en silencio.
    estructura = not inv["tipo"] and not lines and not read_failed
    if estructura:
        issues.append("the document does not look like a goods invoice: no order marker and no item lines")

    all_ok = bool(target) and lines_sum(lines) is not None and abs(lines_sum(lines) - float(target)) < 0.01
    for l in lines:
        l["evidence"] = line_evidence(l, sq_text, ocr_amounts, all_ok)
        l["verified"] = l.get("verified", all_ok) and "modelo" not in l["evidence"].values()
    issues += evidence_issues(lines)
    for l in others:  # no-mercadería: evidencia de sus valores, nunca "verificada" como artículo
        l["evidence"], l["verified"] = line_evidence(l, sq_text, ocr_amounts, False), False
    if others:  # mismo orden en que el 27B las devolvió; las goods del rescate visual no traen orden y van primero
        lines = sorted(lines + others, key=lambda l: l.get("order", -1))
    fields["origen_lineas"] = source
    fields["envios"] = [{"estado": r["items"][0].get("_estado", "") if r["items"] else "", "total": f"{r['total']:.2f}", "suma_lineas": f"{p1.shipment_sum(r):.2f}",
                         "cuadra": p1.shipment_ok(r), "articulos": len(r["items"])} for r in shipments]
    fields["incidencias"] = list(issues)
    return {
        "id": inv_id,
        "estado": "DONE",
        "motivo_revision": "ESTRUCTURA" if estructura else "LECTURA" if issues else "",
        "mismatch": "; ".join(issues),
        "total": total,
        "total_is_printed": total_printed,
        "version_catalogo": "",
        "result_version": RESULT_VERSION,
        "lineas": [goods_line(i, l, divisa, line_ocr(l, pagina=pages[min(l.get("page_rel", 1), len(pages)) - 1] + 1,
                                                    verificado=l["verified"], sq_text=sq_text, extra=l.get("extra")))
                   for i, l in enumerate(lines)],
        "referencia": inv["ref"],
        "ocr": fields,
    }, source


def build_all(res, use_llm=True, llm_server=QWEN27_SERVER, vl_server=VL_SERVER, timeout=600, log=None):
    """Divide el resultado de la fase 1 en facturas y construye el JSON de todas (array)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    invoices = split_invoices(res)
    log(f"  {len(invoices)} factura(s) detectada(s): " + ", ".join(
        f"{inv['tipo'] or '?'} {inv['ref'] or 'sin ref'} p{inv['pages'][0] + 1}-{inv['pages'][-1] + 1}" for inv in invoices))
    out = []
    for g, inv in enumerate(invoices):
        pages = inv["pages"]
        regions = [res["region_pages"][i] if i < len(res["region_pages"]) else [] for i in pages]
        blocks = parse_blocks(regions)
        dups = [j for j, o in enumerate(invoices) if j != g and o["ref"] and o["ref"] == inv["ref"]]
        docs = []
        if blocks:
            full_texts = [res["full_text"].get(i) or "" for i in pages]
            md = "\n\n---\n\n".join(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions))
            order_fields = invoice_fields(md, full_texts, inv, res["pdf"])
            m = ORDER_TOTAL_RE.search(md)
            order_total = money(m.group(1)) if m else ""
            log(f"  -- pedido {inv['ref'] or 'sin referencia'}: {len(blocks)} bloque(s) de envío -> {len(blocks)} factura(s)")
            for k, b in enumerate(blocks):
                docs.append(build_shipment_invoice(len(out) + len(docs), res, inv, b, k, len(blocks), order_fields, order_total, pages, full_texts, currency_of(md)))
                d = docs[-1]
                log(f"     factura {d['id']} [{b['estado']}]: {len(d['lineas'])} línea(s), total {d['total'] or '?'}"
                    f"{'' if d['total_is_printed'] else ' (calculado)'}, revisión: {d['motivo_revision'] or 'no'}" + (f" ({d['mismatch']})" if d["mismatch"] else ""))
        else:
            log(f"  -- factura {len(out)} ({inv['ref'] or 'sin referencia'})")
            doc, source = build_invoice(len(out), res, inv, use_llm, llm_server, vl_server, timeout, log)
            log(f"     {len(doc['lineas'])} línea(s) (origen: {source}), total {doc['total'] or '?'}, revisión: {doc['motivo_revision'] or 'no'}"
                + (f" ({doc['mismatch']})" if doc["mismatch"] else ""))
            docs.append(doc)
        if dups:
            note = "order reference repeated in the file (the same order appears more than once)"
            for doc in docs:
                doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"], note]))
                if doc["motivo_revision"] != "ESTRUCTURA":
                    doc["motivo_revision"] = "LECTURA"
                doc["ocr"]["incidencias"].append(note)
        out.extend(docs)
    return out


LOCK_PATH = Path(__file__).resolve().parent / ".modelos.lock"


@contextlib.contextmanager
def modelos_lock(log=None, on_wait=None):
    """Un solo trabajo a la vez contra los modelos, también entre procesos (API y CLI).
    Bloqueo exclusivo del sistema operativo sobre LOCK_PATH: se libera solo si el proceso muere."""
    log = log or (lambda m: print(m, file=sys.stderr))
    f = open(LOCK_PATH, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock = lambda: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: (f.seek(0), msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            lock = lambda: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(f, fcntl.LOCK_UN)
        f.seek(0)
        waiting = False
        while True:
            try:
                lock()
                break
            except OSError:
                if not waiting:
                    waiting = True
                    log("Esperando a los modelos: otro proceso los está usando")
                    if on_wait:
                        on_wait()
                time.sleep(1)
        try:
            yield
        finally:
            unlock()
    finally:
        f.close()


def main():
    ap = argparse.ArgumentParser(description="Fase 2: JSON estándar de facturas a partir del OCR de fase 1 (un archivo por PDF)")
    ap.add_argument("pdfs", nargs="+", help="PDF(s) a procesar")
    ap.add_argument("-o", "--out-dir", default="salida", help="carpeta de salida; se escribe <nombre>.json por PDF (default salida/)")
    ap.add_argument("--md-dir", help="carpeta donde guardar además el Markdown de la fase 1 por PDF")
    ap.add_argument("--llm-server", default=QWEN27_SERVER, help=f"llama-server con Qwen3.6-27B (default {QWEN27_SERVER})")
    ap.add_argument("--vl-server", default=VL_SERVER, help=f"llama-server con Qwen2.5-VL para el rescate visual (default {VL_SERVER})")
    ap.add_argument("--no-llm", action="store_true", help="no usar el 27B cuando no hay bloques de envío")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    log = lambda m: print(m, file=sys.stderr)
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)

    for pdf in args.pdfs:
        stem = Path(pdf).stem
        log(f"=== {pdf} ===")
        with modelos_lock(log):
            try:
                res = p1.run(pdf, timeout=args.timeout, log=log)
            except RuntimeError as e:
                sys.exit(str(e))
            out = build_all(res, not args.no_llm, args.llm_server, args.vl_server, args.timeout, log)
        if args.md_dir:
            Path(args.md_dir).mkdir(parents=True, exist_ok=True)
            (Path(args.md_dir) / f"{stem}.md").write_text(res["markdown"], encoding="utf-8")
        path = Path(args.out_dir) / f"{stem}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  Guardado en {path}")


if __name__ == "__main__":
    main()
