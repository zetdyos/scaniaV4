# PDF Reder — OCR de facturas

Lee PDF escaneados de recibos de compra (Amazon, Temu, …) con GLM-OCR en llama.cpp, verifica los importes
y entrega un JSON estándar por factura. Incluye un demo web (FastAPI) para evaluación visual.

- Instalación y despliegue: [INSTALL.md](INSTALL.md)
- Mapa del repo, reglas y hechos documentados: [CLAUDE.md](CLAUDE.md)
- Fase 1 (OCR): `ocr_pipeline.py` · Fase 2 (facturas → JSON): `facturas_json.py` · Demo: `demo_api.py` (puerto 8091)
