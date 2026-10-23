"""Fase 3: importe estimado de las líneas de artículo que la fase 2 dejó SIN importe (decisión del usuario 2026-10-22).

Para qué: en exportación solo se declara lo que supera un umbral; una línea sin importe podría quedar sin declarar. WTCP
usa `estimacion.importe_estimado` para el SED. Solo actúa en facturas que ya van a revisión (`motivo_revision` != "") y
en líneas `goods` con `importe_linea` == "". No cambia nada de lo que produce la fase 2: `importe_linea` sigue vacío, la
factura sigue en revisión y el total no se toca; solo añade `estimacion` a cada línea y una frase a `mismatch`.

Fuentes (sin precio de mercado ni nada que no salga del propio documento):
  1. Cuadre del resumen impreso (sin modelos): R = subtotal de artículos impreso − líneas con importe; si no hay
     subtotal, total impreso − envío − impuestos − otros cargos + descuentos − líneas con importe. R es el tope de las
     líneas vacías (`maximo`); con UNA línea vacía, su importe si no hay re-lectura.
  2. Re-lectura de la página con la imagen (27B con visión): la MISMA llamada calibrada de la fase 2 (`lines_with_llm`
     con `page_image`, ya en caché si la factura pasó por el rescate con imagen); se toma el importe de la línea con la
     misma descripción (≥ 0.8) en esa página, solo si es único, no es el de otra línea del mismo producto y no es el
     total entero. Se intenta también con una sola línea vacía: el cuadre da por buenas las demás líneas, y en una
     factura en revisión a menudo no lo son. Si la re-lectura y el cuadre no coinciden, gana la lectura de la línea y
     la frase lo dice.
Sin estimación (la decide una persona): si las líneas con importe ya cubren el resumen (R <= 0: repetida, sin cargo u
otra cifra mal leída) o si la línea es el mismo producto que otra con importe (páginas de detalle que repiten la lista).

`estimacion` = {importe_estimado, maximo, metodo}; metodo: "cuadre", "relectura", "relectura+cuadre" (las lecturas de
todas las líneas vacías suman exactamente R) o "" (sin estimación). Medido el 2026-10-23 sin GPU sobre 411 trabajos con
modelos.json: el resto del JSON idéntico; 6 estimaciones, las 6 correctas contra la verdad auditada."""
import difflib
from decimal import Decimal

SIM = 0.8  # misma descripción: la medida de siempre (desc_key + SequenceMatcher, ver facturas_json)


def _d(v):
    try:
        return Decimal(str(v)) if str(v or "").strip() else None
    except Exception:  # noqa: BLE001
        return None


def _s(v):
    return f"{v:.2f}" if v is not None else ""


def _note(doc, text):
    """Frase para quien revisa, sin cambiar el motivo (la factura ya está en revisión)."""
    doc["mismatch"] = "; ".join(filter(None, [doc["mismatch"], text]))
    doc["ocr"]["incidencias"].append(text)


def summary_rest(doc):
    """(R, base): lo que el resumen impreso deja para las líneas sin importe y de dónde sale ("subtotal" o "total");
    (None, "") si no hay con qué calcularlo. R puede ser <= 0: las líneas con importe ya cubren lo impreso."""
    goods = [l for l in doc["lineas"] if l["line_kind"] == "goods"]
    known = sum((_d(l["importe_linea"]) for l in goods if l["importe_linea"]), Decimal(0))
    o = doc["ocr"]
    sub = _d(o.get("subtotal_articulos"))
    if sub is not None:
        return sub - known, "subtotal"
    if doc.get("total_is_printed") and _d(doc.get("total")) is not None:
        charges = sum((_d(o.get(k)) or Decimal(0) for k in ("envio", "impuestos")), Decimal(0))
        charges += sum((_d(x.get("importe")) or Decimal(0) for x in o.get("other_charges") or []), Decimal(0))
        disc = sum((_d(x.get("importe")) or Decimal(0) for x in o.get("descuentos") or []), Decimal(0))
        return _d(doc["total"]) - charges + disc - known, "total"
    return None, ""


def same_item(a, b):
    """Dos descripciones del mismo producto: parecidas (>= SIM) o una es el principio de la otra (el nombre corto de la
    lista y el largo del detalle)."""
    a, b = desc_key(a), desc_key(b)
    if not a or not b:
        return False
    short, long_ = sorted((a, b), key=len)
    return (len(short) >= 12 and long_.startswith(short)) or difflib.SequenceMatcher(None, a, b).ratio() >= SIM


def desc_key(text):
    import html
    import re
    return re.sub(r"[^a-z0-9]", "", html.unescape(text or "").lower())


def reread_amounts(rs, f2, page_idx, desc, llm_server, timeout, log):
    """Importes que el 27B con la imagen de la página da a la línea con esa descripción (conjunto; vacío si no la halla)."""
    regs = rs["region_pages"][page_idx] if page_idx < len(rs["region_pages"]) else []
    page_md = f2.p1.page_markdown(page_idx + 1, rs["full_text"].get(page_idx) or "", regs)[0]
    data, _ = f2.lines_with_llm(page_md.split(f2.VERIF_HEADER)[0].rstrip(), llm_server, timeout, log,
                                f2.page_image(rs["pdf"], page_idx))
    key, out = desc_key(desc), set()
    for l in (data or {}).get("lineas", []):
        if l.get("line_kind", "goods") != "goods" or not key:
            continue
        if difflib.SequenceMatcher(None, desc_key(l.get("descripcion", "")), key).ratio() < SIM:
            continue
        v = f2.model_number(l.get("importe_linea"), l.get("importe_valor"), "", [])
        if v is not None and v > 0:
            out.add(Decimal(f2.cents(v)))
    return out


def estimate(doc, rs, f2, llm_server, timeout, log, use_llm=True):
    """Añade `estimacion` a las líneas goods (null salvo en las que no tienen importe) de una factura de la fase 2."""
    for l in doc["lineas"]:
        if l["line_kind"] == "goods":
            l["estimacion"] = None
    empty = [l for l in doc["lineas"] if l["line_kind"] == "goods" and not l["importe_linea"]]
    if not empty or not doc["motivo_revision"]:
        return
    rest, base = summary_rest(doc)
    est = {id(l): None for l in empty}
    method = {id(l): "" for l in empty}
    priced = [l for l in doc["lineas"] if l["line_kind"] == "goods" and l["importe_linea"]]
    if rest is not None and rest <= 0:
        # Las líneas con importe ya cubren el resumen impreso: la línea sin importe es el mismo artículo listado otra vez,
        # va sin cargo o hay otra cifra mal leída. Estimarla declararía de más (33335: la re-lectura copiaba el importe
        # de la fila del mismo producto; 33404: el total entero). La decide una persona.
        for l in empty:
            l["estimacion"] = {"importe_estimado": "", "maximo": "0.00", "metodo": ""}
            _note(doc, f"line {l['seq']} without amount: no estimate, the lines with an amount already add up to the "
                       f"printed {'items subtotal' if base == 'subtotal' else 'total'} (repeated or free item?), check it against the paper")
        log(f"     fase 3: {len(empty)} línea(s) sin importe; las demás ya cubren el resumen ({base}, resto {rest:.2f}): sin estimación")
        return
    # Línea sin importe que es el mismo producto que una con importe (31699: las páginas de detalle repiten los artículos
    # sin precio; 33335: fila extra del mismo producto sin precio): puede ser una repetición o unidades sin cargo. No se
    # estima, y el cuadre deja de ser exacto para las demás (solo vale como tope).
    dup = {id(l): next((p["seq"] for p in priced if same_item(p["descripcion"], l["descripcion"])), None) for l in empty}
    exact = not any(v is not None for v in dup.values())
    # Re-lectura de cada línea con la imagen (también con una sola línea vacía: el cuadre da por buenas todas las demás
    # líneas, y en una factura en revisión a menudo no lo son; 31728: corrimiento silencioso, el cuadre daba 9.00 y el
    # papel 19.24). Sin visión en el servidor, solo el cuadre.
    total = _d(doc.get("total")) if doc.get("total_is_printed") else None
    if use_llm and f2.server_has_vision(llm_server):
        for l in empty:
            pg = (l["ocr"].get("pagina") or 0) - 1
            if pg < 0 or not (l["descripcion"] or "").strip() or dup[id(l)] is not None:
                continue
            try:
                vals = reread_amounts(rs, f2, pg, l["descripcion"], llm_server, timeout, log)
            except Exception as e:  # noqa: BLE001  modelo caído o sin respuesta: sin estimación, nunca un error del trabajo
                log(f"     fase 3: re-lectura de la página {pg + 1} falló ({type(e).__name__}: {e})")
                continue
            if len(vals) != 1:
                continue
            v = vals.pop()
            # el importe de OTRA línea del mismo producto no es el de esta (fila repetida sin precio), y una línea no vale
            # el total entero del pedido si hay otros artículos con importe
            sibling = any(Decimal(p["importe_linea"]) == v and same_item(p["descripcion"], l["descripcion"]) for p in priced)
            whole = total is not None and priced and v >= total
            if not sibling and not whole:
                est[id(l)], method[id(l)] = v, "relectura"
    got = sum((est[id(l)] for l in empty if est[id(l)] is not None), Decimal(0))
    left = [l for l in empty if est[id(l)] is None and dup[id(l)] is None]
    disagree = False
    if rest is not None and exact:
        if not left and got == rest:
            for l in empty:
                method[id(l)] = "relectura+cuadre"
        elif len(left) == 1 and rest - got > 0:
            est[id(left[0])], method[id(left[0])] = rest - got, "cuadre"
        elif not left:
            disagree = True  # las lecturas no dan lo que deja el resumen: alguna otra cifra está mal leída
    for l in empty:
        seq, v = l["seq"], est[id(l)]
        l["estimacion"] = {"importe_estimado": _s(v), "maximo": _s(rest), "metodo": method[id(l)]}
        if dup[id(l)] is not None:
            _note(doc, f"line {seq} without amount: no estimate, same product as line {dup[id(l)]} (repeated or free item?), check it against the paper")
        elif v is not None:
            how = {"cuadre": "order summary", "relectura": "page re-read", "relectura+cuadre": "page re-read, matches the order summary"}
            extra = (f", but the order summary leaves {rest:.2f} for the lines without amount: other amounts may be misread"
                     if disagree else "")
            _note(doc, f"estimated amount for line {seq}: {v:.2f} ({how[method[id(l)]]}{extra})")
        elif rest is not None:
            _note(doc, f"line {seq} without amount: no estimate (at most {rest:.2f} for the lines without amount), check it against the paper")
        else:
            _note(doc, f"line {seq} without amount: no estimate, check it against the paper")
    log(f"     fase 3: {len(empty)} línea(s) sin importe; tope del resumen ({base or 'sin base'}) {_s(rest) or '-'}; "
        + ", ".join(f"#{l['seq']} {l['estimacion']['importe_estimado'] or '?'} ({l['estimacion']['metodo'] or '-'})" for l in empty))
