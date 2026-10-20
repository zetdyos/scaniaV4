"""Detección de continuaciones con las funciones FINALES de facturas_json (page_break_continuation) en todos los saltos
de los PDF de varias páginas. Uso: medir_saltos_final.py lista.txt"""
import sys
from pathlib import Path
import fitz
sys.path.insert(0, r'C:\Users\LERIT\scaniaV4')
import facturas_json as f2
from collections import Counter
why = Counter(); n = 0
for p in [l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]:
    try:
        npg = len(fitz.open(p))
        for k in range(npg - 1):
            n += 1
            last, orph, col, w = f2.page_break_continuation(f2.ppocr_rows(p, k), f2.ppocr_rows(p, k + 1))
            why[w] += 1
            if orph:
                print(f"{Path(p).name} {k + 1}->{k + 2}: «{last['text'][:60]}» + " + " / ".join(f"«{o['text'][:50]}»" for o in orph), flush=True)
    except Exception as e:
        print(Path(p).name, "ERROR", e, flush=True)
    f2._PPOCR_CACHE.clear()
print("saltos", n, dict(why), flush=True)
