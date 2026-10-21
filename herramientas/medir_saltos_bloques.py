"""Saltos de página en diseños de BLOQUES (Amazon con fotos y parecidos): cada artículo es un bloque de renglones en una
columna de texto que termina en un renglón con solo el precio («$16.98»). Un bloque partido deja renglones abiertos al
pie de la página A (sin su precio) y la cola (y el precio) arriba de la B. Solo geometría de PP-OCRv6 (ppocr.json de
cada trabajo, ver cache_ppocr.py); sin modelos. Uso: medir_saltos_bloques.py lista.txt"""
import json, re, statistics, sys
from collections import Counter
from pathlib import Path

DEBUG = False
PRICE = re.compile(r"^\$\d{1,3}(?:,\d{3})*\.\d{2}$")


def rows_of(tokens):
    toks = sorted((y0, x0, x1, y1, t) for x0, y0, x1, y1, t in tokens)
    rows = []
    for y0, x0, x1, y1, t in toks:
        c = (y0 + y1) / 2
        if rows and abs(rows[-1]["c"] - c) <= 10:
            rows[-1]["toks"].append((x0, x1, t)); rows[-1]["c"] = (rows[-1]["c"] + c) / 2
        else:
            rows.append({"c": c, "toks": [(x0, x1, t)]})
    for r in rows:
        r["toks"].sort(); r["text"] = " ".join(t for _, _, t in r["toks"])
        # renglón de precio: un solo precio suelto; lo demás, marcas sin letras de la foto (cantidad «*4», «1», «口»)
        prices = [x0 for x0, _, t in r["toks"] if PRICE.match(t)]
        rest = [t for _, _, t in r["toks"] if not PRICE.match(t)]
        r["price"] = len(prices) == 1 and all(len(t) <= 3 and not re.search(r"[A-Za-z]", t) for t in rest)
        # borde izquierdo del texto: la primera caja con palabras (la foto puede colar una marca a la izquierda)
        words = [x0 for x0, _, t in r["toks"] if re.search(r"[A-Za-z]{3,}", t)]
        r["x"] = prices[0] if r["price"] else (words[0] if words else r["toks"][0][0])
    return rows


def norm(t):
    return re.sub(r"[^a-z]", "", t.lower())


def furniture(pages_rows):
    """Renglones de marco (cabecera/pie repetidos en 2+ páginas: fecha, «Order Details», URL, «1/2»): por su texto sin
    cifras, entre los 3 primeros y los 3 últimos de cada página."""
    seen = Counter()
    for rows in pages_rows:
        seen.update({norm(r["text"]) for r in rows[:3] + rows[-3:] if norm(r["text"])})
    return {t for t, n in seen.items() if n >= 2}


def blocks(rows, col):
    """Tamaño (renglones de la columna, con el del precio) de cada bloque completo de la página."""
    out, cur = [], 0
    started = False
    for r in rows:
        if abs(r["x"] - col) > 25:
            continue
        cur += 1
        if r["price"]:
            if started:
                out.append(cur)
            started, cur = True, 0
    return out


def block_break(rows_a, rows_b, furn):
    a = [r for r in rows_a if norm(r["text"]) not in furn or r["price"]]
    b = [r for r in rows_b if norm(r["text"]) not in furn or r["price"]]
    pa = [i for i, r in enumerate(a) if r["price"]]
    pb = [i for i, r in enumerate(b) if r["price"]]
    if len(pa) < 2 or not pb:
        return None, "sin bloques"
    col = statistics.median(a[i]["x"] for i in pa)
    if any(abs(b[i]["x"] - col) > 25 for i in pb[:1]):
        return None, "otra columna en B"
    open_a = [r for r in a[pa[-1] + 1:] if abs(r["x"] - col) <= 25]
    if not open_a:
        return None, "bloque cerrado en A"
    head_b = b[:pb[0]]
    tail_b = [r for r in head_b if abs(r["x"] - col) <= 25]
    if any(re.search(r"\$\s?\d", r["text"]) for r in tail_b):  # un renglón con nombre e importe es un artículo nuevo (31183)
        return None, "otra cosa antes del primer precio de B"
    if any(abs(r["x"] - col) > 25 for r in head_b if re.search(r"[A-Za-z]{2,}|\$\d", r["text"])):  # marcas sueltas no cuentan
        return None, "otra cosa antes del primer precio de B"
    sizes = blocks(a, col) + blocks(b, col)
    print("      tamaños", sizes) if DEBUG else None
    # bloques de 1 renglón (un precio sin texto) son cifras del resumen, no artículos
    sizes = [x for x in sizes if x >= 2]
    R = max(sizes) if sizes else None
    k = len(open_a) + len(tail_b) + 1
    if R is None or k > R + 1:  # abierto en A + cola en B no puede pasar de un bloque: si no, en B empieza otro
        return None, f"tamaño {k} vs bloque mayor {R}"
    return {"abierto": [r["text"] for r in open_a], "cola": [r["text"] for r in tail_b], "precio": b[pb[0]]["text"],
            "k": k, "R": R}, "continuación"


if __name__ == "__main__":
    why = Counter(); n = 0
    for p in [l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]:
        f = Path(p).parent / "ppocr.json"
        if not f.exists():
            continue
        pages = json.loads(f.read_text(encoding="utf-8"))
        rows = [rows_of(pages[str(i)]["tokens"]) for i in range(len(pages))]
        furn = furniture(rows)
        for k in range(len(rows) - 1):
            n += 1
            res, w = block_break(rows[k], rows[k + 1], furn)
            why[w.split(" ")[0]] += 1
            if res:
                print(f"{Path(p).name} {k + 1}->{k + 2}: A «{' / '.join(t[:40] for t in res['abierto'])}» + B «{' / '.join(t[:40] for t in res['cola'])}» {res['precio']} (k={res['k']}, R={res['R']})", flush=True)
    print("saltos", n, dict(why), flush=True)
