# Host de modelos (192.168.1.194): configuración de los servicios

`systemd/_volcado.txt` es una copia literal (2026-10-21) de las unidades `scania-*` y de sus *drop-ins* en
`/etc/systemd/system/`. En el host ya son permanentes (sobreviven a un reinicio; todas `enabled`, dentro de
`scania-models.target`). Esta copia es para reconstruirlas o comparar si algo cambia.

## Distribución de las GPU (desde 2026-10-21)

| GPU | Servicio | Puerto | Ajustes propios (drop-ins) |
|---|---|---|---|
| GPU0 | `scania-mdl-text`: Qwen3.6-27B Q6_K **con su proyector de visión** | 8083 | `v3.conf` (`-c 16384 --parallel 1`, caché q8_0), `zz-vision.conf` (`--mmproj mmproj-Qwen3.6-27B-F16.gguf --image-max-tokens 2048`) |
| GPU1 | `scania-mdl-glm`: GLM-OCR | 8086 | — |
| GPU1 | `scania-mdl-qwen-vl`: Qwen2.5-VL-7B | 8088 | `achicar.conf` (`-c 8192`, caché q8_0, `--no-mmproj-offload` desde 2026-10-22: el codificador de imagen en CPU, el VL fijo en ~4,9 GB en vez de crecer a 8,2 GB; foto ~38 s, página ~100 s) |
| GPU1 | `scania-mdl-ppocr`: PP-OCRv6 (PaddleOCR det+rec) | 8089 | `gpu1.conf` (`CUDA_VISIBLE_DEVICES=1`, arranca tras los modelos de la GPU1). `~/ppocr-svc/app.py` (copia en `ppocr-app.py`): si la GPU se queda sin memoria, reintenta y luego lee en CPU (`enable_mkldnn=False`; ~28 s por página) desde 2026-10-22 |
| GPU1 | `scania-mdl-embed`, `scania-mdl-rerank`: de la V3, **en uso, no tocar** | — | — |

Memoria medida: GPU0 ~22,9/24,5 GB con una imagen procesada; GPU1 ~21,4/24,5 GB en reposo, pico ~22,5 con PP-OCRv6
trabajando. PP-OCRv6 solo cabe en la GPU1 con el Qwen2.5-VL achicado (sin él dio OOM).

## Volver atrás un cambio

Borrar el drop-in correspondiente, `sudo systemctl daemon-reload` y `sudo systemctl restart <servicio>`:
`zz-vision.conf` (27B sin visión), `gpu1.conf` (PP-OCRv6 a la GPU0), `achicar.conf` (VL con `-c 12288` y caché f16; quitar solo `--no-mmproj-offload` devuelve el codificador de imagen a la GPU). PP-OCRv6 sin el paso a CPU: `~/ppocr-svc/app.py.bak-20261022`.
Los cambios del host requieren `sudo` (el usuario `techstudi` lo tiene con contraseña; no se guarda en ningún sitio).
