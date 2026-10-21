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
  - Facturas de varias páginas (vía del 27B): en cada salto de página, la geometría de PP-OCRv6 (servicio :8089 del
    host de modelos) dice si el último artículo de una página continúa al principio de la siguiente; ese texto se
    coloca en su artículo y, si GLM corrió las descripciones de la página siguiente, se realinean (fix_page_breaks).
    El texto es siempre el de GLM; si no se puede realinear con seguridad, revisión.
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
    "calculado" (cantidad x precio), "asumido" (cantidad 1 no impresa; sin incidencia si cuadra),
    "imagen" (cantidad leída por Qwen2.5-VL sobre la foto, confirmada porque cantidad x precio literal
    cuadra exactamente con lo impreso)
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
import difflib
import html
import itertools
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
# ",\d{2}" no puede ser el comienzo de un grupo de miles: "$1,250" no es 1,25 (antes se tomaba así).
AMOUNT_TXT = r"-?\s*\d[\d.,]*(?:\.\d{2}|,\d{2}(?!\d)|,-)"
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
                           "line_kind": {"type": "string", "enum": LINE_KINDS},
                           # El valor de cada cifra como número JSON: la gramática de llama.cpp (json-schema-to-grammar,
                           # primitivo "number") no admite separador de miles ni coma decimal, así que el 27B decide con el
                           # contexto del documento si "1.000" es 1 o 1000; el código solo comprueba (model_number).
                           "cantidad_valor": {"type": "number"}, "importe_valor": {"type": "number"},
                           "precio_unitario_valor": {"type": "number"}},
            "required": ["descripcion", "cantidad", "cantidad_valor", "importe_linea", "importe_valor", "divisa",
                         "precio_unitario", "precio_unitario_valor", "vendedor", "seguimiento", "estado_envio", "line_kind"]}},
        "total": {"type": "string"},
        "total_valor": {"type": "number"},
        "total_is_printed": {"type": "boolean"},
    },
    "required": ["lineas", "total", "total_valor", "total_is_printed"],
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
               "is printed in the document). For cantidad, importe_linea, precio_unitario and total also return the "
               "value as a number (cantidad_valor, importe_valor, precio_unitario_valor, total_valor), reading the "
               "decimal and thousands separators the way this document uses them; 0 when the value is not printed.")

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


# Importe entero impreso, sin céntimos ("$ 299", eBay): con marca de moneda y como único contenido del renglón o de la
# celda, o tras una etiqueta al final del renglón ("Total: $803"); no dentro de una frase ("$ 2pcs", "Gift Card - $75").
INT_AMOUNT_RE = re.compile(r"(?:^|<t[dh][^>]*>|\|)[ \t]*(?:[A-Za-z][\w ()/.*-]*:[ \t]*)?(?:USD|US\$|\$)[ \t]*-?[ \t]*"
                           r"(\d{1,3}(?:,\d{3})+|\d+)[ \t]*(?=\r?\n|\Z|</t[dh]>|\|)", re.M)


def printed_readings(s):
    """Valores que admite una cifra tal como está impresa, según el separador que use el documento: "1.000" -> {1, 1000},
    "1,250" -> {1.25, 1250}, "1,234.56" -> {1234.56}, "0.5" -> {0.5}. Sin signo. Conjunto vacío si no hay cifras.
    No elige entre ellos: eso lo decide el 27B con el contexto del documento; esto solo comprueba su respuesta."""
    from decimal import Decimal
    out = set()
    s = str(s or "")  # con y sin el espacio como separador de miles ("1 234,56" | "3 EA 3")
    for tok in re.findall(r"\d(?:[\d.,'  ]*\d)?", s) + re.findall(r"\d(?:[\d.,']*\d)?", s):
        t = re.sub(r"['  ]", "", tok)
        seps = [c for c in t if c in ".,"]
        if not seps:
            out.add(Decimal(t))
        elif len(set(seps)) == 2:  # "1,234.56" / "1.234,56": el último separador es el decimal
            dec = t[max(t.rfind("."), t.rfind(","))]
            out.add(Decimal(t.replace("." if dec == "," else ",", "").replace(",", ".")))
        else:
            c, parts = seps[0], t.split(seps[0])
            if len(parts) > 2:  # "1,000,000": solo miles
                out.add(Decimal("".join(parts)))
            else:
                out.add(Decimal(parts[0] + "." + parts[1]))
                if len(parts[1]) == 3:  # "1.000" / "1,250": también miles
                    out.add(Decimal(parts[0] + parts[1]))
    return out


def model_number(printed, value, what, issues):
    """Valor que el 27B dio para una cifra (`value`, número JSON) contrastado con lo que copió como impreso (`printed`).
    El valor lo decide el 27B; el código solo comprueba que es una lectura posible de lo impreso. Si no lo es (o falta),
    se anota en `issues` (la factura va a revisión) y se devuelve el del 27B tal cual; si no hay cifra impresa, None."""
    from decimal import Decimal, InvalidOperation
    s = str(printed or "").strip()
    reads = printed_readings(s)
    if not reads:
        return None
    try:
        v = abs(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        v = None
    if v is not None and v in reads:
        return v
    issues.append(f'{what} printed as "{s}" but the model returned {value}; review against the paper')
    return v if v is not None else (next(iter(reads)) if len(reads) == 1 else None)


def vl_value(s):
    """Cifra del VL (texto) con una única lectura posible; None si no hay cifra o admite varias ("1.000")."""
    reads = printed_readings(s)
    return next(iter(reads)) if len(reads) == 1 else None


def cents(v):
    """Decimal -> '1234.56' truncado a céntimos, como se han tratado siempre los precios de 3 a 5 decimales (ver
    fine_amounts: el redondeo y el truncado cuentan como literales)."""
    from decimal import ROUND_DOWN, Decimal
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_DOWN)) if v is not None else ""


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
    out |= {f"{float(x.replace(',', '')):.2f}" for x in INT_AMOUNT_RE.findall(text or "")}
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


def parse_qty(txt):
    """Cantidad como entero, con separador de miles ("1,000 pcs" -> 1000; antes se leía 1). None si no hay número."""
    t = str(txt or "")
    m = re.search(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])", t)  # 1,000 | 10,500.00
    if m:
        return int(float(m.group(0).replace(",", "")))
    m = re.search(r"\d{1,3}(?:\.\d{3})+(?![\d.,])", t)  # 1.000 (punto de miles)
    if m:
        return int(m.group(0).replace(".", ""))
    m = re.search(r"\d+", t)
    return int(m.group(0)) if m else None


def printed_negative(amt, text):
    """El importe ('48.15') aparece impreso con signo menos en el texto ("-$48.15", "- USD 1,234.56")."""
    v = float(amt)
    nums = "|".join(re.escape(n) for n in dict.fromkeys((f"{v:.2f}", f"{v:,.2f}")))
    return bool(re.search(r"-\s*(?:USD\s*)?\$?\s*(?:" + nums + r")(?![\d])", text or ""))


def lines_sum(ls):
    return sum(float(l["amount"]) for l in ls) if ls and all(l["amount"] for l in ls) else None


# Descripción que es solo un identificador (código de seguimiento "TBA335177403481", número de
# pedido, etc.): el modelo tomó un código en vez del nombre del producto.
ID_LIKE_RE = re.compile(r"^[A-Z]{0,4}[\d-]{9,}$")


COUNT_ONLY_RE = re.compile(r"^\s*\d+\s+\w+\s*$")  # "1 item", "2 items", "1 object": un recuento, no un producto


def description_issues(lines):
    bad = [l["desc"] for l in lines if ID_LIKE_RE.match(re.sub(r"\s", "", l["desc"] or "")) or not (l["desc"] or "").strip()
           or COUNT_ONLY_RE.match(l["desc"] or "")]
    out = [f"{len(bad)} line(s) with an empty description or one that looks like an identifier: " + ", ".join(f'"{b}"' for b in bad[:4])] if bad else []
    # Una línea sin importe deja la suma sin calcular y el cuadre sin comprobar: nunca sale sin revisión.
    no_amt = [l["desc"] for l in lines if not l.get("amount")]
    if no_amt:
        out.append(f"{len(no_amt)} item line(s) without an amount: " + ", ".join(f'"{d[:40]}"' for d in no_amt[:3]))
    return out


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


_LLM_CACHE = {}  # (servidor, Markdown de la página) -> respuesta; se vacía por documento en build_all


def lines_with_llm(markdown, server, timeout, log):
    """Una llamada al 27B con el Markdown de UNA página. Devuelve (datos, motivo_de_fallo).
    max_tokens = n_ctx - prompt (antes 4096 fijo: una factura de 54 artículos se cortaba siempre). Si la
    respuesta se corta o no es JSON devuelve (las líneas completas que llegó a escribir o None, motivo) en vez de lanzar:
    la factura va a revisión.
    Temperatura 0 y top_k 1: la misma página da la misma respuesta, así que se reutiliza (corte por pedidos)."""
    if (server, markdown) not in _LLM_CACHE:
        _LLM_CACHE[(server, markdown)] = _lines_with_llm(markdown, server, timeout, log)
    return _LLM_CACHE[(server, markdown)]


def complete_objects(content, key):
    """Objetos completos de la lista `key` de un JSON cortado ('{"lineas": [{...}, {...}, {"descr'): los que el modelo
    llegó a cerrar, tal cual; el último a medias no se usa."""
    from decimal import Decimal
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', content or "")
    if not m:
        return []
    dec, pos, out = json.JSONDecoder(parse_float=Decimal), m.end(), []
    while True:
        while pos < len(content) and content[pos] in " \t\r\n,":
            pos += 1
        try:
            obj, pos = dec.raw_decode(content, pos)
        except json.JSONDecodeError:
            return out
        if isinstance(obj, dict):
            out.append(obj)


def _lines_with_llm(markdown, server, timeout, log):
    user = USER_PROMPT.format(doc=markdown)
    budget = server_n_ctx(server) - count_tokens(server, SYSTEM_PROMPT + "\n" + user) - PROMPT_MARGIN_TOKENS
    if budget < 512:
        return None, "page text too long for the model context"
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        "temperature": 0, "top_k": 1, "max_tokens": budget,
        # cache_prompt false: el README de llama-server (38a5b42) avisa que reutilizar la caché de la orden «can cause
        # nondeterministic results»; _LLM_CACHE y la opción D dan por hecho que la misma página da la misma respuesta.
        "cache_prompt": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "factura", "schema": LINES_SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    rj = post_json(f"{server}/v1/chat/completions", payload, timeout, log=log)
    choice = rj["choices"][0]
    content = re.sub(r"<think>.*?</think>", "", choice["message"]["content"] or "", flags=re.S).strip()
    from decimal import Decimal
    fail = "model output limit reached" if choice.get("finish_reason") == "length" else ""
    try:
        data = json.loads(content, parse_float=Decimal)  # 17.842 exacto, sin pasar por float
    except json.JSONDecodeError:
        fail = fail or "model output is not valid JSON"
        data = None
    if fail:
        # Respuesta incompleta: se conservan las líneas que el 27B llegó a escribir enteras (no se tira su lectura);
        # la factura va a revisión igualmente por el motivo `fail`.
        log(f"  27B: {fail} ({budget} fichas); respuesta: {content}")
        lines = complete_objects(content, "lineas")
        return ({"lineas": lines, "total": "", "total_valor": 0, "total_is_printed": False} if lines else None), fail
    log(f"  27B devolvió {len(data.get('lineas', []))} línea(s), total «{data.get('total', '')}» (max_tokens {budget})")
    log(f"  27B respuesta: {content}")  # tal cual, para poder auditar qué leyó el modelo y qué hizo el código
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
                "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "cache_prompt": False, "max_tokens": 2048,
                "response_format": {"type": "json_schema", "json_schema": {"name": "items", "schema": VL_SCHEMA}},
            }
            rj = post_json(f"{server}/v1/chat/completions", payload, timeout, log=log)
            content = rj["choices"][0]["message"]["content"]
            log(f"  Qwen2.5-VL respuesta p{i + 1}: {content}")  # tal cual, para auditar
            data = json.loads(content)
            for it in data.get("items", []):
                # El VL devuelve texto (prompt del cookbook): solo se usa una cifra con UNA lectura posible. Vacía o
                # ambigua ("1.000") -> sin valor: ese artículo no puede completar ningún cuadre (nunca 1 por defecto).
                q = vl_value(it.get("quantity"))
                qty = int(q) if q is not None and q >= 1 and q == q.to_integral_value() else None
                unit = cents(vl_value(it.get("discounted_unit_price"))) or cents(vl_value(it.get("unit_price")))
                lines.append({"desc": str(it.get("description", "")).strip(), "qty": qty, "unit": unit,
                              "amount": f"{qty * float(unit):.2f}" if unit and qty else "", "src": "qwen-vl",
                              "page_rel": pages.index(i) + 1})
            if not total and vl_value(data.get("total")) is not None:
                total = cents(vl_value(data.get("total")))
            log(f"  Qwen2.5-VL p{i + 1} a {dpi} dpi: {len(data.get('items', []))} artículo(s), total «{data.get('total', '')}»")
    finally:
        doc.close()
    return lines, total


# ----------------------------------------------------------------------------- campos adicionales desde el OCR

AMT_SQ = r"(-?\d[\d.,]*(?:[.,]\d{2}|,-))"  # importe dentro de texto sin espacios
VL_CELL_RE = re.compile(r'<td title="recuperado con Qwen2\.5-VL">.*?</td>', re.S)  # celda del rescate visual de la fase 1
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
    # el "sin dígito delante" solo vale para "Nx" (que "12x" no cuente como "2x"); "qty:N" puede ir pegado al precio
    return bool(re.search(rf"(?:(?:qty|quantity):?{qty}|[x×]{qty}|(?<!\d){qty}[x×])(?!\d)", after))


def qty_leads_desc(qty, md, desc):
    """La cantidad impresa al inicio del renglón, justo antes de la descripción ("2 NILFISK - Filter Bags...",
    columna "Quantity | Item name" que el OCR leyó como texto)."""
    words = re.findall(r"\w+", desc or "")[:3]
    if qty < 1 or len(words) < 2:
        return False
    pat = r"\W+".join(re.escape(w) for w in words)
    return bool(re.search(rf"(?mi)^\W*{qty}\s+{pat}", md or ""))


QTY_SRC = {"glm": "leido", "qwen": "leido"}


def line_evidence(l, sq_text, ocr_amounts, arithmetic_ok, ocr_units=frozenset()):
    """Origen de cada valor de la línea (ver docstring del módulo). ocr_units: precios impresos con más de
    dos decimales, redondeados (cuentan como precio unitario literal)."""
    src = l.get("src", "")
    if src in ("glm", "qwen"):  # fase 1: filas "N of:" leídas de la tabla; 'qwen' = precio rescatado del recorte
        return {"descripcion": "leido", "cantidad": "leido",
                "precio_unitario": "leido" if src == "glm" else "aritmetica", "importe_linea": "calculado"}
    if src == "tabla":  # fila de tabla del OCR que el 27B omitió: los cuatro valores son literales de esa fila
        return {"descripcion": "leido", "cantidad": "leido", "precio_unitario": "leido", "importe_linea": "leido"}
    ev = {}
    key = p1.squash(l.get("desc", ""))[:20]
    # descripción recompuesta en un salto de página (fix_page_breaks): fragmentos de GLM de renglones distintos
    # («B7177» + «LUBE SPIN-ON»); es literal si lo es cada fragmento
    parts = l.get("desc_parts")
    ev["descripcion"] = "leido" if (all(p1.squash(p) in sq_text for p in parts) if parts else key and key in sq_text) else "modelo"
    unit, amt = l.get("unit") or "", l.get("amount") or ""
    ev["precio_unitario"] = ("leido" if unit in ocr_amounts or unit in ocr_units
                             else "aritmetica" if arithmetic_ok else "modelo") if unit else ""
    if src == "qwen-vl" or l.get("amount_calc"):
        ev["importe_linea"] = "calculado" if amt else ""
    else:
        ev["importe_linea"] = ("leido" if amt in ocr_amounts else "aritmetica" if arithmetic_ok else "modelo") if amt else ""
    if l.get("qty_printed") or qty_printed(l["qty"], sq_text, key):
        ev["cantidad"] = "leido"
    elif l.get("qty_vl"):  # leída en la imagen (Amazon la pone sobre la foto) y confirmada por el cuadre exacto
        ev["cantidad"] = "imagen" if arithmetic_ok else "modelo"
    elif l["qty"] == 1:
        ev["cantidad"] = "asumido"      # una unidad por defecto; el cuadre con el total lo corrobora
    elif qty_backed_by_line(l, ev) or arithmetic_ok:
        ev["cantidad"] = "aritmetica"   # p. ej. "x2" ilegible: solo lo sostiene cantidad x precio = total
    else:
        ev["cantidad"] = "modelo"
    return ev


MONEY_CELL_RE = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\.\d{2}$")


def table_item_rows(page_mds):
    """Filas de las tablas HTML del OCR (regiones) que son un artículo completo y literal: una celda de
    descripción, una cantidad entera, un precio unitario y un importe tales que cantidad x precio = importe."""
    rows = []
    for k, pmd in enumerate(page_mds):
        for tr in re.findall(r"<tr>(.*?)</tr>", pmd, re.S):
            cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            money_i = [i for i, c in enumerate(cells) if MONEY_CELL_RE.match(c)]
            texts = [c for c in cells if re.search(r"[A-Za-z]{3}", c)]
            if len(money_i) < 2 or not texts:
                continue
            amt = money(cells[money_i[-1]])
            qtys = {int(c) for c in cells if re.fullmatch(r"\d{1,5}", c)}
            # el precio unitario es la celda de importe con la que cantidad x precio = importe (no siempre la penúltima:
            # una columna «Tax Amount» $0.00 entre precio y total hacía descartar todas las filas, 31661)
            q = unit = None
            for ui in reversed(money_i[:-1]):
                u = money(cells[ui])
                q = next((q for q in sorted(qtys) if q and u and float(u) > 0 and abs(q * float(u) - float(amt)) < 0.01), None)
                if q:
                    unit = u
                    break
            if q and float(amt) > 0:
                # descripción = todas las celdas de texto de la fila (nombre + talla/SKU), todas literales
                rows.append({"desc": " ".join(texts), "texts": texts, "qty": q, "unit": unit, "amount": amt, "page_rel": k + 1})
    return rows


def desc_key(text):
    return re.sub(r"[^a-z0-9]", "", html.unescape(text or "").lower())


def missing_table_rows(page_mds, lines, diff):
    """Filas de tabla del OCR que el 27B no devolvió y que completan EXACTAMENTE la diferencia de la suma.
    Cada línea del 27B consume una fila (mismo importe y descripción parecida); de las sobrantes se busca un
    subconjunto (hasta 3 filas) que sume la diferencia. Solo si hay una única solución; si no, nada."""
    rows = table_item_rows(page_mds)
    free = list(range(len(rows)))
    # parecido con CUALQUIER celda de texto de la fila (la más larga puede ser la talla/SKU y no el nombre, 31661)
    sim = lambda r, d: max(difflib.SequenceMatcher(None, desc_key(t), desc_key(d)).ratio() for t in r.get("texts") or [r["desc"]])
    for l in lines:
        cands = [i for i in free if rows[i]["amount"] == l.get("amount")]
        if cands:
            best = max(cands, key=lambda i: sim(rows[i], l.get("desc")))
            if sim(rows[best], l.get("desc")) >= 0.8:
                free.remove(best)
    free = free[:40]
    for size in (1, 2, 3):
        sols = [c for c in itertools.combinations(free, size) if abs(sum(float(rows[i]["amount"]) for i in c) - diff) < 0.01]
        if len(sols) == 1:
            return [rows[i] for i in sols[0]]
        if sols:
            return []  # varias formas de completar la diferencia: ambiguo, lo decide una persona
    return []


FINE_NUM_RE = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{3,5}(?![\d])")


def fine_nums(text):
    """Números con 3 a 5 decimales del texto, sin separador de miles ("2,141.040" -> "2141.040")."""
    return [x.replace(",", "") for x in FINE_NUM_RE.findall(text or "")]


def fine_amounts(text):
    """Precios impresos con 3 a 5 decimales (421.965, 4.6200, 3.13317): su redondeo y su truncado a céntimos (el 27B
    y money() pueden devolver cualquiera de los dos)."""
    from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
    out = set()
    for x in fine_nums(text):
        out |= {str(Decimal(x).quantize(Decimal("0.01"), rounding=r)) for r in (ROUND_HALF_UP, ROUND_DOWN)}
    return out


def exact_cents(text):
    """Importes impresos con 3 a 5 decimales que son céntimos exactos ("1975.0000", "12.500", "2,141.040"): literales
    sin redondeo."""
    return {f"{float(x):.2f}" for x in fine_nums(text) if set(x.split(".")[1][2:]) <= {"0"}}


def exact_unit(raw, text):
    """Precio unitario con 3 a 5 decimales tal como está impreso ("1.9750"), si aparece así en el texto: para
    cantidad x precio sin truncarlo a céntimos (1000 x 1.9750 = 1975.00, no 1000 x 1.97)."""
    from decimal import Decimal
    m = fine_nums(str(raw or ""))
    return Decimal(m[0]) if m and m[0] in fine_nums(text) else None


def qty_tolerance(qty, unit):
    """Margen de cantidad x precio frente al importe cuando el precio impreso está redondeado a céntimos
    (o tiene más decimales): qty x 0,005, pero siempre menor que medio precio para que la cantidad sea única."""
    return max(0.0099, min(qty * 0.005 + 0.005, float(unit) / 2 - 0.001))


def qty_matches(qty, unit, amt, exact=None):
    """cantidad x precio = importe. Con el precio impreso completo (exact, 3-5 decimales) la cuenta es exacta al
    céntimo; con el precio en céntimos, con el margen de redondeo (qty_tolerance)."""
    if exact is not None and amt and qty >= 1:
        return abs(qty * float(exact) - float(amt)) < 0.006
    return bool(unit and amt) and qty >= 1 and abs(qty * float(unit) - float(amt)) <= qty_tolerance(qty, unit)


def table_rows(md):
    """Celdas de cada fila de las tablas HTML del OCR (regiones)."""
    return [[re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            for tr in re.findall(r"<tr>(.*?)</tr>", md or "", re.S)]


def qty_from_table(l, rows):
    """Cantidad impresa en la misma fila de tabla que la descripción (columna "Quantity", "Qty Ship"...).
    Devuelve la cantidad de esa fila que cumple cantidad x precio = importe; si no hay precio con que
    comprobarla, la cantidad del 27B solo si aparece tal cual en la fila. None si la fila no lo respalda."""
    key = desc_key(l.get("desc"))[:20]
    if len(key) < 8:
        return None
    for cells in rows:
        if key not in desc_key(" ".join(cells)):
            continue
        # la cantidad sola en su celda, o con la unidad de medida ("144 EA", "2 ROL")
        ints = sorted({int(m.group(1).replace(",", "")) for c in cells
                       for m in [re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d{1,5})(?:\s+[A-Za-z]{1,4})?", c)] if m})
        unit, amt = l.get("unit") or "", l.get("amount") or ""
        fit = [q for q in ints if qty_matches(q, unit, amt, l.get("unit_exact"))]
        if len(fit) == 1:
            return fit[0]
        if l["qty"] in ints:  # p. ej. eBay: "Item price" es el total de la línea; la cantidad es su propia celda
            return l["qty"]
    return None


def summary_line_as_goods(lines, fits):
    """Índice de la línea de "artículo" que en realidad es el resumen de los demás (eBay "1 item $X" /
    "1 object $X", fila "MERCHANT/SAMPLE" o por partida arancelaria con el total), o None.
    Solo si TODO se cumple: hay >= 2 artículos con importe; su importe es exactamente la suma de los demás;
    con ella la factura NO cuadra con lo impreso y sin ella SÍ (fits(suma) lo decide: subtotal impreso o
    cuenta de cabecera). Con varias candidatas (dos líneas iguales: un solo producto real), se quita la de
    descripción claramente más corta; si no hay una claramente más corta, None (lo decide una persona)."""
    if len(lines) < 2 or any(not l.get("amount") for l in lines):
        return None
    total = sum(float(l["amount"]) for l in lines)
    if fits(total):
        return None
    cands = [i for i, l in enumerate(lines)
             if abs(float(l["amount"]) - (total - float(l["amount"]))) < 0.005 and fits(total - float(l["amount"]))]
    if len(cands) == 1:
        return cands[0]
    if len(cands) == 2:
        a, b = sorted(cands, key=lambda i: len(desc_key(lines[i].get("desc"))))
        la, lb = len(desc_key(lines[a].get("desc"))), len(desc_key(lines[b].get("desc")))
        if la * 2 <= lb and lb >= 12:  # p. ej. "1object" (7) frente al nombre del producto
            return a
    return None


def single_missing_amount(known, items_subtotal, total, cands, descuentos, otros, literal, charges=frozenset(), max_items=6):
    """Importe del único artículo sin importe: el que hace cuadrar el resumen impreso (subtotal de artículos, o
    total con alguna combinación de cargos/descuentos leídos), SOLO si es un único valor, está literal en el
    OCR (`literal`) y no es un importe que el documento ya identifica como envío, impuesto, descuento, cargo o
    pago (`charges`): un cargo no se convierte en artículo. Ambiguo o no impreso: None."""
    ok = lambda v: v in literal and v not in charges
    if items_subtotal:
        d = round(float(items_subtotal) - known, 2)
        return f"{d:.2f}" if d > 0 and ok(f"{d:.2f}") else None
    if not total:
        return None
    descuentos, otros = descuentos[:max_items], otros[:max_items]
    val = lambda xs: sum(float(x["importe"] or 0) for x in xs)
    subsets = lambda xs: (list(c) for k in range(len(xs) + 1) for c in itertools.combinations(xs, len(xs) - k))
    found = set()
    for e in cands["envio"]:
        for t in cands["impuestos"]:
            for o in subsets(otros):
                for dd in subsets(descuentos):
                    d = round(float(total) - known - float(e or 0) - float(t or 0) - val(o) + val(dd), 2)
                    if d > 0 and ok(f"{d:.2f}"):
                        found.add(f"{d:.2f}")
    return found.pop() if len(found) == 1 else None


def choose_header(base, total, cands, descuentos, otros, max_items=6):
    """Primera combinación (envío, impuestos, descuentos, otros cargos) con la que
    base + envío + impuestos + otros − descuentos = total. Preferencia: candidatos en su orden (etiqueta,
    27B, sin cargo) y quitar el menor número de descuentos/cargos. None si ninguna cuadra."""
    descuentos, otros = descuentos[:max_items], otros[:max_items]
    val = lambda xs: sum(float(x["importe"] or 0) for x in xs)

    def subsets(xs):  # de más completo a menos: quitar 0, 1, 2... elementos
        for k in range(len(xs) + 1):
            for drop in itertools.combinations(range(len(xs)), k):
                yield [x for i, x in enumerate(xs) if i not in drop]
    for e in cands["envio"]:
        for t in cands["impuestos"]:
            fixed = base + float(e or 0) + float(t or 0)
            for o in subsets(otros):
                for d in subsets(descuentos):
                    if abs(fixed + val(o) - val(d) - total) < 0.01:
                        return e, t, d, o
    return None


def vl_quantities(lines, vl_lines, literal, sq_text, goal):
    """Cantidades que solo se ven en la imagen (Amazon las pone sobre la foto del producto; el OCR no las lee).
    Las líneas del 27B se quedan; de Qwen2.5-VL solo se toma la CANTIDAD del mismo artículo (mismo precio
    unitario literal y descripción casi igual), nunca sobre una cantidad impresa. Se acepta solo si entonces
    cantidad x precio de TODOS los artículos da exactamente lo impreso (goal); si no, None."""
    used, out, changed = set(), [], 0
    for l in lines:
        unit = l.get("unit") if literal(l.get("unit")) else (l.get("amount") if l["qty"] == 1 and literal(l.get("amount")) else "")
        if not unit or float(unit) <= 0:
            return None
        key = desc_key(l.get("desc"))[:30]
        m = None
        if not (l.get("qty_printed") or qty_printed(l["qty"], sq_text, p1.squash(l.get("desc", ""))[:20])):
            sim = lambda k: difflib.SequenceMatcher(None, desc_key(vl_lines[k]["desc"])[:30], key).ratio()
            cands = [k for k, v in enumerate(vl_lines) if k not in used and v.get("unit") == unit and (v["qty"] or 0) >= 1]
            best = max(cands, key=sim, default=None)
            if best is not None and sim(best) >= 0.8:
                m = best
                used.add(m)
        q = vl_lines[m]["qty"] if m is not None else l["qty"]
        if q != l["qty"]:
            changed += 1
            out.append(dict(l, qty=q, unit=unit, amount=f"{q * float(unit):.2f}", amount_calc=True, qty_vl=True))
        else:
            out.append(l)
    s = lines_sum(out)
    return out if changed and s is not None and abs(s - goal) < 0.01 else None


# Cantidad sobre la FOTO del artículo (Amazon pinta «2», «10» en la foto; ningún OCR la lee). Calibrado el 2026-10-20 con
# 53 fotos de 28 documentos (verdad fijada a mano contra la imagen; 14 con cantidad, varias con cifras impresas en el
# envase): Qwen2.5-VL-7B con la PÁGINA ENTERA leía los números manuscritos del margen como precios (31732: «31835» ->
# $318.35) y fundía artículos repetidos (11/19); con el RECORTE de la foto + pregunta cerrada (JSON entero) a >= 1024
# fichas de imagen (aviso de llama.cpp clip.cpp para Qwen-VL, issue 16842), 53/53. repeat_penalty y gramática: sin efecto.
# La foto se localiza con PP-OCRv6: bloque «Sold by» alineado a la izquierda con el precio suelto debajo.
PHOTO_PRICE_RE = re.compile(r"^\$\d{1,3}(?:,\d{3})*\.\d{2}$")
PHOTO_QTY_PROMPT = ("This is the photo of one purchased item from an order page. If a small number badge printed on the "
                    "photo shows how many units were bought, return that number as quantity. If there is no such badge, "
                    "return 1.")
PHOTO_QTY_SCHEMA = {"type": "object", "properties": {"quantity": {"type": "integer"}}, "required": ["quantity"]}


def item_photos(pdf, page_idx):
    """Fotos de artículo de una página (diseño de Amazon): [(precio, rect en puntos PDF)] en orden de lectura.
    Artículo = precio suelto («$19.99») con un «Sold by» alineado encima; la foto está a su izquierda, de la altura
    del bloque (descripción, vendedor, precio)."""
    toks, _, height = ppocr_tokens(pdf, page_idx)
    sold = [t for t in toks if t[4].lower().startswith("sold by")]
    out = []
    for x0, y0, x1, y1, t in sorted(toks, key=lambda t: (t[1], t[0])):
        if not PHOTO_PRICE_RE.match(t):
            continue
        s = [u for u in sold if abs(u[0] - x0) <= 20 and 0 < y0 - u[1] < 260]
        if not s:
            continue
        top = max(s, key=lambda u: u[1])[1]
        for _ in range(4):  # descripción en varias líneas encima, sin pasar al artículo anterior
            up = [u for u in toks if abs(u[0] - x0) <= 20 and 0 < top - u[3] < 30]
            if not up or any(PHOTO_PRICE_RE.match(u[4]) or u[4].lower().startswith(("sold by", "return", "buy it")) for u in up):
                break
            top = min(u[1] for u in up)
        h = (y1 + 45) - (top - 15)
        box = (max(0, x0 - 1.15 * h), max(0, top - 15), x0 - 8, min(height, y1 + 45))
        if box[2] - box[0] > 20:
            out.append((money(t), fitz.Rect(*(v * 72 / PPOCR_DPI for v in box))))
    return out


def photo_quantity(pdf, page_idx, rect, server, timeout):
    """Cantidad pintada en la foto según Qwen2.5-VL (el prompt le pide 1 si no hay); None si su respuesta no es un entero
    de 1 a 999. Recorte renderizado desde el PDF a >= 1024 fichas."""
    area = (rect.width / 72) * (rect.height / 72)
    dpi = int(min(max(200, (1024 * 28 * 28 / area) ** 0.5), (4096 * 28 * 28 / area) ** 0.5))
    with fitz.open(pdf) as doc:
        img = base64.b64encode(doc[page_idx].get_pixmap(dpi=dpi, clip=rect, alpha=False).tobytes("png")).decode("ascii")
    rj = post_json(f"{server}/v1/chat/completions", {
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                                                 {"type": "text", "text": PHOTO_QTY_PROMPT}]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "cache_prompt": False, "max_tokens": 20,
        "response_format": {"type": "json_schema", "json_schema": {"name": "q", "schema": PHOTO_QTY_SCHEMA}}}, timeout)
    q = json.loads(rj["choices"][0]["message"]["content"]).get("quantity")
    # Fuera de 1..999 o no entero: sin lectura (None), nunca 1 por defecto; esa foto no aporta cantidad.
    return q if isinstance(q, int) and 1 <= q <= 999 else None


def photo_quantities(pdf, pages, lines, literal, server, timeout, log):
    """Lecturas de cantidad sobre las fotos, en la forma que usa vl_quantities ([{desc, unit, qty}]): cada foto se
    empareja con la línea del 27B de la MISMA página y el MISMO precio literal, en orden de aparición. Vacío si la
    página no tiene fotos de artículo localizables (otros diseños)."""
    out = []
    for k, page_idx in enumerate(pages):
        on_page = [l for l in lines if l.get("page_rel") == k + 1]
        try:
            photos = item_photos(pdf, page_idx) if on_page else []
        except Exception as e:  # noqa: BLE001  sin geometría, sin lectura de fotos (sigue el rescate de siempre)
            log(f"  PP-OCRv6 no disponible ({type(e).__name__}): sin lectura de cantidades en las fotos")
            return []
        used = set()
        for price, rect in photos:
            l = next((l for l in on_page if id(l) not in used and price and
                      price == (l.get("unit") if literal(l.get("unit")) else l.get("amount") if l["qty"] == 1 else "")), None)
            if l is None:
                continue
            used.add(id(l))
            q = photo_quantity(pdf, page_idx, rect, server, timeout)
            log(f"  foto p{page_idx + 1} ${price}: cantidad {q} según Qwen2.5-VL «{l['desc'][:40]}»")
            if q is None:  # sin lectura válida: esta foto no aporta; el cuadre exacto decidirá sin ella
                continue
            out.append({"desc": l["desc"], "unit": price, "qty": q})
    return out


def qty_backed_by_line(l, ev):
    """Cantidad no impresa con un formato reconocible ("3 EA 3"), pero sostenida por la propia línea:
    precio unitario e importe literales en el OCR y cantidad x precio = importe (con el margen de
    redondeo del precio impreso)."""
    unit, amt = l.get("unit") or "", l.get("amount") or ""
    return ev.get("precio_unitario") == ev.get("importe_linea") == "leido" and qty_matches(l["qty"], unit, amt, l.get("unit_exact"))


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
                    cur = {"estado": re.sub(r"\s+", " ", cells[0]).strip(), "items": [], "subtotal": "", "descuento": "", "envio": "",
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
                    # La fase 1 lanza el rescate visual cuando el envío no cuadra, aunque GLM-OCR SÍ haya leído el
                    # precio (p. ej. el total del envío ya trae un cupón descontado). Si GLM leyó el mismo precio
                    # que el VL, el precio es literal del OCR: no cuenta como rescatado.
                    vl = 'title="recuperado con Qwen2.5-VL"' in raw
                    if vl:
                        glm = [c for c in (f2_unescape(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", VL_CELL_RE.sub("", raw), re.S)) if c]
                        glm_price = p1.first_amount(" ".join(glm[1:])) if len(glm) > 1 else None
                        vl = not (glm_price is not None and price is not None and abs(glm_price - price) < 0.005)
                    item = {"qty": int(m.group(1)), "price": price, "desc": p1.item_description(cells[0]),
                            "text": cells[0][:60], "page_rel": pi + 1, "ri": ri, "vendedor": "", "condicion": "",
                            "src": "qwen" if vl else "glm"}
                    cur["items"].append(item)
                    cur["last_item"] = item
                if cur["last_item"] is not None and not re.search(r"subtotal|shipping|total|tax", norm):
                    sel, cond = SELLER_RE.search(txt), CONDITION_RE.search(txt)
                    if sel and not cur["last_item"]["vendedor"]:
                        cur["last_item"]["vendedor"] = sel.group(1).strip()
                    if cond and not cur["last_item"]["condicion"]:
                        cur["last_item"]["condicion"] = cond.group(1)
                for key, pats in (("subtotal", [r"item\(?s?\)?subtotal:?(?:usd)?\$?" + AMT_SQ]),
                                  ("descuento", [r"(?:savings|discount|promotionapplied):?-?(?:usd)?-?\$?" + AMT_SQ]),
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
    # El total del envío puede traer envío, impuestos y un cupón: cuadra si artículos + envío + impuestos − descuento
    # (todos impresos en el propio bloque) = total del envío.
    extra = sum(float(block[k] or 0) for k in ("envio", "impuestos")) - float(block.get("descuento") or 0)
    derived = block.get("total_derived")  # Grand Total impreso - los demás envíos impresos (ver build_all)
    if not total and derived:
        total = derived
        if s is None or abs(s + extra - float(derived)) >= 0.01:
            issues.append(f"shipment block without a printed total; order Grand Total {block['order_total']} - other shipments "
                          f"{block['others_total']} = {derived} for this block, but its lines add "
                          + ("?" if s is None else f"{s:.2f}" + (f" + charges {extra:.2f}" if extra else "")
                             + f" (difference {s + extra - float(derived):+.2f}; items possibly listed in another shipment)"))
    elif not total:
        issues.append("shipment block without a printed total; total computed as the sum of lines")
        total = f"{s:.2f}" if s is not None else ""
    ok = (total_printed or bool(derived)) and s is not None and abs(s - float(total)) < 0.01
    if (total_printed or derived) and s is not None and not ok and extra and abs(s + extra - float(total)) < 0.01:
        ok = True
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

# ----------------------------------------------------------------------------- artículo partido por un salto de página
# GLM-OCR lee cada página como texto SIN posiciones. En las tablas que imprimen la descripción DEBAJO del código
# (21750: «0003 B309 $59.87» y «HIGH PERFORM. GLASS F-FLU» ya en la página siguiente), la descripción del último
# artículo de una página queda arriba de la siguiente y GLM empareja cada fila con el renglón de ENCIMA: toda la
# página siguiente sale corrida un artículo (y el 27B lo copia). Ningún método de solo texto lo distingue.
# PP-OCRv6 (detección + reconocimiento, PP-OCRv6_medium, no generativo) da cada renglón CON su caja: con la
# geometría se ve qué renglones quedan entre la cabecera de la tabla y el primer artículo de la página siguiente.
# Servicio en el host de modelos (scania-mdl-ppocr, GPU0, :8089; mismo resultado que en CPU, ~1 s/página).
# Medido el 2026-10-20 en los 178 PDF de varias páginas (405 saltos): 5 continuaciones, las 5 correctas contra la
# imagen, ningún falso positivo. GLM-OCR sigue siendo el lector: el texto que se coloca tiene que estar LITERAL en
# su OCR; cantidades e importes no se tocan.
PPOCR_SERVER = f"http://{p1.MODELS_HOST}:8089"
PPOCR_DPI = 200
# Importe en un renglón de PP-OCR: con «$» admite 3-4 decimales ($9.9776); sin «$», 2 decimales y sufijo de moneda
# opcional (46.79 US$, «USS» mal leído). Parte entera con o sin separador de miles (1,635.12 / 1635.12). Nunca
# seguido de otra cifra ni de «.cifra»: un teléfono «972.394.9422» o un código HTS «8421.23.0000» no son importes.
GEO_MONEY_RE = re.compile(r"(?<![\d.,])(?:\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,4}|(?:\d{1,3}(?:[,.]\d{3})+|\d+)[.,]\d{2}"
                          r"(?:\s?(?:US\$|USS|USD|\$))?)(?!\d|[.,]\d)")
_PPOCR_CACHE = {}  # (pdf, página) -> renglones; se vacía por documento en build_all


def ppocr_tokens(pdf, page_idx, timeout=120):
    """Cajas de PP-OCRv6 de una página a PPOCR_DPI: ([(x0, y0, x1, y1, texto)], ancho, alto) en píxeles."""
    key = (str(pdf), page_idx, "tokens")
    if key not in _PPOCR_CACHE:
        with fitz.open(pdf) as doc:
            pix = doc[page_idx].get_pixmap(dpi=PPOCR_DPI, alpha=False)
        r = requests.post(f"{PPOCR_SERVER}/ocr", data=pix.tobytes("png"), timeout=timeout)
        r.raise_for_status()
        j = r.json()
        _PPOCR_CACHE[key] = ([(b[0], b[1], b[2], b[3], t.strip()) for t, b in zip(j["texts"], j["boxes"]) if t.strip()],
                             pix.width, pix.height)
    return _PPOCR_CACHE[key]


def ppocr_rows(pdf, page_idx, timeout=120):
    """Renglones de una página según PP-OCRv6: [{c (centro y), toks [(x0, x1, texto)], text, money, span, alpha_x}]."""
    key = (str(pdf), page_idx)
    if key not in _PPOCR_CACHE:
        raw, width, _ = ppocr_tokens(pdf, page_idx, timeout)
        toks = sorted((y0, x0, x1, y1, t) for x0, y0, x1, y1, t in raw)
        rows = []
        for y0, x0, x1, y1, t in toks:  # renglón = cajas con el centro vertical a <= 10 px
            c = (y0 + y1) / 2
            if rows and abs(rows[-1]["c"] - c) <= 10:
                rows[-1]["toks"].append((x0, x1, t))
                rows[-1]["c"] = (rows[-1]["c"] + c) / 2
            else:
                rows.append({"c": c, "toks": [(x0, x1, t)]})
        for rw in rows:
            rw["toks"].sort()
            rw["text"] = " ".join(t for _, _, t in rw["toks"])
            rw["money"] = sum(len(GEO_MONEY_RE.findall(t)) for _, _, t in rw["toks"])
            rw["span"] = (rw["toks"][-1][1] - rw["toks"][0][0]) / width
            rw["alpha_x"] = [x0 for x0, _, t in rw["toks"] if re.search(r"[A-Za-z]{3,}", t)]
        _PPOCR_CACHE[key] = rows
    return _PPOCR_CACHE[key]


def _geo_norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def page_break_continuation(rows_a, rows_b):
    """Renglones al principio de la página B que continúan el último artículo de la página A, solo por geometría.
    Devuelve (renglón del último artículo de A, renglones huérfanos, (lo, hi) columna de descripción, motivo).
    Artículo = renglón con >= 2 importes (precio e importe). Reglas, todas generales:
      - la tabla cerró en A (tras su último artículo hay renglones de un solo importe: subtotal, impuestos, total) -> no;
      - candidatos: entre la última cabecera de tabla de B (renglón ancho, sin cifras) y su primer artículo, sin importe,
        con texto y alineados con la columna de descripción de A; todos los renglones con texto de esa franja, como mucho 3;
      - un renglón que ya está en la cabecera de la tabla de A es una segunda línea de cabecera («ORDERED SHIPPED»);
      - papel del renglón: en A, los renglones de texto entre dos artículos quedan más cerca del SIGUIENTE (lo encabezan:
        descripción encima del importe, cabecera de grupo) o del ANTERIOR (continúan su descripción). Se mira a los de la
        misma forma (mismas columnas) o, si no hay, a los de la columna de descripción: si encabezan, el huérfano es del
        primer artículo de B y no hay continuación."""
    anchors_a = [i for i, r in enumerate(rows_a) if r["money"] >= 2]
    anchors_b = [i for i, r in enumerate(rows_b) if r["money"] >= 2]
    if not anchors_a or not anchors_b:
        return None, [], None, "sin artículos en una de las páginas"
    last_a, first_b = anchors_a[-1], anchors_b[0]
    if any(r["money"] == 1 for r in rows_a[last_a + 1:]):
        return rows_a[last_a], [], None, "la tabla cerró en la página anterior"
    heads = [i for i in range(first_b) if rows_b[i]["span"] > 0.45 and rows_b[i]["money"] == 0
             and not re.search(r"\d", rows_b[i]["text"])]
    if not heads:
        return rows_a[last_a], [], None, "sin cabecera de tabla en la página siguiente"
    cand = [rows_b[i] for i in range(heads[-1] + 1, first_b)]
    desc_x = [x for r in rows_a[max(0, last_a - 6):last_a + 3] for x in r["alpha_x"]]
    if not desc_x:
        return rows_a[last_a], [], None, "sin columna de descripción"
    col = (min(desc_x) - 60, max(desc_x) + 40)
    head_a = {_geo_norm(r["text"]) for r in rows_a[:anchors_a[0]]} | \
        {_geo_norm(t) for r in rows_a[:anchors_a[0]] for _, _, t in r["toks"]}
    orph = [r for r in cand if r["money"] == 0 and r["alpha_x"] and col[0] <= min(r["alpha_x"]) <= col[1]
            and _geo_norm(r["text"]) not in head_a and not all(_geo_norm(t) in head_a for _, _, t in r["toks"])]
    if not orph or len(orph) > 3 or len(orph) != len([r for r in cand if r["alpha_x"]]):
        return rows_a[last_a], [], col, "sin continuación"
    gap = lambda r1, r2: abs(r2["c"] - r1["c"])
    between = [(r, gap(r, rows_a[b]) < gap(rows_a[a], r))
               for a, b in zip(anchors_a, anchors_a[1:]) for r in rows_a[a + 1:b] if r["alpha_x"] and r["money"] == 0]
    same_shape = lambda r, o: abs(r["toks"][0][0] - o["toks"][0][0]) <= 20 and len(r["toks"]) == len(o["toks"]) and \
        all(any(abs(x - y) <= 25 for y, _, _ in r["toks"]) for x, _, _ in o["toks"])
    for o in orph:
        pool = [up for r, up in between if same_shape(r, o)] or \
               [up for r, up in between if col[0] <= min(r["alpha_x"]) <= col[1]]
        if pool and sum(pool) > len(pool) - sum(pool):
            return rows_a[last_a], [], col, "el renglón encabeza el primer artículo de su página"
    return rows_a[last_a], orph, col, "continuación"


def _row_amount(row):
    """Importe de la línea en un renglón de artículo: el último importe (columna de importe extendido)."""
    vals = [m for _, _, t in row["toks"] for m in GEO_MONEY_RE.findall(t)]
    return money(vals[-1]) if vals else ""


def text_column(rows, fallback):
    """Columna de descripción para COMPONER el texto (la detección usa la suya). Se toma de los renglones de solo texto
    (sin importe) entre artículos: el borde izquierdo más frecuente (grupo de bordes a <= 20 px con más renglones) y,
    como borde derecho, el final de sus cajas con palabras. Las cabeceras de grupo empiezan en otra columna y los
    renglones de artículo traen cantidad, unidad, importes: no cuentan."""
    anchors = [i for i, r in enumerate(rows) if r["money"] >= 2][-12:]
    text_rows = [r for a, b in zip(anchors, anchors[1:]) for r in rows[a + 1:b] if not r["money"] and r["alpha_x"]]
    if not text_rows:
        return fallback
    left = sorted(r["toks"][0][0] for r in text_rows)
    groups = []
    for x in left:
        if groups and x - groups[-1][-1] <= 20:
            groups[-1].append(x)
        else:
            groups.append([x])
    g = max(groups, key=len)
    lo = g[0]
    right = [x1 for r in text_rows if lo <= r["toks"][0][0] <= g[-1] for x0, x1, t in r["toks"] if re.search(r"[A-Za-z]{3,}", t)]
    return (lo, max(right)) if right else fallback


def _col_toks(rows, col):
    """Fragmentos (cajas de PP-OCRv6) que tocan la columna de descripción, sin importes (fuera quedan cantidades,
    unidad «EA», importes; dentro, el código impreso justo a la izquierda de la descripción, «B7177»)."""
    return [t for r in rows for x0, x1, t in r["toks"] if x1 > col[0] and x0 < col[1] and not GEO_MONEY_RE.search(t)]


def _col_text(rows, col):
    return " ".join(_col_toks(rows, col)).strip()


def page_furniture(pdf, pages):
    """Texto de marco de página: renglones tras el último artículo que se repiten en 2+ páginas del documento
    («**** CONTINUED ****», pies). No es texto de ningún artículo."""
    seen = {}
    for p in pages:
        rows = ppocr_rows(pdf, p)
        anchors = [i for i, r in enumerate(rows) if r["money"] >= 2]
        for r in rows[anchors[-1] + 1:] if anchors else []:
            seen.setdefault(_geo_norm(r["text"]), set()).add(p)
    return {t for t, ps in seen.items() if t and len(ps) >= 2}


def glm_fragment(frag, words):
    """Texto de GLM-OCR que corresponde a un fragmento leído por PP-OCRv6 en la misma página: la geometría sale de
    PP-OCRv6, pero lo que se coloca es SIEMPRE texto de GLM (el lector del sistema). Mismo texto sin espacios ->
    se usa tal cual; si no, la ventana de palabras de GLM (mismo número ±1) más parecida, con similitud >= 0.85
    (los dos OCR difieren en alguna letra: «Compresslon» / «Compression»). Sin coincidencia clara -> None."""
    sq = p1.squash(frag)
    if not sq:
        return None
    n = len(frag.split())
    best, best_r = None, 0.0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(len(words) - size + 1):
            cand = " ".join(words[i:i + size])
            if p1.squash(cand) == sq:
                return cand
            r = difflib.SequenceMatcher(None, p1.squash(cand), sq).ratio()
            if r > best_r:
                best, best_r = cand, r
    return best if best_r >= 0.85 else None


def fix_page_breaks(lines, pdf, pages, page_texts, log):
    """Artículos partidos por un salto de página (ver arriba). Para cada salto con continuación:
      - el último artículo de la página A (mismo importe que el renglón de PP-OCRv6) recibe los renglones huérfanos;
        un «artículo» sin importe del 27B al principio de B que es ese mismo texto se quita (era la continuación);
      - solo si las descripciones de B quedaron CORRIDAS (el primer artículo de B, según el 27B, lleva el texto
        huérfano), se rehacen por geometría: cada artículo (renglón con importes) se queda con el texto de la columna
        de descripción de su renglón y de los renglones que le siguen hasta el próximo artículo (el papel que la
        geometría comprobó: la descripción va DEBAJO). Cada línea del 27B se empareja por importe, en orden;
        cantidades e importes no cambian.
    El texto colocado es el de GLM-OCR (glm_fragment, por fragmento: una línea de PP-OCRv6 puede juntar celdas que GLM
    leyó por separado). page_texts: {página: texto de GLM sin etiquetas}. Si algo no empareja o un fragmento no tiene
    texto de GLM, no se cambia nada en ese salto y se devuelve la incidencia (la factura va a revisión)."""
    issues = []

    parts_of = {}  # texto compuesto -> fragmentos de GLM (para la evidencia: cada uno es literal)

    def glm(toks, page):
        """Fragmentos de PP-OCRv6 -> texto de GLM de esa página (None si alguno no tiene correspondencia)."""
        words = page_texts.get(page, "").split()
        # un fragmento con menos de 2 letras/cifras (una marca, un «4» suelto) no aporta descripción ni se puede cotejar
        toks = [t for t in toks if len(re.sub(r"[^A-Za-z0-9]", "", t)) >= 2]
        out = [glm_fragment(t, words) for t in toks]
        if not toks or None in out:
            return None
        parts_of[" ".join(out)] = out
        return " ".join(out)

    for k in range(len(pages) - 1):
        goods = [l for l in lines if l.get("kind", "goods") == "goods"]
        if pages[k + 1] != pages[k] + 1:
            continue
        on_a = [l for l in goods if l.get("page_rel") == k + 1]
        on_b = [l for l in goods if l.get("page_rel") == k + 2]
        if not on_a or not on_b:
            continue
        try:
            rows_a, rows_b = ppocr_rows(pdf, pages[k]), ppocr_rows(pdf, pages[k + 1])
        except Exception as e:  # noqa: BLE001  sin geometría se queda como estaba (como antes de este paso)
            log(f"  PP-OCRv6 no disponible ({type(e).__name__}): sin comprobación del salto p{pages[k] + 1}-{pages[k] + 2}")
            continue
        last, orph, col, why = page_break_continuation(rows_a, rows_b)
        if not orph:
            continue
        where = f"page {pages[k] + 1}->{pages[k] + 2}"
        col = text_column(rows_a, col)
        furniture = page_furniture(pdf, pages)
        orph_toks = _col_toks(orph, col)
        orph_text = " ".join(orph_toks)
        # El 27B a veces convierte el renglón huérfano en un «artículo» SIN importe al principio de B: es la
        # continuación, no un artículo (se quita si su texto es el del huérfano).
        phantom = [l for l in on_b[:1] if not l.get("amount") and p1.squash(l.get("desc"))
                   and p1.squash(l["desc"]) in p1.squash(orph_text)]
        on_b = [l for l in on_b if l not in phantom]
        # artículos de B por geometría, en orden vertical: texto de su renglón + renglones siguientes hasta el próximo
        def block(rows, i, end):
            """Texto de un artículo: su renglón y los de texto que le siguen en la columna, hasta el próximo artículo,
            un renglón con importe, otra columna o un hueco (> 2 interlineados); sin el marco de página."""
            steps = sorted(b["c"] - a["c"] for a, b in zip(rows, rows[1:]))
            step = steps[len(steps) // 2] if steps else 0
            below, prev = [], rows[i]
            for r in rows[i + 1:end]:
                if r["money"] or not r["alpha_x"] or not _col_toks([r], col) or (step and r["c"] - prev["c"] > 2 * step):
                    break
                prev = r
                if _geo_norm(r["text"]) not in furniture:
                    below.append(r)
            return _col_toks([rows[i]] + below, col)

        orph_glm = glm(orph_toks, pages[k + 1])
        # ¿Corrimiento? Firma: el primer artículo de B (según el 27B) lleva el texto huérfano (GLM lo pegó a la primera
        # fila de B y todas las descripciones de B quedaron corridas un artículo, 21750). Solo entonces se rehacen las
        # descripciones de B por geometría; si no, B se queda como la dio el 27B (otros diseños: la descripción va en
        # la misma fila y el 27B la tiene bien; rehacerla arrastraría cabeceras de grupo y otras columnas, 31734).
        shifted = orph_glm is not None and bool(on_b) and p1.squash(orph_glm) in p1.squash(on_b[0].get("desc"))
        if shifted:
            # último artículo de A: su texto en A (código, primeras líneas; el 27B lo mezcló con el marco de página)
            # + el renglón huérfano, todo de GLM
            anchors_a = [i for i, r in enumerate(rows_a) if r["money"] >= 2]
            head_toks = block(rows_a, anchors_a[-1], len(rows_a))
            head_text = glm(head_toks, pages[k]) if head_toks else ""
            last_text = None if head_text is None else f"{head_text} {orph_glm}".strip()
            last_parts = parts_of.get(head_text, []) + parts_of.get(orph_glm, [])
        else:
            last_text = None if orph_glm is None else f"{on_a[-1]['desc']} {orph_glm}".strip()
            # la parte del 27B se juzga como siempre (sus primeros 20 caracteres, ver line_evidence)
            last_parts = [p1.squash(on_a[-1]["desc"])[:20]] + parts_of.get(orph_glm, [])
        anchors_b = [i for i, r in enumerate(rows_b) if r["money"] >= 2]
        geo_b = [(_row_amount(rows_b[i]), block(rows_b, i, anchors_b[n + 1] if n + 1 < len(anchors_b) else len(rows_b)))
                 for n, i in enumerate(anchors_b)] if shifted else []
        # emparejar en orden por importe (los renglones de B con importes que no son artículos se saltan)
        pairs, j = [], 0
        for l in on_b if shifted else []:
            while j < len(geo_b) and geo_b[j][0] != l.get("amount"):
                j += 1
            if j == len(geo_b):
                break
            pairs.append((l, glm(geo_b[j][1], pages[k + 1])))
            j += 1
        why_not = ("el último artículo de la página anterior no tiene el importe del renglón de PP-OCRv6 "
                   f"({_row_amount(last)} / {on_a[-1].get('amount')})" if _row_amount(last) != on_a[-1].get("amount") else
                   f"{len(on_b) - len(pairs)} artículo(s) de la página siguiente sin renglón con su importe"
                   if shifted and len(pairs) != len(on_b) else
                   "el renglón huérfano no tiene texto correspondiente en GLM-OCR" if last_text is None else
                   next((f"descripción sin texto correspondiente en GLM-OCR («{l['desc'][:40]}», {l.get('amount')})"
                         for l, d in pairs if d is None), ""))
        if why_not:
            issues.append(f"item description split by the page break ({where}): «{orph_text[:50]}» continues the last "
                          "item of the previous page; descriptions of the next page could not be re-aligned, review them")
            log(f"  salto {where}: continuación «{orph_text}» sin realinear: {why_not}")
            continue
        for l in phantom:
            lines.remove(l)
            log(f"  salto {where}: «{l['desc'][:40]}» sin importe era la continuación, no un artículo")
        old_a = on_a[-1]["desc"]
        on_a[-1]["desc"], on_a[-1]["desc_parts"] = last_text, last_parts
        log(f"  salto {where}: «{orph_text}» continúa el último artículo de la página anterior: «{old_a[:45]}» -> "
            f"«{on_a[-1]['desc'][:60]}» (geometría PP-OCRv6)")
        for l, d in pairs:
            if p1.squash(d) != p1.squash(l["desc"]):
                log(f"    descripción realineada: «{l['desc'][:45]}» -> «{d[:45]}» ({l.get('amount')})")
                l["desc"], l["desc_parts"] = d, parts_of.get(d, [d])
    return issues


def unit_price_candidates(lines):
    """Líneas de artículo con cantidad > 1 y un solo precio impreso (sin precio unitario distinto del importe): en ellas
    el importe impreso puede ser el total de la línea o el precio de UNA unidad («$9.49 x2»)."""
    return [l for l in lines if l.get("kind", "goods") == "goods" and l["qty"] > 1 and l.get("amount")
            and (not l.get("unit") or l["unit"] == l["amount"])]


def build_invoice(inv_id, res, inv, use_llm, llm_server, vl_server, timeout, log, vl=True, unit_alt=False):
    pages = inv["pages"]
    regions = [res["region_pages"][i] if i < len(res["region_pages"]) else [] for i in pages]
    full_texts = [res["full_text"].get(i) or "" for i in pages]
    md = "\n\n---\n\n".join(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions))
    ocr_amounts = doc_amounts(md) | exact_cents(md)  # "1975.0000" también es literal
    # también sin etiquetas HTML ni entidades: en las tablas del OCR la descripción viene partida con <br> ("4130158083<br>4130
    # Chromoly...") o con &quot;, y no se encontraría literal aunque esté leída
    sq_text = p1.squash(md) + "|" + p1.squash(html.unescape(re.sub(r"<[^>]+>", " ", md)))
    divisa = currency_of(md)
    issues = []
    # Con qué deben cuadrar las líneas: el "Item(s) Subtotal" del pedido. El formato de escritorio
    # de Amazon imprime uno por envío y el global al final (caja de pago): se toma el último.
    subs = ITEMS_SUBTOTAL_RE.findall(md)
    items_subtotal = money(subs[-1]) if subs else ""

    shipments, unverified, grand = p1.check_shipments(regions)
    # Totales impresos candidatos, en orden de preferencia: el "Grand Total" de las regiones (fase 1), el de la
    # etiqueta en el texto y (más abajo) el del 27B con respaldo en el OCR. En la vía del 27B se queda el primero
    # con el que cuadra el resumen: en una tabla con las etiquetas en una columna y los valores en otra, la fase 1
    # empareja "Grand Total" con el primer importe de la columna (31814: 2.00 de handling en vez de 29,439.60).
    m = ORDER_TOTAL_RE.search(md)
    total_cands = list(dict.fromkeys(v for v in (f"{grand:.2f}" if grand is not None else "",
                                                 money(m.group(1)) if m else "") if v))
    total = total_cands[0] if total_cands else ""
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
        lines, model_totals, model_currencies = [], [], set()
        for k, (i, regs) in enumerate(zip(pages, regions)):
            data, fail = lines_with_llm(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0], llm_server, timeout, log)
            if fail:  # incompleta: van las líneas que llegó a escribir enteras (si hay) y la factura a revisión
                read_failed = True
                issues.append(f"item list on page {i + 1} could not be read completely ({fail}); review against the paper")
            if data is None:
                continue
            for l in data.get("lineas", []):
                # Cifras: el VALOR lo da el 27B (número JSON); el código comprueba que es una lectura de lo que copió
                # como impreso y, si no, lo anota (revisión). Nunca se sustituye por una parte del texto, por 1 ni sin signo.
                kind = l.get("line_kind") if l.get("line_kind") in LINE_KINDS else "goods"
                what = f'{"item" if kind == "goods" else kind} «{str(l.get("descripcion", ""))[:40]}»'
                amt = cents(model_number(l.get("importe_linea"), l.get("importe_valor"), f"amount of {what}", issues))
                unit_v = model_number(l.get("precio_unitario"), l.get("precio_unitario_valor"), f"unit price of {what}", issues)
                qty = model_number(l.get("cantidad"), l.get("cantidad_valor"), f"quantity of {what}", issues)
                if kind == "goods":
                    if qty is None:
                        issues.append(f'quantity of {what} not returned by the model ("{l.get("cantidad", "")}"); review against the paper')
                    elif qty == 0:
                        issues.append(f"quantity 0 for {what}; review against the paper")
                    if amt and str(l.get("importe_linea", "")).strip().startswith("-"):
                        issues.append(f"negative amount for {what}; review against the paper")
                qty = 1 if qty is None else (int(qty) if qty == qty.to_integral_value() else float(qty))
                # Solo los artículos: un importe de resumen sin respaldo no se usa (ver "backed"), no es incidencia.
                if amt and amt not in ocr_amounts and kind == "goods":
                    issues.append(f'amount {amt} not found in the OCR text ("{l.get("descripcion", "")[:40]}")')
                lines.append({"desc": l.get("descripcion", "").strip(), "qty": qty,
                              "amount": amt, "unit": cents(unit_v), "src": "qwen27b", "page_rel": k + 1,
                              "unit_exact": exact_unit(l.get("precio_unitario"), md),
                              "neg": bool(amt) and str(l.get("importe_linea", "")).strip().startswith("-") and printed_negative(amt, md),
                              "kind": kind,
                              "order": len(lines),
                              "extra": {"vendedor": l.get("vendedor", ""), "seguimiento": l.get("seguimiento", ""),
                                        "estado_envio": l.get("estado_envio", "")}})
                d = str(l.get("divisa") or "").strip().upper()
                if ISO_CURRENCY_RE.match(d):
                    model_currencies.add(d)
                    divisa = d
                elif d:
                    issues.append(f'currency "{l.get("divisa")}" returned by the model for {what} is not an ISO 4217 code; review against the paper')
            data["_total"] = cents(model_number(data.get("total"), data.get("total_valor"), "total", issues))
            if data["_total"]:
                model_totals.append(data)
        if len(model_currencies) > 1:  # una sola divisa por factura en el esquema: no se elige una en silencio
            issues.append(f"the model returned several currencies ({', '.join(sorted(model_currencies))}); review against the paper")
        # El total del documento: el de la última página que lo trae CON respaldo en el OCR (una página
        # puede devolver un importe que no está impreso); si ninguno lo tiene, el último (va a incidencia).
        backed_totals = [d for d in model_totals if d["_total"] in ocr_amounts]
        model_total = (backed_totals or model_totals or [None])[-1]
        t = model_total["_total"] if model_total else ""
        if t and t in ocr_amounts and t not in total_cands:
            total_cands.append(t)
        if t and not total:
            if t in ocr_amounts:
                total, total_printed = t, bool(model_total.get("total_is_printed", True))
            else:
                issues.append(f"total {t} returned by the model not found in the OCR text")
        source = "llm"
        if len(pages) > 1:
            glm_pages = {i: html.unescape(re.sub(r"<[^>]+>", " ", p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0]))
                         for i, regs in zip(pages, regions)}
            issues += fix_page_breaks(lines, res["pdf"], pages, glm_pages, log)
    else:
        lines, source = [], "ninguna"
        issues.append("no shipment blocks and model extraction disabled")
    # Las líneas que el 27B clasifica como no-mercadería (envío, impuesto, descuento, subtotal/total, otros cargos)
    # se apartan: todo lo que sigue (sumas, rescate visual, evidencia, ESTRUCTURA) trabaja solo con las "goods".
    # Se devuelven al final con su line_kind (WTCP solo declara las goods).
    others = [l for l in lines if l.get("kind", "goods") != "goods"]
    lines = [l for l in lines if l.get("kind", "goods") == "goods"]
    ocr_units = fine_amounts(md)
    if source == "llm":
        # Respaldo de la cantidad del 27B (solo comprobación, nunca sustitución): impresa en la misma fila de tabla,
        # «xN»/«Qty N» junto a la descripción o al inicio del renglón; si no, la evidencia la juzga (cantidad x precio
        # literal = importe -> «aritmetica»; si no, «modelo» -> revisión).
        rows = table_rows(md)
        literal = lambda v: bool(v) and (v in ocr_amounts or v in ocr_units)
        # Alternativa «precio por unidad» (ver build_all): solo en la segunda pasada, y solo se acepta si la factura
        # entera termina SIN revisión (todo lo impreso cuadra exacto). La cantidad sigue siendo la del 27B.
        if unit_alt:
            from decimal import ROUND_HALF_UP, Decimal
            for l in unit_price_candidates(lines):
                if literal(l["amount"]):
                    l["unit"], l["unit_exact"] = l["amount"], exact_unit(l["amount"], md)
                    u = l["unit_exact"] or Decimal(l["amount"])
                    l["amount"] = str((Decimal(str(l["qty"])) * u).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    l["amount_calc"] = True
                    log(f"  precio por unidad: {l['qty']} x {l['unit']} = {l['amount']} «{l['desc'][:40]}»")
        # Importe vacío con precio unitario literal: importe = cantidad x precio ("calculado"; si la cantidad del
        # 27B estuviera mal, la factura no cuadra y va a revisión).
        for l in lines:
            if not l.get("amount") and literal(l.get("unit")) and l["qty"] >= 1:
                from decimal import ROUND_HALF_UP, Decimal
                u = l.get("unit_exact") or Decimal(l["unit"])
                l["amount"] = str((Decimal(str(l["qty"])) * u).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                l["amount_calc"] = True
                log(f"  importe = cantidad x precio literal: {l['qty']} x {l['unit']} = {l['amount']} «{l['desc'][:40]}»")
        for l in lines:
            if l["qty"] % 1:  # cantidad decimal (la del 27B): cuenta como impresa solo si cantidad x precio literal = importe exacto
                u = float(l.get("unit_exact") or l.get("unit") or 0)
                if literal(l.get("unit")) and l.get("amount") in ocr_amounts and abs(l["qty"] * u - float(l["amount"])) < 0.006:
                    l["qty_printed"] = True
                continue
            # La CANTIDAD la decide el 27B (lo que el OCR/VL leyeron, interpretado por el modelo). El código solo la
            # COMPRUEBA contra lo impreso, nunca la sustituye (decisión del usuario 2026-10-20: «los modelos están ahí
            # por una razón, no inventes tú»; 32226: «$39.29 x2», el 27B dio 2 y una regla del código lo pasó a 1).
            # Si no se puede respaldar, la evidencia de la cantidad queda «modelo» y la factura va a revisión.
            q = qty_from_table(l, rows)
            if q is not None:
                if q == l["qty"]:
                    l["qty_printed"] = True
                else:
                    log(f"  cantidad del 27B {l['qty']}; la fila de tabla del OCR muestra {q}: se deja la del 27B «{l['desc'][:40]}»")
                continue
            if qty_printed(l["qty"], sq_text, p1.squash(l.get("desc", ""))[:20]):
                continue
            if qty_leads_desc(l["qty"], md, l.get("desc")):
                l["qty_printed"] = True
                continue
            unit, amt = l.get("unit") or "", l.get("amount") or ""
            if literal(unit) and amt in ocr_amounts and float(unit) > 0:
                n = round(float(amt) / float(l.get("unit_exact") or unit))
                if n != l["qty"] and qty_matches(n, unit, amt, l.get("unit_exact")):
                    log(f"  cantidad del 27B {l['qty']}; importe / precio (literales) da {n}: se deja la del 27B «{l['desc'][:40]}»")
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
        cands = {}
        for key, kind in (("envio", "shipping"), ("impuestos", "tax")):
            amts = list(dict.fromkeys(l["amount"] for l in backed if l["kind"] == kind))
            from_llm = f"{sum(float(a) for a in amts):.2f}" if amts else ""
            # candidatos: el de la etiqueta, el del 27B y "sin cargo"; se queda el que cuadre el resumen
            cands[key] = list(dict.fromkeys([v for v in (fields[key], from_llm) if v] + [""]))
        # Descuentos y otros cargos: el mismo mecanismo que envío e impuestos. Candidatos en orden: la lista de las
        # etiquetas y la de las líneas del 27B con importe literal en el OCR; se queda la primera con la que cuadra el
        # resumen (ver choose_header más abajo). Antes, si las etiquetas encontraban algo («FG Coupon Discount -$0.00»),
        # los descuentos del 27B no se miraban (29907, 32104: «Redeemed Rewards -$1.63» quedaba fuera).
        llm_lists = {}
        for key, kind in (("descuentos", "discount"), ("other_charges", "other_charges")):
            src = [l for l in backed if l["kind"] == kind and not (kind == "other_charges" and l.get("neg"))]
            seen = dict.fromkeys((p1.squash(l["desc"]), l["amount"]) for l in src)
            llm_lists[key] = [{"concepto": next(l["desc"] for l in src if (p1.squash(l["desc"]), l["amount"]) == k),
                               "importe": k[1]} for k in seen]
            if not fields[key]:
                fields[key] = list(llm_lists[key])
        # Créditos: un "cargo" o "pago" impreso en NEGATIVO ("Refund -$48.15", "Gift Card Amount -$14.99") resta del
        # total; el signo es el del documento. Van con los descuentos (importe en positivo), como candidatos del cuadre.
        for l in backed:
            if l.get("neg") and l["kind"] in ("other_charges", "payment"):
                for lst in (fields["descuentos"], llm_lists["descuentos"]):
                    if not any(d["importe"] == l["amount"] for d in lst):
                        lst.append({"concepto": l["desc"], "importe": l["amount"]})
        options = {key: [fields[key]] + ([llm_lists[key]] if llm_lists[key] and llm_lists[key] != fields[key] else [])
                   for key in ("descuentos", "other_charges")}
        if not fields["subtotal_articulos"]:
            fields["subtotal_articulos"] = items_subtotal
        # Un único artículo sin importe: el importe que cuadra el resumen impreso, si es único y está literal en el OCR.
        missing = [l for l in lines if not l.get("amount")]
        if len(missing) == 1 and len(lines) >= 1:
            known = sum(float(l["amount"]) for l in lines if l.get("amount"))
            charges = {o["amount"] for o in others if o.get("amount") and o["kind"] in
                       ("shipping", "tax", "discount", "other_charges", "payment")}
            charges |= {v for v in (fields["envio"], fields["impuestos"]) if v}
            v = single_missing_amount(known, items_subtotal, total, cands, fields["descuentos"], fields["other_charges"],
                                      ocr_amounts, charges)
            if v:
                missing[0]["amount"] = v
                log(f"  único artículo sin importe: {v} (cuadra el resumen impreso y está literal en el OCR) «{missing[0]['desc'][:40]}»")
        s = lines_sum(lines)
        # Cabecera que cuadra el resumen impreso: (subtotal de artículos o suma de artículos) + envío + impuestos
        # + otros cargos − descuentos = total. Entre etiqueta y 27B, y entre los descuentos/cargos leídos, se
        # elige la primera combinación que cuadra (preferencia: etiqueta, luego 27B; quitar lo mínimo). Los
        # descuentos que no entran son informativos ("Price includes $560 off", "You saved", monedas) y se
        # quitan de la cabecera; un "cargo" que no entra (base exenta, puntos) también. Si nada cuadra, se deja
        # lo de siempre (etiqueta primero, todo incluido) y la factura va a revisión por el descuadre.
        base = float(items_subtotal) if items_subtotal else s
        pick = None
        for tc in (total_cands if base is not None else []):
            for d_opt, o_opt in itertools.product(options["descuentos"], options["other_charges"]):  # etiqueta, luego 27B
                pick = choose_header(base, float(tc), cands, d_opt, o_opt)
                if pick:
                    break
            if pick:
                if tc != total:
                    log(f"  total impreso que cuadra el resumen: {tc} (en vez de {total})")
                total = tc
                break
        if pick:
            fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = pick
        else:
            fields["envio"], fields["impuestos"] = cands["envio"][0], cands["impuestos"][0]
            # Las líneas pueden cuadrar con el "Item(s) Subtotal" y aun así la cabecera no cerrar con el total
            # (un cargo o crédito mal clasificado): esos campos van a WTCP, así que nunca sin revisión.
            if items_subtotal and total and base is not None:
                issues.append(f"order summary does not add up: items {base:.2f} + shipping + tax + other charges "
                              f"- discounts does not match total {total}")
        # Sin subtotal de artículos impreso, la cuenta completa de la cabecera también cuadra la factura:
        # artículos + envío + impuestos + otros cargos − descuentos = total (todos los importes leídos del OCR).
        extra = (float(fields["envio"] or 0) + float(fields["impuestos"] or 0)
                 + sum(float(c["importe"] or 0) for c in fields["other_charges"])
                 - sum(float(d["importe"] or 0) for d in fields["descuentos"]))
        if not items_subtotal and total and s is not None:
            if abs(s + extra - float(total)) < 0.01:
                target = f"{s:.2f}"
        # Una "línea de artículo" que es el resumen de los demás (eBay "1 item $X", fila con el total): solo se
        # aparta si el propio documento lo demuestra (ver summary_line_as_goods). Queda en `lineas` como
        # subtotal_or_total, visible para la revisión.
        full_d, full_o = list(fields["descuentos"]), list(fields["other_charges"])

        def fits(x):
            if items_subtotal:
                return abs(x - float(items_subtotal)) < 0.01
            return bool(total) and choose_header(x, float(total), cands, full_d, full_o) is not None
        k = summary_line_as_goods(lines, fits) if s is not None else None
        if k is not None:
            dup = lines.pop(k)
            log(f"  línea de resumen tomada como artículo, apartada: {dup['amount']} «{dup['desc'][:40]}» "
                f"(= suma de los demás artículos; sin ella cuadra con lo impreso)")
            others.append(dict(dup, kind="subtotal_or_total"))
            s = lines_sum(lines)
            if not items_subtotal:
                pick = choose_header(s, float(total), cands, full_d, full_o)
                if pick:
                    fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = pick
                extra = (float(fields["envio"] or 0) + float(fields["impuestos"] or 0)
                         + sum(float(c["importe"] or 0) for c in fields["other_charges"])
                         - sum(float(d["importe"] or 0) for d in fields["descuentos"]))
                if abs(s + extra - float(total)) < 0.01:
                    target = f"{s:.2f}"
        # Línea de artículo que el 27B devolvió más veces de las que aparece en el texto de página completa del OCR
        # (31755: rellenó a un vendedor sin artículo al pie de la página repitiendo el anterior). Se quita la copia
        # sobrante solo si así cuadra EXACTAMENTE con lo impreso y con ella no.
        if s is not None and not fits(s):
            full_sq = p1.squash(" ".join(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0].split("### Tablas detectadas")[0]
                                          for i, regs in zip(pages, regions)))
            groups = {}
            for idx, l in enumerate(lines):
                groups.setdefault((desc_key(l["desc"])[:40], l.get("amount")), []).append(idx)
            extra_copies = []
            for (key, amt), idxs in groups.items():
                seen_in_ocr = full_sq.count(p1.squash(lines[idxs[0]]["desc"])[:30])
                if amt and len(idxs) > 1 and 0 < seen_in_ocr < len(idxs):
                    extra_copies += idxs[seen_in_ocr:]
            if extra_copies:
                s2 = s - sum(float(lines[i]["amount"]) for i in extra_copies)
                if fits(s2):
                    for i in sorted(extra_copies, reverse=True):
                        dup = lines.pop(i)
                        log(f"  línea repetida por el 27B (en el OCR aparece menos veces), quitada: {dup['amount']} «{dup['desc'][:40]}»")
                    s = lines_sum(lines)
                    if not items_subtotal:
                        pick = choose_header(s, float(total), cands, full_d, full_o)
                        if pick:
                            fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = pick
                        extra = (float(fields["envio"] or 0) + float(fields["impuestos"] or 0)
                                 + sum(float(c["importe"] or 0) for c in fields["other_charges"])
                                 - sum(float(d["importe"] or 0) for d in fields["descuentos"]))
                    if items_subtotal and abs(s - float(items_subtotal)) < 0.01:
                        target = items_subtotal
                    elif not items_subtotal and abs(s + extra - float(total)) < 0.01:
                        target = f"{s:.2f}"
        # Artículos que el 27B omitió (p. ej. dos filas casi idénticas que el texto de página completa perdió):
        # si la suma no cuadra, se buscan en las tablas del OCR filas completas y literales (descripción,
        # cantidad, precio, importe, con cantidad x precio = importe) que completen EXACTAMENTE la diferencia.
        if s is not None and (items_subtotal or total) and abs(s - float(target)) >= 0.01:
            diff = round((float(items_subtotal) if items_subtotal else float(total) - extra) - s, 2)
            if diff > 0:
                page_mds = [p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions)]
                for r in missing_table_rows(page_mds, lines, diff):
                    lines.append(dict(r, src="tabla", kind="goods", qty_printed=True, order=10 ** 6 + len(lines),
                                      extra={"vendedor": "", "seguimiento": "", "estado_envio": ""}))
                    log(f"  fila de tabla que el 27B omitió, añadida (literal del OCR): {r['qty']} x {r['unit']} = {r['amount']} «{r['desc'][:50]}»")
                s = lines_sum(lines)
                if items_subtotal and abs(s - float(items_subtotal)) < 0.01:
                    target = items_subtotal
                elif not items_subtotal and abs(s + extra - float(total)) < 0.01:
                    target = f"{s:.2f}"

    # Rescate visual: solo en la vía del 27B, cuando la suma no reproduce el total impreso, y solo si el 27B
    # encontró artículos (el VL corrige artículos; no los crea: sin ninguno, el documento va a revisión tal cual).
    # Lo que el VL devuelva debe cuadrar con lo que suman los ARTÍCULOS (subtotal impreso, o total − cargos de
    # la cabecera), no con el total con impuestos: si no, un cargo puede "completar" la suma como si fuera artículo.
    vl_goal = None
    if source == "llm" and target:
        vl_goal = float(items_subtotal) if items_subtotal else float(total) - extra if total else None
    if vl and source == "llm" and lines and total_printed and vl_goal is not None and (lines_sum(lines) is None or abs(lines_sum(lines) - float(target)) >= 0.01):
        # Primero, solo las cantidades pintadas en las fotos (recorte de cada foto; ver item_photos): si con ellas
        # cantidad x precio literal de todos los artículos da exactamente lo impreso, se aceptan y no hace falta leer la
        # página entera con el VL.
        photo_merged = None
        if p1.server_ok(vl_server):
            try:
                photo_lines = photo_quantities(res["pdf"], pages, lines, literal, vl_server, timeout, log)
            except Exception as e:  # noqa: BLE001  fallo del VL en un recorte: sigue el rescate de siempre
                log(f"  lectura de cantidades en las fotos falló ({type(e).__name__}); sigue el rescate de siempre")
                photo_lines = []
            photo_merged = vl_quantities(lines, photo_lines, literal, sq_text, vl_goal) if photo_lines else None
        if photo_merged:
            for l in photo_merged:
                if l.get("qty_vl"):
                    log(f"  cantidad leída en la foto (Qwen2.5-VL): {l['qty']} x {l['unit']} = {l['amount']} «{l['desc'][:40]}»")
            lines = photo_merged
            target = items_subtotal or f"{lines_sum(lines):.2f}"
            log(f"  con esas cantidades, cantidad x precio literal = {target}: cuadra exactamente con lo impreso")
        elif p1.server_ok(vl_server):
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
            # Lo que el 27B ya clasificó como no-artículo (subtotal, envío, impuesto, pago...) no puede volver como
            # artículo desde la imagen.
            not_goods = {desc_key(o["desc"]) for o in others if len(desc_key(o["desc"])) >= 4}
            n0 = len(kept)
            kept = [v for v in kept if not any(difflib.SequenceMatcher(None, desc_key(v["desc"]), k).ratio() >= 0.85
                                               for k in not_goods)]
            if len(kept) < n0:
                issues.append(f"vision model (Qwen2.5-VL) returned {n0 - len(kept)} summary line(s) (shipping, subtotal...) "
                              "as items; discarded")
            literal_price = bool(kept) and all(v.get("unit") and (v["unit"] in ocr_amounts or v["unit"] in ocr_units) for v in kept)
            vl_sum = lines_sum(kept)
            merged = vl_quantities(lines, kept, literal, sq_text, vl_goal) if kept else None
            if merged:
                for l in merged:
                    if l.get("qty_vl"):
                        log(f"  cantidad leída en la imagen (Qwen2.5-VL): {l['qty']} x {l['unit']} = {l['amount']} «{l['desc'][:40]}»")
                lines = merged
                target = items_subtotal or f"{lines_sum(lines):.2f}"
                # las lecturas del VL que se descartaron no se usaron: su aviso no es motivo de revisión
                issues = [i for i in issues if not i.startswith("vision model (Qwen2.5-VL) returned")]
                log(f"  con esas cantidades, cantidad x precio literal = {target}: cuadra exactamente con lo impreso")
            elif kept and literal_price and vl_sum is not None and abs(vl_sum - vl_goal) < 0.01:
                if len(kept) == len(lines):
                    for v, o in zip(kept, lines):
                        v["extra"] = o.get("extra", {})
                lines, source = kept, "qwen-vl"
                target = items_subtotal or f"{vl_sum:.2f}"
                issues = [i for i in issues if not i.startswith("amount ")]
                issues.append("lines read from the image by the vision model (Qwen2.5-VL), unit prices found literally in "
                              "the OCR text; quantity x price matches the printed "
                              + ("items subtotal" if items_subtotal else "total"))
            elif kept and not literal_price:
                issues.append("vision model (Qwen2.5-VL) rejected: unit price(s) not found literally in the OCR text")
            else:
                issues.append("vision model (Qwen2.5-VL) does not add up either (sum " + ("?" if vl_sum is None else f"{vl_sum:.2f}")
                              + f" vs {vl_goal:.2f})")
        else:
            issues.append(f"vision model (Qwen2.5-VL) not responding at {vl_server}; no visual check")

    issues += description_issues(lines)
    # Artículo con importe 0.00 pero precio unitario impreso: no se envió (pendiente, "B/O") o la cantidad no es la
    # enviada. WTCP declararía mercancía que no viene: siempre a revisión (decisión del usuario, 2026-10-19).
    zero = [l for l in lines if l.get("amount") and float(l["amount"]) == 0 and l.get("unit")
            and float(l["unit"]) > 0 and (l["unit"] in ocr_amounts or l["unit"] in ocr_units)]
    if zero:
        issues.append(f"{len(zero)} item line(s) with amount 0.00 but a printed unit price (not shipped or back-ordered?): "
                      + ", ".join(f'"{l["desc"][:40]}"' for l in zero[:3]))
    if not total and lines_sum(lines) is not None:
        total, total_printed = f"{lines_sum(lines):.2f}", False
        target = total
    # Sin total impreso, la suma de las líneas comparada consigo misma siempre "cuadra": no prueba que la lista esté
    # completa. Solo un subtotal de artículos impreso con el que cuadren las líneas lo prueba; si no, revisión
    # (decisión del usuario 2026-10-20: nunca fabricar; el total que va a WTCP es la suma, total_is_printed false).
    if lines and not total_printed and not (items_subtotal and lines_sum(lines) is not None
                                            and abs(lines_sum(lines) - float(items_subtotal)) < 0.01):
        issues.append("no printed total: the item list cannot be checked for completeness; review against the paper")
    if target and lines_sum(lines) is not None and abs(lines_sum(lines) - float(target)) >= 0.01:
        issues.append(f"sum of lines {lines_sum(lines):.2f} vs " + ("items subtotal" if items_subtotal else "total") + f" {target}")
    # Sin marcador de pedido y sin ninguna línea: no parece una factura de mercadería (albarán,
    # confirmación de envío, recibo de pago...). Lo decide una persona: nunca se descarta en silencio.
    estructura = not inv["tipo"] and not lines and not read_failed
    if estructura:
        issues.append("the document does not look like a goods invoice: no order marker and no item lines")
    elif not lines:  # con marcador de pedido pero sin ningún artículo: nunca sale sin revisión
        issues.append("no item lines found in the document; review against the paper")

    all_ok = bool(target) and lines_sum(lines) is not None and abs(lines_sum(lines) - float(target)) < 0.01
    for l in lines:
        l["evidence"] = line_evidence(l, sq_text, ocr_amounts, all_ok, ocr_units)
        l["verified"] = l.get("verified", all_ok) and "modelo" not in l["evidence"].values()
    issues += evidence_issues(lines)
    for l in others:  # no-mercadería: evidencia de sus valores, nunca "verificada" como artículo
        l["evidence"], l["verified"] = line_evidence(l, sq_text, ocr_amounts, False, ocr_units), False
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
        # interno (build_all lo retira): las líneas cuadran con un total o subtotal IMPRESO
        "_cuadra": all_ok and total_printed,
        "_unit_candidates": bool(unit_price_candidates(lines)) and not unit_alt,  # ver opción D en build_all
    }, source


def split_by_orders(g0, res, inv, llm_server, vl_server, timeout):
    """Un PDF con varios pedidos sin marcador conocido (facturas Shein, eBay o de proveedor una tras otra): se
    corta en grupos de páginas consecutivas que cuadran CADA UNO con su propio total o subtotal impreso. Solo si
    todos los grupos cuadran (si no, el documento queda entero, como antes, y va a revisión). Sin VL: un grupo
    que solo cuadrara con la lectura visual no prueba el corte. Devuelve las facturas o None."""
    pages, groups, start = inv["pages"], [], 0
    quiet = lambda m: None
    for end in range(len(pages)):
        sub = dict(inv, pages=pages[start:end + 1])
        doc, source = build_invoice(g0 + len(groups), res, sub, True, llm_server, vl_server, timeout, quiet, vl=False)
        if source == "llm" and doc["_cuadra"]:
            groups.append(doc)
            start = end + 1
    return groups if start == len(pages) and len(groups) > 1 else None


# Identificador impreso (número de pedido, de artículo, de seguimiento): 12+ caracteres alfanuméricos (guiones
# aparte) con 4+ cifras. Deja fuera lo que comparten todos los documentos del mismo cliente: código postal ZIP+4
# (33186-6700), teléfono (10–11 cifras), nombre de usuario.
ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9-])(?=(?:[A-Za-z-]*\d){4})(?=(?:-*[A-Za-z0-9]){12})[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9](?![A-Za-z0-9-])")


def page_texts(markdown):
    """Markdown de la fase 1 ("## Página N" ...) -> {N: texto de la página}."""
    pos = [(int(m.group(1)), m.start()) for m in re.finditer(r"^## Página (\d+)", markdown or "", re.M)]
    return {n: markdown[p:(pos[k + 1][1] if k + 1 < len(pos) else len(markdown))] for k, (n, p) in enumerate(pos)}


def order_fingerprints(docs, markdown):
    """Huella de cada factura para detectar el mismo pedido dos veces: total + artículos (importe y descripción) +
    los identificadores impresos en sus páginas. Dos facturas son el mismo pedido si coinciden total y artículos
    Y comparten un identificador (dos compras iguales de la misma camiseta tienen números de pedido distintos)."""
    texts = page_texts(markdown)
    out = []
    for d in docs:
        goods = sorted([l["importe_linea"], desc_key(l["descripcion"])[:20]] for l in d["lineas"] if l["line_kind"] == "goods")
        a, b = d["ocr"]["paginas"]
        ids = sorted({x.upper() for n in range(a, b + 1) for x in ORDER_ID_RE.findall(texts.get(n, ""))})
        out.append({"id": d["id"], "total": d["total"], "goods": goods, "ids": ids,
                    "envios": d["ocr"].get("origen_lineas") == "envios"})
    return out


def same_order(a, b):
    return bool(a["goods"]) and a["total"] == b["total"] and a["goods"] == b["goods"] and bool(set(a["ids"]) & set(b["ids"]))


def mark_review(doc, note):
    doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"], note]))
    if doc["motivo_revision"] != "ESTRUCTURA":
        doc["motivo_revision"] = "LECTURA"
    doc["ocr"]["incidencias"].append(note)


def build_all(res, use_llm=True, llm_server=QWEN27_SERVER, vl_server=VL_SERVER, timeout=600, log=None):
    """Divide el resultado de la fase 1 en facturas y construye el JSON de todas (array)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    _LLM_CACHE.clear()
    _PPOCR_CACHE.clear()
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
            # El Grand Total impreso del pedido: un único bloque sin "Total for This Shipment" toma Grand Total − los demás
            # envíos impresos (todo literal); y si todos traen total, deben sumar el Grand Total.
            missing = [b for b in blocks if not b["total"]]
            others = sum(float(b["total"]) for b in blocks if b["total"])
            if order_total and len(missing) == 1 and float(order_total) - others >= 0:
                missing[0].update(total_derived=f"{float(order_total) - others:.2f}", order_total=order_total,
                                  others_total=f"{others:.2f}")
            for k, b in enumerate(blocks):
                docs.append(build_shipment_invoice(len(out) + len(docs), res, inv, b, k, len(blocks), order_fields, order_total, pages, full_texts, currency_of(md)))
                d = docs[-1]
                log(f"     factura {d['id']} [{b['estado']}]: {len(d['lineas'])} línea(s), total {d['total'] or '?'}"
                    f"{'' if d['total_is_printed'] else ' (calculado)'}, revisión: {d['motivo_revision'] or 'no'}" + (f" ({d['mismatch']})" if d["mismatch"] else ""))
            if order_total and not missing and abs(others - float(order_total)) >= 0.01:
                for d in docs:
                    mark_review(d, f"shipments add up to {others:.2f} but the order Grand Total is {order_total}")
        else:
            log(f"  -- factura {len(out)} ({inv['ref'] or 'sin referencia'})")
            # Primero sin VL: si cuadra, es lo mismo que con VL (el rescate solo actúa cuando no cuadra).
            doc, source = build_invoice(len(out), res, inv, use_llm, llm_server, vl_server, timeout, log, vl=False)
            # Opción D (decisión del usuario 2026-10-20): si no cuadra y hay líneas «cantidad > 1 con un solo precio
            # impreso», se prueba UNA alternativa -- ese precio es por unidad (importe = cantidad x precio) -- y solo se
            # acepta si con ella la factura entera termina sin revisión. El documento lo confirma, no una regla.
            if source == "llm" and not doc["_cuadra"] and doc.get("_unit_candidates"):
                alt_log = []
                alt, alt_source = build_invoice(len(out), res, inv, use_llm, llm_server, vl_server, timeout, alt_log.append,
                                                vl=False, unit_alt=True)
                if alt["_cuadra"] and not alt["motivo_revision"]:
                    for m in alt_log:
                        if "precio por unidad" in m:
                            log(m)
                    log("     con el precio impreso por unidad, todo lo impreso cuadra exactamente")
                    doc, source = alt, alt_source
            parts = None
            if source == "llm" and not doc["_cuadra"] and len(pages) > 1:
                parts = split_by_orders(len(out), res, inv, llm_server, vl_server, timeout)
            if parts:
                log(f"     no cuadra entero; {len(parts)} pedido(s) que cuadran cada uno con su total impreso: páginas "
                    + ", ".join(f"{p['ocr']['paginas'][0]}-{p['ocr']['paginas'][1]}" for p in parts))
                docs.extend(parts)
            else:
                if source == "llm" and not doc["_cuadra"]:  # el 27B está en caché: solo se añade la lectura visual
                    doc, source = build_invoice(len(out), res, inv, use_llm, llm_server, vl_server, timeout, log)
                docs.append(doc)
            for doc in docs:
                log(f"     {len(doc['lineas'])} línea(s) (origen: {doc['ocr']['origen_lineas']}), total {doc['total'] or '?'}, "
                    f"revisión: {doc['motivo_revision'] or 'no'}" + (f" ({doc['mismatch']})" if doc["mismatch"] else ""))
        if dups:
            for doc in docs:
                mark_review(doc, "order reference repeated in the file (the same order appears more than once)")
        out.extend(docs)
    for doc in out:
        doc.pop("_cuadra", None)
        doc.pop("_unit_candidates", None)
    # El mismo pedido dos veces en el PDF sin referencia conocida (p. ej. la misma factura Shein escaneada dos
    # veces): ver same_order. Los bloques de envío de un mismo pedido de Amazon son envíos distintos: no cuentan.
    fps = order_fingerprints(out, res.get("markdown", ""))
    for i, a in enumerate(fps):
        for j, b in enumerate(fps):
            if i != j and not (a["envios"] and b["envios"]) and same_order(a, b):
                mark_review(out[i], f"same order as invoice {b['id']} of this file (identical items and total; "
                                    "possible duplicate declaration)")
                break
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
