"""PP-OCRv6 (det+rec, PP-OCRv6_medium) en GPU0 para SCANIA: devuelve cada renglón con su caja.
POST /ocr (cuerpo = imagen PNG/JPG) -> {"texts": [...], "boxes": [[x0,y0,x1,y1], ...], "scores": [...], "size": [w, h]}
Arranque: python app.py --host 0.0.0.0 --port 8089 -m PP-OCRv6_medium. El --port y el -m de la linea de
comandos son los que usa hw_exporter.py (_pids_por_puerto) para atribuir VRAM/CPU a este modelo, igual que
con llama-server."""
import threading
import cv2, numpy as np, paddle
from fastapi import FastAPI, Request, HTTPException
from paddleocr import PaddleOCR

ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="gpu:0")
lock = threading.Lock()
_cpu = []


def cpu_ocr():
    """PP-OCRv6 en CPU, creado la primera vez que la GPU se queda sin memoria (2026-10-22, 32560: OOM en la GPU1)."""
    if not _cpu:
        _cpu.append(PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device="cpu", enable_mkldnn=False))  # oneDNN falla en este paddle (NotImplementedError PIR)
    return _cpu[0]
app = FastAPI(title="SCANIA PP-OCRv6")


@app.get("/health")
def health():
    return {"status": "ok", "model": "PP-OCRv6_medium det+rec", "device": "gpu:0"}


@app.post("/ocr")
async def run(request: Request):
    img = cv2.imdecode(np.frombuffer(await request.body(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "imagen no válida")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)  # el prototipo local pasa RGB
    with lock:
        try:
            r = ocr.predict(img)[0]
        except Exception:  # memoria de la GPU justa (comparte con otros modelos): soltar la caché del asignador y reintentar
            paddle.device.cuda.empty_cache()
            try:
                r = ocr.predict(img)[0]
            except Exception:  # sigue sin memoria en la GPU: la misma lectura en CPU (más lenta, ~20 s por página)
                r = cpu_ocr().predict(img)[0]
        finally:
            paddle.device.cuda.empty_cache()  # el asignador retiene bloques libres; sin esto se fragmenta hasta el tope
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
