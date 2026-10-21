"""Compara las pasadas de calibra_27b.py contra la verdad auditada (verdad/27b.jsonl). Por documento: estado de cada
factura en cada variante y, si cambian los artículos (cantidad, importe, descripción) respecto a la referencia, cuáles.
Uso: compara_27b.py muestra.json dir base v1 [v2 ...]"""
import json, sys
from pathlib import Path
REPO = Path(r"C:\Users\LERIT\scaniaV4")
sample = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
D, variants = Path(sys.argv[2]), sys.argv[3:]
truth = {}
for l in (REPO / "verdad" / "27b.jsonl").read_text(encoding="utf-8").splitlines():
    if l.strip():
        v = json.loads(l)
        truth.setdefault(v["job"], {})[v["factura"]] = v

goods = lambda inv: [(str(l["cantidad"]), l["importe_linea"], l["descripcion"][:45].replace("\n", " "))
                     for l in inv["lineas"] if l["line_kind"] == "goods"]
tally = {v: {"ok": 0, "inv": 0} for v in variants}
for grupo in ("err", "ctrl"):
    print(f"\n===== {'documentos con error del 27B' if grupo == 'err' else 'control (auditados correctos)'}")
    for job in sample[grupo]:
        res = {}
        for v in variants:
            f = D / v / f"{job}.json"
            res[v] = json.loads(f.read_text(encoding="utf-8"))["result"] if f.exists() else None
        if any(r is None for r in res.values()):
            continue
        pdf = sample[grupo][job]
        states = {v: [i["motivo_revision"] or "ok" for i in r] for v, r in res.items()}
        for v, r in res.items():
            tally[v]["inv"] += len(r); tally[v]["ok"] += sum(1 for i in r if not i["motivo_revision"])
        base = res[variants[0]]
        diffs = []
        for v in variants[1:]:
            if len(res[v]) != len(base):
                diffs.append(f"   {v}: facturas {len(base)} -> {len(res[v])}")
                continue
            for a, b in zip(base, res[v]):
                ga, gb = goods(a), goods(b)
                if ga != gb or a["total"] != b["total"]:
                    diffs.append(f"   {v} #{b['id']}: -{[x for x in ga if x not in gb][:4]} +{[x for x in gb if x not in ga][:4]}"
                                 + (f" total {a['total']}->{b['total']}" if a["total"] != b["total"] else ""))
        errs = "; ".join(f"#{k}: " + ", ".join(e["campo"] + "/" + e["modelo"] for e in t["errores"])
                         for k, t in truth.get(job, {}).items() if t["correcta"] is False)
        flag = "" if len({str(s) for s in states.values()}) == 1 and not diffs else "  <-- CAMBIA"
        print(f"{pdf}: " + " | ".join(f"{v} {s}" for v, s in states.items()) + (f"   [verdad: {errs}]" if errs else "") + flag)
        for x in diffs:
            print(x)
print("\nsin revisión:", ", ".join(f"{v} {t['ok']}/{t['inv']}" for v, t in tally.items()))
