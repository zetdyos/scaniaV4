"""Saltos de página: UN solo mecanismo por geometría (sustituye a join_split_items, fix_page_breaks, fix_block_breaks,
block_break y row_break de facturas_json; plan del 2026-10-23).

El 27B lee cada página sobre el texto de GLM-OCR, que no tiene posiciones. Un artículo partido por el corte (descripción al
pie de la página A, precio arriba de la B) deja el último artículo de A sin importe y, en B, cada descripción emparejada con
el precio que tiene ENCIMA: toda la página se corre un artículo y el último precio queda huérfano. La suma no cambia, así
que ningún cuadre lo ve (31728, 30637, 27692). Lo único que ve el corte es la geometría de PP-OCRv6 (cajas con posición).

Este módulo es puro: recibe las cajas de PP-OCRv6 de cada página (ya pedidas por la fase 2 para las cifras: coste cero) y
devuelve decisiones. No llama a la red, no importa facturas_json (lo importa facturas_json) y no guarda estado.
  1. `rows_from_tokens`: renglones (cajas con el centro vertical a <= 10 px), con sus importes, su «precio solo» (renglón
     que es solo un precio, como cierra cada bloque de Amazon con fotos) y sus columnas.
  2. `furniture`: marco de página (cabeceras/pies repetidos en 2+ páginas): no es texto de ningún artículo.
  3. `block_page` / `table_page`: segmentación de UNA página en artículos por geometría. Dos diseños:
       - BLOQUES (Amazon con fotos, eBay app, Temu, Shein…): ancla = renglón de precio solo; el texto PRECEDE a su precio.
       - TABLAS: ancla = fila con >= 2 importes (precio e importe); el texto va DEBAJO del ancla (21750, descripción bajo el
         código) o ENCIMA (FashionGo 32836, descripción sobre su fila). El estilo se vota por documento (`doc_style`).
  4. `continuation`: ¿el corte entre A y B parte un artículo? Solo geometría, con las cotas medidas desde 2026-10-20 (todas
     conservadas con el documento que las motivó, ver cada función). Devuelve el precio del artículo partido y sus renglones.
  5. (S3/S4) `fix_breaks`: coteja el emparejamiento descripción<->precio del 27B con los artículos geométricos y solo cambia
     el EMPAREJAMIENTO cuando la geometría afirma el corte Y la salida del 27B muestra su firma. El texto es siempre de GLM
     (vía el 27B o glm_fragment), las cifras del 27B/PP-OCRv6, las cantidades no se tocan. Nunca inventa una línea.

PP-OCRv6 solo aporta GEOMETRÍA y cifras (decisión del usuario): su texto se usa únicamente para emparejar, nunca sale."""
import difflib
import html
import re
import statistics

ROW_TOL = 10        # cajas en el mismo renglón: centro vertical a <= 10 px (200 dpi)
COL_TOL = 25        # misma columna de precios: borde izquierdo a <= 25 px
DESC_TOL = 40       # misma columna de descripción (tablas): <= 40 px
AMT_COL_TOL = 60    # un importe en la columna de importes de la tabla: <= 60 px
MAX_TAIL_SHIFTED = 2  # hoja desplazada: la cola de un bloque partido nunca pasa de 2 renglones (30972, 32382, 31794)
MAX_ORPHAN_ROWS = 3   # tabla: renglones huérfanos de descripción arriba de B, como mucho 3
SIM = 0.8             # misma descripción (27B <-> geometría)
GLM_SIM = 0.85        # fragmento de PP-OCRv6 <-> ventana de palabras de GLM
STYLE_MAJORITY = 2 / 3

# Renglón que es solo un precio («$16.98», «US$59.99»): cierra cada bloque. Lo demás del renglón solo pueden ser marcas sin
# letras de la foto («*4», «1», «口»).
BLOCK_PRICE_RE = re.compile(r"^(?:US)?\$\d{1,3}(?:,\d{3})*\.\d{2}$")
# Importe en un renglón de PP-OCRv6: con «$» admite 3-4 decimales ($9.9776); sin «$», 2 decimales y sufijo de moneda
# opcional (46.79 US$, «USS» mal leído). Nunca seguido de otra cifra ni de «.cifra» (teléfonos, códigos HTS).
GEO_MONEY_RE = re.compile(r"(?<![\d.,])(?:\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,4}|(?:\d{1,3}(?:[,.]\d{3})+|\d+)[.,]\d{2}"
                          r"(?:\s?(?:US\$|USS|USD|\$))?)(?!\d|[.,]\d)")
AMOUNT_TXT = re.compile(r"-?\s*\d[\d.,]*(?:\.\d{2}|,\d{2}(?!\d)|,-)")
WORD_RE = re.compile(r"[A-Za-z]{3,}")


def money(txt):
    """Texto con un importe -> '1234.56' (igual que facturas_json.money: '1.234,56' -> 1234.56, '$9.9776' -> 9.97)."""
    m = AMOUNT_TXT.search(str(txt or ""))
    if not m:
        return ""
    t = re.sub(r"\s", "", m.group(0)).lstrip("-")
    if t.endswith(",-"):
        t = t[:-2]
    if "," in t and "." in t:
        dec = t[-3] if t[-3] in ".," else None
        t = t.replace("," if dec == "." else ".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{2}$", t) else t.replace(",", "")
    try:
        return f"{abs(float(t)):.2f}"
    except ValueError:
        return ""


def norm_alnum(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def norm_alpha(t):
    return re.sub(r"[^a-z]", "", (t or "").lower())


def squash(t):
    return re.sub(r"\s+", "", t or "").lower()


# ----------------------------------------------------------------------------- renglones y marco

def rows_from_tokens(tokens, width):
    """Cajas de PP-OCRv6 [(x0, y0, x1, y1, texto)] -> renglones en orden de lectura:
    {c, toks [(x0, x1, texto)], text, amounts [...], money, lone_price, x, x_text, price_x, alpha_x, span, na, nl}."""
    rows = []
    for y0, x0, x1, y1, t in sorted((y0, x0, x1, y1, t) for x0, y0, x1, y1, t in tokens):
        c = (y0 + y1) / 2
        if rows and abs(rows[-1]["c"] - c) <= ROW_TOL:
            rows[-1]["toks"].append((x0, x1, t))
            rows[-1]["c"] = (rows[-1]["c"] + c) / 2
        else:
            rows.append({"c": c, "toks": [(x0, x1, t)]})
    for r in rows:
        r["toks"].sort()
        r["text"] = " ".join(t for _, _, t in r["toks"])
        r["amounts"] = [money(m) for _, _, t in r["toks"] for m in GEO_MONEY_RE.findall(t)]
        r["money"] = len(r["amounts"])
        prices = [(x0, t) for x0, _, t in r["toks"] if BLOCK_PRICE_RE.match(t)]
        rest = [t for _, _, t in r["toks"] if not BLOCK_PRICE_RE.match(t)]
        r["lone_price"] = money(prices[0][1]) if len(prices) == 1 and all(
            len(t) <= 3 and not re.search(r"[A-Za-z]", t) for t in rest) else ""
        r["alpha_x"] = [x0 for x0, _, t in r["toks"] if WORD_RE.search(t)]
        r["x_text"] = r["alpha_x"][0] if r["alpha_x"] else r["toks"][0][0]
        r["x"] = prices[0][0] if r["lone_price"] else r["x_text"]   # borde izquierdo para la columna de bloques
        money_x = [x0 for x0, _, t in r["toks"] if GEO_MONEY_RE.search(t)]
        r["price_x"] = money_x[-1] if money_x else None               # columna de importes de una tabla (el último)
        r["span"] = (r["toks"][-1][1] - r["toks"][0][0]) / width if width else 0.0
        r["na"] = norm_alnum(r["text"])
        r["nl"] = norm_alpha(r["text"])
    return rows


def furniture(pages_rows):
    """Marco de página, dos lecturas (las de hoy): bloques = solo letras de los 3 primeros y 3 últimos renglones de cada
    página que se repiten en 2+ páginas («Order Details», URL, «Page 1 of 3» -> «pageof»); tablas = letras y cifras de los
    renglones tras la última fila de importes que se repiten en 2+ páginas («**** CONTINUED ****»)."""
    alpha, alnum = {}, {}
    for p, rows in pages_rows.items():
        for r in rows[:3] + rows[-3:]:
            if r["nl"]:
                alpha.setdefault(r["nl"], set()).add(p)
        anchors = [i for i, r in enumerate(rows) if r["money"] >= 2]
        for r in rows[anchors[-1] + 1:] if anchors else []:
            if r["na"]:
                alnum.setdefault(r["na"], set()).add(p)
    return {"alpha": {t for t, ps in alpha.items() if len(ps) >= 2}, "alnum": {t for t, ps in alnum.items() if len(ps) >= 2}}


# ----------------------------------------------------------------------------- bloques (Amazon con fotos y parecidos)

def block_page(rows, furn):
    """Segmentación de una página en el diseño de bloques. Columna = mediana del borde de SUS precios (una hoja escaneada
    desplazada corre toda la columna: 27692, x 341 en A y 311 en B). Devuelve None si no hay precios.
    {col, prices [idx], rows (sin marco; los renglones de precio se quedan), open [renglones de la columna tras el último
    precio: bloque abierto al pie], head [renglones de la columna antes del primer precio], head_other [renglones con
    letras o «$» fuera de la columna antes del primer precio], head_amount (algún «$cifra» en head), sizes [tamaño de
    cada bloque completo, >= 2 renglones], items [{rows, price, anchor}]}."""
    a = [r for r in rows if r["lone_price"] or r["nl"] not in furn["alpha"]]
    pa = [i for i, r in enumerate(a) if r["lone_price"]]
    if not pa:
        return None
    col = statistics.median(a[i]["x"] for i in pa)
    in_col = lambda r: abs(r["x"] - col) <= COL_TOL
    before = a[:pa[0]]
    head = [r for r in before if in_col(r)]
    head_other = [r for r in before if not in_col(r) and re.search(r"[A-Za-z]{2,}|\$\d", r["text"])]
    head_amount = any(re.search(r"\$\s?\d", r["text"]) for r in head)
    open_rows = [r for r in a[pa[-1] + 1:] if in_col(r)]
    sizes, items, cur, started = [], [], [], False
    for r in a:
        if not in_col(r):
            continue
        cur.append(r)
        if r["lone_price"]:
            if started and len(cur) >= 2:  # 1 renglón = una cifra del resumen, no un artículo
                sizes.append(len(cur))
            if started:
                items.append({"rows": cur[:-1], "price": r["lone_price"], "anchor": r, "page_rows": cur})
            started, cur = True, []
    return {"col": col, "prices": pa, "rows": a, "open": open_rows, "head": head, "head_other": head_other,
            "head_amount": head_amount, "sizes": sizes, "items": items, "first_price": a[pa[0]],
            "title_after_first": next((r for r in a[pa[0] + 1:] if in_col(r) and not r["lone_price"]), None)}


def block_continuation(pa, pb):
    """Bloque partido entre A y B (block_break de hoy, guardas intactas): {price, a_rows, b_rows, title_b} o (None, motivo).
    - los precios de cada página en SU columna; un solo precio en B con columna desplazada > 25 px -> sin columna fiable (27692);
    - A tiene >= 2 precios y un bloque abierto (renglones de la columna tras su último precio);
    - antes del primer precio de B solo renglones de esa columna y sin importes: un renglón con nombre e importe es otro
      artículo (IKEA 31183);
    - columna desplazada > 25 px -> la cola de B tiene <= 2 renglones: un bloque completo en B no es continuación
      (32205 p9 AVEDA, 28858 p4 Dunzoom; colas reales <= 2: 30972, 32382, 31794);
    - abierto + cola + 1 <= bloque mayor de A y B + 1 (si no, en B empieza otro artículo)."""
    if pa is None or pb is None:
        return None, "sin precios en una de las páginas"
    if len(pa["prices"]) < 2:
        return None, "menos de 2 precios en A"
    offset = abs(pb["col"] - pa["col"]) > COL_TOL
    if len(pb["prices"]) < 2 and offset:
        return None, "un solo precio en B con la columna desplazada"
    if not pa["open"]:
        return None, "sin bloque abierto al pie de A"
    if pb["head_other"]:
        return None, "texto de otra columna antes del primer precio de B"
    if pb["head_amount"]:
        return None, "importe en la cola de B (otro artículo)"
    if offset and len(pb["head"]) > MAX_TAIL_SHIFTED:
        return None, "columna desplazada y cola de más de 2 renglones (bloque completo en B)"
    sizes = pa["sizes"] + pb["sizes"]
    if not sizes:
        return None, "sin bloques completos de referencia"
    if len(pa["open"]) + len(pb["head"]) + 1 > max(sizes) + 1:
        return None, "abierto + cola más largo que el bloque mayor"
    return {"kind": "block", "price": pb["first_price"]["lone_price"], "a_rows": pa["open"], "b_rows": pb["head"],
            "b_anchor": pb["first_price"], "title_b": pb["title_after_first"]["text"] if pb["title_after_first"] else ""}, "continuación"


# ----------------------------------------------------------------------------- tablas (fila con >= 2 importes)

def table_page(rows, furn):
    """Segmentación de una página en el diseño de tablas. Ancla = fila con >= 2 importes. Devuelve None si no hay anclas.
    {rows (sin marco), anchors [idx], amt_x (columna de importes: mediana del último importe de cada ancla), between
    [renglones de texto entre anclas], col_text (mediana del borde de las descripciones entre anclas, o None), tail
    [renglones tras la última ancla], head [renglones antes de la primera], heads [idx de cabeceras de tabla antes de la
    primera ancla: anchas, sin cifras], closed_single (algún renglón de UN importe tras la última ancla: subtotal, total),
    closed_amt (algún importe en la columna de importes tras la última ancla), votes (antes, después): renglones de texto
    entre dos anclas más cerca de la SIGUIENTE (encabezan su fila: texto ANTES del ancla) o de la ANTERIOR (DESPUÉS)}."""
    a = [r for r in rows if r["na"] not in furn["alnum"]]
    anchors = [i for i, r in enumerate(a) if r["money"] >= 2]
    if not anchors:
        return None
    amt_x = statistics.median(a[i]["price_x"] for i in anchors)
    between = [r for s, e in zip(anchors, anchors[1:]) for r in a[s + 1:e] if r["alpha_x"] and not r["money"]]
    col_text = statistics.median(r["alpha_x"][0] for r in between) if between else None
    gap = lambda r1, r2: abs(r2["c"] - r1["c"])
    ups = [gap(r, a[e]) < gap(a[s], r) for s, e in zip(anchors, anchors[1:]) for r in a[s + 1:e] if r["alpha_x"] and not r["money"]]
    tail = a[anchors[-1] + 1:]
    heads = [i for i in range(anchors[0]) if a[i]["span"] > 0.45 and a[i]["money"] == 0 and not re.search(r"\d", a[i]["text"])]
    return {"rows": a, "anchors": anchors, "amt_x": amt_x, "between": between, "col_text": col_text, "tail": tail,
            "head": a[:anchors[0]], "heads": heads,
            "closed_single": any(r["money"] == 1 for r in tail),
            "closed_amt": any(abs(x0 - amt_x) <= AMT_COL_TOL for r in tail for x0, _, t in r["toks"] if GEO_MONEY_RE.search(t)),
            "votes": (sum(ups), len(ups) - sum(ups))}


def in_text_col(r, col):
    """Renglón en la columna de descripción de la tabla (regla de row_break: si su primera palabra queda muy a la
    izquierda, un código, se mira la última)."""
    if col is None or not r["alpha_x"]:
        return False
    return abs(r["alpha_x"][-1 if r["alpha_x"][0] < col - DESC_TOL else 0] - col) <= DESC_TOL


def table_before_continuation(pa, pb):
    """Descripción ENCIMA de su fila de importes (FashionGo 32836; row_break de hoy): A termina con texto en la columna de
    descripción tras su última fila (descripción abierta) sin cerrar la tabla (ningún importe en la columna de importes
    después), y en B la primera fila llega antes que cualquier texto de esa columna. Esa primera fila de B es del artículo
    abierto de A: su importe, cantidad y precio unitario."""
    if pa is None or pb is None:
        return None, "sin filas en una de las páginas"
    if len(pa["anchors"]) < 2:
        return None, "menos de 2 filas en A"
    if not pa["between"]:
        return None, "sin descripciones entre filas"
    if pa["closed_amt"]:
        return None, "la tabla cerró en A"
    open_rows = [r for r in pa["tail"] if in_text_col(r, pa["col_text"])]
    if not open_rows:
        return None, "sin descripción abierta en A"
    if any(in_text_col(r, pa["col_text"]) for r in pb["head"] if not r["money"]):
        return None, "texto antes de la primera fila de B"
    up, down = pa["votes"]
    if up <= down:
        return None, "las descripciones no encabezan su fila"
    b_anchor = pb["rows"][pb["anchors"][0]]
    return {"kind": "row", "price": b_anchor["amounts"][-1], "a_rows": open_rows, "b_rows": [], "b_anchor": b_anchor,
            "title_b": ""}, "continuación"


def table_after_continuation(pa, pb, doc_style):
    """Descripción DEBAJO de su fila (21750: «0003 B309 $59.87» y «HIGH PERFORM. GLASS F-FLU» ya en la página siguiente;
    page_break_continuation de hoy): la tabla no cerró en A (ningún renglón de un solo importe tras la última fila); B
    tiene cabecera de tabla; los huérfanos son los renglones con texto entre la última cabecera de B y su primera fila,
    sin importe, en la columna de descripción de A, no repetidos de la cabecera de A («ORDERED SHIPPED»), como mucho 3 y
    TODOS los de esa franja. Voto de forma (hoy): si en A los renglones de la misma forma encabezan la fila siguiente, el
    huérfano es del primer artículo de B. Además el estilo del documento (mayoría) no puede ser «antes» (32560, cabeceras
    de grupo). El artículo partido es la ÚLTIMA fila de A: el precio es el suyo (a_anchor)."""
    if pa is None or pb is None:
        return None, "sin artículos en una de las páginas"
    if pa["closed_single"]:
        return None, "la tabla cerró en la página anterior"
    if not pb["heads"]:
        return None, "sin cabecera de tabla en la página siguiente"
    a, b = pa["rows"], pb["rows"]
    last_a = pa["anchors"][-1]
    cand = b[pb["heads"][-1] + 1:pb["anchors"][0]]
    desc_x = [x for r in a[max(0, last_a - 6):last_a + 3] for x in r["alpha_x"]]
    if not desc_x:
        return None, "sin columna de descripción"
    col = (min(desc_x) - 60, max(desc_x) + 40)
    head_a = {norm_alnum(r["text"]) for r in a[:pa["anchors"][0]]} | {norm_alnum(t) for r in a[:pa["anchors"][0]] for _, _, t in r["toks"]}
    orph = [r for r in cand if r["money"] == 0 and r["alpha_x"] and col[0] <= min(r["alpha_x"]) <= col[1]
            and norm_alnum(r["text"]) not in head_a and not all(norm_alnum(t) in head_a for _, _, t in r["toks"])]
    if not orph or len(orph) > MAX_ORPHAN_ROWS or len(orph) != len([r for r in cand if r["alpha_x"]]):
        return None, "sin continuación"
    gap = lambda r1, r2: abs(r2["c"] - r1["c"])
    anchors = pa["anchors"]
    between = [(r, gap(r, a[e]) < gap(a[s], r)) for s, e in zip(anchors, anchors[1:]) for r in a[s + 1:e] if r["alpha_x"] and r["money"] == 0]
    same_shape = lambda r, o: abs(r["toks"][0][0] - o["toks"][0][0]) <= 20 and len(r["toks"]) == len(o["toks"]) and \
        all(any(abs(x - y) <= 25 for y, _, _ in r["toks"]) for x, _, _ in o["toks"])
    for o in orph:
        pool = [up for r, up in between if same_shape(r, o)] or [up for r, up in between if col[0] <= min(r["alpha_x"]) <= col[1]]
        if pool and sum(pool) > len(pool) - sum(pool):
            return None, "el renglón encabeza el primer artículo de su página"
    # Tabla agrupada (32560, Rip Curl): cada grupo empieza por un renglón con su código de artículo («12TWSW PARAISO
    # SLIDING TRI 2XS XS M») encima de sus filas. Un renglón que empieza por un código es la CABEZA de un grupo, nunca la
    # cola de la descripción anterior: si A ya tiene 2+ renglones así entre sus filas y en la franja huérfana hay alguno,
    # ahí empieza un grupo nuevo y no hay continuación (32560 p1->2 «12TWSW…», p3->4 «1AYWRV COOLING ZT XS S»).
    code_like = lambda r: bool(re.fullmatch(r"(?=.*\d)(?=.*[A-Z])[A-Z0-9-]{5,}", (r["text"].split() or [""])[0]))
    if sum(code_like(r) for r, _ in between) >= 2 and any(code_like(o) for o in orph):
        return None, "la franja huérfana contiene un renglón que empieza por un código de artículo (cabecera de grupo)"
    if doc_style == "before":
        return None, "en este documento las descripciones encabezan su fila (estilo del documento)"
    a_anchor = a[last_a]
    return {"kind": "table", "price": a_anchor["amounts"][-1], "a_rows": [a_anchor], "b_rows": orph, "b_anchor": None,
            "a_anchor": a_anchor, "col": col, "title_b": ""}, "continuación"


# ----------------------------------------------------------------------------- documento

def segment(pages_tokens):
    """{página: (tokens, ancho)} -> {página: {block, table, rows}} con el marco del documento ya aplicado."""
    rows = {p: rows_from_tokens(toks, w) for p, (toks, w) in pages_tokens.items()}
    furn = furniture(rows)
    return {p: {"rows": r, "block": block_page(r, furn), "table": table_page(r, furn)} for p, r in rows.items()}, furn


def doc_style(seg):
    """Estilo de las tablas del documento: 'before' (las descripciones encabezan su fila), 'after' (van debajo) o None
    (sin mayoría de 2/3 o sin votos)."""
    up = sum(s["table"]["votes"][0] for s in seg.values() if s["table"])
    down = sum(s["table"]["votes"][1] for s in seg.values() if s["table"])
    if up + down == 0:
        return None
    if up >= STYLE_MAJORITY * (up + down):
        return "before"
    if down >= STYLE_MAJORITY * (up + down):
        return "after"
    return None


def continuation(seg, page_a, page_b, style):
    """Corte entre dos páginas contiguas: (join|None, motivo). Primero bloques (precio solo), luego tablas según el estilo
    del documento (debajo -> 21750; encima -> FashionGo). Devuelve también los motivos de los diseños que no aplican."""
    sa, sb = seg.get(page_a), seg.get(page_b)
    if not sa or not sb:
        return None, "sin geometría"
    why = {}
    j, why["block"] = block_continuation(sa["block"], sb["block"])
    if j:
        return j, "continuación (bloques)"
    if style != "before":
        j, why["table"] = table_after_continuation(sa["table"], sb["table"], style)
        if j:
            return j, "continuación (tabla, descripción debajo)"
    if style != "after":
        j, why["row"] = table_before_continuation(sa["table"], sb["table"])
        if j:
            return j, "continuación (tabla, descripción encima)"
    return None, "; ".join(f"{k}: {v}" for k, v in why.items())


# ----------------------------------------------------------------------------- texto de GLM

def glm_fragment(frag, words):
    """Texto de GLM-OCR que corresponde a un fragmento leído por PP-OCRv6 en la misma página: lo que se coloca es SIEMPRE
    texto de GLM. Mismo texto sin espacios -> tal cual; si no, la ventana de palabras de GLM (mismo número ±1) más parecida
    con similitud >= 0.85 («Compresslon» / «Compression»). Sin coincidencia clara -> None."""
    sq = squash(frag)
    if not sq:
        return None
    n = len(frag.split())
    best, best_r = None, 0.0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(len(words) - size + 1):
            cand = " ".join(words[i:i + size])
            if squash(cand) == sq:
                return cand
            r = difflib.SequenceMatcher(None, squash(cand), sq).ratio()
            if r > best_r:
                best, best_r = cand, r
    return best if best_r >= GLM_SIM else None


# ----------------------------------------------------------------------------- líneas del 27B <-> artículos geométricos

ID_LIKE_RE = re.compile(r"^[A-Z]{0,4}[\d-]{9,}$")
COUNT_ONLY_RE = re.compile(r"^\s*\d+\s+(?:\w+|\w+\s+(?:items?|objects?))\s*$", re.I)


def desc_key(text):
    return re.sub(r"[^a-z0-9]", "", html.unescape(text or "").lower())


def no_desc(d):
    """Descripción vacía, identificador («TBA335177403481») o recuento («1 item»)."""
    return not (d or "").strip() or bool(ID_LIKE_RE.match(re.sub(r"\s", "", d or ""))) or bool(COUNT_ONLY_RE.match(d or ""))


def same_text(desc, text):
    """La descripción del 27B es el artículo geométrico (texto de PP-OCRv6, solo para emparejar): 30 primeros caracteres
    alfanuméricos con similitud >= 0.8 (el criterio medido del código anterior). Sin exigir el mismo arranque: PP-OCRv6 y
    GLM confunden I/1, O/0 («Bifenthrin I/T» y «1/T», 31173) y el artículo abierto pasaba por «no devuelto»."""
    a, b = desc_key(desc)[:30], desc_key(text)[:30]
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIM


def item_text(item):
    return " ".join(r["text"] for r in item["rows"])


def line_is_item(desc, item):
    """La descripción del 27B es la de este artículo geométrico: casa con el texto del bloque o con ALGUNO de sus
    renglones (el título no siempre es el primer renglón: en una variante de Amazon la cola del artículo anterior,
    «Fit Type/Color/Size: …», se imprime DESPUÉS de su precio y encabeza el bloque siguiente; 32177 p4)."""
    return same_text(desc, item_text(item)) or any(same_text(desc, r["text"]) for r in item["rows"])


def match_lines(lines, items, exact_first=False):
    """Empareja líneas del 27B con artículos geométricos en orden de lectura: {id(línea): índice de artículo}. Cada
    artículo se usa una vez; empate (varios artículos parecidos) -> el primero libre en orden. Con `exact_first`, antes
    las parejas con el MISMO importe: una descripción que el 27B repitió no le quita el artículo a la línea que lleva su
    precio (31420, «Vagabond…» con 17.99 y con 159.00). Solo para la firma CONSISTENTE: en un corrimiento la copia que
    lleva el último precio se quedaría con el artículo y la firma se perdería (30637)."""
    used, out = set(), {}
    for exact in ((True, False) if exact_first else (False,)):
        for l in lines:
            if id(l) in out:
                continue
            for j, it in enumerate(items):
                if j not in used and line_is_item(l.get("desc"), it) and (not exact or l.get("amount") == it["price"]):
                    used.add(j)
                    out[id(l)] = j
                    break
    return out


def qty_ok(qty, unit, amount):
    try:
        return abs(float(qty) * float(unit) - float(amount)) < 0.006
    except (TypeError, ValueError):
        return False


def retarget(line, new):
    """La línea toma el importe impreso de SU artículo. Cantidad intacta (la decide el 27B). Precio unitario: vacío sigue
    vacío; si era el importe viejo con cantidad 1, pasa al nuevo; si sigue cuadrando con la cantidad, se queda; si no, se
    vacía (no se deja un precio viejo que luego rellene la línea como «calculado»)."""
    old = line.get("amount") or ""
    line["amount"], line["amount_exact"] = new, None
    unit = line.get("unit") or ""
    if not unit:
        if line.get("qty", 1) == 1:  # una unidad: el precio impreso es el unitario (como hoy)
            line["unit"], line["unit_exact"] = new, None
        return
    if unit == old and line.get("qty", 1) == 1:
        line["unit"], line["unit_exact"] = new, None
    elif not qty_ok(line.get("qty", 1), unit, new):
        line["unit"], line["unit_exact"] = "", None


def amount_in_text(v, text):
    """El importe '1234.56' aparece impreso en el texto (1234.56, 1,234.56, 1234,56, 1.234,56), no pegado a otra cifra."""
    val = float(v)
    us = f"{val:,.2f}"
    forms = {f"{val:.2f}", us, f"{val:.2f}".replace(".", ","), us.replace(",", "X").replace(".", ",").replace("X", ".")}
    return any(re.search(r"(?<![\d.,])" + re.escape(f) + r"(?!\d)", text or "") for f in forms)


def join_text_rule(lines, k, log):
    """R0 (join_split_items de hoy, sin geometría): la última línea de artículo de la página k (relativa) tiene descripción
    y no importe, y la primera de la k+1 tiene importe y descripción vacía, identificador o la MISMA (28858): se juntan
    (descripción y cantidad de la primera, importe de la segunda) y se quita la segunda. Ninguna cifra cambia."""
    goods = [l for l in lines if l.get("kind", "goods") == "goods"]
    on_a = [l for l in goods if l.get("page_rel") == k + 1]
    on_b = [l for l in goods if l.get("page_rel") == k + 2]
    if not on_a or not on_b:
        return
    a, b = on_a[-1], on_b[0]
    same = desc_key(a.get("desc")) and desc_key(a.get("desc")) == desc_key(b.get("desc"))
    if a.get("amount") or no_desc(a.get("desc")) or not b.get("amount") or not (no_desc(b.get("desc")) or same):
        return
    a.update(amount=b["amount"], amount_exact=b.get("amount_exact"))
    if not a.get("unit"):
        a.update(unit=b.get("unit"), unit_exact=b.get("unit_exact"))
    lines.remove(b)
    log(f"  artículo partido por el salto de página {k + 1}->{k + 2}: «{a['desc'][:40]}» + importe {a['amount']} de la página siguiente")


def glm_quote(rows, glm_text, n=40):
    """Texto de GLM para citar unos renglones en una incidencia: el primer renglón con correspondencia en GLM
    (glm_fragment). El texto de PP-OCRv6 nunca sale a WTCP (32420: «口 FRAMaR … Rruch»). Sin correspondencia -> ""."""
    words = (glm_text or "").split()
    return next((g[:n] for r in rows for g in [glm_fragment(r["text"], words)] if g), "")


def _not_returned(a_rows, p0, pages, k, glm_pages):
    """Incidencia del artículo abierto que el 27B no devolvió (cita de GLM; sin correspondencia, sin cita)."""
    quote = glm_quote(a_rows, glm_pages.get(pages[k], ""))
    what = f"(«{quote}», price {p0}" if quote else f"(price {p0}"
    return (f"item printed at the foot of page {pages[k] + 1} {what} at the top of page {pages[k] + 2}) was not returned "
            "by the model; review the items around the page break")


def reconcile_block(lines, k, pages, seg, join, glm_pages, log):
    """Bloque partido (el precio P0 arriba de B es del bloque abierto al pie de A). Coteja las líneas del 27B de A y B con
    los artículos geométricos de B y actúa solo con la firma completa (ver docstring del módulo). Devuelve incidencias."""
    where = f"page {pages[k] + 1}->{pages[k] + 2}"
    p0 = join["price"]
    goods = [l for l in lines if l.get("kind", "goods") == "goods"]
    la = [l for l in goods if l.get("page_rel") == k + 1]
    lb = [l for l in goods if l.get("page_rel") == k + 2]
    others_b = [l for l in lines if l.get("kind", "goods") != "goods" and l.get("page_rel") == k + 2]
    # La cifra huérfana puede venir en una línea sin descripción o pegada a un renglón de la COLA del bloque partido
    # («Return or replace items: Eligible…» como other_charges 139.99, 30972): no es un cargo ni un artículo.
    tail_like = lambda l: no_desc(l.get("desc")) or any(same_text(l.get("desc"), r["text"]) for r in join["b_rows"])
    ob = [l for l in others_b if tail_like(l)]
    open_text = " ".join(r["text"] for r in join["a_rows"])
    if not la:  # el 27B no devolvió líneas en A (página no leída): nada que reconciliar, como hoy
        return []
    log(f"  salto {where}: bloque partido; {p0} (arriba de la página siguiente) es de «{open_text[:45]}» (geometría PP-OCRv6)")
    a_line = la[-1] if same_text(la[-1].get("desc"), open_text) else None
    if a_line is not None and a_line.get("amount"):
        if a_line["amount"] == p0:
            return []  # el 27B ya lo emparejó bien
        # ¿El bloque no estaba abierto (PP-OCRv6 leyó mal su precio al pie de A, 28858 p4)? Entonces GLM leyó ese importe
        # DESPUÉS del título del bloque. Una cifra igual impresa en otro artículo de A no cuenta (32177: 17.49 tres veces).
        glm_a = glm_pages.get(pages[k], "")
        title = glm_quote(join["a_rows"], glm_a, 60)
        after = glm_a[glm_a.index(title):] if title and title in glm_a else (glm_a if not title else "")
        if amount_in_text(a_line["amount"], after):
            log(f"    «{a_line['desc'][:40]}» ya tiene su importe {a_line['amount']} impreso tras su título en la página {pages[k] + 1}: el bloque no estaba abierto")
            return []
        return [f"item split by the page break ({where}): the price {p0} at the top of the next page belongs to "
                f"«{a_line['desc'][:40]}», which the model gave {a_line['amount']}; review the items around the page break"]
    items = seg[pages[k + 1]]["block"]["items"]
    # un artículo geométrico que el 27B devolvió como línea NO-goods con concepto (una cifra del resumen en su renglón) no cuenta
    taken = set(match_lines([l for l in others_b if not no_desc(l.get("desc"))], items).values())
    items = [it for j, it in enumerate(items) if j not in taken]
    prices = [p0] + [it["price"] for it in items]

    def signatures(m):
        matched = [l for l in lb if id(l) in m]
        consistent = all(l.get("amount") == items[m[id(l)]]["price"] for l in matched)
        shifted = (items and len(matched) == len(items) and all(l.get("amount") == prices[m[id(l)]] for l in matched))
        return matched, consistent, shifted
    m = match_lines(lb, items)  # por orden de lectura: así se ve la firma del corrimiento
    matched, consistent, shifted = signatures(m)
    if not consistent and not shifted:
        # Una descripción repetida por el 27B se queda con el artículo de la línea que lleva su precio (31420): se
        # reintenta con las parejas de mismo importe primero, y solo vale si así la página es CONSISTENTE.
        m2 = match_lines(lb, items, exact_first=True)
        matched2, consistent2, _ = signatures(m2)
        if consistent2:
            m, matched, consistent = m2, matched2, True
    glm_b = squash(glm_pages.get(pages[k + 1], ""))

    def orphan(l):
        """Línea sobrante que carga con una cifra del corte: sin descripción o cola del bloque (tail_like), o una descripción
        que el 27B repitió más veces de las que GLM la imprimió (30637; 31420: «Vagabond…» con el 17.99 del DinoFire)."""
        if tail_like(l):
            return True
        key = squash(l.get("desc"))[:30]
        return bool(key) and glm_b.count(key) < sum(1 for x in lb if squash(x.get("desc"))[:30] == key)
    issues = []
    if consistent and not shifted:
        for l in [x for x in lb + ob if id(x) not in m and orphan(x) and x.get("amount") == p0]:
            lines.remove(l)
            log(f"    «{(l.get('desc') or '')[:30]}» ({l.get('kind', 'goods')}, {p0}) era la cifra del artículo partido, no una línea")
        if a_line is None:
            issues.append(_not_returned(join["a_rows"], p0, pages, k, glm_pages))
            log(f"    el 27B no devolvió el artículo abierto «{open_text[:40]}»")
        else:
            retarget(a_line, p0)
            log(f"    importe {p0} (impreso arriba de la página siguiente) para «{a_line['desc'][:40]}»")
        return issues
    if shifted:
        last_price = prices[-1]
        rest = [l for l in lb + ob if id(l) not in m]
        # El bloque abierto al pie de B (su precio está en la página siguiente): en un corrimiento su línea carga con el
        # último precio de B (32177 p3: «100th Day… Truck» con el 17.49 del Root Beer). No es una línea sobrante: se queda
        # sin importe y el corte siguiente le dará el suyo.
        open_b = seg[pages[k + 1]]["block"]["open"]
        open_text_b = " ".join(r["text"] for r in open_b)
        next_open = [l for l in rest if open_b and l.get("kind", "goods") == "goods" and same_text(l.get("desc"), open_text_b)]
        rest = [l for l in rest if l not in next_open[:1]]
        if (all(l.get("amount") == last_price and orphan(l) for l in rest)
                and all(l.get("amount") == last_price for l in next_open[:1])):
            for l in matched:
                new = items[m[id(l)]]["price"]
                if l.get("amount") != new:
                    log(f"    importe realineado: «{l['desc'][:40]}» {l.get('amount')} -> {new}")
                    retarget(l, new)
            for l in next_open[:1]:
                l["amount"], l["amount_exact"] = "", None
                if l.get("unit") == last_price:
                    l["unit"], l["unit_exact"] = "", None
                log(f"    «{l['desc'][:40]}» es el bloque abierto al pie de la página {pages[k] + 2}: su precio está en la siguiente")
            for l in rest:
                lines.remove(l)
                log(f"    «{(l.get('desc') or '')[:30]}» ({l.get('kind', 'goods')}, {last_price}) era la cifra sobrante del corrimiento, no una línea")
            if a_line is None:
                issues.append(_not_returned(join["a_rows"], p0, pages, k, glm_pages))
            else:
                retarget(a_line, p0)
                log(f"    importe {p0} (impreso arriba de la página siguiente) para «{a_line['desc'][:40]}»")
            return issues
    quote = glm_quote(join["a_rows"], glm_pages.get(pages[k], ""))
    issues.append(f"items around the page break ({where}) could not be paired with the printed prices"
                  + (f": «{quote}» / price {p0}" if quote else f" (price {p0} at the top of page {pages[k] + 2})") + "; review them")
    log(f"    sin firma clara del corte (líneas de B {len(lb)}, artículos {len(items)}, emparejadas {len(matched)}): sin cambios")
    return issues


def reconcile_row(lines, k, pages, seg, join, log):
    """Tabla con la descripción ENCIMA de su fila (FashionGo 32836): la primera fila de B es del artículo abierto de A. Si el
    27B corrió las filas de B (cada descripción con la fila de encima), cada línea toma la fila siguiente (importe,
    cantidad y precio unitario de la fila: cifras del propio 27B, solo cambia el emparejamiento), se quita el texto de la
    fila que pegó al principio de la descripción y la última «línea» que era solo la fila. Si no, nada o incidencia."""
    where = f"page {pages[k] + 1}->{pages[k] + 2}"
    p0 = join["price"]
    goods = [l for l in lines if l.get("kind", "goods") == "goods"]
    la = [l for l in goods if l.get("page_rel") == k + 1]
    lb = [l for l in goods if l.get("page_rel") == k + 2]
    open_text = " ".join(r["text"] for r in join["a_rows"])
    log(f"  salto {where}: fila partida; {p0} (primera fila de la página siguiente) es de «{open_text[:45]}» (geometría PP-OCRv6)")
    if not la:
        return []
    cut = la[-1]
    if cut.get("amount") == p0:
        return []
    carrier = next((l for l in lb if l.get("amount") == p0), None)
    if cut.get("amount") or carrier is None or carrier is not lb[0]:
        return [f"item split by the page break ({where}): the price {p0} at the top of the next page belongs to "
                f"«{cut['desc'][:40]}»; review the items around the page break"]
    if squash(carrier["desc"])[:25] == squash(cut["desc"])[:25]:
        lines.remove(carrier)
        cut["amount"], cut["amount_exact"] = p0, None
        log(f"    «{carrier['desc'][:40]}» era la cola del artículo partido, no un artículo")
        return []
    if len(lb) < 2:
        return [f"item split by the page break ({where}): prices of the next page shifted by one item; review them"]
    keys = ("amount", "amount_exact", "qty", "unit", "unit_exact", "qty_printed")
    vals = [{key: l.get(key) for key in keys} for l in lb]
    for l, v in zip(lb, vals[1:]):
        log(f"    importe realineado: «{l['desc'][:40]}» {l.get('amount')} -> {v['amount']}")
        mm = re.search(r"\$?\s?" + re.escape(l.get("amount") or "x") + r"(?![\d])", l["desc"])
        if mm:
            l["desc"] = l["desc"][mm.end():].strip() or l["desc"]
        l.update(v)
    cut.update({key: vals[0][key] for key in ("qty", "unit", "unit_exact", "qty_printed")})
    cut["_requalified"] = True  # su cantidad y precio vienen ahora de su fila: la incidencia «quantity … not returned» caduca
    last = lb[-1]
    if vals[-1]["amount"] and amount_in_text(vals[-1]["amount"], last["desc"]):
        lines.remove(last)
        log(f"    «{last['desc'][:40]}» era la fila de importes, no un artículo: quitada")
    else:
        # su fila no la devolvió el 27B. Sin el precio unitario viejo (era el de la fila con la que estaba mal emparejada;
        # con él, cantidad x precio rellenaba un importe falso «calculado»: 32836, 13.20 por 33.00). Si B tiene exactamente
        # una fila por línea (la primera es la del artículo partido), la última fila es la suya: su importe y el precio
        # unitario que cuadra con la cantidad del 27B son cifras impresas (PP-OCRv6), como el P0 de la primera fila.
        last["amount"], last["amount_exact"] = "", None
        last["unit"], last["unit_exact"] = "", None
        tb = seg[pages[k + 1]]["table"]
        if tb and len(tb["anchors"]) == len(lb) + 1:
            row = tb["rows"][tb["anchors"][-1]]
            last["amount"] = row["amounts"][-1]
            last["unit"] = next((a for a in row["amounts"][:-1] if qty_ok(last.get("qty", 1), a, last["amount"])), "")
            log(f"    «{last['desc'][:40]}» toma su fila (última de la página): importe {last['amount']}, unitario {last['unit'] or '-'}")
        else:
            log(f"    «{last['desc'][:40]}» queda sin importe ni precio unitario (el 27B no devolvió su fila)")
    cut["amount"], cut["amount_exact"] = p0, None
    return []


def _col_toks(rows, col):
    """Fragmentos (cajas) que tocan la columna de descripción, sin importes."""
    return [t for r in rows for x0, x1, t in r["toks"] if x1 > col[0] and x0 < col[1] and not GEO_MONEY_RE.search(t)]


def text_column(rows, fallback):
    """Columna de descripción para COMPONER el texto de una tabla: el borde izquierdo más frecuente de los renglones de solo
    texto entre artículos y, como borde derecho, el final de sus cajas con palabras."""
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
    right = [x1 for r in text_rows if g[0] <= r["toks"][0][0] <= g[-1] for x0, x1, t in r["toks"] if WORD_RE.search(t)]
    return (g[0], max(right)) if right else fallback


def reconcile_table(lines, k, pages, seg, join, glm_pages, furn, log):
    """Tabla con la descripción DEBAJO del código (21750): los renglones huérfanos arriba de B continúan el último artículo
    de A. Solo si las descripciones de B quedaron CORRIDAS (la primera línea de B lleva el texto huérfano) se rehacen por
    geometría, emparejando por importe en orden; el texto colocado es SIEMPRE de GLM (glm_fragment) con desc_parts para la
    evidencia. Cantidades e importes no cambian. Algo sin correspondencia -> incidencia, sin cambios."""
    where = f"page {pages[k] + 1}->{pages[k] + 2}"
    goods = [l for l in lines if l.get("kind", "goods") == "goods"]
    on_a = [l for l in goods if l.get("page_rel") == k + 1]
    on_b = [l for l in goods if l.get("page_rel") == k + 2]
    if not on_a or not on_b:
        return []
    rows_a, rows_b = seg[pages[k]]["table"]["rows"], seg[pages[k + 1]]["table"]["rows"]
    col = text_column(rows_a, join["col"])
    parts_of = {}

    def glm(toks, page):
        words = glm_pages.get(page, "").split()
        toks = [t for t in toks if len(re.sub(r"[^A-Za-z0-9]", "", t)) >= 2]
        out = [glm_fragment(t, words) for t in toks]
        if not toks or None in out:
            return None
        parts_of[" ".join(out)] = out
        return " ".join(out)

    orph_toks = _col_toks(join["b_rows"], col)
    orph_text = " ".join(orph_toks)
    phantom = [l for l in on_b[:1] if not l.get("amount") and squash(l.get("desc")) and squash(l["desc"]) in squash(orph_text)]
    on_b = [l for l in on_b if l not in phantom]

    def block(rows, i, end):
        steps = sorted(b["c"] - a["c"] for a, b in zip(rows, rows[1:]))
        step = steps[len(steps) // 2] if steps else 0
        below, prev = [], rows[i]
        for r in rows[i + 1:end]:
            if r["money"] or not r["alpha_x"] or not _col_toks([r], col) or (step and r["c"] - prev["c"] > 2 * step):
                break
            prev = r
            if r["na"] not in furn["alnum"]:
                below.append(r)
        return _col_toks([rows[i]] + below, col)

    orph_glm = glm(orph_toks, pages[k + 1])
    shifted = orph_glm is not None and bool(on_b) and squash(orph_glm) in squash(on_b[0].get("desc"))
    anchors_a = [i for i, r in enumerate(rows_a) if r["money"] >= 2]
    last = rows_a[anchors_a[-1]]
    if shifted:
        head_toks = block(rows_a, anchors_a[-1], len(rows_a))
        head_text = glm(head_toks, pages[k]) if head_toks else ""
        last_text = None if head_text is None else f"{head_text} {orph_glm}".strip()
        last_parts = parts_of.get(head_text, []) + parts_of.get(orph_glm, [])
    else:
        last_text = None if orph_glm is None else f"{on_a[-1]['desc']} {orph_glm}".strip()
        last_parts = [squash(on_a[-1]["desc"])[:20]] + parts_of.get(orph_glm, [])
    anchors_b = [i for i, r in enumerate(rows_b) if r["money"] >= 2]
    geo_b = [(rows_b[i]["amounts"][-1], block(rows_b, i, anchors_b[n + 1] if n + 1 < len(anchors_b) else len(rows_b)))
             for n, i in enumerate(anchors_b)] if shifted else []
    pairs, j = [], 0
    for l in on_b if shifted else []:
        while j < len(geo_b) and geo_b[j][0] != l.get("amount"):
            j += 1
        if j == len(geo_b):
            break
        pairs.append((l, glm(geo_b[j][1], pages[k + 1])))
        j += 1
    last_amt = last["amounts"][-1] if last["amounts"] else ""
    why_not = (f"el último artículo de la página anterior no tiene el importe del renglón de PP-OCRv6 ({last_amt} / {on_a[-1].get('amount')})"
               if last_amt != on_a[-1].get("amount") else
               f"{len(on_b) - len(pairs)} artículo(s) de la página siguiente sin renglón con su importe" if shifted and len(pairs) != len(on_b) else
               "el renglón huérfano no tiene texto correspondiente en GLM-OCR" if last_text is None else
               next((f"descripción sin texto correspondiente en GLM-OCR («{l['desc'][:40]}», {l.get('amount')})" for l, d in pairs if d is None), ""))
    if why_not:
        log(f"  salto {where}: continuación «{orph_text}» sin realinear: {why_not}")
        quote = orph_glm or glm_quote(join["b_rows"], glm_pages.get(pages[k + 1], ""), 50)
        return [f"item description split by the page break ({where}): " + (f"«{quote[:50]}» continues" if quote else "a description row continues")
                + " the last item of the previous page; descriptions of the next page could not be re-aligned, review them"]
    for l in phantom:
        lines.remove(l)
        log(f"  salto {where}: «{l['desc'][:40]}» sin importe era la continuación, no un artículo")
    old_a = on_a[-1]["desc"]
    on_a[-1]["desc"], on_a[-1]["desc_parts"] = last_text, last_parts
    log(f"  salto {where}: «{orph_text}» continúa el último artículo de la página anterior: «{old_a[:45]}» -> «{on_a[-1]['desc'][:60]}» (geometría PP-OCRv6)")
    for l, d in pairs:
        if squash(d) != squash(l["desc"]):
            log(f"    descripción realineada: «{l['desc'][:45]}» -> «{d[:45]}» ({l.get('amount')})")
            l["desc"], l["desc_parts"] = d, parts_of.get(d, [d])
    return []


def fix_breaks(lines, pdf, pages, glm_pages, log, *, tokens_of):
    """Única entrada desde la fase 2. `lines`: líneas del 27B (todas las clases, con page_rel 1..n relativo a `pages`);
    `pages`: índices absolutos de página de la factura; `glm_pages`: {página: texto de GLM sin etiquetas};
    `tokens_of(pdf, página)` -> (cajas, ancho, alto) de PP-OCRv6 (ya en caché por la lectura de cifras).
    Sin geometría: solo la regla de texto R0 y UNA incidencia (la factura va a revisión: no se pudo comprobar el corte).
    Con geometría: por cada corte entre páginas contiguas, continuación por geometría -> reconciliación del diseño; sin
    continuación -> R0. Devuelve las incidencias (en inglés)."""
    try:
        seg, furn = segment({p: tokens_of(pdf, p)[:2] for p in pages})
    except Exception as e:  # noqa: BLE001  PP-OCRv6 caído: nunca un error del trabajo, pero tampoco silencio
        log(f"  PP-OCRv6 no disponible ({type(e).__name__}): saltos de página sin comprobar")
        for k in range(len(pages) - 1):
            if pages[k + 1] == pages[k] + 1:
                join_text_rule(lines, k, log)
        return ["page breaks could not be checked (PP-OCRv6 unavailable); review the items around the page breaks"], []
    issues = []
    style = doc_style(seg)
    before = list(lines)
    for k in range(len(pages) - 1):
        if pages[k + 1] != pages[k] + 1:
            continue
        join, _ = continuation(seg, pages[k], pages[k + 1], style)
        if not join:
            join_text_rule(lines, k, log)
        elif join["kind"] == "block":
            issues += reconcile_block(lines, k, pages, seg, join, glm_pages, log)
        elif join["kind"] == "row":
            issues += reconcile_row(lines, k, pages, seg, join, log)
        else:
            issues += reconcile_table(lines, k, pages, seg, join, glm_pages, furn, log)
    # Las líneas quitadas (cifra huérfana, fantasma, cola) no existen: una incidencia de lectura generada para ellas
    # («quantity of item «ISO 32…» not returned by the model», 31053) no debe llegar a WTCP.
    removed = [l for l in before if not any(l is x for x in lines)]
    requalified = [l for l in lines if l.pop("_requalified", False)]
    return issues, removed + requalified


def drop_issues_of(issues, stale):
    """Incidencias que citan la descripción («…[:40]») de una línea que el módulo quitó, o la incidencia «quantity of
    item «…» not returned» de una línea cuya cantidad asignó el realineado de filas (32836: el 27B no dio cantidad a la
    descripción partida; su fila, arriba de la página siguiente, la trae)."""
    keys = {f"«{(l.get('desc') or '')[:40]}»" for l in stale if (l.get("desc") or "").strip()}
    return [i for i in issues if not any(k in i for k in keys)]
