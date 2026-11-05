"""Idioma de cada factura (pre-análisis global; usuario, 2026-11-05: «todos los WTCP clientes usarán varios idiomas, eso
es incontrolable»). Solo ANOTA `ocr.idioma`: no cambia ninguna regla, cifra ni motivo de revisión. Sirve para medir por
idioma (vigilante, auditoría) y, después, para elegir las etiquetas de ese idioma sin tocar las de los demás.

- Detector: lingua-py (`lingua-language-detector`), determinista, sin red y sin GPU; no es un modelo de lenguaje (el 27B
  cambia sus respuestas con cualquier cambio de entrada y Claude cuesta): https://github.com/pemistahl/lingua-py
- Entrada: el texto de página completa de GLM-OCR de la fase 1 (`res["full_text"]`), limpio de cifras, importes, URL,
  correos y etiquetas HTML: solo cuentan las palabras.
- Idiomas candidatos (`CANDIDATOS`): los que se ven o se esperan en las facturas de WTCP. Restringirlos mejora el acierto
  en textos cortos (documentación de lingua). Un idioma fuera de la lista sale como el más parecido, con poca confianza.
- Por página: idioma + confianza; por factura: el texto de todas sus páginas junto. Sin texto suficiente: "" (no se
  adivina).
"""
import re

from lingua import Language, LanguageDetectorBuilder

CANDIDATOS = (Language.ENGLISH, Language.DUTCH, Language.SPANISH, Language.GERMAN, Language.FRENCH,
              Language.PORTUGUESE, Language.ITALIAN)
MIN_LETRAS = 40  # menos letras que esto (una captura con un número de pedido y un importe): sin idioma
MIN_CONF = 0.70  # por debajo, "" (no se adivina). Medido sobre 2434 documentos del historial: el inglés más bajo dio
# 0.76; el único error visto (34195, factura inglesa de 28 palabras con «FOKKERWEG CURACAO») dio neerlandés con 0.59
_detector = None


def _det():
    global _detector
    if _detector is None:
        _detector = LanguageDetectorBuilder.from_languages(*CANDIDATOS).build()
    return _detector


_LIMPIA = [re.compile(p) for p in (r"<[^>]+>", r"https?://\S+|www\.\S+", r"\S+@\S+", r"&\w+;", r"[^\W\d_]*\d[\w.,/-]*")]


def limpiar(texto):
    t = str(texto or "")
    for r in _LIMPIA:
        t = r.sub(" ", t)
    return " ".join(w for w in re.findall(r"[^\W\d_]{2,}", t))


def detectar(texto):
    """(código ISO 639-1 en minúsculas, confianza 0-1, letras usadas) o ("", 0.0, letras) sin texto suficiente."""
    t = limpiar(texto)
    letras = sum(len(w) for w in t.split())
    if letras < MIN_LETRAS:
        return "", 0.0, letras
    v = _det().compute_language_confidence_values(t)
    if not v:
        return "", 0.0, letras
    conf = round(float(v[0].value), 2)
    if conf < MIN_CONF:
        return "", conf, letras
    return v[0].language.iso_code_639_1.name.lower(), conf, letras


def metodo():
    try:
        from importlib.metadata import version
        return f"lingua {version('lingua-language-detector')}"
    except Exception:  # noqa: BLE001
        return "lingua"


def anotar(facturas, res):
    """Pone `ocr.idioma` en cada factura con el texto de sus páginas (`ocr.paginas` [primera, última], 1 = primera)."""
    textos = {int(k): v for k, v in (res.get("full_text") or {}).items()}  # índice de página desde 0
    por_pagina = {}
    for i, txt in textos.items():
        c, conf, _ = detectar(txt)
        por_pagina[i + 1] = (c, conf)
    m = metodo()
    for f in facturas:
        o = f.setdefault("ocr", {})
        pag = o.get("paginas") or []
        nums = list(range(pag[0], pag[-1] + 1)) if len(pag) == 2 and all(isinstance(x, int) for x in pag) else sorted(por_pagina)
        c, conf, _ = detectar(" ".join(textos.get(n - 1) or "" for n in nums))
        o["idioma"] = {"codigo": c, "confianza": conf,
                       "paginas": {str(n): por_pagina[n][0] for n in nums if n in por_pagina}, "metodo": m}
    return facturas
