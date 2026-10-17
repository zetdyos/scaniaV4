"""
Extrae todo el texto de un PDF (incluidas tablas) usando GLM-OCR servido por llama.cpp.

Cada página se renderiza como imagen y se envía al modelo de visión, que devuelve
el contenido en Markdown (las tablas salen como tablas Markdown/HTML).

Uso:
    py pdf_ocr.py documento.pdf                 # imprime el texto por consola
    py pdf_ocr.py documento.pdf -o salida.md    # además lo guarda en un archivo
    py pdf_ocr.py documento.pdf --pages 1-3,5   # solo algunas páginas
    py pdf_ocr.py documento.pdf --dpi 200       # más resolución para letra chica

Requiere: pip install pymupdf requests
Servidor (debe estar iniciado antes de correr el script):
    llama-server -m GLM-OCR-Q8_0.gguf --mmproj mmproj-GLM-OCR-Q8_0.gguf -ngl 99 -c 16384 --port 8086
"""

import argparse
import base64
import os
import sys

import fitz  # PyMuPDF
import requests

# Host donde corren los llama-server (GLM-OCR en 8086, Qwen3.6-27B en 8083, Qwen2.5-VL en 8088).
# Se puede cambiar con la variable de entorno OCR_MODELS_HOST (p. ej. 127.0.0.1 si van por túnel).
MODELS_HOST = os.environ.get("OCR_MODELS_HOST", "192.168.1.194")
DEFAULT_SERVER = f"http://{MODELS_HOST}:8086"

PROMPTS = {
    # Prompts nativos de GLM-OCR
    "text": "Text Recognition:",
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
}


def server_ok(server):
    try:
        return requests.get(f"{server}/health", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def parse_pages(spec, total):
    """'1-3,5' -> [0,1,2,4] (índices base 0)."""
    if not spec:
        return list(range(total))
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a) - 1, min(int(b), total)))
        else:
            pages.append(int(part) - 1)
    return [p for p in pages if 0 <= p < total]


def render_page(page, dpi):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


def ocr_image(server, image_b64, prompt, max_tokens, timeout):
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    r = requests.post(f"{server}/v1/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def page_has_tables(page):
    try:
        return len(page.find_tables().tables) > 0
    except Exception:
        return False


def extract_pdf(path, server, dpi, pages_spec, mode, max_tokens, timeout):
    doc = fitz.open(path)
    results = []
    for idx in parse_pages(pages_spec, doc.page_count):
        page = doc[idx]
        print(f"[{idx + 1}/{doc.page_count}] procesando...", file=sys.stderr)
        img = render_page(page, dpi)

        text = ocr_image(server, img, PROMPTS["text"], max_tokens, timeout)

        # En modo auto, si PyMuPDF detecta tablas en la página, se pide además
        # el reconocimiento de tabla para tener la estructura fiel.
        if mode == "table" or (mode == "auto" and page_has_tables(page)):
            table = ocr_image(server, img, PROMPTS["table"], max_tokens, timeout)
            text = f"{text}\n\n### Tablas\n\n{table}"

        results.append(f"## Página {idx + 1}\n\n{text}")
    doc.close()
    return "\n\n---\n\n".join(results)


def main():
    ap = argparse.ArgumentParser(description="OCR de PDF con GLM-OCR (llama.cpp)")
    ap.add_argument("pdf", help="ruta al PDF")
    ap.add_argument("-o", "--output", help="archivo de salida (.md / .txt)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"URL de llama-server (default {DEFAULT_SERVER})")
    ap.add_argument("--dpi", type=int, default=150, help="resolución de render (default 150)")
    ap.add_argument("--pages", help="páginas a procesar, ej: 1-3,5")
    ap.add_argument("--mode", choices=["auto", "text", "table"], default="auto",
                    help="auto: texto + tablas si se detectan; text: solo texto; table: siempre texto + tablas")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=600, help="timeout por página en segundos")
    args = ap.parse_args()

    if not server_ok(args.server):
        sys.exit(f"llama-server no responde en {args.server}; iniciálo antes de correr el script.")

    result = extract_pdf(args.pdf, args.server, args.dpi, args.pages, args.mode, args.max_tokens, args.timeout)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Guardado en {args.output}", file=sys.stderr)

    sys.stdout.reconfigure(encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
