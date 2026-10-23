import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()


def rep(old, new):
    global s
    assert s.count(old) == 1, old[:60]
    s = s.replace(old, new)


rep('''_VISION = {}


def server_has_vision(server):''', '''# Salto de página corregido con las dos páginas a la vista (propuesta del usuario 2026-10-22): el 27B lee cada página
# por separado y un artículo partido por el salto queda en dos líneas incompletas, o los importes de B se corren. Segunda
# llamada solo en ese salto: las DOS imágenes completas + el JSON que el propio 27B devolvió para esas páginas, con la
# orden de corregir solo lo que las imágenes muestran mal. Mismo esquema, con la página de cada línea.
FIX_SCHEMA = json.loads(json.dumps(LINES_SCHEMA))
FIX_SCHEMA["properties"]["lineas"]["items"]["properties"]["pagina"] = {"type": "integer", "enum": [1, 2]}
FIX_SCHEMA["properties"]["lineas"]["items"]["required"].append("pagina")
FIX_PROMPT = ("The two images are two consecutive pages of the same document: the first image is page 1, the second "
              "image is page 2. Each page was read separately; this is the result, as JSON (each line has \\"pagina\\": "
              "the page it was read from):\\n\\n{json}\\n\\n"
              "Reading each page alone can break an item that continues across the page break: its description at the "
              "bottom of page 1 and its amount at the top of page 2 end up as two incomplete lines, or the amounts of "
              "page 2 shift by one item. Compare the JSON with the images and return the corrected JSON for both pages, "
              "in the same format: every line of both pages, each with \\"pagina\\". Fix only what the images show to be "
              "wrong; keep every other line and value exactly as given.")


def fix_break_llm(pdf, pa, pb, lines_a, lines_b, server, timeout, log):
    """Corrección del salto pa->pb: (líneas corregidas con «pagina», o None)."""
    given = {"lineas": [dict(l, pagina=1) for l in lines_a] + [dict(l, pagina=2) for l in lines_b]}
    user = FIX_PROMPT.format(json=json.dumps(given, ensure_ascii=False, default=str))
    budget = server_n_ctx(server) - count_tokens(server, SYSTEM_PROMPT + "\\n" + user) - PROMPT_MARGIN_TOKENS - 2 * IMAGE_MAX_TOKENS
    if budget < 512:
        log(f"  salto {pa + 1}->{pb + 1}: no cabe en el contexto del 27B")
        return None
    img = lambda i: {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page_image(pdf, i)}"}}
    payload = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": [img(pa), img(pb), {"type": "text", "text": user}]}],
        "temperature": 0, "top_k": 1, "max_tokens": budget, "cache_prompt": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "factura", "schema": FIX_SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    rj = post_json(f"{server}/v1/chat/completions", payload, timeout, log=log)
    ch = rj["choices"][0]
    content = re.sub(r"<think>.*?</think>", "", ch["message"]["content"] or "", flags=re.S).strip()
    MODEL_RAW.setdefault("27b_salto", {})[f"{pa}-{pb}"] = {"content": content, "finish_reason": ch.get("finish_reason")}
    if ch.get("finish_reason") == "length":
        log(f"  salto {pa + 1}->{pb + 1}: respuesta cortada")
        return None
    from decimal import Decimal
    try:
        new = json.loads(content, parse_float=Decimal).get("lineas", [])
    except json.JSONDecodeError:
        return None
    sig = lambda l: (str(l.get("descripcion", ""))[:60], str(l.get("cantidad", "")), str(l.get("importe_linea", "")), l.get("line_kind"))
    old = [sig(l) for l in given["lineas"]]
    newsig = [sig(l) for l in new]
    changed = [x for x in newsig if x not in old]
    gone = [x for x in old if x not in newsig]
    log(f"  salto {pa + 1}->{pb + 1} corregido con las dos páginas: {len(old)} -> {len(new)} línea(s); "
        f"cambian/entran {changed}; salen {gone}")
    return new


def strip_signals(doc, pages):
    """Saltos (k: entre la página relativa k y k+1) con algo abierto según las líneas: el último artículo de A sin importe
    o el primero de B sin descripción (vacía, identificador, recuento)."""
    goods = [l for l in doc["lineas"] if l["line_kind"] == "goods"]
    no_desc = lambda d: not (d or "").strip() or ID_LIKE_RE.match(re.sub(r"\\s", "", d or "")) or COUNT_ONLY_RE.match(d or "")
    out = []
    for k in range(1, len(pages)):
        a = [l for l in goods if l["ocr"]["pagina"] == pages[k - 1] + 1]
        b = [l for l in goods if l["ocr"]["pagina"] == pages[k] + 1]
        if (a and not a[-1]["importe_linea"]) or (b and no_desc(b[0]["descripcion"])):
            out.append(k)
    return out


_VISION = {}


def server_has_vision(server):''')

rep('''def build_invoice(inv_id, res, inv, use_llm, llm_server, vl_server, timeout, log, vl=True, unit_alt=False, img=False):''',
    '''def build_invoice(inv_id, res, inv, use_llm, llm_server, vl_server, timeout, log, vl=True, unit_alt=False, img=False, fix_breaks=()):''')

rep('''        lines, model_totals, model_currencies = [], [], set()
        for k, (i, regs) in enumerate(zip(pages, regions)):''', '''        lines, model_totals, model_currencies = [], [], set()
        fixed = {}
        for kb in fix_breaks:  # salto entre la página relativa kb y kb+1: las dos imágenes + el JSON de esas páginas
            raw = []
            for kk in (kb - 1, kb):
                pm = p1.page_markdown(pages[kk] + 1, res["full_text"].get(pages[kk]) or "", regions[kk])[0]
                d, _ = lines_with_llm(pm.split(VERIF_HEADER)[0].rstrip(), llm_server, timeout, log, None)
                raw.append((d or {}).get("lineas", []))
            new = fix_break_llm(res["pdf"], pages[kb - 1], pages[kb], raw[0], raw[1], llm_server, timeout, log)
            if new is not None:
                fixed[kb - 1] = [l for l in new if l.get("pagina") == 1]
                fixed[kb] = [l for l in new if l.get("pagina") == 2]
        for k, (i, regs) in enumerate(zip(pages, regions)):''')

rep('''            if data is None:
                continue
            for l in data.get("lineas", []):
                # Cifras: el VALOR lo da el 27B''', '''            if k in fixed:
                data = dict(data or {"total": "", "total_valor": 0, "total_is_printed": False}, lineas=fixed[k])
            if data is None:
                continue
            for l in data.get("lineas", []):
                # Cifras: el VALOR lo da el 27B''')

rep('''            # Lectura con la imagen (27B con su proyector de visión): si no cuadra, se repite con la imagen de cada página''',
    '''            # Salto de página con las dos páginas a la vista: si no cuadra y algún salto tiene un artículo abierto, el 27B
            # corrige su propio JSON de esas dos páginas mirando las dos imágenes; se acepta solo sin revisión.
            if source == "llm" and not doc["_cuadra"] and len(inv["pages"]) > 1 and server_has_vision(llm_server):
                ks = strip_signals(doc, inv["pages"])
                if ks:
                    fx_log = []
                    alt, alt_source = build_invoice(len(out), rs, inv, use_llm, llm_server, vl_server, timeout, fx_log.append,
                                                    vl=False, fix_breaks=ks)
                    for m in fx_log:
                        if "salto" in m and ("corregido" in m or "no cabe" in m or "cortada" in m):
                            log(m)
                    if alt_source == "llm" and alt["_cuadra"] and not alt["motivo_revision"]:
                        log(f"     con los saltos {ks} corregidos a la vista de las dos páginas, todo lo impreso cuadra exactamente")
                        alt["ocr"]["origen_lineas"] = "llm+salto"
                        doc, source = alt, alt_source
                    else:
                        log(f"     saltos {ks} corregidos, pero sigue en revisión: {alt['mismatch'][:200]}")
            # Lectura con la imagen (27B con su proyector de visión): si no cuadra, se repite con la imagen de cada página''')
open(p, "w", encoding="utf-8").write(s)
print("parche aplicado")
