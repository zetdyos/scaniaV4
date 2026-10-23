import sys
S = sys.argv[1]
src = open(S + "/replay_cmp.py", encoding="utf-8").read()
src = src.replace("p1.server_ok = lambda url: True\n", """p1.server_ok = lambda url: True
_orig_fix = f2.fix_break_llm
def _locked_fix(*a, **k):
    with f2.modelos_lock(log=lambda m: None):
        return _orig_fix(*a, **k)
f2.fix_break_llm = _locked_fix
""", 1)
src = src.replace('for job in jobs:', '''import os
jobs = [j for j in jobs if len(f2.page_texts((REPO / "demo_jobs" / j / "ocr.md").read_text(encoding="utf-8"))) > 1]
if os.environ.get("ONLY"):
    jobs = [j for j in jobs if j in os.environ["ONLY"].split(",")]
for job in jobs:''', 1)
src = src.replace("        new = f2.build_all(res, log=logs.append)\n", """        new = f2.build_all(res, log=logs.append)
        fr = [m for m in logs if "salto" in m and ("corregido" in m or "no cabe" in m or "cortada" in m or "saltos" in m)]
        if fr:
            print("FIX", job, meta["pdf"], " || ".join(fr), flush=True)
""", 1)
open(S + "/replay_fixb.py", "w", encoding="utf-8").write(src)
print("ok")
