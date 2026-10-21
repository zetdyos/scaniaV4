# Instalación y despliegue — PDF Reder

## 1. Requisitos

- Máquina de la app: Linux, Python 3.12, sin GPU (el layout PP-DocLayoutV3 corre en CPU; 16 hilos van bien). Internet solo la primera vez (descarga de paquetes y del modelo de layout desde Hugging Face, ~400 MB).
- Host de modelos (`192.168.1.194` por defecto) con llama.cpp **posterior al 2026-02-18** (arreglo de GLM-OCR, PR 19677) y acceso desde la app a los puertos 8086, 8083, 8088 y 8089.
- Modelos GGUF en el host de modelos: `GLM-OCR-Q8_0.gguf` + `mmproj-GLM-OCR-Q8_0.gguf`, `Qwen2.5-VL-7B-Q4_K_M.gguf` + su mmproj, `Qwen3.6-27B-Q6_K.gguf`.
- PP-OCRv6 (`PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`, PaddleOCR 3.7.0) como servicio HTTP en el host de modelos (sección 2.1).

## 2. Servidores de modelos (en 192.168.1.194)

Un `llama-server` por modelo, escuchando en todas las interfaces:

```bash
# GLM-OCR (OCR de página completa y de regiones) — puerto 8086
llama-server -m GLM-OCR-Q8_0.gguf --mmproj mmproj-GLM-OCR-Q8_0.gguf -ngl 99 -c 16384 --host 0.0.0.0 --port 8086

# Qwen2.5-VL-7B (rescate visual de precios y cantidades) — puerto 8088
llama-server -m Qwen2.5-VL-7B-Q4_K_M.gguf --mmproj <mmproj-Qwen2.5-VL-7B>.gguf -ngl 99 -c 12288 --host 0.0.0.0 --port 8088

# Qwen3.6-27B (estructuración de líneas; solo texto) — puerto 8083
llama-server -m Qwen3.6-27B-Q6_K.gguf -ngl 99 -c 16384 --host 0.0.0.0 --port 8083 --jinja
```

Notas: `--jinja` es necesario en el 27B para `chat_template_kwargs.enable_thinking=false`. Con un slot por servidor la app procesa un PDF a la vez. Comprobación desde la máquina de la app:

```bash
for p in 8086 8083 8088 8089; do curl -s http://192.168.1.194:$p/health; echo; done
curl -s http://192.168.1.194:8086/props | python3 -m json.tool | grep -E "model_path|n_ctx|build_info"
```

En 192.168.1.194 los servidores son unidades systemd `scania-mdl-*` agrupadas en `scania-models.target` (habilitado: arrancan con el sistema).

### 2.1 PP-OCRv6 (geometría de los saltos de página) — puerto 8089

No es un llama-server: detección + reconocimiento de texto de PaddleOCR (no generativo) que devuelve cada renglón con su caja. La fase 2 lo usa solo en facturas de varias páginas, para ver qué renglones del principio de una página continúan el último artículo de la anterior (ver `fix_page_breaks` en `facturas_json.py`). Corre en la **GPU0** junto al 27B (petición expresa del usuario; usa ~2 GB, tope `FLAGS_gpu_memory_limit_mb=2048`), ~1 s por página.

```bash
# en 192.168.1.194, usuario techstudi
python3 -m venv ~/ppocr-gpu-venv
~/ppocr-gpu-venv/bin/pip install paddlepaddle-gpu==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
~/ppocr-gpu-venv/bin/pip install paddleocr==3.7.0 paddlex==3.7.2 fastapi uvicorn
# ~/ppocr-svc/app.py: FastAPI con PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
#   use_textline_orientation=False, device="gpu:0"); GET /health; POST /ocr (cuerpo = PNG) ->
#   {"texts": [...], "boxes": [[x0,y0,x1,y1]...], "scores": [...], "size": [w,h]}. Tras cada página
#   paddle.device.cuda.empty_cache(): el asignador de Paddle retiene bloques libres y, sin esto, se
#   fragmenta hasta el tope (OOM con solo 666 MB en uso real).
```

Unidad `/etc/systemd/system/scania-mdl-ppocr.service` (`WantedBy=scania-models.target`, `PartOf=scania-models.target`): `CUDA_VISIBLE_DEVICES=0`, `FLAGS_gpu_memory_limit_mb=2048`, `After=scania-mdl-text.service` y un `ExecStartPre` que espera hasta 5 min al `/health` del 27B (puerto 8083) para que el 27B reserve su memoria de GPU0 antes (en la unidad el `$` se escribe `$$`). Comprobar: `systemctl is-enabled scania-mdl-ppocr` → `enabled`; `curl -s http://192.168.1.194:8089/health`.

Si el servicio no responde, la fase 2 sigue como antes de este paso (registra «PP-OCRv6 no disponible» y no realinea nada).

## 3. Entorno Python de la app

```bash
cd "PDF Reder"
python3 -m venv .venv
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu   # ~200 MB, sin CUDA
.venv/bin/pip install -r requirements.txt
```

El Python del sistema en Ubuntu/Pop!_OS bloquea `pip install` global (PEP 668): usar siempre el `.venv`.
La primera ejecución descarga `PaddlePaddle/PP-DocLayoutV3_safetensors` desde Hugging Face a la caché del usuario.

## 4. Configuración

- `OCR_MODELS_HOST`: host de los llama-server y de PP-OCRv6 (8089). Por defecto `192.168.1.194`. Si los modelos llegan por túnel local, `export OCR_MODELS_HOST=127.0.0.1`.
- `glmocr.yaml`: configuración del SDK de GLM-OCR. `api_host` se sobreescribe con `OCR_MODELS_HOST`; el resto (prompts, dpi 200, `max_workers` 4, layout en CPU) no necesita cambios.

## 5. Uso por línea de comandos

```bash
.venv/bin/python facturas_json.py factura.pdf otra.pdf -o salida/ --md-dir salida_md/
```

Genera `salida/<nombre>.json` (array de facturas) y, opcionalmente, el Markdown de la fase 1. Solo fase 1:
`.venv/bin/python ocr_pipeline.py factura.pdf -o factura.md`.

## 6. API y demo web

**Acceso (contrato WTCP, `C:\Share\CONTRATO-WTCP.md` §7):** todas las rutas `/api/...` y el WebSocket exigen un
API-Token en la cabecera `X-API-Key`; sin él responden `401`. La página demo `/` solo se abre desde `127.0.0.1`
(y desde ahí no se pide token). `/docs`, `/redoc` y `/openapi.json` son públicos (solo esquemas).

Los tokens viven en `api_keys.json` en la raíz del repo (**fuera de git**; otra ruta con `SCANIA_API_KEYS_FILE`):

```json
{"wtcp-prueba": "<token>", "wtcp-produccion": "<token>"}
```

Crear o rotar un token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`, editar el archivo y
reiniciar la API. Sin `api_keys.json`, la API solo responde desde `127.0.0.1`.

```bash
.venv/bin/python demo_api.py                                  # 0.0.0.0:8091 por defecto (variable DEMO_PORT)
# equivalente: .venv/bin/uvicorn demo_api:app --host 0.0.0.0 --port 8091
```

- Interfaz: `http://127.0.0.1:8091/` (solo local) · Swagger: `/docs` (botón *Authorize* para el token) · ReDoc: `/redoc` · salud de modelos: `/api/health`.
- Entrada: PDF, PNG, JPG/JPEG o BMP, nombre ASCII, hasta 50 MB. Las imágenes se convierten a un PDF de una página.
- Progreso en tiempo real por WebSocket `/ws/jobs/{id}` (la UI cae a sondeo si el WebSocket falla).
- Resultados persistentes en `demo_jobs/<id>/` (PDF, `result.json`, `ocr.md`, `ocr.html`, `meta.json`).
- Parar: `fuser -k 8091/tcp`. Reiniciar: parar y volver a lanzar en una orden separada.

Arranque automático en Windows (al iniciar sesión, sin consola): copiar `deploy\windows\SCANIA-api.vbs`
a la carpeta de Inicio (`Win+R` → `shell:startup`). Llama a `deploy\windows\lanzar_api.ps1`, que usa el
`pythonw` del `.venv`, fija `OCR_MODELS_HOST=192.168.1.194` si no viene del entorno, no arranca una segunda
instancia si el puerto ya escucha y deja los logs en `logs\api.out.log` / `logs\api.err.log`.
Parar en Windows: `Get-NetTCPConnection -LocalPort 8091 -State Listen | % { Stop-Process -Id $_.OwningProcess }`.

Servicio systemd (opcional), `/etc/systemd/system/pdf-reder-demo.service`:

```ini
[Unit]
Description=PDF Reder demo OCR
After=network.target

[Service]
WorkingDirectory=/ruta/a/PDF Reder
Environment=OCR_MODELS_HOST=192.168.1.194
Environment=DEMO_PORT=8091
ExecStart=/ruta/a/PDF Reder/.venv/bin/python demo_api.py
Restart=on-failure
User=<usuario>

[Install]
WantedBy=multi-user.target
```

## 7. Comprobación de la instalación

```bash
export OCR_MODELS_HOST=192.168.1.194
.venv/bin/python facturas_json.py old/31896.pdf -o salida/
# Esperado: 1 factura, línea "2 × 9.05 = 18.10", total 18.10 impreso, revisión LECTURA (líneas vía Qwen2.5-VL).
.venv/bin/python facturas_json.py 31143.pdf -o salida/
# Esperado: 6 facturas (bloques de envío), 5 cuadran, 1 bloque sin línea de total.
```

## 8. Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| `llama-server no responde en http://…:8086` | Servidor apagado o puerto cerrado. Probar `curl /health`. Si van por túnel, `OCR_MODELS_HOST=127.0.0.1`. |
| Trabajos del demo "En cola" mucho tiempo | Hay un PDF en proceso (un slot). Ver qué corre en la barra de progreso o en `/api/jobs`. |
| `Connection aborted` durante la fase 2 | Dos clientes a la vez contra el túnel. Hay reintentos; evitar CLI y demo simultáneos. |
| El 27B devuelve vacío o tarda mucho | Está pensando: comprobar que el servidor tenga `--jinja` (necesario para `enable_thinking=false`). |
| `ModuleNotFoundError: fitz` | Se está usando el Python del sistema. Usar `.venv/bin/python`. |
| Página completa con dígitos "8" por "6" en texto gris | Límite del escaneo y de GLM-OCR; la fase 1 está congelada. Los valores quedan marcados para revisión cuando no cuadran. |
