# PDF Reder — OCR de facturas (LERRIT)

Lee PDF escaneados de recibos de compra (Amazon, Temu, …) y entrega un JSON estándar por factura
para los pasos siguientes (clasificación arancelaria, que **todavía no** forma parte de este repo).
Todo en español. Sin GPU local: los modelos corren en llama-server remotos.

## Mapa del repo

| Archivo | Rol |
|---|---|
| `ocr.py` | Lectura de página completa con GLM-OCR ("Text Recognition:"). Define `MODELS_HOST`/`DEFAULT_SERVER`. |
| `ocr_pipeline.py` | **Fase 1 (CONGELADA).** Página completa + regiones (SDK glmocr, layout PP-DocLayoutV3 en CPU) + verificación de importes y envíos + rescate de precios con Qwen2.5-VL. Expone `run(pdf, ...)`. |
| `glmocr.yaml` | Configuración del SDK glmocr (copia del `config.yaml` del paquete con cambios mínimos; el cargador NO fusiona YAML parciales). |
| `facturas_json.py` | **Fase 2.** Divide cada PDF en facturas, construye el JSON del esquema acordado, enriquece con `ocr` y aplica las reglas de evidencia. Expone `build_all(res)`. |
| `demo_api.py` | Demo FastAPI para evaluación visual: UI en `/`, Swagger `/docs`, ReDoc `/redoc`, WebSocket `/ws/jobs/{id}`. Trabajos en `demo_jobs/`. |
| `salida/`, `salida_md/` | Salidas del CLI (JSON por PDF y Markdown de la fase 1). |
| `old/`, `*.pdf` | PDF de prueba del usuario. `Enigdocu.mdb` ya no está en el repo. |
| `INSTALL.md` | Instalación y despliegue. |

## Reglas que no se negocian (pedidas por el usuario)

1. **La fase 1 está congelada.** No modificar `ocr_pipeline.py`, `ocr.py` ni `glmocr.yaml` salvo petición explícita. La fase 2 solo consume `ocr_pipeline.run()`. Cambios de host/config son la única excepción aceptada hasta ahora.
2. **No arrancar servidores de modelos en esta máquina.** Los llama-server viven en el host de modelos (`192.168.1.194`, variable `OCR_MODELS_HOST`): GLM-OCR en 8080, Qwen3.6-27B en 8083 (cargado SIN visión), Qwen2.5-VL-7B en 8088. Antes de usarlos, comprobar `/health` y `/props`.
3. **No experimentar al azar con prompts ni resoluciones.** Cada decisión debe apoyarse en documentación (tarjeta del modelo, repo oficial, código de llama.cpp). Lo verificado está en "Hechos documentados".
4. **No inventar valores.** Todo lo que venga de un modelo de lenguaje se contrasta con el texto OCR; lo que no tenga respaldo se marca (`evidencia: modelo`) y la línea no se da por verificada.
5. **Los números manuscritos no interesan.**

## Qué es una factura

- Amazon formato de escritorio ("Details for Order #…"): **cada bloque de envío** ("Not Yet Shipped", "Shipped on …", con su "Total for This Shipment") es una factura. Los datos del pedido (referencia, fecha, pago, dirección, total del pedido) se repiten en cada una bajo `ocr`.
- Amazon app móvil (un pedido por página, "Order # …", precios con coma decimal) y Temu ("Order ID: PO-…"): una factura por pedido.
- `id` es secuencial dentro del PDF; `referencia` es el número de pedido impreso (campo extra respecto al esquema original, aceptado por el usuario).

## Esquema de salida

Claves originales del usuario, intactas: `id, estado, motivo_revision, mismatch, total, total_is_printed, version_catalogo, result_version, lineas[]` con `line_kind, seq, descripcion, cantidad, importe_linea, divisa, codigo, hs6, estado, confianza_modelo, candidatos[]`. Los campos arancelarios quedan vacíos.
Añadidos: `referencia` y los objetos `ocr` (factura y línea; ver docstring de `facturas_json.py`). `importe_linea` = importe total de la línea; las líneas se cuadran contra el **último** "Item(s) Subtotal" (sin envío ni impuestos).

## Hechos documentados (no volver a investigar)

- GLM-OCR solo admite tres prompts: `Text Recognition:`, `Table Recognition:`, `Formula Recognition:`. Instrucciones libres se ignoran. Página completa pierde columnas de precio; regiones las recuperan pero fallan en cajas cortadas por salto de página (issues 240/241 del repo oficial). Por eso la fase 1 combina ambas y verifica.
- llama.cpp limita GLM-OCR y Qwen2.5-VL a 4096 fichas de imagen (28×28 px) y reescala bilineal por encima: una Letter a 200 dpi se degrada; `fit_dpi` calcula el dpi por página. Qwen-VL pierde precisión por debajo de 1024 fichas.
- llama-server ignora `repetition_penalty` (solo `repeat_penalty`). Para JSON por gramática usar `response_format: {"type":"json_schema","json_schema":{"name":…,"schema":{…}}}` (el README muestra `schema` suelto y el servidor lo ignora en silencio).
- Qwen3.6-27B piensa por defecto: para extracción `chat_template_kwargs: {"enable_thinking": false}`, temperatura 0, esquema JSON. Qwen2.5-VL: prompt de extracción clave-valor del cookbook oficial (`Extract the key-value information in the format:{…}`), `repeat_penalty` 1.05.
- Clasificación arancelaria: ningún LLM solo es fiable (ATLAS, HSGraphAgent, marco de consenso); cuando toque, será recuperación sobre el catálogo oficial + LLM como juez. El usuario la dejó para después.

## Cómo correr

```bash
export OCR_MODELS_HOST=192.168.1.194            # o 127.0.0.1 si los modelos llegan por túnel
.venv/bin/python ocr_pipeline.py doc.pdf -o doc.md          # fase 1 sola
.venv/bin/python facturas_json.py doc.pdf -o salida/        # fase 1 + 2 → salida/doc.json
.venv/bin/python demo_api.py                                # demo en 0.0.0.0:8091 (DEMO_PORT)
fuser -k 8091/tcp                                           # parar el demo (NO usar pkill -f con el texto del relanzamiento en la misma línea: se mata a sí mismo)
```

Pruebas de regresión manuales: `31143.pdf` (Amazon escritorio → 6 facturas, 5 cuadran, 1 bloque sin total), `old/31896.pdf` (Temu → 1 factura, 2 × 9.05 = 18.10 vía Qwen2.5-VL), `old/31112-15-feas.pdf` (app móvil → 15 facturas, 3 a revisar).

## Convenciones de trabajo

- Explicar al usuario en español, con decisiones basadas en documentación citada.
- Antes de tocar la fase 2, correr las tres regresiones. Antes de proponer tocar la fase 1, preguntar.
- Un trabajo a la vez contra los modelos (1 slot): no correr CLI y demo a la vez contra el mismo host.
