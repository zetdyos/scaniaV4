"""Medición de los saltos de página SOLO con geometría (sin modelos, sin GPU): el módulo nuevo `saltos` frente a los
detectores de hoy de facturas_json (block_break, row_break, page_break_continuation) sobre la geometría de PP-OCRv6
guardada de cada trabajo (modelos.json["ppocr"] desde el 2026-10-21 08:43, ppocr.json en los anteriores).
Para cada documento de varias páginas (último trabajo por nombre de PDF) y cada factura (ocr.paginas del result.json):
cada corte entre páginas contiguas -> (hoy, nuevo). Imprime acuerdos, desacuerdos, histograma de motivos y de estilos, y
compara con herramientas/saltos_conocidos.txt (continuaciones reales comprobadas contra la imagen y falsos conocidos).
Uso: medir_saltos.py [-o salida.json] [PDF ...]   (sin PDF: todos)"""
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import facturas_json as f2  # noqa: E402
import saltos  # noqa: E402

GEO = {}


def fake_tokens(pdf, page_idx, timeout=120):
    g = GEO.get(str(page_idx))
    if g is None:
        raise RuntimeError("sin geometría guardada")
    return [tuple(t) for t in g["tokens"]], g["w"], g["h"]


f2.ppocr_tokens = fake_tokens


def geometry_of(d):
    m = d / "modelos.json"
    if m.exists():
        g = json.loads(m.read_text(encoding="utf-8")).get("ppocr") or {}
        if g:
            return g
    p = d / "ppocr.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def latest_jobs(only):
    latest = {}
    for d in sorted((REPO / "demo_jobs").iterdir()):
        meta = d / "meta.json"
        if not meta.exists() or not (d / "result.json").exists():
            continue
        pdf = json.loads(meta.read_text(encoding="utf-8")).get("pdf")
        if only and Path(pdf).stem not in only and pdf not in only:
            continue
        latest[pdf] = d
    return latest


def today(pages, k):
    """Detección del código anterior (facturas_json hasta 556a819) en el corte pages[k] -> pages[k+1]: [('block'|'row'|
    'table', precio)]. Vacía cuando ese código ya no existe (borrado tras la medición del 2026-10-23: 814 cortes,
    812 iguales, los 2 distintos son los falsos positivos de 32560 que el módulo nuevo rechaza)."""
    if not hasattr(f2, "block_break"):
        return []
    f2._PPOCR_CACHE.clear()
    out = []
    try:
        furn = f2.block_furniture("x", pages)
        A, B = f2.block_rows("x", pages[k]), f2.block_rows("x", pages[k + 1])
        bb = f2.block_break(A, B, furn)
        if bb:
            out.append(("block", bb["precio"]))
        else:
            rb, _ = f2.row_break(f2.ppocr_rows("x", pages[k]), f2.ppocr_rows("x", pages[k + 1]), f2.page_furniture("x", pages))
            if rb and rb["importe"]:
                out.append(("row", rb["importe"]))
    except Exception as e:  # noqa: BLE001
        out.append(("error", type(e).__name__))
    try:
        last, orph, _, why = f2.page_break_continuation(f2.ppocr_rows("x", pages[k]), f2.ppocr_rows("x", pages[k + 1]))
        if orph:
            out.append(("table", f2._row_amount(last)))
    except Exception as e:  # noqa: BLE001
        out.append(("error", type(e).__name__))
    return out


def known():
    out = {}
    f = REPO / "herramientas" / "saltos_conocidos.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                pdf, brk, verdict, *rest = line.split()
                out[(pdf, brk)] = (verdict, " ".join(rest))
    return out


def main():
    args = sys.argv[1:]
    out_path = None
    if "-o" in args:
        i = args.index("-o")
        out_path = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    only = set(args)
    ref = known()
    results, why_new, styles, layouts = [], Counter(), Counter(), Counter()
    n_docs = n_breaks = n_nogeo = 0
    for pdf, d in latest_jobs(only).items():
        geo = geometry_of(d)
        if not geo:
            continue
        res = json.loads((d / "result.json").read_text(encoding="utf-8"))
        n_pages = max(int(k) for k in geo) + 1
        if n_pages < 2:
            continue
        n_docs += 1
        GEO.clear()
        GEO.update(geo)
        for inv in res:
            a, b = inv["ocr"].get("paginas") or [1, n_pages]
            pages = list(range(a - 1, b))
            if len(pages) < 2:
                continue
            have = {p for p in pages if str(p) in geo}
            seg, _ = saltos.segment({p: ([tuple(t) for t in geo[str(p)]["tokens"]], geo[str(p)]["w"]) for p in have})
            style = saltos.doc_style(seg)
            styles[style] += 1
            layouts[("block" if sum(1 for s in seg.values() if s["block"]) > sum(1 for s in seg.values() if s["table"]) else "table")] += 1
            for k in range(len(pages) - 1):
                if pages[k + 1] != pages[k] + 1:
                    continue
                n_breaks += 1
                brk = f"{pages[k] + 1}->{pages[k] + 2}"
                if pages[k] not in have or pages[k + 1] not in have:
                    n_nogeo += 1
                    continue
                old = today(pages, k)
                new, why = saltos.continuation(seg, pages[k], pages[k + 1], style)
                why_new[why.split(";")[0][:60] if not new else "continuación"] += 1
                old_s = ", ".join(f"{kd}:{pr}" for kd, pr in old) or "-"
                new_s = f"{new['kind']}:{new['price']}" if new else "-"
                agree = (not hasattr(f2, "block_break")) or ((bool(old) and old[0][0] != "error") == bool(new)
                                                            and (not new or any(pr == new["price"] for _, pr in old)))
                rec = {"pdf": pdf, "job": d.name, "inv": inv["id"], "salto": brk, "hoy": old_s, "nuevo": new_s, "igual": agree,
                       "motivo": why, "estilo": style, "conocido": ref.get((pdf, brk))}
                results.append(rec)
                mark = "=" if agree else "!"
                tag = f" [{rec['conocido'][0]}]" if rec["conocido"] else ""
                if new or old or rec["conocido"]:
                    print(f"{mark} {pdf} #{inv['id']} {brk}: hoy {old_s} | nuevo {new_s} | estilo {style}{tag}", flush=True)
                    if not agree:
                        print(f"    motivo nuevo: {why[:200]}", flush=True)
    print(f"\ndocumentos {n_docs}, saltos {n_breaks} (sin geometría {n_nogeo})")
    print("iguales", sum(1 for r in results if r["igual"]), "distintos", sum(1 for r in results if not r["igual"]))
    print("detecciones hoy", sum(1 for r in results if r["hoy"] != "-"), "nuevo", sum(1 for r in results if r["nuevo"] != "-"))
    print("estilos por factura:", dict(styles), "| diseños:", dict(layouts))
    print("motivos (nuevo):", why_new.most_common())
    if ref:
        for (pdf, brk), (verdict, note) in sorted(ref.items()):
            r = next((x for x in results if x["pdf"] == pdf and x["salto"] == brk), None)
            got = r["nuevo"] if r else "(sin medir)"
            ok = (verdict in ("real", "hoy") and r and r["nuevo"] != "-") or (verdict == "falso" and r and r["nuevo"] == "-") \
                or (r is None)  # el corte cae entre facturas distintas del PDF: producción tampoco lo mira
            print(f"  {'OK ' if ok else 'XX '} {verdict:5} {pdf} {brk}: nuevo {got} | {note}")
    if out_path:
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
