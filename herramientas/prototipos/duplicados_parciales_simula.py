import sys
import json, sys, importlib.util
sys.path.insert(0, r"C:SERSeritscaniav4")
from pathlib import Path
S = sys.argv[1]
spec = importlib.util.spec_from_file_location("facturas_json", S + "/final/facturas_json.py")
f2 = importlib.util.module_from_spec(spec); sys.modules["facturas_json"] = f2; spec.loader.exec_module(f2)
J = Path(r"C:\Users\LERIT\scaniaV4\demo_jobs")
jobs = []
for d in sorted(J.iterdir()):
    try:
        m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if m.get("client") != "wtcp-produccion" or not (d / "result.json").exists(): continue
        jobs.append((d.name, m["pdf"], json.loads((d / "result.json").read_text(encoding="utf-8")), (d / "ocr.md").read_text(encoding="utf-8")))
    except Exception: pass
latest = {}; full = part = 0
for name, pdf, r, md in jobs:
    mine = f2.order_fingerprints(r, md)
    for opdf, (oname, ofps) in latest.items():
        if opdf == pdf: continue
        for a in mine:
            if any(f2.same_order(a, b) for b in ofps): full += 1; continue
            b = next((b for b in ofps if f2.partial_same_order(a, b)), None)
            if b: part += 1; print("PARCIAL", pdf, a["id"], "<->", opdf, b["id"], a["ref"])
    latest[pdf] = (name, mine)
print("completos", full, "parciales", part)
