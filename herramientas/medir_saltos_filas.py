"""Saltos de página en tablas con la descripción ENCIMA de su fila de importes (FashionGo, 32836): la página A termina
con una descripción abierta (texto tras la última fila con importes) y la B empieza con una fila de importes antes de
cualquier descripción. Solo geometría de PP-OCRv6 (ppocr.json). Uso: medir_saltos_filas.py lista.txt"""
import json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, r"C:\Users\LERIT\scaniaV4")
import facturas_json as f2

if __name__ == "__main__":
    why, n = Counter(), 0
    for p in [l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]:
        fj = Path(p).parent / "ppocr.json"
        if not fj.exists():
            continue
        pages = json.loads(fj.read_text(encoding="utf-8"))
        f2.ppocr_tokens = lambda pdf, i, timeout=120: ([tuple(t) for t in pages[str(i)]["tokens"]], pages[str(i)]["w"], pages[str(i)]["h"])
        f2._PPOCR_CACHE.clear()
        furn = f2.page_furniture(p, list(range(len(pages))))
        for k in range(len(pages) - 1):
            n += 1
            r, w = f2.row_break(f2.ppocr_rows(p, k), f2.ppocr_rows(p, k + 1), furn)
            why[w] += 1
            if r:
                print(f"{Path(p).name} {k + 1}->{k + 2}: A «{r['abierto'][:60]}» + fila B «{r['fila'][:70]}»", flush=True)
    print("saltos", n, dict(why))
