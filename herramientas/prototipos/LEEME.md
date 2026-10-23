# Prototipos medidos y NO aplicados (2026-10-22)

- `salto_dos_paginas_patch.py` (propuesta del usuario): cuando una factura de varias páginas no cuadra y un salto
  tiene un artículo abierto, el 27B recibe las DOS imágenes de página + su propio JSON de esas páginas y lo corrige.
  Aplicar sobre una copia de `facturas_json.py`: `python salto_dos_paginas_patch.py COPIA/facturas_json.py`.
  Medido con `salto_dos_paginas_replay.py` (genera el replay desde `herramientas/replay_cmp.py`) sobre 134 PDF de
  varias páginas: arregla bien el salto en 32939, 28858 y 32253, pero ninguna factura sale de revisión (tienen otros
  problemas) y en 31202 añade líneas y en 33597 (notas manuscritas) reescribe cifras. Aparcado: volver a medir cuando
  haya casos en que el salto sea el único problema.
- `duplicados_parciales_simula.py`: simula `partial_same_order` sobre el historial de WTCP (ya aplicado en 4cdf98d).

Herramienta de medición: `herramientas/replay_cmp.py MODDIR OUT.json` repite la fase 2 de todos los trabajos con
`modelos.json` usando el `facturas_json.py` de MODDIR (comparar con una pasada del código de producción).
