"""Fase 4: la Claude API en la LECTURA de facturas (contrato §9, autorización de WTCP del 2026-10-24; propuesta de SCANIA
en el contrato, «Propuesta de SCANIA del 2026-10-24»). En la clasificación SCANIA es solo un intermediario: aquí no.

Solo para las facturas que la fase 2 deja en revisión (motivo_revision distinto de "" y de DUPLICADO: un duplicado bien
detectado ya es la respuesta correcta). La Claude API recibe las páginas de esa factura (imagen) y el texto de GLM-OCR, y
devuelve TODOS los documentos que hay en ellas (un archivo puede traer varias facturas y la fase 2 no siempre las
separa: 34293 traía un pedido de G2 Wireless y una factura de Suresh; el prompt anterior pedía UNA factura y Claude
elegía una). Cada documento se comprueba por separado; los que validan salen sin revisión como facturas propias (el
primero en la factura del grupo, los demás nuevas al final del array) y los demás quedan juntos en la factura en
revisión (`resolver`). El modelo propone, el código comprueba (regla 4 de CLAUDE.md, «no inventar valores»):
  - cada importe (líneas, precio unitario usado, envío, impuestos, descuentos, otros cargos, subtotal, total) está
    impreso: lo leyó GLM-OCR o PP-OCRv6 en esas páginas (los mismos lectores de cifras que la fase 2);
  - cada PALABRA de la descripción la leyó GLM-OCR o PP-OCRv6 (los dos son lectores: no generan texto), con una letra de
    tolerancia en palabras de 5+ letras (error de lectura de un OCR); la fase 2 solo
    acepta GLM porque allí la descripción la escribe el 27B a partir de ese texto; aquí la escribe Claude mirando el papel
    y un lector la confirma (33733: GLM perdió «3 Ton Scissor Trolley Jack», PP-OCRv6 la leyó);
  - cantidad: entera ≥ 1; si es > 1, cantidad × precio unitario impreso = importe, o la cantidad está impresa junto
    a la descripción;
  - artículos + envío + impuestos + otros cargos − descuentos = total impreso (y = subtotal impreso si lo hay), al céntimo;
  - el modelo dice que la factura está completa y que en esas páginas no hay OTRO documento que dejó fuera (`fuera`);
  - NUNCA SE PIERDE UNA CIFRA IMPRESA: toda línea de la lectura anterior (fases 1-2) cuyo importe está impreso sigue en
    alguna línea de Claude, importe por importe. 34293 (2026-10-29): el PDF traía el pedido de G2 Wireless (22540.00, 13
    líneas que el 27B leyó bien) y la factura de Suresh (15500.00); Claude devolvió solo Suresh, cuadraba, y WTCP la
    recibió SIN revisión sin los 22540. Lo mismo en 33692 (tres proveedores fuera). Con cualquiera de las dos cosas la
    factura queda en revisión y el `mismatch` nombra lo que falta.
Si todo se cumple, la factura sale SIN revisión con esas líneas (`ocr.origen_lineas = "claude"`; lo que había antes en
`ocr.fase4`). Si no, y Claude devolvió líneas, la factura sigue en revisión (LECTURA) CON las líneas de Claude: la lectura
se paga y se aprovecha (decisión del usuario 2026-10-29, 33477: 0 líneas a WTCP aunque Claude leyó las 5). Cada línea
lleva su evidencia (lo que ningún OCR respalda, «modelo», y la línea sin verificar); la cabecera solo toma cifras
impresas; el `mismatch` dice qué no se confirmó y la explicación del modelo; la lectura anterior entera queda en
`ocr.fase4.antes` (`lineas_antes`, `cabecera`). Nunca peor que lo que había: si la lectura anterior tiene más líneas con
importe o más líneas verificadas, se queda ella y la de Claude va entera a `ocr.fase4.lineas_claude` (con sus `fallos`).
Sin líneas de Claude, la factura sigue como estaba con su explicación.

Configuración en `scania_config.json` -> `fase4` (se lee en cada factura, sin reiniciar): `activa` (true/false: el
interruptor; con false no se llama nunca, haya o no clave), `modelo` (por defecto claude-sonnet-5-5, el probado el
2026-10-23) y `precios_usd_por_millon` (precio de lista de la Claude API). La clave va en ANTHROPIC_API_KEY o en
anthropic_key.txt (fuera de git), o en el archivo de `archivo_clave`; activa sin clave = apagada, con aviso en el registro. Para pruebas, la variable
FASE4=cli usa el CLI de Claude Code en lugar de la API.

Coste: el de la Claude API, no el nuestro. Con la API, las fichas de entrada y salida que ella devuelve (`usage`) x el
precio de lista del modelo; con el CLI de pruebas, estimado con las mismas reglas de la API (imagen: ceil(ancho/28) x
ceil(alto/28) fichas, máximo 4784 por imagen; texto ~ caracteres/4; salida: las fichas que informa el CLI). Va al
registro del trabajo por factura y por documento, y a modelos.json (MODEL_RAW["claude"][].coste_api_usd)."""
import base64
import collections
import copy
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
MAX_TOKENS = 24000  # salida: pensamiento + JSON (8000 se agotaron solo pensando en 34196)
TIMEOUT = 900  # s; una salida larga sin streaming tarda minutos


def _clave():
    """Clave de la Claude API: ANTHROPIC_API_KEY, o el archivo de scania_config.json -> fase4.archivo_clave, o
    anthropic_key.txt del repo (fuera de git). Nunca se escribe en ningún registro."""
    k = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    for f in (config().get("archivo_clave"), KEY_FILE):
        if k:
            break
        try:
            k = Path(f).read_text(encoding="utf-8").strip() if f and Path(f).exists() else ""
        except OSError:
            k = ""
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


PROMPT = """You are reading scanned pages from a customs file. The pages may hold ONE OR SEVERAL documents: purchase
invoices, order summaries or confirmations, sales orders, receipts, packing lists, serial-number lists, shipping labels.
Return EVERY document you find, each one separately and in page order. Never merge two documents into one and never
leave one out. The page images are the truth; the OCR text below is a helper and can be wrong (missing columns, shifted
numbers).

For each document:
- tipo: "invoice" for any invoice, order or receipt that prints item prices; "packing_list", "serial_numbers", "label" or
  "other" for a document without item prices (a packing list or serial-number list that repeats the items of an invoice
  on these pages is its own document with no lineas).
- vendedor: the seller's name as printed. referencia: the printed order or invoice number ("" if none). fecha: the
  printed order or invoice date exactly as printed ("" if none). paginas: [first, last] page of the document, counting
  the pages given here from 1.
- lineas: its items. Copy descriptions EXACTLY as printed (same words, same order); do not translate or summarize.
- Amounts exactly as printed (digits only, dot decimal, no currency sign), e.g. "1234.50".
- Handwritten numbers and notes are NOT part of the document: ignore them. Items crossed out BY HAND are not declared on
  this sheet: leave them out.
- An item whose printed list price is struck through next to a lower price: use the lower (paid) price as the line
  amount and do not list that per-item saving as a discount.
- importe = the line total of the item (quantity x unit price when both are printed).
- Discounts/coupons/gift cards subtracted INSIDE the order summary before the printed total are discounts. Payments
  ("Paid", "Balance Due", card charges, "Amount Due") are NOT discounts and are not charges.
- subtotal = the printed subtotal of the ITEMS ONLY; "" if the only printed subtotal also includes shipping, tax or
  other charges.
- impuestos: one entry per printed tax amount (e.g. county tax and state tax separately), exactly as printed.
- completa = false if this document's item list is not complete on these pages (a page is missing, items cut off,
  illegible amounts) so that its printed totals cannot be reproduced from its items.
- explicacion: ONE short English sentence for a human reviewer saying what is wrong or missing on the paper for this
  document (e.g. "page 1 of 3 is missing: 101.98 of items are not in the file"), or "" if it is complete and adds up.

Answer ONLY with this JSON (no text before or after):
{"documentos": [{"tipo": "invoice", "vendedor": "", "referencia": "", "fecha": "", "paginas": [1, 1],
  "lineas": [{"descripcion": "", "cantidad": 1, "precio_unitario": "", "importe": ""}],
  "subtotal": "", "envio": "", "impuestos": [{"concepto": "", "importe": ""}], "descuentos": [{"concepto": "", "importe": ""}],
  "otros_cargos": [{"concepto": "", "importe": ""}], "total": "", "completa": true, "explicacion": ""}]}
Use "" for values that are not printed.
"""
PROMPT_TEXTO = "\nOCR text of these pages:\n"


def _prompt(texto, pista=""):
    """Prompt completo: instrucciones, la pista de qué documento extraer (solo si la factura comparte páginas con otra del
    mismo archivo: dos pedidos en la misma hoja, 34142) y el texto OCR."""
    aviso = (f"\nIMPORTANT: these pages hold more than one order or invoice. Return every document, but the one to "
             f"extract for this reading is the one whose printed order or invoice number is {pista}.\n") if pista else ""
    return PROMPT + aviso + PROMPT_TEXTO + texto


# ----------------------------------------------------------------------------- llamada al modelo

def _paginas_png(pdf, paginas):
    out = []
    with fitz.open(pdf) as doc:
        for i in range(paginas[0] - 1, paginas[1]):
            pg = doc[i]
            dpi = min(200, int(MAX_LADO / max(pg.rect.width, pg.rect.height) * 72))
            out.append(pg.get_pixmap(dpi=dpi, alpha=False).tobytes("png"))
    return out


def _llamar_api(pngs, texto, pista=""):
    """Una petición a la Claude API (imágenes + texto). max_tokens cubre el pensamiento del modelo (Sonnet 5.5 piensa por
    defecto y cuenta en la salida: 34196, 14 páginas y 208 líneas, gastó los 8000 de antes en pensar y no escribió nada,
    0.32 USD tirados) y el JSON de una factura larga. El `stop_reason` vuelve en `uso` para saber si se cortó."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(p).decode()}}
               for p in pngs]
    content.append({"type": "text", "text": _prompt(texto, pista)})
    r = requests.post(API_URL, timeout=TIMEOUT, headers={"x-api-key": _clave(), "anthropic-version": "2023-06-01",
                                                          "content-type": "application/json"},
                      json={"model": modelo(), "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    ent = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return ("".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"),
            {"fichas_entrada": ent, "fichas_salida": u.get("output_tokens", 0), "estimado": False,
             "stop_reason": d.get("stop_reason")})


def _llamar_cli(pngs, texto, pista=""):
    with tempfile.TemporaryDirectory(prefix="fase4_") as tmp:
        for k, p in enumerate(pngs, 1):
            (Path(tmp) / f"pagina{k}.png").write_bytes(p)
        prompt = (f"Read the page images pagina1.png..pagina{len(pngs)}.png in this folder with the Read tool, then follow "
                  "these instructions.\n\n" + _prompt(texto, pista))
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
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def _documentos(p):
    """Documentos de la respuesta: forma nueva {"documentos": [...]}; forma vieja (un solo objeto con `lineas`, las
    respuestas guardadas hasta el 2026-10-29) = un documento."""
    if not isinstance(p, dict):
        return []
    if isinstance(p.get("documentos"), list):
        return [d for d in p["documentos"] if isinstance(d, dict)]
    return [p] if "lineas" in p else []


def _paginas_doc(d, paginas):
    """Páginas absolutas [a, b] de un documento a partir de las relativas de Claude (1 = primera página enviada),
    recortadas al grupo; sin dato válido, las del grupo."""
    try:
        a, b = int(d["paginas"][0]), int(d["paginas"][1])
    except (KeyError, TypeError, ValueError, IndexError):
        return list(paginas)
    n = paginas[1] - paginas[0] + 1
    a, b = max(1, min(a, n)), max(1, min(b, n))
    if b < a:
        a, b = b, a
    return [paginas[0] + a - 1, paginas[0] + b - 1]


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
    fuera = str(p.get("fuera") or "").strip()
    if fuera:
        fallos.append(f"the AI model reports other document(s) on these pages that it did not include: {fuera[:300]}")
    # El prompt pide `explicacion` vacía si la factura está completa y cuadra: cualquier texto es el modelo avisando de
    # algo que una persona tiene que ver. De las 45 facturas resueltas hasta el 2026-10-29, las 7 con explicación eran
    # archivos con varios documentos (34293: «…a separate G2 Wireless sales order (22540.00) that was not included»).
    if str(p.get("explicacion") or "").strip():
        fallos.append("the AI model reports a problem on the paper (see its explanation)")
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
        elif amt == 0:
            # La misma regla que la fase 2 (decisión del usuario): un artículo impreso a 0.00 puede ser gratis o no
            # enviado (B/O) y el papel no lo dice. 34279: Claude unas veces omite esas líneas y otras las lista.
            fallos.append(f"item «{desc[:30]}» printed with amount 0.00: free or not shipped, the paper does not say")
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


def perdidas(viejas, nuevas, impresos):
    """Líneas `goods` de la lectura anterior con importe IMPRESO que ninguna línea de Claude conserva (por importe, una a
    una: dos artículos a 375.00 necesitan dos líneas a 375.00). Una cifra impresa que desaparece nunca se acepta en
    silencio (34293: 13 líneas de G2 Wireless, 22540.00). No cuentan: importe 0.00 (sin valor: 34279, 31059, 33115) y la
    cifra que sigue en una línea de Claude como precio unitario impreso de cantidad > 1 (el 27B dio el unitario como
    importe y Claude lo corrigió: 32745 2 × 5.54 = 11.08, 33058 3 × 19.76 = 59.28)."""
    libres = collections.Counter(l["amount"] for l in nuevas if l["amount"])
    libres.update(l["unit"] for l in nuevas if l["unit"] and l["qty"] > 1)
    out = []
    for l in viejas:
        a = l.get("importe_linea") or ""
        if l.get("line_kind") != "goods" or not a or a not in impresos or Decimal(a) == 0:
            continue
        if libres[a] > 0:
            libres[a] -= 1
        else:
            out.append(l)
    return out


# ----------------------------------------------------------------------------- aplicar a una factura

def _texto_factura(res, paginas, para_claude=False):
    """Texto de GLM-OCR de las páginas de la factura (página completa + tablas de regiones). A Claude va SIN la sección
    «### Verificación de importes» de cada página: es un diagnóstico de SCANIA, no el papel (el 27B tampoco la recibe);
    para `Leido` se usa entero (sus cifras ya están en las tablas, no se pierde nada)."""
    md = res["markdown"]
    if not para_claude:
        return divisas.invoice_text(md, paginas) or md
    parts = re.split(r"(?m)^## Página (\d+)\s*$", md)
    a, b = paginas
    paginas_md = [parts[i + 1] for i in range(1, len(parts) - 1, 2) if a <= int(parts[i]) <= b] or [md]
    return "\n".join(p.split(f2.VERIF_HEADER)[0].rstrip() for p in paginas_md)


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


def _lineas_json(lineas, leido, divisa, pagina, estimadas, en_revision):
    """Líneas del esquema a partir de las normalizadas por `comprobar`, cada una con su evidencia (lo que ningún OCR
    respalda va como «modelo» y la línea no queda verificada, regla 4 de CLAUDE.md)."""
    out = []
    for i, l in enumerate(lineas):
        key = p1.squash(l["desc"])[:20]
        amt = Decimal(l["amount"]) if l["amount"] else None
        printed_amt = l["amount"] in leido.importes if l["amount"] else False
        qty_ev = ("leido" if f2.qty_printed(l["qty"], leido.sq, key)
                  else "aritmetica" if l["qty"] > 1 and l["unit"] and amt is not None and l["qty"] * Decimal(l["unit"]) == amt
                  else "asumido" if l["qty"] == 1 else "modelo")
        ev = {"descripcion": "leido" if leido.descripcion(l["desc"]) else "modelo",
              "importe_linea": ("leido" if printed_amt else "modelo") if l["amount"] else "",
              "precio_unitario": "leido" if l["unit"] else "", "cantidad": qty_ev}
        ocr = {"precio_unitario": l["unit"] or (f"{amt / l['qty']:.2f}" if amt is not None else ""),
               "cantidad_impresa": qty_ev == "leido",
               "vendedor": "", "seguimiento": "", "estado_envio": "", "condicion": "", "pagina": pagina,
               "origen": "claude",
               "verificado": ev["descripcion"] == "leido" and ev["importe_linea"] == "leido" and qty_ev != "modelo",
               "evidencia": ev}
        line = f2.goods_line(i, {"desc": l["desc"], "qty": l["qty"], "amount": l["amount"]}, divisa, ocr)
        # fase 3: una línea que sigue sin importe conserva la estimación que tenía la misma descripción
        line["estimacion"] = None if (l["amount"] or not en_revision) else estimadas.get(p1.squash(l["desc"])[:30])
        out.append(line)
    return out


def _cabecera_aceptada(o, e):
    """Cabecera de un documento validado (todas sus cifras están impresas y cuadran)."""
    cab, listas, lineas = e["cab"], e["listas"], e["lineas"]
    o["subtotal_articulos"] = f"{sum(Decimal(l['amount']) for l in lineas)}"
    o["envio"] = f"{cab['envio']}" if cab["envio"] is not None else ""
    o["impuestos"] = f"{sum(Decimal(c['importe']) for c in listas['impuestos'])}" if listas["impuestos"] else ""
    o["descuentos"], o["other_charges"] = listas["descuentos"], listas["otros_cargos"]


def _cabecera_revision(doc, pendientes, leido):
    """Cabecera de la factura que queda en revisión: solo cifras impresas; con varios documentos pendientes no hay una
    cabecera común (subtotal = suma de las líneas si todas tienen importe; lo demás vacío; el total queda como estaba)."""
    o = doc["ocr"]
    lineas = [l for e in pendientes for l in e["lineas"]]
    o["subtotal_articulos"] = f"{sum(Decimal(l['amount']) for l in lineas)}" if lineas and all(l["amount"] for l in lineas) else ""
    if len(pendientes) != 1:
        o["envio"], o["impuestos"], o["descuentos"], o["other_charges"] = "", "", [], []
        return
    e = pendientes[0]
    cab, listas = e["cab"], e["listas"]
    impreso = lambda v: v is not None and v != 0 and f"{v}" in leido.importes  # noqa: E731
    o["envio"] = f"{cab['envio']}" if impreso(cab["envio"]) else ""
    imps = [c for c in listas["impuestos"] if c["importe"] in leido.importes]
    o["impuestos"] = f"{sum(Decimal(c['importe']) for c in imps)}" if imps else ""
    o["descuentos"] = [c for c in listas["descuentos"] if c["importe"] in leido.importes]
    o["other_charges"] = [c for c in listas["otros_cargos"] if c["importe"] in leido.importes]
    if impreso(cab["total"]):
        doc.update(total=f"{cab['total']}", total_is_printed=True)


ORDEN_TEXTO = ("fecha_pedido", "metodo_pago", "destinatario", "direccion_envio", "proveedor", "total_antes_impuestos")


def _factura_nueva(base, e, nuevas, nuevo_id, k, n):
    """Factura nueva (misma forma que las de la fase 2: copia del esqueleto de la factura del grupo) para un documento
    validado que no es el primero del grupo. Los datos de cabecera de texto del grupo (fecha, pago, destinatario…) no
    se heredan: pueden ser de otro documento; la fecha es la de Claude solo si está impresa."""
    d = copy.deepcopy(base)
    o = d["ocr"]
    for key in ("duplicado", "envio_bloque", "pedido", "total_pedido"):
        o.pop(key, None)
    o.update({key: "" for key in ORDEN_TEXTO if key in o})
    o.update(paginas=e["paginas"], envios=[], incidencias=[], origen_lineas="claude", fecha_pedido=e["fecha"],
             fase4={"modelo": modelo(), "documento": [k, n], "tipo": e["tipo"], "vendedor": e["vendedor"]})
    _cabecera_aceptada(o, e)
    d.update(id=nuevo_id, estado="DONE", motivo_revision="", mismatch="", total=f"{e['cab']['total']}",
             total_is_printed=True, referencia=e["referencia"], lineas=nuevas)
    return d


def _etiqueta(e, n):
    return (f"document {e['k']} of {n}" + (f" ({e['vendedor']}" + (f", {e['referencia']}" if e["referencia"] else "") + ")"
                                         if e["vendedor"] else "")) if n > 1 else ""


def resolver(doc, res, log, llamar=None, pista="", out=None):
    """Pasa UNA factura en revisión por la Claude API. Claude devuelve TODOS los documentos de sus páginas (un archivo
    puede traer varias facturas; la fase 2 no siempre las separa: 34293, 33692, 33151, 34160-B); cada uno se comprueba
    por separado con las reglas de siempre. Los que validan salen sin revisión: el primero en la propia factura y los
    demás como facturas NUEVAS al final de `out` (los `id` de WTCP nunca cambian); los que no validan quedan juntos en
    la factura, en LECTURA, con sus líneas y su evidencia (aceptación parcial, decisión del usuario 2026-10-29). Nada se
    acepta si entre todos los documentos falta una cifra impresa de la lectura anterior. Devuelve cuántas facturas
    quedaron sin revisión. `pista`: número impreso del documento a extraer cuando la factura comparte páginas con otra
    (ver `aplicar`)."""
    paginas = (doc.get("ocr") or {}).get("paginas") or [1, res.get("n_pages") or 1]
    texto = _texto_factura(res, paginas)
    llamar = llamar or (_llamar_api if modo() == "api" else _llamar_cli)
    t0 = time.time()
    try:
        crudo, uso = llamar(_paginas_png(res["pdf"], paginas), _texto_factura(res, paginas, para_claude=True), pista)
        p = _respuesta(crudo)
    except Exception as e:  # noqa: BLE001  sin red, sin saldo, CLI ausente: la factura sigue como estaba
        log(f"  fase 4: factura {doc['id']}: Claude no respondió ({type(e).__name__}: {str(e)[:120]})")
        return 0
    coste = coste_api(uso.get("fichas_entrada", 0), uso.get("fichas_salida", 0))
    f2.MODEL_RAW.setdefault("claude", []).append({"factura": doc["id"], "paginas": paginas, "modelo": modelo(),
                                                  "pista": pista, "respuesta": crudo, "uso": uso,
                                                  "coste_api_usd": coste, "segundos": round(time.time() - t0, 1)})
    log(f"  fase 4: factura {doc['id']}: Claude API {modelo()}, {uso.get('fichas_entrada', 0)} fichas de entrada + "
        f"{uso.get('fichas_salida', 0)} de salida = {coste if coste is not None else '?'} USD"
        + (" (estimado)" if uso.get("estimado") else ""))
    if uso.get("stop_reason") == "max_tokens":
        log(f"  fase 4: factura {doc['id']}: respuesta CORTADA por max_tokens ({MAX_TOKENS}): la llamada no sirve")
    docs = _documentos(p)
    if not docs:
        log(f"  fase 4: factura {doc['id']}: respuesta sin JSON o sin documentos")
        return 0
    leido = Leido(res, paginas, texto, log)
    if pista:  # páginas compartidas: solo el documento pedido; los demás son las facturas hermanas
        docs = [d for d in docs if p1.squash(str(d.get("referencia") or "")) == p1.squash(pista)]
        if not docs:
            log(f"  fase 4: factura {doc['id']}: Claude no devolvió el documento {pista}: sigue como estaba")
            return 0
    n = len(docs)
    evals = []
    for k, d in enumerate(docs, 1):
        fallos, lineas, cab, listas = comprobar(d, leido)
        ref = str(d.get("referencia") or "").strip()
        fecha = str(d.get("fecha") or "").strip()
        evals.append({"k": k, "tipo": str(d.get("tipo") or ""), "vendedor": str(d.get("vendedor") or "").strip()[:80],
                      "referencia": ref if ref and p1.squash(ref) and p1.squash(ref) in leido.sq else "",
                      "fecha": fecha if fecha and fecha.lower() in texto.lower() else "",
                      "paginas": _paginas_doc(d, paginas), "fallos": fallos, "lineas": lineas, "cab": cab,
                      "listas": listas, "explicacion": str(d.get("explicacion") or "").strip(),
                      "facturable": any(l["amount"] for l in lineas) or bool(cab["total"])})
    # Puerta de cobertura: nunca se pierde una cifra impresa. Lo que la lectura anterior leyó con importe impreso tiene
    # que seguir en la UNIÓN de las líneas de todos los documentos (34293: 22540.00 desaparecieron).
    union = [l for e in evals for l in e["lineas"]]
    fuera = perdidas(doc["lineas"], union, leido.importes)
    cobertura = ""
    if fuera:
        suma = sum(Decimal(l["importe_linea"]) for l in fuera)
        cobertura = (f"leaves out {len(fuera)} item line(s) with printed amounts (sum {suma}) found by the OCR reading: "
                     + "; ".join(f"«{l['descripcion'][:40]}» {l['importe_linea']}" for l in fuera[:6])
                     + ("; …" if len(fuera) > 6 else ""))
    facturables = [e for e in evals if e["facturable"]]
    aceptadas = [e for e in facturables if not e["fallos"] and not fuera]
    if out is None and len(aceptadas) > 1:  # sin lista donde añadir facturas no se separa nada: todo a revisión
        aceptadas = []
    pendientes = [e for e in facturables if e not in aceptadas]
    explicaciones = [f"AI reading (unverified): {_etiqueta(e, n) + ': ' if n > 1 else ''}{e['explicacion']}"
                     for e in evals if e["explicacion"]]
    o = doc["ocr"]
    # La lectura anterior entera y el desglose por documento quedan siempre en ocr.fase4 (34293 solo conservó el número
    # de líneas y hubo que ir a modelos.json para ver qué se había perdido).
    o["fase4"] = {"modelo": modelo(),
                  "antes": {"motivo_revision": doc["motivo_revision"], "mismatch": doc["mismatch"], "total": doc["total"],
                            "total_is_printed": doc["total_is_printed"], "lineas": len(doc["lineas"]),
                            "lineas_antes": doc["lineas"],
                            "cabecera": {k: o.get(k) for k in ("subtotal_articulos", "envio", "impuestos", "descuentos",
                                                               "other_charges")}},
                  "documentos": [{"documento": e["k"], "tipo": e["tipo"], "vendedor": e["vendedor"],
                                  "referencia": e["referencia"], "paginas": e["paginas"], "lineas": len(e["lineas"]),
                                  "total": f"{e['cab']['total']}" if e["cab"]["total"] is not None else "",
                                  "validada": e in aceptadas, "fallos": e["fallos"], "explicacion": e["explicacion"]}
                                 for e in evals]}
    if cobertura:
        o["fase4"]["cobertura"] = cobertura
    if not union or not facturables:  # nada que aprovechar: la factura sigue como estaba, con los avisos
        notas = ([f"AI reading (Claude) {cobertura}"] if cobertura else []) + explicaciones
        if notas:
            doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"]] + notas))
            o["incidencias"].extend(notas)
        log(f"  fase 4: factura {doc['id']} sigue en revisión: " + "; ".join(
            f for e in evals for f in e["fallos"][:2]) + (f"; {cobertura}" if cobertura else ""))
        return 0
    divisa = next((l.get("divisa") for l in doc["lineas"] if l.get("divisa")), "") or "USD"
    estimadas = {p1.squash(l["descripcion"])[:30]: l["estimacion"] for l in doc["lineas"]
                 if l.get("line_kind") == "goods" and l.get("estimacion")}
    for e in evals:
        e["nuevas"] = _lineas_json(e["lineas"], leido, divisa, e["paginas"][0], estimadas, e not in aceptadas)
    pend_lineas = [l for e in pendientes for l in e["nuevas"]]
    for i, l in enumerate(pend_lineas):
        l["seq"] = i
    fallos_pend = [(_etiqueta(e, n) + ": " if n > 1 else "") + "; ".join(e["fallos"]) for e in pendientes]
    if not aceptadas:
        # Nunca peor que lo que ya había: si la lectura anterior tiene más líneas con importe o más verificadas (34086:
        # 100 líneas, Claude 28; 34160-A: 43, Claude 7), se queda, y la lectura de Claude va entera en ocr.fase4.
        viejas = [l for l in doc["lineas"] if l.get("line_kind") == "goods"]
        cuenta = lambda ls: (sum(1 for l in ls if l["importe_linea"]), sum(1 for l in ls if l["ocr"].get("verificado")))  # noqa: E731
        if any(a < v for a, v in zip(cuenta(pend_lineas), cuenta(viejas))):
            o["fase4"].update(lineas_claude=pend_lineas, fallos=fallos_pend)
            o["fase4"].pop("antes")
            notas = ([f"AI reading (Claude) {cobertura}"] if cobertura else []) + explicaciones
            if notas:
                doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"]] + notas))
                o["incidencias"].extend(notas)
            log(f"  fase 4: factura {doc['id']} sigue en revisión con su lectura anterior ({len(viejas)} líneas; Claude "
                f"{len(pend_lineas)}, en ocr.fase4): " + "; ".join(fallos_pend)[:300])
            return 0
    if pendientes:
        # La factura del grupo queda en LECTURA con los documentos no validados (se paga por la lectura de Claude: se
        # aprovecha siempre; 33477: el 27B no vio los precios y salían 0 líneas), cada línea con su evidencia.
        doc["lineas"] = pend_lineas
        o["origen_lineas"] = "claude"
        if len(pendientes) == 1 and len(facturables) == 1:
            o["paginas"] = list(paginas)
        elif len(pendientes) == 1:
            o["paginas"] = pendientes[0]["paginas"]
        if n > 1:  # la factura en revisión ya no es el grupo entero: la referencia y el total del grupo no le pertenecen
            doc["referencia"] = pendientes[0]["referencia"] if len(pendientes) == 1 else ""
            doc.update(total="", total_is_printed=False)
            o.update({key: "" for key in ORDEN_TEXTO if key in o})
            if len(pendientes) == 1:
                o["fecha_pedido"] = pendientes[0]["fecha"]
        _cabecera_revision(doc, pendientes, leido)
        notas = ["item lines read by the AI model (Claude), not confirmed: " + " | ".join(fallos_pend)]
        if cobertura:
            notas.append(f"AI reading (Claude) {cobertura}")
        notas += explicaciones
        o["incidencias"] = notas
        doc.update(motivo_revision="LECTURA", mismatch="; ".join(notas))
        log(f"  fase 4: factura {doc['id']} sigue en revisión con las {len(pend_lineas)} líneas de Claude"
            f"{f' ({len(pendientes)} de {n} documentos)' if n > 1 else ''}: " + "; ".join(fallos_pend)[:300])
    nuevas_facturas = []
    for j, e in enumerate(aceptadas):
        if j == 0 and not pendientes:  # el primer documento validado ocupa la factura del grupo (mismo id)
            doc["lineas"] = e["nuevas"]
            o["origen_lineas"] = "claude"
            o["incidencias"] = []
            o["fase4"].update(documento=[e["k"], n], tipo=e["tipo"], vendedor=e["vendedor"])
            if n > 1:
                o["paginas"] = e["paginas"]
                o.update({key: "" for key in ORDEN_TEXTO if key in o})
                o["fecha_pedido"] = e["fecha"]
            _cabecera_aceptada(o, e)
            # con varios documentos, la referencia del grupo (la fase 2 tomó la de cualquiera de ellos: 34293 llevaba la
            # de Suresh para el pedido de G2) no vale: la de ESTE documento, si está impresa
            doc.update(motivo_revision="", mismatch="", total=f"{e['cab']['total']}", total_is_printed=True,
                       referencia=(e["referencia"] if n > 1 else (doc.get("referencia") or e["referencia"])))
            log(f"  fase 4: factura {doc['id']} resuelta por Claude ({len(e['nuevas'])} líneas, total {doc['total']}"
                f"{', ' + _etiqueta(e, n) if n > 1 else ''}): sin revisión")
        else:
            nueva = _factura_nueva(doc, e, e["nuevas"], len(out) + len(nuevas_facturas), e["k"], n)
            nuevas_facturas.append(nueva)
            log(f"  fase 4: factura NUEVA {nueva['id']} ({_etiqueta(e, n)}, páginas {e['paginas']}): "
                f"{len(e['nuevas'])} líneas, total {nueva['total']}, sin revisión")
    out.extend(nuevas_facturas) if nuevas_facturas else None
    return len(aceptadas)


def _solapan(a, b):
    """¿Dos rangos de páginas [a1, a2] y [b1, b2] comparten alguna página?"""
    return bool(a and b) and a[0] <= b[1] and b[0] <= a[1]


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
    n, n_out = 0, len(out)
    for d in candidatas:
        # Facturas que comparten páginas con otra del mismo archivo: Claude recibe la misma imagen para las dos y no sabe
        # cuál es cada una (34142: los dos envíos de un pedido de Amazon salieron con los mismos 3 artículos). Con número
        # de pedido impreso propio se le dice cuál leer; mismo número (envíos) o sin número, no se manda.
        otras = [o for o in out if o is not d and _solapan(o.get("ocr", {}).get("paginas"), d.get("ocr", {}).get("paginas"))]
        pista = ""
        if otras:
            ref = (d.get("referencia") or "").strip()
            if not ref or any((o.get("referencia") or "").strip() == ref for o in otras):
                log(f"  fase 4: factura {d['id']} comparte páginas con la factura {otras[0]['id']} "
                    f"({'mismo pedido' if ref else 'sin número de pedido'}): no se manda a Claude")
                continue
            pista = ref
        n += resolver(d, res, log, llamar, pista, out)
    if len(out) > n_out:
        # Facturas nuevas: el mismo chequeo de duplicados dentro del archivo que hace build_all (una factura y la
        # confirmación del mismo pedido con las mismas cifras se declararían dos veces).
        f2.mark_file_duplicates(out, res.get("markdown", ""), Path(res["pdf"]).name)
        log(f"Fase 4: {len(out) - n_out} factura(s) nueva(s) separada(s) por Claude (ids {n_out}..{len(out) - 1})")
    costes = [c.get("coste_api_usd") or 0 for c in f2.MODEL_RAW.get("claude", [])[antes:]]
    log(f"Fase 4: {n} factura(s) sin revisión de {len(candidatas)} en revisión; coste Claude API del documento "
        f"{sum(costes):.4f} USD")
    return n
