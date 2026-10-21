# Herramientas de trabajo (no forman parte de la API)

Uso desde la raíz del repo con el `.venv`. Ninguna toca producción; las que usan modelos toman `modelos_lock`,
así que **solo se lanzan con WTCP parado** (si no, retrasan sus trabajos: 2026-10-20, 32234 esperó 51 min).

| Script | Qué hace |
|---|---|
| `vigilar.py` | Vigilante de la API (:8091) y de los modelos de .194: trabajos nuevos/listos, facturas a revisión con su motivo, comparación con el último resultado anterior a `DEPLOY`, regla de cantidad (el 27B decide), PP-OCRv6 caído. |
| `regresion_head.py pdf,pdf` | Fase 2 del último commit (`git show HEAD:facturas_json.py > herramientas/facturas_json_head.py`) frente al árbol de trabajo, sobre la misma lectura (ocr.md) y las mismas respuestas del 27B. |
| `regresion_guardado.py DESDE [pdf,pdf]` | Fase 2 del árbol de trabajo frente al `result.json` guardado de los trabajos desde `DESDE` (yyyymmdd-hhmm): válido solo si esos trabajos los procesó el código de HEAD. |
| `regresion_replay.py [pdf,pdf] [v]` | Fase 2 del commit frente al árbol **sin modelos**: la respuesta del 27B se reconstruye desde result.json (aproximada: sin signo negativo, sin líneas descartadas; VL y PP-OCRv6 apagados). Solo cuenta los documentos donde la versión del commit reproduce lo guardado. |
| `medir_saltos_final.py lista.txt` | Detección de continuaciones en saltos de página (PP-OCRv6, sin 27B) en una lista de PDF. |
| `cache_ppocr.py lista.txt` | Guarda las cajas de PP-OCRv6 de cada página en `ppocr.json` del trabajo (una vez, ~1 s/página; solo PP-OCRv6). `regresion_replay.py` y `medir_saltos_bloques.py` las leen de ahí. |
| `medir_saltos_bloques.py lista.txt` | Saltos de página en diseños de bloques (Amazon con fotos): continuaciones detectadas con la geometría guardada, sin modelos. |
| `badge_crops.py` / `calibra_badge.py` | Recortes de fotos de Amazon y calibración de Qwen2.5-VL para la cantidad sobre la foto (53/53). |

Regla del usuario: medir solo en las facturas donde el cambio puede aplicar, no en las que no pueden cambiar.

Límite de `regresion_head.py`: reconstruye la fase 1 solo desde `ocr.md` (sin regiones, `check_shipments` vacío), así
que en Amazon de escritorio no salen los bloques de envío (31143 da 1 factura en vez de 6, en HEAD y en el árbol). Un
«IGUAL» ahí no prueba la vía de envíos; esa vía se comprueba con el CLI completo o contra el `result.json` guardado.
