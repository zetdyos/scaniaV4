"""Auditoría de un trabajo contra la imagen: verdad para calibrar el 27B (y casos de céntimos en superíndice).
Solo lee: renderiza las páginas del PDF del trabajo a PNG y resume lo que salió (facturas, líneas, cabecera, motivo)
para compararlo a mano con la imagen. No llama a ningún modelo.
Uso:
  auditar.py JOB_ID [dpi]              -> PNG por página en verdad/png/JOB_ID/ y resumen por pantalla
  auditar.py --veredicto JOB_ID JSON   -> añade el veredicto a verdad/27b.jsonl (una línea por factura)
  auditar.py --resumen                 -> recuento: correctas, errores sin revisión, por modelo y etapa
Veredicto (JSON): {"factura": 0, "correcta": true|false|null (dudosa), "errores": [{"campo": "cantidad|importe|
descripcion|tipo|falta|sobra|total|cabecera", "linea": "...", "salio": "...", "impreso": "...",
"etapa": ETAPAS, "modelo": MODELOS, "causa": "..."}], "nota": "..."}
etapa: dónde nace el error; modelo: quién lo cometió («codigo» si fue una regla nuestra, no un modelo).
Criterio del usuario (2026-10-20, 32358): una línea a 0.00 está bien SOLO si en el papel también vale 0 (componentes
de kit: precio, impuesto y total 0.00); entonces omitirla o incluirla no es error. Si en el papel tiene valor y sale
0.00 (OCR o modelo), SÍ es error. Comprobarlo siempre contra la imagen.
verdad/ está fuera de git (datos de clientes)."""
import json, sys, time
from pathlib import Path
import fitz

REPO = Path(r"C:\Users\LERIT\scaniaV4")
JOBS, VERDAD = REPO / "demo_jobs", REPO / "verdad"
ETAPAS = ["fase1-lectura", "fase1-rescate", "fase2-extraccion", "fase2-saltos", "fase2-foto", "fase2-rescate-vl",
          "fase2-cuadre", "fase2-cabecera", "fase2-division"]
MODELOS = ["GLM-OCR", "Qwen3.6-27B", "Qwen2.5-VL", "PP-OCRv6", "codigo"]


def resumen(job, dpi=110):
    d = JOBS / job
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = json.loads((d / "result.json").read_text(encoding="utf-8"))
    out = VERDAD / "png" / job
    out.mkdir(parents=True, exist_ok=True)
    with fitz.open(d / meta["pdf"]) as doc:
        for i, pg in enumerate(doc):
            pg.get_pixmap(dpi=dpi).save(out / f"p{i + 1}.png")
        n = len(doc)
    print(f"{meta['pdf']} ({job}): {n} pág -> {out}")
    for f in res:
        o = f["ocr"]
        print(f"#{f['id']} {f['motivo_revision'] or 'SIN REVISIÓN'} total {f['total']} impreso={f['total_is_printed']} "
              f"págs {o.get('paginas')} ref {f.get('referencia')!r} origen {o.get('origen_lineas')}")
        print(f"   cabecera: sub {o.get('subtotal_articulos')!r} envío {o.get('envio')!r} imp {o.get('impuestos')!r} "
              f"desc {o.get('descuentos')} otros {o.get('other_charges')}")
        for l in f["lineas"]:
            ev = l["ocr"].get("evidencia", {})
            print(f"   {l['line_kind'][:8]:8} {l['cantidad']:>6} x {l['importe_linea']:>9} p{l['ocr'].get('pagina')} "
                  f"«{l['descripcion'][:70]}» {''.join(k[0] + '=' + v[:3] for k, v in ev.items() if v not in ('leido', ''))}")
        if f["mismatch"]:
            print(f"   motivo: {f['mismatch'][:400]}")


def veredicto(job, js):
    v = json.loads(js)
    for e in v.get("errores", []):  # cada error dice dónde nace y quién lo cometió
        if e.get("etapa") not in ETAPAS or e.get("modelo") not in MODELOS:
            sys.exit(f"error sin etapa/modelo válidos: {e}\netapas: {ETAPAS}\nmodelos: {MODELOS}")
    d = JOBS / job
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    f = next(x for x in json.loads((d / "result.json").read_text(encoding="utf-8")) if x["id"] == v["factura"])
    v.update(job=job, pdf=meta["pdf"], revision=f["motivo_revision"] or "", fecha=time.strftime("%Y-%m-%d %H:%M"),
             lineas=len([l for l in f["lineas"] if l["line_kind"] == "goods"]))
    VERDAD.mkdir(exist_ok=True)
    with open(VERDAD / "27b.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(v, ensure_ascii=False) + "\n")
    print("guardado:", json.dumps(v, ensure_ascii=False)[:300])


def resumen_verdad():
    from collections import Counter
    vs = [json.loads(x) for x in (VERDAD / "27b.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = [v for v in vs if v["correcta"] is True]
    mal = [v for v in vs if v["correcta"] is False]
    dud = [v for v in vs if v["correcta"] is None]
    falsos = [v for v in mal if not v["revision"]]
    print(f"{len(vs)} facturas auditadas: {len(ok)} correctas, {len(mal)} con error ({len(falsos)} SIN revisión), {len(dud)} dudosas")
    print(f"sin revisión: {sum(1 for v in vs if not v['revision'])}; de ellas con error: {len(falsos)}")
    for titulo, key in (("por modelo", "modelo"), ("por etapa", "etapa"), ("por campo", "campo")):
        c = Counter(e[key] for v in mal for e in v["errores"])
        cf = Counter(e[key] for v in falsos for e in v["errores"])
        print(f"{titulo}: " + ", ".join(f"{k} {n} ({cf.get(k, 0)} sin revisión)" for k, n in c.most_common()))
    for v in falsos:
        print(f"  SIN REVISIÓN {v['pdf']}#{v['factura']}: " + "; ".join(f"{e['campo']} [{e['modelo']}/{e['etapa']}]" for e in v["errores"]))


if __name__ == "__main__":
    if sys.argv[1] == "--veredicto":
        veredicto(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "--resumen":
        resumen_verdad()
    else:
        resumen(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 110)
