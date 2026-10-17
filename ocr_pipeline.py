"""
OCR de PDF con GLM-OCR (llama-server) combinando las dos formas documentadas de usar el modelo
y verificando que no se pierdan importes.

Lecturas:
  A. Página completa con "Text Recognition:" (funciones de ocr.py). Lee bien el texto corrido,
     direcciones, fechas y totales, pero tiende a omitir columnas de precios alineadas a la derecha.
  B. Regiones vía el SDK oficial glmocr (glmocr.yaml): PP-DocLayoutV3 detecta las cajas en CPU,
     cada recorte se reconoce con el prompt nativo de su tipo ("Table Recognition:" para tablas).
     Recupera los precios de las tablas, pero lee peor el texto pequeño fuera de ellas.

  C. Rescate con Qwen2.5-VL (llama-server en el puerto 8088 del host de modelos), solo cuando la verificación
     falla: la región afectada se recorta del PDF y se le pide al modelo la lista de artículos
     con cantidad y precio, siguiendo el patrón de extracción clave-valor del cookbook oficial
     de Qwen ("Extract the key-value information in the format:{...}"). El precio recuperado
     se incorpora y se vuelve a comprobar la suma.

Salida por página: el texto de A, las tablas detectadas por B (HTML) y una verificación:
  - importes que están en una lectura y faltan en la otra;
  - si el documento tiene bloques de envío al estilo Amazon ("N of: ... $precio" y
    "Total for This Shipment: $X"), se comprueba que cantidad x precio cuadre con el total.

Uso:
    .venv/bin/python ocr_pipeline.py documento.pdf -o salida.md
    .venv/bin/python ocr_pipeline.py documento.pdf --pages 1-3 --dpi 200
"""

import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import requests
from glmocr import GlmOcr

from ocr import DEFAULT_SERVER, MODELS_HOST, PROMPTS, ocr_image, parse_pages, render_page, server_ok

HERE = Path(__file__).resolve().parent
CFG_REGIONS = HERE / "glmocr.yaml"

# llama.cpp limita las imágenes de GLM-OCR (proyector glm4v) a 4096 tokens y reescala con filtro
# bilineal lo que se pase (tools/mtmd/clip.cpp: set_limit_image_tokens(8, 4096)). Cada token cubre
# un bloque de 28x28 píxeles (patch 14, fusión 2x2). Si la página entera cabe en ese tope, el servidor
# no la toca y el texto pequeño llega nítido; por eso el dpi efectivo se calcula por página.
LLAMA_MAX_IMAGE_TOKENS = 4096
PX_PER_TOKEN = 14 * 14 * 2 * 2

# Qwen2.5-VL-7B-Instruct en el puerto 8088 del host de modelos (ver ocr.MODELS_HOST). Documentación aplicada:
# - Prompt: patrón "Visual Information Extraction" del cookbook oficial (plantilla JSON con
#   claves vacías), imagen primero y texto después, sin mensaje de sistema.
# - generation_config.json del modelo: temperatura ~0 y repetition_penalty 1.05 (llama-server
#   lo llama repeat_penalty).
# - Resolución: la tarjeta recomienda 256-1280 fichas de imagen y dice que más resolución mejora;
#   llama.cpp admite hasta 4096 fichas para Qwen2.5-VL y avisa de pérdida de precisión por
#   debajo de 1024. El recorte se renderiza desde el PDF al dpi que lo deja en ese rango.
QWEN_SERVER = f"http://{MODELS_HOST}:8088"
QWEN_MIN_TOKENS, QWEN_MAX_TOKENS = 1024, 4096
QWEN_KIE_PROMPT = ('Extract the key-value information in the format:'
                   '{"items": [{"quantity": "", "description": "", "unit_price": ""}], '
                   '"total_for_this_shipment": ""}')
# llama-server admite response_format con esquema JSON (muestreo guiado por gramática). Ojo: el
# código del servidor (server-common.cpp) espera {"type":"json_schema","json_schema":{"schema":...}},
# el envoltorio al estilo OpenAI; con "schema" suelto la restricción se ignora en silencio.
QWEN_KIE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"quantity": {"type": "string"}, "description": {"type": "string"}, "unit_price": {"type": "string"}},
            "required": ["quantity", "description", "unit_price"]}},
        "total_for_this_shipment": {"type": "string"},
    },
    "required": ["items", "total_for_this_shipment"],
}

AMOUNT_RE = re.compile(r"(?:USD|\$)\s*-?\s*(\d[\d,]*\.\d{2})|-\s*\$\s*(\d[\d,]*\.\d{2})", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def amounts(text):
    """Importes con marca de moneda, normalizados a '1234.56'."""
    out = set()
    for a, b in AMOUNT_RE.findall(text or ""):
        v = (a or b).replace(",", "")
        out.add(f"{float(v):.2f}")
    return out


def fmt(values):
    return ", ".join(f"${v}" for v in sorted(values, key=float)) if values else "ninguno"


# ----------------------------------------------------------------------------- lectura A

def fit_dpi(page, dpi, image_max_tokens=LLAMA_MAX_IMAGE_TOKENS):
    """Mayor dpi <= pedido con el que la página entera no supera el tope de tokens de imagen."""
    w_in, h_in = page.rect.width / 72, page.rect.height / 72
    fit = int((image_max_tokens * PX_PER_TOKEN / (w_in * h_in)) ** 0.5)
    return max(72, min(dpi, fit))


def read_full_pages(pdf, pages_spec, server, dpi, max_tokens, timeout, log, image_max_tokens=LLAMA_MAX_IMAGE_TOKENS):
    doc = fitz.open(pdf)
    try:
        idxs = parse_pages(pages_spec, doc.page_count)
        texts = {}
        for i in idxs:
            d = fit_dpi(doc[i], dpi, image_max_tokens)
            log(f"  página completa {i + 1}/{doc.page_count} a {d} dpi...")
            texts[i] = ocr_image(server, render_page(doc[i], d), PROMPTS["text"], max_tokens, timeout)
        return texts, doc.page_count
    finally:
        doc.close()


# ----------------------------------------------------------------------------- lectura B

def read_regions(pdf):
    """glmocr: lista de páginas -> lista de regiones {label, content, bbox_2d}."""
    with GlmOcr(config_path=str(CFG_REGIONS), mode="selfhosted", log_level="WARNING", ocr_api_host=MODELS_HOST) as parser:
        res = parser.parse(str(pdf))
    data = res.json_result
    if isinstance(data, str):
        data = json.loads(data)
    if data and isinstance(data[0], dict):
        pages = {}
        for r in data:
            pages.setdefault(r.get("page_idx", 0), []).append(r)
        data = [pages[k] for k in sorted(pages)]
    return data


# ----------------------------------------------------------------------------- verificación

ITEM_RE = re.compile(r"^\s*(\d+)\s*of:", re.I)
# GLM-OCR a veces omite los espacios ("TotalforThisShipment:$87.89"), así que cabeceras y
# etiquetas se comparan sobre el texto sin espacios y en minúsculas.
BLOCK_KEYS = ("notyetshipped", "shippedon", "delivered", "arriving")


def squash(text):
    return re.sub(r"\s+", "", text or "").lower()


def first_amount(text):
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    return float((m.group(1) or m.group(2)).replace(",", ""))


DESC_STOP = re.compile(r"(Sold by|Seller Credentials|Business Price|Condition:|Shipping Address)", re.I)


def item_description(cell_text):
    """'3 of: Red tray ... Sold by ...' -> 'Red tray ...' (corta en los campos que siguen al título)."""
    desc = cell_text.split("of:", 1)[1] if "of:" in cell_text else cell_text
    desc = DESC_STOP.split(desc, 1)[0]
    return re.sub(r"\s+", " ", html.unescape(desc)).strip(" -–,;")


def rows_of(content):
    """Divide el contenido de una región en filas de celdas (HTML) o líneas (texto)."""
    if "<table" in content:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.S):
            cells = [html.unescape(TAG_RE.sub(" ", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            cells = [c for c in cells if c]
            if cells:
                yield cells
    else:
        for line in html.unescape(content).splitlines():
            if line.strip():
                yield [line.strip()]


def check_shipments(region_pages):
    """Comprueba cantidad x precio contra 'Total for This Shipment' en documentos tipo Amazon."""
    items, results, unverified, grand = [], [], [], None
    for pi, regions in enumerate(region_pages):
        for ri, r in enumerate(regions):
            for cells in rows_of(str(r.get("content") or "")):
                norm, first = squash(" ".join(cells)), squash(cells[0])
                if len(cells) == 1 and len(first) < 40 and first.startswith(BLOCK_KEYS):
                    if items:
                        unverified.append({"page": pi + 1, "items": items})
                        items = []
                    continue
                m = ITEM_RE.match(cells[0])
                if m:
                    price = first_amount(" ".join(cells[1:]))
                    if price is None:
                        price = first_amount(cells[0].split("of:", 1)[1])
                    items.append({"page": pi + 1, "ri": ri, "qty": int(m.group(1)), "price": price,
                                  "text": cells[0][:60], "desc": item_description(cells[0]), "src": "glm"})
                    continue
                if "totalforthisshipment" in norm:
                    total = first_amount(norm.split("totalforthisshipment", 1)[1])
                    if total is not None:
                        results.append({"page": pi + 1, "items": items, "total": total})
                        items = []
                elif "grandtotal" in norm and first_amount(norm.split("grandtotal", 1)[1]) is not None:
                    grand = first_amount(norm.split("grandtotal", 1)[1])
    if items:
        unverified.append({"page": len(region_pages), "items": items})
    return results, unverified, grand


def shipment_sum(res):
    return sum(it["qty"] * it["price"] for it in res["items"] if it["price"] is not None)


def shipment_ok(res):
    return all(it["price"] is not None for it in res["items"]) and abs(shipment_sum(res) - res["total"]) < 0.01


def shipments_markdown(results, unverified, grand):
    if not results and not unverified:
        return ""
    lines = ["## Verificación de envíos", ""]
    ok = 0
    for res in results:
        page, items, total, s = res["page"], res["items"], res["total"], shipment_sum(res)
        detail = " + ".join(f"{it['qty']}×${it['price']:.2f}" + ("*" if it["src"] == "qwen" else "")
                            for it in items if it["price"] is not None)
        missing = [it for it in items if it["price"] is None]
        if missing:
            lines.append(f"- ⚠ Envío que cierra en la página {page}: {len(missing)} artículo(s) sin precio leído; "
                         f"suma parcial ${s:.2f} vs total ${total:.2f}. Sin precio: "
                         + "; ".join(f"«{it['text']}»" for it in missing))
        elif abs(s - total) < 0.01:
            ok += 1
            lines.append(f"- ✓ Envío que cierra en la página {page}: {detail} = ${total:.2f}")
        else:
            lines.append(f"- ✗ Envío que cierra en la página {page}: {detail} = ${s:.2f}, pero el total dice ${total:.2f} "
                         f"(diferencia ${abs(s - total):.2f})")
    for blk in unverified:
        lines.append(f"- · Bloque sin línea de total en la página {blk['page']}: {len(blk['items'])} artículo(s) no verificables")
    if grand is not None:
        s = sum(r["total"] for r in results)
        n_unv = sum(len(b["items"]) for b in unverified)
        lines.append(f"- Total general ${grand:.2f}; suma de los envíos con línea de total ${s:.2f}"
                     + (f"; diferencia ${abs(grand - s):.2f} ({n_unv} artículo(s) en bloques sin total)" if abs(grand - s) >= 0.01 else ""))
    if any(it["src"] == "qwen" for r in results for it in r["items"]):
        lines.append("- (*) precio recuperado con Qwen2.5-VL a partir del recorte de la región")
    lines.append(f"\n{ok} de {len(results)} envíos cuadran.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- rescate con Qwen2.5-VL

def crop_b64(doc, page_idx, bbox, pad=0.005):
    """Recorta la región (bbox normalizado 0-1000) y la renderiza al dpi que la deja entre
    QWEN_MIN_TOKENS y QWEN_MAX_TOKENS fichas de imagen (28x28 px por ficha)."""
    page = doc[page_idx]
    W, H = page.rect.width, page.rect.height
    x1, y1, x2, y2 = bbox
    rect = fitz.Rect(max(0, (x1 / 1000 - pad) * W), max(0, (y1 / 1000 - pad) * H),
                     min(W, (x2 / 1000 + pad) * W), min(H, (y2 / 1000 + pad) * H))
    area_in2 = (rect.width / 72) * (rect.height / 72)
    dpi_max = (QWEN_MAX_TOKENS * PX_PER_TOKEN / area_in2) ** 0.5
    dpi_min = (QWEN_MIN_TOKENS * PX_PER_TOKEN / area_in2) ** 0.5
    dpi = int(max(dpi_min, min(dpi_max, 300)))  # 300: no inventar detalle por encima del escaneo típico
    pix = page.get_pixmap(dpi=dpi, clip=rect, alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode("ascii"), dpi, (pix.width, pix.height)


def qwen_extract_items(server, image_b64, timeout):
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            {"type": "text", "text": QWEN_KIE_PROMPT},
        ]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": 1.05, "max_tokens": 1024,
        # Forma que lee llama-server (tools/server/server-common.cpp): json_schema -> {schema}
        "response_format": {"type": "json_schema", "json_schema": {"name": "items", "schema": QWEN_KIE_SCHEMA}},
    }
    r = requests.post(f"{server}/v1/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    if not data.get("items"):
        # Respaldo: respuesta en viñetas "Quantity: 1 / Description: ... / Unit Price: $79.99"
        data = {"items": [{"quantity": q, "description": d, "unit_price": u} for q, d, u in re.findall(
            r"Quantity\W+(\d+).*?Description\W+(.+?)\s*[-*\n]+\s*Unit Price\W+(\$?[\d,]+\.\d{2})", content, re.S | re.I)]}
    items = []
    for it in data.get("items", []) or []:
        q = re.search(r"\d+", str(it.get("quantity", "")))
        price = first_amount(str(it.get("unit_price", "")))
        if price is None:
            pm = re.search(r"\d[\d,]*\.\d{2}", str(it.get("unit_price", "")))
            price = float(pm.group(0).replace(",", "")) if pm else None
        items.append({"qty": int(q.group(0)) if q else None, "price": price,
                      "desc": squash(str(it.get("description", "")))})
    return items, content


def _row_span(row):
    """Columnas que ocupa una fila: cada <td> cuenta 1, o su colspan si lo tiene."""
    total = 0
    for m in re.finditer(r"<td([^>]*)>", row):
        c = re.search(r'colspan="?(\d+)', m.group(1))
        total += int(c.group(1)) if c else 1
    return total


def _table_columns(content):
    """Ancho habitual de la tabla: el número de columnas más frecuente entre sus filas."""
    spans = [_row_span(r) for r in re.findall(r"<tr[^>]*>.*?</tr>", content, re.S)]
    return max(set(spans), key=spans.count) if spans else 0


def insert_price_in_region(region, item_text, price):
    """Escribe el precio recuperado en la fila del artículo dentro del contenido de la región:
    en tablas HTML rellena la primera celda vacía de esa fila (la columna de precio que el OCR
    dejó en blanco) o añade una celda, y luego reduce el colspan de la fila para que no exceda
    las columnas del resto de la tabla; en texto plano lo agrega al final de la línea."""
    content = str(region.get("content") or "")
    cell = f'<td title="recuperado con Qwen2.5-VL">${price:.2f}</td>'
    key = squash(item_text)[:30]
    if "<table" in content:
        ncols = _table_columns(content)

        def fix(m):
            row = m.group(0)
            if key not in squash(html.unescape(TAG_RE.sub(" ", row))):
                return row
            new, n = re.subn(r"<td(?:\s[^>]*)?>\s*</td>", cell, row, count=1)
            if not n:
                new = row.replace("</tr>", cell + "</tr>")
            # Si la fila ahora es más ancha que la tabla, recortar el colspan mayor.
            while ncols and _row_span(new) > ncols:
                spans = [(int(x.group(1)), x) for x in re.finditer(r'colspan="?(\d+)"?', new) if int(x.group(1)) > 1]
                if not spans:
                    break
                biggest = max(spans, key=lambda t: t[0])[1]
                new = new[:biggest.start()] + f'colspan="{int(biggest.group(1)) - 1}"' + new[biggest.end():]
            return new
        new_content = re.sub(r"<tr[^>]*>.*?</tr>", fix, content, flags=re.S)
    else:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if key in squash(line):
                lines[i] = line.rstrip() + f" ${price:.2f}"
                break
        new_content = "\n".join(lines)
    region["content"] = new_content
    return new_content != content


def recover_with_qwen(results, region_pages, pdf, server, timeout, log):
    """Para cada envío que no cuadra, relee con Qwen2.5-VL las regiones de sus artículos sin
    precio (o de todos si hay precios pero la suma no cuadra) y completa los precios."""
    pending = [r for r in results if not shipment_ok(r)]
    if not pending:
        return
    if not server_ok(server):
        log(f"  Qwen2.5-VL no responde en {server}; se omite el rescate")
        return
    doc = fitz.open(pdf)
    try:
        for res in pending:
            targets = [it for it in res["items"] if it["price"] is None] or res["items"]
            regions = sorted({(it["page"] - 1, it["ri"]) for it in targets})
            for page_idx, ri in regions:
                region = region_pages[page_idx][ri]
                bbox = region.get("bbox_2d")
                if not bbox:
                    continue
                img, dpi, size = crop_b64(doc, page_idx, bbox)
                log(f"  Qwen2.5-VL: p{page_idx + 1} región {ri + 1} a {dpi} dpi ({size[0]}x{size[1]} px)...")
                try:
                    qitems, raw = qwen_extract_items(server, img, timeout)
                except Exception as e:  # noqa: BLE001
                    log(f"    fallo: {e}")
                    continue
                log(f"    Qwen devolvió {len(qitems)} artículo(s): " + ", ".join(
                    f"{q['qty']}×{('$%.2f' % q['price']) if q['price'] is not None else '?'}" for q in qitems))
                for it in targets:
                    if it["page"] - 1 != page_idx or it["ri"] != ri:
                        continue
                    key = squash(it["text"].split("of:", 1)[-1])[:15]
                    for q in qitems:
                        if q["price"] is not None and q["qty"] == it["qty"] and (not key or key in q["desc"] or q["desc"][:15] in squash(it["text"])):
                            if it["price"] is None or abs(shipment_sum(res) - res["total"]) >= 0.01:
                                it["price"], it["src"] = q["price"], "qwen"
                                if insert_price_in_region(region, it["text"], q["price"]):
                                    log(f"    precio ${q['price']:.2f} insertado en la tabla de p{page_idx + 1} región {ri + 1}")
                            break
            state = "cuadra" if shipment_ok(res) else "sigue sin cuadrar"
            log(f"    envío de ${res['total']:.2f}: {state} (suma ${shipment_sum(res):.2f})")
    finally:
        doc.close()


# ----------------------------------------------------------------------------- ensamblado

def page_markdown(num, full_text, regions):
    tables = [str(r.get("content") or "") for r in regions if r.get("label") == "table" and r.get("content")]
    all_regions = " ".join(str(r.get("content") or "") for r in regions)
    a_full, a_reg = amounts(full_text), amounts(all_regions)
    parts = [f"## Página {num}", "", (full_text or "").strip() or "_(sin texto)_"]
    if tables:
        parts += ["", "### Tablas detectadas", ""] + [t.strip() + "\n" for t in tables]
    parts += ["", "### Verificación de importes", "",
              f"- En las regiones pero no en el texto completo: {fmt(a_reg - a_full)}",
              f"- En el texto completo pero no en las regiones: {fmt(a_full - a_reg)}"]
    return "\n".join(parts), a_reg - a_full, a_full - a_reg


def run(pdf, server=DEFAULT_SERVER, dpi=200, pages=None, max_tokens=8192, timeout=600,
        image_max_tokens=LLAMA_MAX_IMAGE_TOKENS, qwen_server=QWEN_SERVER, use_qwen=True, log=None):
    """Ejecuta las lecturas A, B y (si hace falta) C. Devuelve un dict con todo lo intermedio
    y el Markdown final, para que otros módulos (fase 2) consuman los datos estructurados."""
    log = log or (lambda m: print(m, file=sys.stderr))
    if not server_ok(server):
        raise RuntimeError(f"llama-server no responde en {server}; iniciálo (o el túnel) antes de correr el script.")

    log("Lectura A: página completa con Text Recognition")
    full, n_pages = read_full_pages(pdf, pages, server, dpi, max_tokens, timeout, log, image_max_tokens)
    log("Lectura B: regiones con glmocr (layout en CPU + Table/Text Recognition)")
    region_pages = read_regions(pdf)

    results, unverified, grand = check_shipments(region_pages)
    if use_qwen and any(not shipment_ok(r) for r in results):
        log("Lectura C: rescate con Qwen2.5-VL de los envíos que no cuadran")
        recover_with_qwen(results, region_pages, pdf, qwen_server, timeout, log)
    ship_md = shipments_markdown(results, unverified, grand)

    out, only_reg, only_full = [], set(), set()
    for i in range(n_pages):
        if i not in full:
            continue
        md, r, f = page_markdown(i + 1, full[i], region_pages[i] if i < len(region_pages) else [])
        out.append(md)
        only_reg |= r
        only_full |= f
    if ship_md:
        out.append(ship_md)
    return {
        "pdf": str(pdf), "n_pages": n_pages, "full_text": full, "region_pages": region_pages,
        "shipments": results, "unverified": unverified, "grand_total": grand,
        "only_in_regions": only_reg, "only_in_full": only_full,
        "markdown": "\n\n---\n\n".join(out) + "\n",
    }


def main():
    ap = argparse.ArgumentParser(description="OCR de PDF con GLM-OCR: página completa + regiones + verificación")
    ap.add_argument("pdf", help="ruta al PDF")
    ap.add_argument("-o", "--output", help="archivo de salida (.md)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"URL de llama-server (default {DEFAULT_SERVER})")
    ap.add_argument("--dpi", type=int, default=200, help="dpi máximo de la página completa (default 200); se baja si la página no cabe en el tope de tokens de imagen")
    ap.add_argument("--image-max-tokens", type=int, default=LLAMA_MAX_IMAGE_TOKENS,
                    help=f"tope de tokens de imagen del llama-server (default {LLAMA_MAX_IMAGE_TOKENS}, el de llama.cpp para GLM-OCR)")
    ap.add_argument("--save-regions", action="store_true", help="guardar también las regiones de glmocr en <salida>.regions.json")
    ap.add_argument("--qwen-server", default=QWEN_SERVER, help=f"URL del llama-server con Qwen2.5-VL para el rescate (default {QWEN_SERVER})")
    ap.add_argument("--no-qwen", action="store_true", help="no usar Qwen2.5-VL aunque la verificación falle")
    ap.add_argument("--pages", help="páginas para la lectura de página completa, ej: 1-3,5 (las regiones siempre se calculan para todo el PDF)")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=600, help="timeout por página en segundos")
    args = ap.parse_args()
    log = lambda m: print(m, file=sys.stderr)

    try:
        res = run(args.pdf, args.server, args.dpi, args.pages, args.max_tokens, args.timeout,
                  args.image_max_tokens, args.qwen_server, not args.no_qwen, log)
    except RuntimeError as e:
        sys.exit(str(e))
    if args.save_regions:
        rpath = Path(args.output or Path(args.pdf).stem + ".md").with_suffix(".regions.json")
        rpath.write_text(json.dumps(res["region_pages"], ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"Regiones guardadas en {rpath}")
    log(f"Resumen: {len(res['only_in_regions'])} importe(s) solo en regiones [{fmt(res['only_in_regions'])}], "
        f"{len(res['only_in_full'])} solo en texto completo [{fmt(res['only_in_full'])}]")
    if res["shipments"]:
        ok = sum(shipment_ok(r) for r in res["shipments"])
        log(f"{ok} de {len(res['shipments'])} envíos cuadran.")
    if args.output:
        Path(args.output).write_text(res["markdown"], encoding="utf-8")
        log(f"Guardado en {args.output}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(res["markdown"])


if __name__ == "__main__":
    main()
