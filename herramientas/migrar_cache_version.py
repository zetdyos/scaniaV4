"""migrar_cache_version.py -> marca con la versión del clasificador (`vc`) las respuestas de clasif_jobs/cache.json obtenidas
desde una fecha, para que el despliegue de la caché con versiones (2026-10-27) no las vuelva a pedir. Las anteriores
quedan sin `vc` y se vuelven a pedir al clasificador cuando WTCP las pida (eran de una versión anterior).

Uso (con la API parada o sin clasificaciones en curso; deja una copia de seguridad):
    .venv/Scripts/python.exe herramientas/migrar_cache_version.py "2026-10-26 08:05" '{"hs": ..., "revision": "revision_2"}'
"""
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import clasificacion  # noqa: E402

desde = time.mktime(time.strptime(sys.argv[1], "%Y-%m-%d %H:%M"))
vc = clasificacion.firma(json.loads(sys.argv[2]))
p = clasificacion.CACHE_FILE
shutil.copy(p, p.with_name(f"cache.respaldo-{time.strftime('%Y%m%d-%H%M%S')}.json"))
cache = json.loads(p.read_text(encoding="utf-8"))
n = 0
for v in cache.values():
    if v.get("ts", 0) >= desde and not v.get("vc"):
        v["vc"] = vc
        n += 1
clasificacion._guardar(p, cache)
print(f"{n} de {len(cache)} respuestas marcadas con vc={vc}; las demás se volverán a pedir al clasificador")
