"""Completa `modelos.json` de trabajos antiguos (anteriores al 2026-10-21 08:43, sin respuestas guardadas de los modelos)
para que entren en la repetición EXACTA sin GPU (replay_full / repetir_trabajo). Corre la fase 1 y la fase 2 REALES
(GLM-OCR, regiones, 27B, VL, PP-OCRv6) sobre el PDF del trabajo, bajo el bloqueo de modelos, y escribe en OUT/<job>/:
meta.json (copia), ocr.md, modelos.json (MODEL_RAW + regiones de la fase 1) y result.json (salida del árbol actual).
No toca demo_jobs. Solo con WTCP parado y permiso del usuario (ocupa el único turno de los modelos).
Uso: completar_modelos.py OUT JOB [JOB...]"""
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import facturas_json as f2  # noqa: E402
import ocr_pipeline as p1  # noqa: E402


def main():
    out = Path(sys.argv[1])
    for job in sys.argv[2:]:
        d = REPO / "demo_jobs" / job
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        dst = out / job
        dst.mkdir(parents=True, exist_ok=True)
        log = lambda m: print(f"  {m}"[:200], flush=True)
        print(f"=== {meta['pdf']} ({job})", flush=True)
        with f2.modelos_lock(log):
            res = p1.run(str(d / meta["pdf"]), log=log)
            res_out = f2.build_all(res, log=log)
        shutil.copy(d / "meta.json", dst / "meta.json")
        (dst / "ocr.md").write_text(res["markdown"], encoding="utf-8")
        (dst / "modelos.json").write_text(json.dumps(dict(f2.MODEL_RAW, fase1_regiones=res.get("region_pages")),
                                                     ensure_ascii=False, default=str), encoding="utf-8")
        (dst / "result.json").write_text(json.dumps(res_out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {dst}: {[i['motivo_revision'] or 'ok' for i in res_out]}", flush=True)


if __name__ == "__main__":
    main()
