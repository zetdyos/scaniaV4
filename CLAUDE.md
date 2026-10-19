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
| `demo_api.py` | API FastAPI (cliente: WTCP) + demo: UI en `/` (solo 127.0.0.1), Swagger `/docs`, ReDoc `/redoc`, WebSocket `/ws/jobs/{id}`. `/api/...` exige `X-API-Key` (tokens en `api_keys.json`, fuera de git). Acepta PDF, PNG, JPG/JPEG, BMP. Trabajos en `demo_jobs/`. |
| `docs/CONTRATO-WTCP.md` | Contrato con WTCP: qué rutas y campos lee el cliente. **Cualquier cambio de esquema se contrasta con él.** |
| `deploy/windows/` | Arranque de la API al iniciar sesión en Windows (`SCANIA-api.vbs` → `lanzar_api.ps1`). |
| `salida/`, `salida_md/` | Salidas del CLI (JSON por PDF y Markdown de la fase 1). |
| `old/`, `*.pdf` | PDF de prueba del usuario. `Enigdocu.mdb` ya no está en el repo. |
| `INSTALL.md` | Instalación y despliegue. |

## Reglas que no se negocian (pedidas por el usuario)

1. **La fase 1 está congelada.** No modificar `ocr_pipeline.py`, `ocr.py` ni `glmocr.yaml` salvo petición explícita. La fase 2 solo consume `ocr_pipeline.run()`. Cambios de host/config son la única excepción aceptada hasta ahora.
2. **No arrancar servidores de modelos en esta máquina.** Los llama-server viven en el host de modelos (`192.168.1.194`, variable `OCR_MODELS_HOST`): GLM-OCR en 8086, Qwen3.6-27B en 8083 (cargado SIN visión), Qwen2.5-VL-7B en 8088. Antes de usarlos, comprobar `/health` y `/props`.
3. **No experimentar al azar con prompts ni resoluciones.** Cada decisión debe apoyarse en documentación (tarjeta del modelo, repo oficial, código de llama.cpp). Lo verificado está en "Hechos documentados".
4. **No inventar valores.** Todo lo que venga de un modelo de lenguaje se contrasta con el texto OCR; lo que no tenga respaldo se marca (`evidencia: modelo`) y la línea no se da por verificada.
5. **Los números manuscritos no interesan.**

## Qué es una factura

- Amazon formato de escritorio ("Details for Order #…"): **cada bloque de envío** ("Not Yet Shipped", "Shipped on …", con su "Total for This Shipment") es una factura. Los datos del pedido (referencia, fecha, pago, dirección, total del pedido) se repiten en cada una bajo `ocr`.
- Amazon app móvil (un pedido por página, "Order # …", precios con coma decimal) y Temu ("Order ID: PO-…"): una factura por pedido.
- `id` es secuencial dentro del PDF; `referencia` es el número de pedido impreso (campo extra respecto al esquema original, aceptado por el usuario).

## Esquema de salida

Claves originales del usuario, intactas: `id, estado, motivo_revision, mismatch, total, total_is_printed, version_catalogo, result_version, lineas[]` con `line_kind, seq, descripcion, cantidad, importe_linea, divisa, codigo, hs6, estado, confianza_modelo, candidatos[]`. Los campos arancelarios quedan vacíos.
Añadidos: `referencia`, los objetos `ocr` (factura y línea; ver docstring de `facturas_json.py`), `ocr.other_charges` (lista `{concepto, importe}`; en la vía del 27B sale de sus líneas `other_charges` con importe literal en el OCR) y, por el contrato WTCP, `confianza`, `canal`, `calibrado` (`null`) en cada línea.
- `line_kind`: en la vía del 27B el propio modelo clasifica cada línea en `goods`, `shipping`, `tax`, `discount`, `subtotal_or_total`, `other_charges` o `payment` (pagado/pendiente, no es cargo) (enum en el esquema JSON; el prompt pide **todas** las líneas con importe, también las del resumen, en cualquier idioma; definición general en el prompt, **sin listas de palabras**). Solo las `goods` cuentan para sumas, rescate VL y evidencia; las demás se quedan en `lineas` y WTCP no las declara. Medido el 2026-10-18 sobre 55 documentos: ningún producto clasificado como no-`goods`. `importe_linea` = importe total de la línea; las líneas se cuadran contra el **último** "Item(s) Subtotal" (sin envío ni impuestos). Sin esa etiqueta (vía del 27B, cuadre calculado tras su respuesta): vale su línea `subtotal_or_total` que iguale la suma de artículos, o la cuenta de cabecera artículos + envío + impuestos + otros cargos − descuentos = total. Cabecera vacía por etiquetas → se rellena con las líneas no-`goods` del 27B con importe literal en el OCR. Cantidades (vía del 27B), siempre con respaldo literal: cantidad impresa en la misma fila de tabla que la descripción (si hay pedida/enviada, la que cumple cantidad × precio = importe), al inicio del renglón («2 NILFISK…») o «Qty/Quantity N»; si no está impresa, importe ÷ precio (ambos literales) cuando da entero; un único artículo + «Item(s) Subtotal» impreso → subtotal ÷ precio. Importe que falta: cantidad × precio unitario literal (evidencia `calculado`, la línea no queda verificada); si falta el importe de **un solo** artículo, el único valor literal del OCR que cuadra el resumen impreso y no es importe de envío/impuestos/descuento/otros cargos/pago (medido: 4 facturas, todas correctas). Precios con 3–5 decimales cuentan como literales, con margen de redondeo qty×0,005 (< medio precio). Cabecera: entre etiqueta, 27B y «sin cargo», y quitando descuentos/cargos, la primera combinación que cuadra el resumen; lo que no entra es informativo. Total: el de la última página con respaldo en el OCR. Rescate VL: solo si el 27B halló artículos; descarta lo que el 27B clasificó como no-artículo; cuadra contra total − cargos. Factura sin artículos → nunca sin revisión. Línea de «artículo» que es el resumen de los demás (eBay «1 item/1 object $X», fila con el total): se aparta (queda como subtotal_or_total) solo si su importe = suma de los demás y sin ella cuadra con lo impreso y con ella no; con empate, la de descripción claramente más corta; si no, revisión. Medido sobre 581 facturas: se aplica en 8, todas correctas.
- `motivo_revision`: `ESTRUCTURA` (sin marcador de pedido y sin líneas) > `LECTURA` > `""`. `CLASIFICACION` reservado para la fase de clasificación. `""` significa que WTCP usa la factura **sin revisión humana**.
- `mismatch` (y `ocr.incidencias`) en **inglés**: WTCP lo muestra tal cual. `ocr.descuentos[].importe` en positivo.
- Clasificación arancelaria: fase futura e interna de V4, distinta de V3. Hasta entonces solo se respeta la estructura de esos campos; no crear endpoints ni lógica de clasificación.

## Extracción de líneas (fase 2): no depende del tamaño del documento

Orden: bloques de envío de Amazon (`envios`) → Qwen3.6-27B **página por página** (`llm`) con `max_tokens = n_ctx (/props) − prompt (/tokenize)` → rescate con Qwen2.5-VL. Si una respuesta del 27B se corta (`finish_reason: length`) o no es JSON, esa página queda para revisión (`LECTURA`), nunca un error del trabajo.
Origen del problema que lo motivó: con `max_tokens` 4096 fijo, una factura Shein de 54 artículos se cortaba siempre (`JSONDecodeError`).

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

Pruebas de regresión manuales: `31143.pdf` (Amazon escritorio → 6 facturas, 4 sin revisión; 2 en LECTURA: bloque sin total y precio rescatado con Qwen2.5-VL), `old/31896.pdf` (Temu → 1 factura, 2 × 9.05 = 18.10 vía Qwen2.5-VL), `old/31112-15-feas.pdf` (app móvil → 15 facturas, 3 a revisar), `d-29484.pdf` (Shein 4 págs, 54 artículos: antes JSONDecodeError por max_tokens 4096). Los PDF de `old/` no están en esta máquina; los demás están en `demo_jobs/`.
Método (pedido por el usuario: "no arregles esto y rompas otra cosa"): cachear `ocr_pipeline.run()` por documento y comparar la fase 2 del commit (`git show HEAD:facturas_json.py`) con la nueva sobre la misma caché; explicar cada diferencia. **No añadir reglas motivadas por una sola factura**: si algo no cuadra, va a LECTURA.

## Convenciones de trabajo

- Explicar al usuario en español, con decisiones basadas en documentación citada.
- Antes de tocar la fase 2, correr las tres regresiones. Antes de proponer tocar la fase 1, preguntar.
- Un trabajo a la vez contra los modelos (1 slot): la API encola (un hilo) y `facturas_json.modelos_lock()` (bloqueo del SO sobre `.modelos.lock`) hace que API y CLI se esperen entre sí. El CLI suelto de `ocr_pipeline.py` NO toma el bloqueo: no correrlo mientras la API trabaja.
