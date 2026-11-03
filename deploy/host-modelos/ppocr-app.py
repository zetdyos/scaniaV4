"""PP-OCRv6 (det+rec, PP-OCRv6_medium) en la GPU para SCANIA: devuelve cada renglón con su caja.
POST /ocr (cuerpo = imagen PNG/JPG) -> {"texts": [...], "boxes": [[x0,y0,x1,y1], ...], "scores": [...], "size": [w, h]}
Arranque: python app.py --host 0.0.0.0 --port 8089 -m PP-OCRv6_medium. El --port y el -m de la linea de
comandos son los que usa hw_exporter.py (_pids_por_puerto) para atribuir VRAM/CPU a este modelo, igual que
con llama-server.

Autorreparación (2026-11-03): la memoria de la GPU que retiene Paddle crece con el uso (434 MiB al arrancar, 2006 tras
19 h) hasta el tope FLAGS_gpu_memory_limit_mb; entonces una página densa no cabe, se lee en CPU (1-3 min por página) y
así se queda hasta reiniciar. Ahora, si una lectura tuvo que ir a la CPU o la memoria reservada pasa del 80 % del tope,
el servicio sale (código 3) en cuanto lleva IDLE_S sin peticiones y systemd lo vuelve a arrancar (Restart=on-failure),
limpio y en la GPU. SCANIA reintenta mientras tanto (facturas_json.ppocr_tokens). /ocr es síncrono (FastAPI lo corre en
su grupo de hilos): /health contesta también mientras se lee una página."""
import os
import sys
import threading
import time

import cv2, numpy as np, paddle
from fastapi import Depends, FastAPI, Request, HTTPException
from paddleocr import PaddleOCR

ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="gpu:0")
lock = threading.Lock()
_cpu = []
TOPE_MIB = int(os.environ.get("FLAGS_gpu_memory_limit_mb") or 0)
UMBRAL = 0.8   # fracción del tope a partir de la cual se programa el reinicio
IDLE_S = 30    # segundos sin peticiones antes de reiniciar
estado = {"reiniciar": "", "ultima": time.time(), "en_cpu": 0, "lecturas": 0}


def cpu_ocr():
    """PP-OCRv6 en CPU, creado la primera vez que la GPU se queda sin memoria (2026-10-22, 32560: OOM en la GPU1)."""
    if not _cpu:
        _cpu.append(PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="cpu", enable_mkldnn=False))  # oneDNN falla en este paddle (NotImplementedError PIR)
    return _cpu[0]


def reservada_mib():
    try:
        return int(paddle.device.cuda.memory_reserved() / 2**20)
    except Exception:  # noqa: BLE001
        return -1


def vigia():
    """Sale del proceso (systemd lo reinicia) cuando hay un reinicio pendiente y el servicio lleva IDLE_S parado."""
    while True:
        time.sleep(5)
        if estado["reiniciar"] and not lock.locked() and time.time() - estado["ultima"] >= IDLE_S:
            print(f"PP-OCRv6: reinicio programado ({estado['reiniciar']}); saliendo para que systemd lo arranque limpio",
                  file=sys.stderr, flush=True)
            os._exit(3)


threading.Thread(target=vigia, daemon=True).start()
app = FastAPI(title="SCANIA PP-OCRv6")


@app.get("/health")
def health():
    return {"status": "ok", "model": "PP-OCRv6_medium det+rec", "device": "gpu:0",
            "reservada_mib": reservada_mib(), "tope_mib": TOPE_MIB, "lecturas": estado["lecturas"],
            "lecturas_en_cpu": estado["en_cpu"], "reinicio_pendiente": estado["reiniciar"]}


async def _cuerpo(request: Request):
    """El cuerpo en bruto (la imagen) se lee en una dependencia asíncrona para que /ocr pueda ser síncrono."""
    return await request.body()


@app.post("/ocr")
def run(data: bytes = Depends(_cuerpo)):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "imagen no válida")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)  # el prototipo local pasa RGB
    with lock:
        try:
            try:
                r = ocr.predict(img)[0]
            except Exception:  # memoria de la GPU justa (comparte con otros modelos): soltar la caché del asignador y reintentar
                paddle.device.cuda.empty_cache()
                try:
                    r = ocr.predict(img)[0]
                except Exception:  # sigue sin memoria en la GPU: la misma lectura en CPU (más lenta, ~20 s por página)
                    r = cpu_ocr().predict(img)[0]
                    estado["en_cpu"] += 1
                    estado["reiniciar"] = estado["reiniciar"] or "una lectura tuvo que ir a la CPU"
        finally:
            paddle.device.cuda.empty_cache()  # el asignador retiene bloques libres; sin esto se fragmenta hasta el tope
            estado["lecturas"] += 1
            estado["ultima"] = time.time()
            m = reservada_mib()
            if TOPE_MIB and m >= UMBRAL * TOPE_MIB and not estado["reiniciar"]:
                estado["reiniciar"] = f"memoria reservada {m} MiB de {TOPE_MIB}"
    return {"texts": list(r["rec_texts"]), "boxes": [[int(v) for v in b] for b in r["rec_boxes"]],
            "scores": [round(float(s), 4) for s in r["rec_scores"]], "size": [img.shape[1], img.shape[0]]}


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("-m", "--model", default="PP-OCRv6_medium", help="informativo (lo lee hw_exporter.py)")
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, workers=1)
