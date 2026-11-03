# Herramientas de trabajo (no forman parte de la API)

Uso desde la raíz del repo con el `.venv`. Ninguna toca producción; las que usan modelos toman `modelos_lock`,
así que **solo se lanzan con WTCP parado** (si no, retrasan sus trabajos: 2026-10-20, 32234 esperó 51 min).

| Script | Qué hace |
|---|---|
| `vigilar.py` | SOLO cuenta (no audita: la auditoría por factura contra la imagen la hace Claude con `auditar.py`, ver CLAUDE.md «Vigilancia de un lote de WTCP»). Vigilante de la API (:8091) y de los modelos de .194: trabajos nuevos/listos, facturas a revisión con su motivo, comparación con el último resultado anterior a `DEPLOY`, regla de cantidad (el 27B decide), PP-OCRv6 caído. Guarda su recuento en `verdad/vigilante_contador.json` tras cada trabajo; `VIGILAR_CONTADOR=continuar` sigue desde ahí. Clasificaciones (desde 2026-11-02): cada 30 min (`VIGILAR_CLASIF_CADA` s) llamadas de WTCP a `POST /api/classify` con su código, descripciones nuevas, mandadas al clasificador, entregadas (segura/dudosa/no_clasificado) y en curso; al momento, un código distinto de 200, un error del clasificador en una línea o el clasificador caído (lee `logs/api.out.log`, `clasif_jobs/en_curso.json` y `/api/health` y `/api/lines/{id}` de :8092). |
| `chequeo_arranque.py` | SOLO lee. Tras un reinicio de .194, de este PC o de la sesión: las dos GPU, cada modelo con memoria gráfica y en su tarjeta (`--ssh`: la real de `nvidia-smi` por bus PCI, y si la métrica dice la misma), /health de los cuatro puertos, el 27B con n_ctx 16384 y visión, API sin `.no_aceptar`, clasificador vivo, restos de monitores, clasificaciones en curso y contador del vigilante. Termina con «todo bien» o la lista de fallos (2026-11-02: tras la caída de la GPU0 los modelos daban /health 200 en CPU). |
| `regresion_head.py pdf,pdf` | Fase 2 del último commit (`git show HEAD:facturas_json.py > herramientas/facturas_json_head.py` y `git show HEAD:saltos.py > herramientas/saltos_head.py`) frente al árbol de trabajo, sobre la misma lectura (ocr.md) y las mismas respuestas del 27B. |
| `regresion_guardado.py DESDE [pdf,pdf]` | Fase 2 del árbol de trabajo frente al `result.json` guardado de los trabajos desde `DESDE` (yyyymmdd-hhmm): válido solo si esos trabajos los procesó el código de HEAD. |
| `regresion_replay.py [pdf,pdf] [v]` | Fase 2 del commit frente al árbol **sin modelos**: la respuesta del 27B se reconstruye desde result.json (aproximada: sin signo negativo, sin líneas descartadas; VL y PP-OCRv6 apagados). Solo cuenta los documentos donde la versión del commit reproduce lo guardado. |
| `repetir_trabajo.py JOB [JOB…]` | Repite la fase 2 de un trabajo con las respuestas guardadas en `modelos.json` (27B, VL, PP-OCRv6, regiones): **exacto y sin GPU**. Solo trabajos desde el 2026-10-21 08:43. |
| `cache_ppocr.py lista.txt` | Guarda las cajas de PP-OCRv6 de cada página en `ppocr.json` del trabajo (una vez, ~1 s/página; solo PP-OCRv6). `regresion_replay.py` y `medir_saltos_bloques.py` las leen de ahí. |
| `medir_saltos.py [-o out.json] [PDF…]` | Saltos de página SOLO con geometría (sin modelos): `saltos.continuation` sobre la geometría guardada de cada trabajo (modelos.json / ppocr.json), por factura y corte; compara con `saltos_conocidos.txt` (continuaciones reales y falsos conocidos) e imprime histogramas de motivos y estilos. Medido 2026-10-23: 472 documentos, 814 cortes. |
| `completar_modelos.py OUT JOB…` | Trabajos antiguos sin `modelos.json`: corre la fase 1 y 2 REALES (bajo el bloqueo; solo con WTCP parado) y escribe OUT/<job>/{meta.json, ocr.md, modelos.json, result.json} sin tocar demo_jobs; desde entonces el documento entra en la repetición exacta sin GPU (`replay_full.py --root OUT`). |
| `fotos_verdad.py dir lista.txt` / `calibra_fotos.py dir variante…` | Conjunto de calibración de la cantidad en la foto con el localizador de producción (recortes, hojas de contacto, verdad.json a mano) y medida de variantes de Qwen2.5-VL (respuestas guardadas por variante). |
| `badge_crops.py` / `calibra_badge.py` | Recortes de fotos de Amazon y calibración de Qwen2.5-VL para la cantidad sobre la foto (53/53). |

Regla del usuario: medir solo en las facturas donde el cambio puede aplicar, no en las que no pueden cambiar.

Límite de `regresion_head.py`: reconstruye la fase 1 solo desde `ocr.md` (sin regiones, `check_shipments` vacío), así
que en Amazon de escritorio no salen los bloques de envío (31143 da 1 factura en vez de 6, en HEAD y en el árbol). Un
«IGUAL» ahí no prueba la vía de envíos; esa vía se comprueba con el CLI completo o contra el `result.json` guardado.
