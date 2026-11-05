"""¿Están los modelos de SCANIA en sus GPU? Para retener el trabajo mientras el host de modelos se recupera (2026-11-04).

Hecho que lo motivó (2026-11-02 15:57): la GPU0 de .194 se cayó del bus (Xid 79, «Node Reboot Required» en las dos);
los llama-server se reiniciaron SIN tarjeta y seguían dando /health 200; la API decía «ok», WTCP siguió subiendo y tres
trabajos del mismo archivo acabaron en INTERNAL. Decisión del usuario (2026-11-04): durante una caída de las GPU la cola
se detiene y el trabajo que falló a mitad se repite cuando vuelven («retener y repetir»; WTCP espera sin límite un
trabajo queued/running, §3 del contrato); en .194 los modelos no arrancan sin su tarjeta y el host se reinicia solo con
freno (deploy/host-modelos/recuperacion/).

Fuente: el exportador de SCANIA-HW en :9100 del host (solo lee nvidia-smi): `DCGM_FI_DEV_GPU_TEMP{gpu}` por tarjeta y
`llm_modelo_vram_mib{modelo}` por modelo; mismos umbrales que herramientas/chequeo_arranque.py. Solo cuenta la
EVIDENCIA de GPU (tarjeta que falta, modelo que contesta sin memoria gráfica, host entero inalcanzable): un modelo que
se reinicia con su GPU presente (PP-OCRv6 se repara solo) no detiene nada, como antes.
"""
import time

import requests

import ocr as p1

EXPORTADOR = f"http://{p1.MODELS_HOST}:9100/metrics"
# nombre en /api/health -> (alias del exportador, puerto)
MODELOS = {"GLM-OCR": ("glm-194", 8086), "Qwen3.6-27B": ("text", 8083), "Qwen2.5-VL": ("qwen-vl", 8088),
           "PP-OCRv6": ("ppocr", 8089)}
ANALISIS = tuple(MODELOS)  # un análisis usa los cuatro
CLASIFICACION = ("Qwen3.6-27B",)  # el clasificador de :8092 usa el 27B
# VL dormido (--sleep-idle-seconds 30): ~300 MiB; PP-OCRv6 recién arrancado 430-900 MiB; los demás cargados, mucho más.
MIN_VRAM = {"text": 1000, "glm-194": 1000, "qwen-vl": 200, "ppocr": 300}
# Las dos tarjetas: si una se cae, nvidia-smi renumera la otra como 0, así que el índice no dice cuál falta.
GPUS = 2
ESPERA_S = 30
_cache = {"t": 0.0, "v": None}


def _metricas(cache_s=10.0):
    """(n.º de GPU vistas, {alias: MiB}) o None si el exportador no responde."""
    if time.time() - _cache["t"] < cache_s:
        return _cache["v"]
    v = None
    try:
        txt = requests.get(EXPORTADOR, timeout=10).text
        gpus, vram = set(), {}
        for ln in txt.splitlines():
            if ln.startswith("DCGM_FI_DEV_GPU_TEMP{"):
                gpus.add(ln.split('gpu="')[1].split('"')[0])
            elif ln.startswith("llm_modelo_vram_mib{") and 'modelo="' in ln:
                vram[ln.split('modelo="')[1].split('"')[0]] = float(ln.rsplit(" ", 1)[1])
        v = (len(gpus), vram)
    except (requests.RequestException, ValueError, IndexError):
        pass
    _cache.update(t=time.time(), v=v)
    return v


def _responde(puerto):
    try:
        return requests.get(f"http://{p1.MODELS_HOST}:{puerto}/health", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def sin_gpu(nombre, responde=True, cache_s=10.0):
    """True si hay evidencia de que el modelo NO está en su GPU (tarjeta que falta o contesta sin memoria gráfica);
    False si está bien o no se sabe (exportador caído): sin evidencia no se cambia nada."""
    m = _metricas(cache_s)
    if m is None:
        return False
    n, vram = m
    alias = MODELOS[nombre][0]
    return n < GPUS or (responde and vram.get(alias, 0.0) < MIN_VRAM[alias])


def listos(nombres=ANALISIS, cache_s=10.0):
    """(True, "") si se puede trabajar; (False, motivo) si hay una caída de GPU o el host no se alcanza."""
    try:
        m = _metricas(cache_s)
        caidos = [n for n in nombres if not _responde(MODELOS[n][1])]
        if m is None:
            if caidos:  # exportador y modelos sin respuesta: el host está caído o reiniciándose
                return False, "host de modelos sin respuesta (" + ", ".join(caidos) + ")"
            return True, ""
        n, vram = m
        if n < GPUS:
            return False, f"GPU caída en el host de modelos ({n} de {GPUS} tarjetas visibles)"
        cpu = [x for x in nombres if x not in caidos and vram.get(MODELOS[x][0], 0.0) < MIN_VRAM[MODELOS[x][0]]]
        if cpu:
            return False, "sin memoria gráfica (en CPU): " + ", ".join(cpu)
        return True, ""
    except Exception:  # noqa: BLE001  un fallo de esta comprobación nunca detiene la cola
        return True, ""


def esperar(nombres=ANALISIS, aviso=None):
    """Bloquea mientras haya una caída de GPU. `aviso(motivo)` se llama cuando el motivo cambia. Devuelve los segundos
    esperados (0 si no hubo que esperar)."""
    t0, ultimo = time.time(), None
    while True:
        ok, motivo = listos(nombres, cache_s=0.0)
        if ok:
            if ultimo is not None:
                print(f"gpus: modelos listos de nuevo tras {time.time() - t0:.0f} s", flush=True)
            return round(time.time() - t0)
        if motivo != ultimo:
            print(f"gpus: esperando: {motivo}", flush=True)
            if aviso:
                aviso(motivo)
            ultimo = motivo
        time.sleep(ESPERA_S)
