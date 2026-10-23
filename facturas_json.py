"""
Fase 2: estandariza la salida del OCR (fase 1, ocr_pipeline.run) en JSON con el esquema acordado
(contrato con WTCP: C:/Share/CONTRATO-WTCP.md). No clasifica: los campos arancelarios quedan con su forma
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
    coloca en su artículo y, si GLM corrió las descripciones de la página siguiente, se realinean (saltos.fix_breaks).
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
  - linea.estimacion (fase 3, fase3.py): en las líneas goods sin importe de una factura en revisión,
    {importe_estimado, maximo, metodo}; null en las demás. importe_linea sigue vacío.
  - Moneda: build_all deja los importes en la moneda impresa; divisas.convert_all (API y CLI) los pasa a
    la del cliente y guarda lo original en ocr (moneda_factura, tipo_cambio, *_original).

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

import fase3
import ocr_pipeline as p1
import saltos

RESULT_VERSION = 1
QWEN27_SERVER = f"http://{p1.MODELS_HOST}:8083"
VL_SERVER = p1.QWEN_SERVER  # Qwen2.5-VL-7B, puerto 8088

# Marcadores de inicio de factura: (tipo, regex con grupo 1 = referencia). Tolerantes a espacios
# perdidos por el OCR.
EBAY_ORDER_RE = re.compile(r"Order\s*number:?\s*(\d{2}-\d{5}-\d{5})(?!\d)", re.I)
# Número de pedido/factura impreso con su etiqueta en el MISMO renglón, para `referencia` cuando no hay marcador
# conocido (2026-10-21: «Order No: 25085528», «Order# W7709007», «Order 361365724» de RockAuto, «INVOICE # IN260376»).
# Nunca el renglón siguiente: ahí suele estar el número manuscrito del expediente («INVOICE #16548 — Hayden LA\n32949»).
# Número: letras/cifras/guiones con >= 4 cifras; sin «$», puntos ni barras («Order Total $…», «Order Date 10.10.2026»).
_REF_NUM = r"([A-Z0-9][A-Z0-9-]*[A-Z0-9])(?![A-Z0-9.,/$-])"
PRINTED_REF_RES = [re.compile(r"(?<![A-Za-z])Order[ \t]*(?:No\.?|Number|#|ID)?[ \t]*(?:[:#][ \t]*){0,2}" + _REF_NUM, re.I),
                   re.compile(r"(?<![A-Za-z])Invoice[ \t]*(?:No\.?|Number|#)?[ \t]*(?:[:#][ \t]*){0,2}" + _REF_NUM, re.I)]


def printed_ref(text):
    """Primer número de pedido (si no, de factura) impreso con su etiqueta en el mismo renglón; "" si no hay."""
    for rx in PRINTED_REF_RES:
        for m in rx.finditer(text or ""):
            # «Purchase Order Number» es el número del comprador, no el pedido del vendedor
            if (text[max(0, m.start() - 10):m.start()].strip().lower().endswith("purchase")
                    or sum(c.isdigit() for c in m.group(1)) < 4):
                continue
            return m.group(1)
    return ""


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
               "printed, or \"1\" when a single unit is implied), importe_linea (the amount printed for that line; when the line shows both an amount before tax and a total that includes tax, use the amount before tax), "
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
    """Suma de los importes de las líneas. Un importe impreso con 3 a 5 decimales (Extended «$71.208») cuenta EXACTO
    (amount_exact, el valor del 27B) y la suma se trunca a céntimos como el total impreso («$4,005.154» -> 4005.15):
    sumar los importes ya truncados se queda corto (32569: 4005.11). Solo si amount_exact sigue siendo ese importe."""
    if not ls or not all(l["amount"] for l in ls):
        return None
    from decimal import Decimal
    if not any(l.get("amount_exact") is not None and cents(l["amount_exact"]) == l["amount"] for l in ls):
        return sum(float(l["amount"]) for l in ls)
    tot = sum((l["amount_exact"] if l.get("amount_exact") is not None and cents(l["amount_exact"]) == l["amount"]
               else Decimal(l["amount"])) for l in ls)
    return float(cents(tot))


# Descripción que es solo un identificador (código de seguimiento "TBA335177403481", número de
# pedido, etc.): el modelo tomó un código en vez del nombre del producto.
ID_LIKE_RE = re.compile(r"^[A-Z]{0,4}[\d-]{9,}$")


# "1 item", "2 items", "1 object", "1 Electronics item" (28647): un recuento, no un producto
COUNT_ONLY_RE = re.compile(r"^\s*\d+\s+(?:\w+|\w+\s+(?:items?|objects?))\s*$", re.I)


def description_issues(lines):
    bad = [l["desc"] for l in lines if ID_LIKE_RE.match(re.sub(r"\s", "", l["desc"] or "")) or not (l["desc"] or "").strip()
           or COUNT_ONLY_RE.match(l["desc"] or "")]
    out = [f"{len(bad)} line(s) with an empty description or one that looks like an identifier: " + ", ".join(f'"{b}"' for b in bad[:4])] if bad else []
    # Una línea sin importe deja la suma sin calcular y el cuadre sin comprobar: nunca sale sin revisión.
    no_amt = [l["desc"] for l in lines if not l.get("amount")]
    if no_amt:
        out.append(f"{len(no_amt)} item line(s) without an amount: " + ", ".join(f'"{d[:40]}"' for d in no_amt[:3]))
    return out


# Descripción tomada de la columna equivocada (33355 «COPE-ZR61KCE-TFD», 33598 «MARMAFDERBYCLUBHC», 33733 «LI-TJ3T»): el
# 27B devolvió la celda del código cuando la MISMA fila imprime la descripción. Se comprueba por posición, sin modelo:
# la fila de tabla de la fase 1 (o el renglón de PP-OCRv6) que lleva el importe de la línea y una celda igual a su
# descripción. Con una descripción así, el código arancelario saldría mal: la factura va a revisión (decisión del
# usuario 2026-10-22, opción A).
_MONEY_IN = re.compile(r"\d[.,]\d{2}(?!\d)")


def _alpha_words(t):
    return [w for w in re.split(r"\s+", (t or "").strip()) if re.fullmatch(r"[A-Za-z][A-Za-z'’&]{2,}[.,;)]?", w)]


def code_like(t):
    """Celda tipo código: sin espacios y con a lo sumo una palabra (COPE-ZR61KCE-TFD, MARMAFDERBYCLUBHC, LI-TJ3T)."""
    t = (t or "").strip()
    return bool(t) and " " not in t and len(_alpha_words(t)) <= 1


def descriptive(t):
    """Celda con texto de producto: dos o más palabras y sin importes."""
    return len(_alpha_words(t)) >= 2 and not _MONEY_IN.search(t or "")


def desc_column_issues(lines, md, pdf, pages, log):
    rows = table_rows(md)
    found = []
    for l in lines:
        if not l.get("amount") or not code_like(l.get("desc")):
            continue
        k = desc_key(l["desc"])
        better = None
        for cells in rows:  # 1) tabla de la fase 1 (celdas)
            if any(money(c) == l["amount"] for c in cells) and any(desc_key(c) == k for c in cells):
                better = next((c for c in cells if desc_key(c) != k and descriptive(c)), None)
                if better:
                    break
        if better is None and pdf:  # 2) sin tabla: renglón de PP-OCRv6 con el importe y el código
            try:
                for r in ppocr_rows(pdf, pages[min(l.get("page_rel", 1), len(pages)) - 1]):
                    toks = [t for _, _, t in r["toks"]]
                    # PP-OCRv6 y GLM difieren en alguna letra del código («LI-TIJ3T» / «LI-TJ3T»): parecido >= 0.8
                    same = [t for t in toks if difflib.SequenceMatcher(None, desc_key(t), k).ratio() >= 0.8]
                    if any(money(t) == l["amount"] for t in toks) and same:
                        better = next((t for t in toks if t not in same and descriptive(t)), None)
                        if better:
                            break
            except Exception as e:  # noqa: BLE001  sin geometría no se comprueba por esa vía
                log(f"  PP-OCRv6 no disponible ({type(e).__name__}): sin comprobar la columna de descripción")
        if better:
            found.append((l["desc"], better))
    if found:
        log("  descripción en la columna del código: " + "; ".join(f"«{a}» (la fila imprime «{b[:50]}»)" for a, b in found))
        return [f"{len(found)} line(s) whose description is an item code while the same row prints a description: "
                + ", ".join(f'"{a}" -> "{b[:50]}"' for a, b in found[:3])]
    return []


# ----------------------------------------------------------------------------- división en facturas

def page_text(res, i):
    regs = res["region_pages"][i] if i < len(res["region_pages"]) else []
    return (res["full_text"].get(i) or "") + "\n" + " ".join(str(r.get("content") or "") for r in regs)


def split_invoices(res):
    """Agrupa las páginas del PDF en facturas según los marcadores de inicio. Una página con DOS O MÁS pedidos distintos
    (21551: dos «Order Summary» de Amazon, cada uno con su Order # y su Grand Total) se corta en trozos de su texto de
    página completa, uno por pedido, desde cada marcador (inv["slices"]: {página: (inicio, fin)}; ver sliced_res)."""
    invoices = []
    for i in range(res["n_pages"]):
        text = page_text(res, i)
        full = res["full_text"].get(i) or ""
        found = sorted({(m.start(), tipo, m.group(1)) for tipo, rx in INVOICE_START for m in rx.finditer(full)})
        refs = list(dict.fromkeys(r for _, _, r in found))
        if len(refs) >= 2:
            starts = [next((pos, tipo, r) for pos, tipo, r in found if r == ref) for ref in refs]
            for k, (pos, tipo, ref) in enumerate(starts):
                a = 0 if k == 0 else full.rfind("\n", 0, pos) + 1  # desde el renglón del marcador
                b = len(full) if k + 1 == len(starts) else full.rfind("\n", 0, starts[k + 1][0]) + 1
                if k == 0 and invoices and invoices[-1]["ref"] == ref:
                    invoices[-1]["pages"].append(i)
                    invoices[-1].setdefault("slices", {})[i] = (a, b)
                else:
                    invoices.append({"tipo": tipo, "ref": ref, "pages": [i], "slices": {i: (a, b)}})
            continue
        hit = next(((tipo, m.group(1)) for tipo, rx in INVOICE_START for m in [rx.search(text)] if m), None)
        if hit and (not invoices or hit[1] != invoices[-1]["ref"]):
            invoices.append({"tipo": hit[0], "ref": hit[1], "pages": [i]})
        elif invoices:
            invoices[-1]["pages"].append(i)
        else:
            invoices.append({"tipo": "", "ref": "", "pages": [i]})
    return invoices


def sliced_res(res, inv):
    """El resultado de la fase 1 visto por una factura con trozos de página (split_invoices): en esas páginas, solo su
    trozo del texto de página completa y sin regiones (las regiones mezclan los dos pedidos)."""
    if not inv.get("slices"):
        return res
    ft, rp = dict(res["full_text"]), list(res["region_pages"])
    for i, (a, b) in inv["slices"].items():
        ft[i] = (res["full_text"].get(i) or "")[a:b]
        if i < len(rp):
            rp[i] = []
    return dict(res, full_text=ft, region_pages=rp)


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


VERIF_HEADER = "\n### Verificación de importes"  # sección de diagnóstico que la fase 1 añade al Markdown de cada página
_LLM_CACHE = {}  # (servidor, Markdown de la página) -> respuesta; se vacía por documento en build_all
# Respuestas crudas de los modelos del último documento (build_all las vacía al empezar): la API las guarda en
# modelos.json de cada trabajo para medir cambios del código y calibrar los modelos sin volver a llamarlos.
MODEL_RAW = {"27b": {}, "27b_imagen": {}, "vl_pagina": {}, "vl_foto": {}, "ppocr": {}}
# 27B con su proyector de visión (desde 2026-10-21, --image-max-tokens 2048 en el host): cuando una factura no cuadra con
# la lectura de texto, se repite con la imagen de cada página delante del texto de GLM (ver build_all).
IMAGE_MAX_TOKENS = 2048
IMAGE_PREFACE = ("The image is the page itself. The OCR text below was read from it and can contain errors: misread "
                 "digits, rows paired with the wrong description, struck-through prices, text lost at the edges. Read "
                 "each line and value from the image; use the OCR text only as help.\n\n")


def page_image(pdf, page_idx):
    """PNG en base64 de una página, al dpi que cabe en IMAGE_MAX_TOKENS fichas (p1.fit_dpi, como la fase 1)."""
    with fitz.open(pdf) as doc:
        pg = doc[page_idx]
        return base64.b64encode(pg.get_pixmap(dpi=p1.fit_dpi(pg, 200, IMAGE_MAX_TOKENS), alpha=False).tobytes("png")).decode("ascii")


_VISION = {}


def server_has_vision(server):
    """El servidor del 27B tiene proyector de visión (GET /props, modalities.vision); se pregunta una vez por proceso."""
    if server not in _VISION:
        try:
            _VISION[server] = bool(requests.get(f"{server}/props", timeout=10).json().get("modalities", {}).get("vision"))
        except Exception:  # noqa: BLE001
            _VISION[server] = False
    return _VISION[server]


def md_key(markdown):
    import hashlib
    return hashlib.sha1((markdown or "").encode("utf-8")).hexdigest()


def lines_with_llm(markdown, server, timeout, log, image=None):
    """Una llamada al 27B con el Markdown de UNA página. Devuelve (datos, motivo_de_fallo).
    max_tokens = n_ctx - prompt (antes 4096 fijo: una factura de 54 artículos se cortaba siempre). Si la
    respuesta se corta o no es JSON devuelve (las líneas completas que llegó a escribir o None, motivo) en vez de lanzar:
    la factura va a revisión.
    Temperatura 0 y top_k 1: la misma página da la misma respuesta, así que se reutiliza (corte por pedidos)."""
    key = (server, markdown, md_key(image) if image else "")
    if key not in _LLM_CACHE:
        _LLM_CACHE[key] = _lines_with_llm(markdown, server, timeout, log, image)
    return _LLM_CACHE[key]


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


def _lines_with_llm(markdown, server, timeout, log, image=None):
    user = USER_PROMPT.format(doc=markdown)
    if image:  # la imagen de la página delante; sus fichas cuentan en el contexto (tope --image-max-tokens del host)
        user = IMAGE_PREFACE + user
    budget = (server_n_ctx(server) - count_tokens(server, SYSTEM_PROMPT + "\n" + user) - PROMPT_MARGIN_TOKENS
              - (IMAGE_MAX_TOKENS if image else 0))
    if budget < 512:
        return None, "page text too long for the model context"
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                                                  {"type": "text", "text": user}] if image else user}],
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
    MODEL_RAW["27b_imagen" if image else "27b"][md_key(markdown)] = {"content": content, "finish_reason": choice.get("finish_reason")}
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
            MODEL_RAW["vl_pagina"][str(i)] = content
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
NEG_RE = re.compile(r"^([A-Za-z][^$\d-]{2,40}?)\s*:?\s*(?:-\s*(?:USD|\$)|(?:USD|\$)\s*-)\s*(\d[\d.,]*[.,]\d{2})\s*$", re.I)
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
    # Envío bonificado: un renglón con importe impreso en NEGATIVO igual al envío impreso lo anula («Free Shipping:
    # -USD 6.99» bajo «Shipping & Handling: USD 6.99»; 30725, 28074). Sin lista de palabras: lo decide la cifra.
    if f["envio"] and float(f["envio"]) > 0:
        for line in md.splitlines():
            d = NEG_RE.match(line.strip())
            if d and not line.lstrip().startswith("<") and money(d.group(2)) == f["envio"]:
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
    # descripción recompuesta en un salto de página (saltos.reconcile_table): fragmentos de GLM de renglones distintos
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


def printed_count(amount, texts):
    """Cuántas veces está impreso un importe ("1234.50") en los textos de página completa del OCR (sin las tablas
    de regiones, que repiten las cifras): con o sin separador de miles, con punto o coma decimal."""
    v = float(amount)
    forms = {f"{v:.2f}", f"{v:,.2f}", f"{v:.2f}".replace(".", ","), f"{v:,.2f}".translate(str.maketrans(",.", ".,"))}
    pat = re.compile(r"(?<![\d.,])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\d])")
    return sum(len(pat.findall(t or "")) for t in texts)


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
    """Celdas de cada fila de las tablas del OCR: las HTML (regiones) y las de Markdown con barras del texto de página
    completa («| 9 | 1000275 | Dr. C. Tuna … | $150.00 |»; 32833: la región leyó otra palabra en la descripción y la
    fila no se encontraba)."""
    rows = [[re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            for tr in re.findall(r"<tr>(.*?)</tr>", md or "", re.S)]
    for line in (md or "").splitlines():
        t = line.strip()
        if t.startswith("|") and t.endswith("|") and t.count("|") >= 3 and not re.fullmatch(r"[|\s:-]+", t):
            rows.append([c.strip() for c in t[1:-1].split("|")])
    return rows


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
        # la cantidad sola en su celda, o con la unidad de medida ("144 EA", "2 ROL"); también con decimales a cero
        # ("6.00", "2,000.00": 33480, Complete Framer's Supply)
        ints = sorted({int(m.group(1).replace(",", "")) for c in cells
                       for m in [re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d{1,5})(?:\.0{1,3})?(?:\s+[A-Za-z]{1,4})?", c)] if m})
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


def photo_unit(l, literal):
    """Precio por unidad de una línea para leer su cantidad en la foto: el precio unitario literal; si no hay, el único
    precio impreso de la línea (importe literal) cuando la cantidad del 27B es 1 o cuando es > 1 con un solo precio
    impreso (el caso de la opción D, 31144: «2» en la foto, «$84.52», el 27B dio 2 x 84.52 en el importe). Sin él, "".
    Que ese precio sea por unidad lo confirma solo el cuadre exacto de vl_quantities."""
    if literal(l.get("unit")):
        return l["unit"]
    if literal(l.get("amount")) and (l["qty"] == 1 or l in unit_price_candidates([l])):
        return l["amount"]
    return ""


def vl_quantities(lines, vl_lines, literal, sq_text, goal):
    """Cantidades que solo se ven en la imagen (Amazon las pone sobre la foto del producto; el OCR no las lee).
    Las líneas del 27B se quedan; de Qwen2.5-VL solo se toma la CANTIDAD del mismo artículo (mismo precio
    unitario literal y descripción casi igual), nunca sobre una cantidad impresa. Se acepta solo si entonces
    cantidad x precio de TODOS los artículos da exactamente lo impreso (goal); si no, None."""
    used, out, changed = set(), [], 0
    for l in lines:
        unit = photo_unit(l, literal)
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
        # la misma cantidad que el 27B también cambia la línea si su importe era el precio de una unidad (31144)
        if q != l["qty"] or (m is not None and l["qty"] > 1 and not literal(l.get("unit")) and q * float(unit) != float(l["amount"])):
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
PHOTO_PRICE_RE = re.compile(r"^(?:US)?\$\d{1,3}(?:,\d{3})*\.\d{2}$")  # «$19.99», «US$59.99» (Amazon en español)
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
    if not sold:
        return block_photos(pdf, page_idx, height)
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


PHOTO_OCR_PROMPT = "Read all the text in the image."  # prompt de OCR del cookbook oficial de Qwen2.5-VL


def badge_text_number(raw):
    """Número de la lectura OCR de la esquina: el contenido (bloque de código, entre comillas o tal cual) es SOLO un
    entero 1-999 -> ese entero; sin ningún entero -> 0; otra cosa -> None."""
    m = re.search(r"```\s*\n?(.*?)\n?```", raw or "", re.S) or re.search(r'"([^"]*)"', raw or "")
    c = (m.group(1) if m else raw or "").strip()
    if re.fullmatch(r"\d{1,3}", c) and 1 <= int(c) <= 999:
        return int(c)
    return None if re.search(r"(?<![\d.,])\d{1,3}(?![\d.,%])", c) else 0


def _vl_image(pdf, page_idx, rect):
    area = (rect.width / 72) * (rect.height / 72)
    dpi = int(min(max(200, (1024 * 28 * 28 / area) ** 0.5), (4096 * 28 * 28 / area) ** 0.5))
    with fitz.open(pdf) as doc:
        return base64.b64encode(doc[page_idx].get_pixmap(dpi=dpi, clip=rect, alpha=False).tobytes("png")).decode("ascii")


def block_photos(pdf, page_idx, height):
    """Fotos de artículo sin la etiqueta «Sold by» (otro idioma: «Vendido por», 32079): por la geometría del bloque. El
    artículo es un bloque de renglones en la columna de texto que termina en el renglón del precio suelto (block_rows);
    su principio es el primer renglón de la columna tras el precio anterior (como mucho 7 renglones). La foto está a la
    izquierda del bloque, de su altura, como en item_photos."""
    rows = block_rows(pdf, page_idx)
    prices = [i for i, r in enumerate(rows) if r["price"]]
    out = []
    for n, i in enumerate(prices):
        col = rows[i]["x"]
        top = rows[i]
        for r in reversed(rows[(prices[n - 1] + 1) if n else 0:i]):
            if not re.search(r"[A-Za-z]{2,}", r["text"]):
                continue  # marcas de la foto (la cantidad «2», iconos): no son el borde del bloque
            if abs(r["x"] - col) > 25 or rows[i]["c"] - r["c"] > 260:
                break
            top = r
        if top is rows[i]:
            continue  # precio sin texto encima: una cifra del resumen, no un artículo
        y0, y1 = top["c"] - 20, rows[i]["c"] + 20
        h = (y1 + 45) - (y0 - 15)
        box = (max(0, col - 1.15 * h), max(0, y0 - 15), col - 8, min(height, y1 + 45))
        if box[2] - box[0] > 20:
            out.append((rows[i]["price"], fitz.Rect(*(v * 72 / PPOCR_DPI for v in box))))
    return out


def photo_quantity(pdf, page_idx, rect, server, timeout):
    """Cantidad pintada en la foto según Qwen2.5-VL; None si no hay lectura fiable. Recortes a >= 1024 fichas.
    1) Pregunta cerrada sobre la foto entera (esquema JSON entero; 1 si no hay número).
    2) Si responde > 1, se comprueba con el prompt de OCR del cookbook sobre la esquina inferior derecha de la foto (donde
       Amazon pinta la cantidad): se acepta solo si esa lectura es exactamente el mismo número. La pregunta cerrada sola
       cuenta las unidades DIBUJADAS en el producto («3 botellas») o lee números del envase («40 Page», «8PCS»): 19 falsos
       en 202 fotos con verdad a mano (2026-10-21); con la comprobación, 0 falsos y 188/202 (antes 180/202)."""
    rj = post_json(f"{server}/v1/chat/completions", {
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_vl_image(pdf, page_idx, rect)}"}},
                                                 {"type": "text", "text": PHOTO_QTY_PROMPT}]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "cache_prompt": False, "max_tokens": 20,
        "response_format": {"type": "json_schema", "json_schema": {"name": "q", "schema": PHOTO_QTY_SCHEMA}}}, timeout)
    raw = rj["choices"][0]["message"]["content"]
    key = f"{page_idx}:{rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f}"
    MODEL_RAW["vl_foto"][key] = raw
    q = json.loads(raw).get("quantity")
    # Fuera de 1..999 o no entero: sin lectura (None), nunca 1 por defecto; esa foto no aporta cantidad.
    if not (isinstance(q, int) and 1 <= q <= 999):
        return None
    if q == 1:
        return 1
    corner = fitz.Rect(rect.x0 + rect.width / 2, rect.y0 + rect.height / 2, rect.x1, rect.y1)
    rj = post_json(f"{server}/v1/chat/completions", {
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_vl_image(pdf, page_idx, corner)}"}},
                                                 {"type": "text", "text": PHOTO_OCR_PROMPT}]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "cache_prompt": False, "max_tokens": 300}, timeout)
    raw2 = rj["choices"][0]["message"]["content"]
    MODEL_RAW["vl_foto"][key + ":esquina"] = raw2
    return q if badge_text_number(raw2) == q else None


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
            l = next((l for l in on_page if id(l) not in used and price and price == photo_unit(l, literal)), None)
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
                # envío bonificado: renglón con importe en negativo igual al envío del bloque («Free Shipping: -USD 6.99»)
                if (cur["envio"] and float(cur["envio"]) > 0 and not re.search(r"savings|discount|promotionapplied", norm)
                        and re.search(r"[a-z]:?-(?:usd)?-?\$?" + re.escape(cur["envio"]) + r"$", norm)):
                    cur["envio_bonificado"] = cur["envio"]
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
    extra = (sum(float(block[k] or 0) for k in ("envio", "impuestos")) - float(block.get("descuento") or 0)
             - float(block.get("envio_bonificado") or 0))
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
    # La cabecera del bloque también va a WTCP: si las líneas cuadran solas con el total pero envío + impuestos −
    # cupón no es 0, esa cabecera no cierra (32188: etiquetas y valores en celdas separadas; «Sales Tax» se emparejó
    # con 149.95, el «Total before tax»). Nunca sin revisión.
    if ok and total and s is not None and abs(extra) >= 0.01 and abs(s - float(total)) < 0.01:
        issues.append(f"shipment summary does not add up: items {s:.2f} + shipping {block['envio'] or 0} + tax "
                      f"{block['impuestos'] or 0} - coupon {block.get('descuento') or 0} does not match shipment total {total}")
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

# ----------------------------------------------------------------------------- geometría de PP-OCRv6
# GLM-OCR lee cada página como texto SIN posiciones. PP-OCRv6 (detección + reconocimiento, no generativo) da cada renglón
# CON su caja: la fase 2 lo usa como lector de CIFRAS (ppocr_text), para localizar las fotos de Amazon (block_photos),
# para la columna de descripción (desc_column_issues) y, sobre todo, para los saltos de página: un solo mecanismo por
# geometría en saltos.py (2026-10-23), que sustituyó a los cinco parches anteriores (join_split_items, fix_page_breaks,
# fix_block_breaks, block_break, row_break). Servicio en el host de modelos (scania-mdl-ppocr, GPU1, :8089, ~1 s/página).
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
        MODEL_RAW["ppocr"][str(page_idx)] = {"tokens": _PPOCR_CACHE[key][0], "w": pix.width, "h": pix.height}
    return _PPOCR_CACHE[key]


def ppocr_text(pdf, pages, log=None):
    """Texto de PP-OCRv6 de esas páginas, una caja por renglón (para las cifras impresas); "" si no está disponible."""
    out = []
    for p in pages:
        try:
            out += [t for *_, t in ppocr_tokens(pdf, p)[0]]
        except Exception as e:  # noqa: BLE001  sin PP-OCRv6 las cifras se comprueban solo contra GLM (como antes)
            if log:
                log(f"  PP-OCRv6 no disponible ({type(e).__name__}): cifras comprobadas solo contra GLM")
            return ""
    return "\n".join(out)


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


# Saltos de página en diseños de BLOQUES (Amazon con fotos y parecidos): cada artículo es un bloque de renglones en una
# columna de texto que termina en un renglón con solo el precio («$16.98»). El corte deja el bloque abierto al pie de la
# página A (sin su precio) y la cola con el precio arriba de la B. Medido el 2026-10-21 sobre 850 saltos de 341 PDF:
# 16 continuaciones, todas reales contra la imagen (herramientas/medir_saltos_bloques.py).
BLOCK_PRICE_RE = re.compile(r"^(?:US)?\$\d{1,3}(?:,\d{3})*\.\d{2}$")


def block_rows(pdf, page_idx):
    """Renglones de PP-OCRv6 para el diseño de bloques: {c, text, x (borde izquierdo del texto), price (solo un precio
    suelto; lo demás, marcas sin letras de la foto: «*4», «1», «口»)}."""
    raw, _, _ = ppocr_tokens(pdf, page_idx)
    rows = []
    for y0, x0, x1, y1, t in sorted((y0, x0, x1, y1, t) for x0, y0, x1, y1, t in raw):
        c = (y0 + y1) / 2
        if rows and abs(rows[-1]["c"] - c) <= 10:
            rows[-1]["toks"].append((x0, x1, t)); rows[-1]["c"] = (rows[-1]["c"] + c) / 2
        else:
            rows.append({"c": c, "toks": [(x0, x1, t)]})
    for r in rows:
        r["toks"].sort()
        r["text"] = " ".join(t for _, _, t in r["toks"])
        prices = [(x0, t) for x0, _, t in r["toks"] if BLOCK_PRICE_RE.match(t)]
        rest = [t for _, _, t in r["toks"] if not BLOCK_PRICE_RE.match(t)]
        r["price"] = money(prices[0][1]) if len(prices) == 1 and all(
            len(t) <= 3 and not re.search(r"[A-Za-z]", t) for t in rest) else ""
        words = [x0 for x0, _, t in r["toks"] if re.search(r"[A-Za-z]{3,}", t)]
        r["x"] = prices[0][0] if r["price"] else (words[0] if words else r["toks"][0][0])
    return rows


def unit_price_candidates(lines):
    """Líneas de artículo con cantidad > 1 y un solo precio impreso (sin precio unitario distinto del importe): en ellas
    el importe impreso puede ser el total de la línea o el precio de UNA unidad («$9.49 x2»)."""
    return [l for l in lines if l.get("kind", "goods") == "goods" and l["qty"] > 1 and l.get("amount")
            and (not l.get("unit") or l["unit"] == l["amount"])]


def build_invoice(inv_id, res, inv, use_llm, llm_server, vl_server, timeout, log, vl=True, unit_alt=False, img=False):
    pages = inv["pages"]
    regions = [res["region_pages"][i] if i < len(res["region_pages"]) else [] for i in pages]
    full_texts = [res["full_text"].get(i) or "" for i in pages]
    md = "\n\n---\n\n".join(p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions))
    ocr_amounts = doc_amounts(md) | exact_cents(md)  # "1975.0000" también es literal
    # PP-OCRv6 también es lector de CIFRAS (decisión del usuario 2026-10-22): no genera texto, lee los caracteres de cada
    # caja; una cifra que GLM perdió (columnas de la derecha, precio arriba de la página siguiente: 31635, 31824) cuenta
    # como impresa si PP-OCRv6 la leyó en estas páginas. Solo importes: las descripciones siguen saliendo de GLM.
    pp_text = ppocr_text(res["pdf"], pages, log)
    pp_amounts = (doc_amounts(pp_text) | exact_cents(pp_text)) - ocr_amounts
    ocr_amounts |= pp_amounts
    # también sin etiquetas HTML ni entidades: en las tablas del OCR la descripción viene partida con <br> ("4130158083<br>4130
    # Chromoly...") o con &quot;, y no se encontraría literal aunque esté leída
    sq_text = p1.squash(md) + "|" + p1.squash(html.unescape(re.sub(r"<[^>]+>", " ", md)))
    divisa = currency_of(md)
    issues = []
    header_open = False  # cabecera (vía 27B, sin subtotal impreso) que no cerró: se comprueba al final
    ambiguous = False  # descuento impreso fuera del cuadre y subtotal impreso = líneas + descuento (ver más abajo)
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
            # Al 27B va el texto de la página y sus tablas, sin la nota «### Verificación de importes» de la fase 1: es un
            # diagnóstico, no parte del documento (31940: la tomó como un cargo). Las comprobaciones de literalidad siguen
            # usando el Markdown completo. Calibrado el 2026-10-21 (con la frase del importe antes de impuestos del prompt):
            # 68 documentos auditados, 88 -> 92 facturas sin revisión, todas correctas, sin empeorar ninguna.
            page_md = p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0]
            data, fail = lines_with_llm(page_md.split(VERIF_HEADER)[0].rstrip(), llm_server, timeout, log,
                                        page_image(res["pdf"], i) if img else None)
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
                amt_v = model_number(l.get("importe_linea"), l.get("importe_valor"), f"amount of {what}", issues)
                amt = cents(amt_v)
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
                              "amount": amt, "amount_exact": amt_v if amt_v is not None and amt_v != amt_v.quantize(amt_v.__class__("0.01")) else None, "unit": cents(unit_v), "src": "qwen27b", "page_rel": k + 1,
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
        # Lo pagado (cobro a tarjeta, tarjeta regalo, saldo pendiente) es información aparte: el total que va a WTCP es
        # el valor del pedido (declaraciones, seguro), no lo que quedó por pagar (decisión del usuario 2026-10-21; 32328:
        # el 27B dio «Balance Due $0.00» como total). Con líneas `payment`, sus líneas subtotal_or_total literales también
        # son candidatas a total; en el cuadre de la cabecera gana la primera que cierra (un «TOTAL DUE» sin nada pagado
        # sigue valiendo: 31206).
        paid = {l["amount"] for l in lines if l.get("kind") == "payment" and l.get("amount")}
        sub_totals = []
        if paid:  # con pagos impresos, el total impreso puede ser el que queda tras ellos (Amazon: Grand Total tras la tarjeta regalo)
            sub_totals = [l["amount"] for l in reversed(lines) if l.get("kind") == "subtotal_or_total" and l.get("amount")
                          and l["amount"] in ocr_amounts and float(l["amount"]) > 0 and l["amount"] not in total_cands]
        if t and t in ocr_amounts and t not in total_cands:
            total_cands.append(t)
        total_cands += [v for v in dict.fromkeys(sub_totals) if v not in total_cands]
        if t and not total:
            if t in ocr_amounts:
                total, total_printed = t, bool(model_total.get("total_is_printed", True))
                # Sin etiqueta de total, la cifra del 27B solo prueba un total si está impresa además de como importe
                # de un artículo (31865: página suelta sin resumen; el 27B dio el precio del único artículo como
                # total y la suma «cuadraba» consigo misma). Si no, es un documento sin total impreso -> revisión.
                if total_printed and printed_count(t, [res["full_text"].get(i) or "" for i in pages]) <= sum(
                        l.get("amount") == t for l in lines if l.get("kind", "goods") == "goods"):
                    total_printed = False
                    log(f"  total del 27B {t}: solo impreso como importe de un artículo; no cuenta como total impreso")
            else:
                issues.append(f"total {t} returned by the model not found in the OCR text")
        source = "llm"
        if len(pages) > 1:
            glm_pages = {i: html.unescape(re.sub(r"<[^>]+>", " ", p1.page_markdown(i + 1, res["full_text"].get(i) or "", regs)[0]))
                         for i, regs in zip(pages, regions)}
            # Saltos de página: un solo mecanismo por geometría (saltos.py). ppocr_tokens se pasa tardío para que las
            # repeticiones sin GPU (herramientas) puedan sustituirlo.
            issues += saltos.fix_breaks(lines, res["pdf"], pages, glm_pages, log, tokens_of=lambda p, i: ppocr_tokens(p, i))
    else:
        lines, source = [], "ninguna"
        issues.append("no shipment blocks and model extraction disabled")
    # Las líneas que el 27B clasifica como no-mercadería (envío, impuesto, descuento, subtotal/total, otros cargos)
    # se apartan: todo lo que sigue (sumas, rescate visual, evidencia, ESTRUCTURA) trabaja solo con las "goods".
    # Se devuelven al final con su line_kind (WTCP solo declara las goods).
    others = [l for l in lines if l.get("kind", "goods") != "goods"]
    lines = [l for l in lines if l.get("kind", "goods") == "goods"]
    ocr_units = fine_amounts(md) | fine_amounts(pp_text)
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
            # candidatos: el de la etiqueta, el del 27B y "sin cargo"; se queda el que cuadre el resumen. «Sin cargo» no
            # si el 27B devolvió ese envío/impuesto impreso distinto de 0 con respaldo literal: no se desecha para hacer
            # cuadrar (32862, 32357: líneas con el impuesto dentro cuadraban «sin impuesto»). La etiqueta sola no lo
            # impide: sin importe a su lado toma la cifra siguiente (31397 p2: «Shipping» -> 31.89, el del artículo).
            cands[key] = list(dict.fromkeys([v for v in (fields[key], from_llm) if v]
                                            + ([] if from_llm and float(from_llm) else [""])))
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
        # Créditos: un "cargo" impreso en NEGATIVO ("Refund -$48.15") resta del total; el signo es el del documento. Va
        # con los descuentos (importe en positivo), como candidato del cuadre. Un PAGO no («Gift Card Amount», «Store
        # Credit», «Payments/Credits»): es información aparte, el total es el valor del pedido (usuario, 2026-10-21).
        for l in backed:
            if l.get("neg") and l["kind"] == "other_charges":
                for lst in (fields["descuentos"], llm_lists["descuentos"]):
                    if not any(d["importe"] == l["amount"] for d in lst):
                        lst.append({"concepto": l["desc"], "importe": l["amount"]})
        options = {key: [fields[key]] + ([llm_lists[key]] if llm_lists[key] and llm_lists[key] != fields[key] else [])
                   for key in ("descuentos", "other_charges")}
        if not fields["subtotal_articulos"]:
            fields["subtotal_articulos"] = items_subtotal
        # Recuento de eBay en el resumen («1 item $76.00») que el 27B dio como artículo mientras el artículo real (en la
        # página siguiente, sin precio impreso) quedó sin importe (32697, 32738): si su número coincide con los demás
        # artículos y ninguno de ellos tiene importe, es el resumen, no un artículo; se aparta (subtotal_or_total) y
        # el importe del artículo lo decide la regla siguiente (único valor literal que cuadra lo impreso).
        counts = [l for l in lines if COUNT_ONLY_RE.match(l.get("desc") or "") and l.get("amount")]
        rest = [l for l in lines if l not in counts]
        if len(counts) == 1 and rest and not any(l.get("amount") for l in rest) \
                and int(counts[0]["desc"].split()[0]) == len(rest):
            lines.remove(counts[0])
            others.append(dict(counts[0], kind="subtotal_or_total"))
            log(f"  «{counts[0]['desc']}» {counts[0]['amount']} es el recuento del resumen, no un artículo")
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
                total_printed = total_printed or tc in sub_totals  # línea de total del 27B, literal en el OCR
                break
        if pick:
            fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = pick
            # Un «descuento» con el mismo importe que una línea de pago del propio 27B es lo pagado (tarjeta regalo,
            # crédito de la tienda: 32962), que nunca resta del valor del pedido. El 27B se contradice: revisión.
            paid_amts = {o["amount"] for o in others if o.get("kind") == "payment" and o.get("amount")
                         and float(o["amount"]) > 0 and o["amount"] in ocr_amounts}
            for d in fields["descuentos"]:
                if d.get("importe") and float(d["importe"]) > 0 and d["importe"] in paid_amts:
                    issues.append(f"discount «{d['concepto'][:40]}» {d['importe']} is also listed as a payment "
                                  f"(gift card or store credit applied is not a discount)")
        else:
            fields["envio"], fields["impuestos"] = cands["envio"][0], cands["impuestos"][0]
            # Las líneas pueden cuadrar con el "Item(s) Subtotal" y aun así la cabecera no cerrar con el total
            # (un cargo o crédito mal clasificado): esos campos van a WTCP, así que nunca sin revisión.
            if items_subtotal and total and base is not None:
                issues.append(f"order summary does not add up: items {base:.2f} + shipping + tax + other charges "
                              f"- discounts does not match total {total}")
            else:  # sin subtotal impreso: se vuelve a comprobar al final, con las líneas ya completas (filas de tabla, VL)
                header_open = True
        # Caso ambiguo (32079): las líneas cuadran con el total solo dejando fuera un descuento impreso que el 27B devolvió,
        # y líneas + ese descuento = otro subtotal impreso (Amazon: «Productos 119.98, Promoción −59.99, Total 59.99»).
        # Desde lo impreso es igual que un precio ya rebajado con un ahorro informativo (Home Depot 32925, Temu), y solo
        # la foto los distingue: se prueba la cantidad pintada en las fotos y se acepta solo si con ella las líneas dan
        # EXACTAMENTE ese subtotal; entonces la cabecera lleva el descuento. Si no, queda como estaba.
        ambiguous = False
        if pick and total_printed and lines and lines_sum(lines) is not None:
            dropped = [d for d in llm_lists["descuentos"] if d not in fields["descuentos"] and float(d["importe"] or 0) > 0]
            s0 = lines_sum(lines)
            goals = [o["amount"] for o in others if o.get("kind") == "subtotal_or_total" and o.get("amount") in ocr_amounts
                     and dropped and abs(s0 + sum(float(d["importe"]) for d in dropped) - float(o["amount"])) < 0.01
                     and abs(float(o["amount"]) - s0) >= 0.01]
            ambiguous = bool(goals)
            if goals and vl and p1.server_ok(vl_server):
                try:
                    photo_lines = photo_quantities(res["pdf"], pages, lines, literal, vl_server, timeout, log)
                except Exception as e:  # noqa: BLE001
                    log(f"  lectura de cantidades en las fotos falló ({type(e).__name__})")
                    photo_lines = []
                merged = vl_quantities(lines, photo_lines, literal, sq_text, float(goals[0])) if photo_lines else None
                full = choose_header(float(goals[0]), float(total), cands, llm_lists["descuentos"], fields["other_charges"]) if merged else None
                if merged and full:
                    lines = merged
                    fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = full
                    fields["subtotal_articulos"] = items_subtotal = target = goals[0]  # las líneas cuadran con él
                    for l in lines:
                        if l.get("qty_vl"):
                            log(f"  cantidad leída en la foto (Qwen2.5-VL): {l['qty']} x {l['unit']} = {l['amount']} «{l['desc'][:40]}»")
                    log(f"  con esas cantidades las líneas dan {goals[0]} (subtotal impreso) y la cabecera lleva el descuento")
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
    if source == "llm":
        issues += desc_column_issues(lines, md, res["pdf"], pages, log)
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
    # La excepción del subtotal solo vale si la cabecera no tiene cargos ni descuentos: con ellos el total a WTCP no
    # es la suma de las líneas y no está impreso (30887: descuento 74.72; total enviado 747.18, impreso 672.46).
    charged = any(float(fields.get(k) or 0) for k in ("envio", "impuestos")) or any(
        float(c.get("importe") or 0) for c in (fields.get("descuentos") or []) + (fields.get("other_charges") or []))
    if lines and not total_printed and (charged or not (items_subtotal and lines_sum(lines) is not None
                                                        and abs(lines_sum(lines) - float(items_subtotal)) < 0.01)):
        issues.append("no printed total: the item list cannot be checked for completeness; review against the paper")
    if target and lines_sum(lines) is not None and abs(lines_sum(lines) - float(target)) >= 0.01:
        issues.append(f"sum of lines {lines_sum(lines):.2f} vs " + ("items subtotal" if items_subtotal else "total") + f" {target}")
    elif header_open and total and total_printed and lines_sum(lines) is not None:
        # Sin subtotal impreso, las líneas pueden igualar el total y la cabecera no cerrar (líneas con el impuesto
        # dentro: 32862, 32357): la cabecera también va a WTCP, así que nunca sin revisión.
        pick = next((p for d_opt, o_opt in itertools.product(options["descuentos"], options["other_charges"])
                     for p in [choose_header(lines_sum(lines), float(total), cands, d_opt, o_opt)] if p), None)
        if pick:
            fields["envio"], fields["impuestos"], fields["descuentos"], fields["other_charges"] = pick
        else:
            issues.append(f"order summary does not add up: items {lines_sum(lines):.2f} + shipping + tax + other "
                          f"charges - discounts does not match total {total}")
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
        # Sin marcador conocido: el número de pedido de eBay impreso («Order number: 13-15047-54805»; si hay varios, uno
        # por grupo de artículos del mismo pedido, el primero). No corta facturas: solo la referencia.
        "referencia": inv["ref"] or next(iter(EBAY_ORDER_RE.findall(md)), "") or printed_ref(md),
        "ocr": fields,
        "_ambiguo": source == "llm" and ambiguous,
        # interno (build_all lo retira): las líneas cuadran con un total o subtotal IMPRESO
        "_cuadra": all_ok and total_printed,
        "_unit_candidates": bool(unit_price_candidates(lines)) and not unit_alt,  # ver opción D en build_all
    }, source


def split_by_orders(g0, res, inv, llm_server, vl_server, timeout):
    """Un PDF con varios pedidos sin marcador conocido (facturas Shein, eBay o de proveedor una tras otra): se
    corta en grupos de páginas consecutivas que cuadran CADA UNO con su propio total o subtotal impreso. Sin VL: un
    grupo que solo cuadrara con la lectura visual no prueba el corte. Desde cada página se busca el grupo más corto que
    cuadra; una página desde la que ninguno cuadra queda pendiente y las pendientes seguidas forman UNA factura que va
    a revisión como siempre (antes era todo o nada: una página mal leída dejaba el documento entero en revisión,
    30276, 31620, 32413). Devuelve las facturas, o None si no hay al menos un grupo que cuadre y otra factura."""
    pages, out, i, pending = inv["pages"], [], 0, []
    quiet = lambda m: None

    def own(pg):  # cada grupo con el número de pedido impreso en SUS páginas (32901: Tire Rack heredaba el de Amazon)
        ref = next((m.group(1) for k in pg for _, rx in INVOICE_START for m in [rx.search(page_text(res, k))] if m), "")
        return dict(inv, pages=list(pg), ref=ref, tipo=inv["tipo"] if ref else "")
    abort = []

    def flush():
        if pending:  # páginas que no cuadran con nada: una factura, con la lectura visual de siempre
            doc, _ = build_invoice(g0 + len(out), res, own(pending), True, llm_server, vl_server,
                                   timeout, quiet)
            # Sin total impreso propio, esas páginas pueden ser el principio del pedido siguiente («Continued on next
            # page», 31272: la última página sola «cuadraba» con su resumen tomado como artículo): no se corta nada.
            if not doc["total_is_printed"]:
                abort.append(True)
            out.append(doc)
            pending.clear()
    n_ok = 0
    while i < len(pages):
        for end in range(i, len(pages)):
            doc, source = build_invoice(g0 + len(out) + (1 if pending else 0), res, own(pages[i:end + 1]),
                                        True, llm_server, vl_server, timeout, quiet, vl=False)
            if source == "llm" and doc["_cuadra"]:
                flush()
                doc["id"] = g0 + len(out)
                out.append(doc)
                n_ok += 1
                i = end + 1
                break
        else:
            pending.append(pages[i])
            i += 1
    flush()
    # El mismo total impreso en dos grupos: un pedido que repite su resumen en cada página (30852: «Grand Total
    # $1,008.06» en las 11 páginas; una página sola «cuadraba»). No se corta.
    # Solo en el corte parcial (con páginas pendientes); si todos los grupos cuadran es el corte de siempre (27897: el
    # mismo pedido dos veces, que luego marca same_order).
    totals = [d["total"] for d in out if d["total"]]
    if n_ok < len(out) and len(set(totals)) < len(totals):
        abort.append(True)
    return out if n_ok and len(out) > 1 and not abort else None


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
        out.append({"id": d["id"], "total": d["total"], "goods": goods, "ids": ids, "ref": (d.get("referencia") or "").upper(),
                    "envios": d["ocr"].get("origen_lineas") == "envios"})
    return out


def same_order(a, b):
    return bool(a["goods"]) and a["total"] == b["total"] and a["goods"] == b["goods"] and bool(set(a["ids"]) & set(b["ids"]))


def partial_same_order(a, b):
    """El mismo pedido o factura impreso (misma referencia) con al menos un artículo en común (mismo importe y
    descripción), aunque cambien el total o los demás artículos: una versión ampliada o recortada del mismo papel
    (33135/32396: Streamline SLV-00488547 con 2 y luego 4 artículos; 33155/32671: el pedido entero y luego una parte).
    Dos envíos distintos del mismo pedido de Amazon no comparten artículos y no cuentan. Medido 2026-10-22 sobre el
    historial de WTCP: 14 parejas, todas para revisión humana."""
    if not a.get("ref") or a.get("ref") != b.get("ref"):
        return False
    return bool({tuple(g) for g in a["goods"]} & {tuple(g) for g in b["goods"]})


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
    for v in MODEL_RAW.values():
        v.clear()
    invoices = split_invoices(res)
    log(f"  {len(invoices)} factura(s) detectada(s): " + ", ".join(
        f"{inv['tipo'] or '?'} {inv['ref'] or 'sin ref'} p{inv['pages'][0] + 1}-{inv['pages'][-1] + 1}" for inv in invoices))
    out = []
    for g, inv in enumerate(invoices):
        rs = sliced_res(res, inv)
        pages = inv["pages"]
        regions = [rs["region_pages"][i] if i < len(rs["region_pages"]) else [] for i in pages]
        blocks = parse_blocks(regions)
        dups = [j for j, o in enumerate(invoices) if j != g and o["ref"] and o["ref"] == inv["ref"]]
        docs = []
        if blocks:
            full_texts = [rs["full_text"].get(i) or "" for i in pages]
            md = "\n\n---\n\n".join(p1.page_markdown(i + 1, rs["full_text"].get(i) or "", regs)[0] for i, regs in zip(pages, regions))
            order_fields = invoice_fields(md, full_texts, inv, rs["pdf"])
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
                docs.append(build_shipment_invoice(len(out) + len(docs), rs, inv, b, k, len(blocks), order_fields, order_total, pages, full_texts, currency_of(md)))
                d = docs[-1]
                log(f"     factura {d['id']} [{b['estado']}]: {len(d['lineas'])} línea(s), total {d['total'] or '?'}"
                    f"{'' if d['total_is_printed'] else ' (calculado)'}, revisión: {d['motivo_revision'] or 'no'}" + (f" ({d['mismatch']})" if d["mismatch"] else ""))
            if order_total and not missing and abs(others - float(order_total)) >= 0.01:
                for d in docs:
                    mark_review(d, f"shipments add up to {others:.2f} but the order Grand Total is {order_total}")
        else:
            log(f"  -- factura {len(out)} ({inv['ref'] or 'sin referencia'})")
            # Primero sin VL: si cuadra, es lo mismo que con VL (el rescate solo actúa cuando no cuadra).
            doc, source = build_invoice(len(out), rs, inv, use_llm, llm_server, vl_server, timeout, log, vl=False)
            # Opción D (decisión del usuario 2026-10-20): si no cuadra y hay líneas «cantidad > 1 con un solo precio
            # impreso», se prueba UNA alternativa -- ese precio es por unidad (importe = cantidad x precio) -- y solo se
            # acepta si con ella la factura entera termina sin revisión. El documento lo confirma, no una regla.
            if source == "llm" and not doc["_cuadra"] and doc.get("_unit_candidates"):
                alt_log = []
                alt, alt_source = build_invoice(len(out), rs, inv, use_llm, llm_server, vl_server, timeout, alt_log.append,
                                                vl=False, unit_alt=True)
                if alt["_cuadra"] and not alt["motivo_revision"]:
                    for m in alt_log:
                        if "precio por unidad" in m:
                            log(m)
                    log("     con el precio impreso por unidad, todo lo impreso cuadra exactamente")
                    doc, source = alt, alt_source
            # Lectura con la imagen (27B con su proyector de visión): si no cuadra, se repite con la imagen de cada página
            # delante del texto de GLM. Se acepta solo si con ella la factura entera termina SIN revisión: las reglas de
            # siempre siguen valiendo (una cifra que no esté literal en el OCR deja la línea sin verificar -> revisión).
            if source == "llm" and not doc["_cuadra"] and server_has_vision(llm_server):
                img_log = []
                alt, alt_source = build_invoice(len(out), rs, inv, use_llm, llm_server, vl_server, timeout, img_log.append,
                                                vl=False, img=True)
                # La imagen solo completa la lectura de texto, nunca la contradice: cada importe de artículo que la vía de
                # texto leyó literal sigue en alguna línea (como importe o como precio unitario: «3» en la foto), y el
                # total es el mismo total impreso que ya había encontrado la vía de texto (33349, 33440, 33447, 33117).
                txt_amts = {l["importe_linea"] for l in doc["lineas"] if l["line_kind"] == "goods" and l["importe_linea"]
                            and l["ocr"]["evidencia"].get("importe_linea") == "leido"}
                alt_vals = {v for l in alt["lineas"] if l["line_kind"] == "goods"
                            for v in (l["importe_linea"], l["ocr"].get("precio_unitario"))}
                keeps = (txt_amts <= alt_vals and doc["total_is_printed"] and doc["total"]
                         and alt["total"] and float(alt["total"]) == float(doc["total"]))
                if alt_source == "llm" and alt["_cuadra"] and not alt["motivo_revision"] and not keeps:
                    log("     con la imagen cuadraría, pero cambia lo que la vía de texto leyó (importe o total): se descarta")
                if alt_source == "llm" and alt["_cuadra"] and not alt["motivo_revision"] and keeps:
                    log("     con la imagen de la página (27B con visión), todo lo impreso cuadra exactamente")
                    alt["ocr"]["origen_lineas"] = "llm+imagen"
                    doc, source = alt, alt_source
            parts = None
            if source == "llm" and not doc["_cuadra"] and len(pages) > 1:
                parts = split_by_orders(len(out), rs, inv, llm_server, vl_server, timeout)
            if parts:
                log(f"     no cuadra entero; {len(parts)} pedido(s) que cuadran cada uno con su total impreso: páginas "
                    + ", ".join(f"{p['ocr']['paginas'][0]}-{p['ocr']['paginas'][1]}" for p in parts))
                docs.extend(parts)
            else:
                if source == "llm" and (not doc["_cuadra"] or doc.get("_ambiguo")):  # el 27B está en caché: solo se añade la lectura visual
                    doc, source = build_invoice(len(out), rs, inv, use_llm, llm_server, vl_server, timeout, log)
                docs.append(doc)
            for doc in docs:
                log(f"     {len(doc['lineas'])} línea(s) (origen: {doc['ocr']['origen_lineas']}), total {doc['total'] or '?'}, "
                    f"revisión: {doc['motivo_revision'] or 'no'}" + (f" ({doc['mismatch']})" if doc["mismatch"] else ""))
        if dups:
            for doc in docs:
                mark_review(doc, "order reference repeated in the file (the same order appears more than once)")
        # Fase 3: importe estimado de las líneas sin importe (solo facturas en revisión; ver fase3.py). No cambia nada de
        # lo anterior: añade `estimacion` a las líneas y una frase a mismatch.
        for doc in docs:
            fase3.estimate(doc, rs, sys.modules[__name__], llm_server, timeout, log, use_llm)
        out.extend(docs)
    for doc in out:
        doc.pop("_cuadra", None)
        doc.pop("_unit_candidates", None)
        doc.pop("_ambiguo", None)
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
    ap.add_argument("--moneda", default="USD", help="moneda del cliente: todos los importes de salida en ella (default USD; ver divisas.py)")
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
        import divisas
        divisas.convert_all(out, args.moneda.upper(), log, res["markdown"])
        if args.md_dir:
            Path(args.md_dir).mkdir(parents=True, exist_ok=True)
            (Path(args.md_dir) / f"{stem}.md").write_text(res["markdown"], encoding="utf-8")
        path = Path(args.out_dir) / f"{stem}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  Guardado en {path}")


if __name__ == "__main__":
    main()
