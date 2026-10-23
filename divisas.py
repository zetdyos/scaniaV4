"""Importes en la moneda del cliente (decisión del usuario 2026-10-22).

Cada cliente (API-Token, ver demo_api) tiene su moneda; los tokens de WTCP, USD. Una factura en otra moneda (EUR…) se
convierte con la tasa de referencia oficial del Banco Central Europeo del día de la factura (`ocr.fecha_pedido`; el último
día publicado anterior si ese día no hay tasa; sin fecha legible, el día del análisis). Si no se puede obtener la tasa de
ese día (BCE caído, sin red, fecha aún no publicada) se usa la ÚLTIMA disponible (caché local) y su fecha real queda en
`ocr.tipo_cambio.fecha` (decisión del usuario 2026-10-23). La norma de la EEI (15 CFR 30.6(a)(17)) exige el valor en
dólares pero no fija fuente ni fecha de la tasa.

Se convierten todos los importes de salida (líneas, precio unitario, total, cabecera, descuentos, otros cargos y
`estimacion` de la fase 3); `divisa` pasa a ser la del cliente y lo original queda en `ocr` (moneda_factura,
tipo_cambio, total_original; en la línea importe_original y precio_unitario_original). Las comprobaciones de las fases 2
y 3 se hicieron antes, en la moneda impresa. Una moneda sin tasa del BCE (ANG/XCG…) o que no se pudo determinar: no se
convierte y la factura va a LECTURA (no se inventa una tasa)."""
import csv
import io
import re
import time
import zipfile
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
CACHE = Path(__file__).resolve().parent / "divisas_cache" / "eurofxref-hist.csv"
RETRY_SECONDS = 3600  # tras un fallo de descarga no se reintenta en cada factura
_STATE = {"rates": None, "loaded": None, "failed_at": 0.0}


def _parse(text):
    """CSV del BCE -> [(fecha, {moneda: unidades por 1 EUR})] de la más reciente a la más antigua."""
    rows = []
    rd = csv.reader(io.StringIO(text))
    head = [h.strip() for h in next(rd)]
    for r in rd:
        if not r or not r[0].strip():
            continue
        d = date.fromisoformat(r[0].strip())
        vals = {"EUR": Decimal(1)}
        for h, v in zip(head[1:], r[1:]):
            v = v.strip()
            if h and v and v != "N/A":
                vals[h] = Decimal(v)
        rows.append((d, vals))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows


def _rates(log):
    """Tasas del BCE: se descargan una vez al día; si la descarga falla, la caché local (la última disponible)."""
    today = date.today()
    if _STATE["rates"] is not None and _STATE["loaded"] == today:
        return _STATE["rates"]
    if time.time() - _STATE["failed_at"] > RETRY_SECONDS:
        try:
            r = requests.get(ECB_URL, timeout=30)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                text = z.read(z.namelist()[0]).decode("utf-8")
            rates = _parse(text)
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(text, encoding="utf-8")
            _STATE.update(rates=rates, loaded=today)
            return rates
        except Exception as e:  # noqa: BLE001  sin red / BCE caído: la última disponible
            _STATE["failed_at"] = time.time()
            log(f"  divisas: no se pudo descargar la tasa del BCE ({type(e).__name__}); se usa la última guardada")
    if _STATE["rates"] is None and CACHE.exists():
        _STATE["rates"] = _parse(CACHE.read_text(encoding="utf-8"))
    return _STATE["rates"] or []


def rate(src, dst, day, log):
    """(tasa src->dst, fecha de la tasa) del último día publicado <= day con las dos monedas; si day es posterior a todo lo
    publicado, la última disponible. None si alguna moneda no tiene tasa nunca."""
    rows = _rates(log)
    with_both = [(d, v) for d, v in rows if src in v and dst in v]
    if not with_both:
        return None
    d, v = next(((d, v) for d, v in with_both if d <= day), with_both[0])
    return v[dst] / v[src], d


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTHS.update({"ene": 1, "abr": 4, "ago": 8, "dic": 12, "mär": 3, "mai": 5, "okt": 10, "dez": 12, "janv": 1, "févr": 2,
               "mars": 3, "avr": 4, "juin": 6, "juil": 7, "août": 8, "déc": 12})


def parse_date(text, dayfirst=True):
    """Fecha impresa -> date, o None. «September 26, 2026», «26 Sep 2026», «2026-10-20», «20.10.2026», «10/20/2026».
    Con día y mes ambiguos (los dos ≤ 12) decide `dayfirst` (facturas en otra moneda: día primero)."""
    t = (text or "").strip().lower()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
        return _mk(y, mo, d)
    m = re.search(r"([a-zäéû]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
    if m and _month(m.group(1)):
        return _mk(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\.?\s+(?:de\s+)?([a-zäéû]{3,})\.?,?\s+(?:de\s+)?(\d{4})", t)
    if m and _month(m.group(2)):
        return _mk(int(m.group(3)), _month(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)", t)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        if a > 12:
            return _mk(y, b, a)
        if b > 12:
            return _mk(y, a, b)
        return _mk(y, b, a) if dayfirst else _mk(y, a, b)
    return None


# Fecha con etiqueta en el texto de la factura, cuando la fase 2 no dejó fecha_pedido (solo reconoce los formatos de
# tienda conocidos; 26746: «Invoice date: 09.07.2026» de un proveedor europeo). Primero las etiquetas de factura/pedido.
DATE_LABEL_RE = re.compile(r"(?:invoice|order|purchase|bill|document)\s*date|(?:placed|ordered|purchased)\s+on|order\s+placed|date\s+of\s+(?:invoice|order)|fecha(?:\s+de\s+(?:factura|pedido|emisi[oó]n))?"
                           r"|rechnungsdatum|bestelldatum|datum|date\s+de\s+(?:facture|commande)|\bdate\b", re.I)


def labeled_date(text, dayfirst=True, today=None):
    """Primera fecha legible que sigue a una etiqueta de fecha (en el mismo renglón), en el orden de DATE_LABEL_RE. Una
    fecha posterior a hoy no es la de la factura (33409: «hasta el 21 Nov. 2026», validez de una oferta)."""
    today = today or date.today()
    best = None
    for line in (text or "").splitlines():
        m = DATE_LABEL_RE.search(line)
        if not m:
            continue
        d = parse_date(line[m.end():m.end() + 60], dayfirst)
        if d and d <= today:
            rank = 0 if re.match(r"(?:invoice|order|purchase|bill|document|placed|ordered|purchased|fecha|rechnungs|bestell|date\s+(?:of|de))", m.group(0), re.I) else 1
            if best is None or rank < best[0]:
                best = (rank, d)
            if rank == 0:
                break
    if best:
        return best[1]
    # sin etiqueta (30751: «28 Sept 2026» en la cabecera): la primera fecha legible de la factura, más cerca de la real que
    # el día del análisis
    for line in (text or "").splitlines():
        d = parse_date(line, dayfirst)
        if d and d <= today:
            return d
    return None


def invoice_text(markdown, paginas):
    """Texto de las páginas [a, b] de la factura en el Markdown de la fase 1 («## Página N»)."""
    if not markdown or not paginas:
        return ""
    parts = re.split(r"(?m)^## Página (\d+)\s*$", markdown)
    a, b = paginas
    return "\n".join(parts[i + 1] for i in range(1, len(parts) - 1, 2) if a <= int(parts[i]) <= b)


def _month(word):
    w = word.lower()
    return MONTHS.get(w[:4]) or MONTHS.get(w[:3])


def _mk(y, mo, d):
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _conv(v, r):
    if not str(v or "").strip():
        return v
    return str((Decimal(str(v)) * r).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def convert_invoice(doc, dst, log, today=None, markdown=""):
    """Pasa los importes de una factura a la moneda `dst`. Devuelve True si convirtió. `markdown` (fase 1) sirve para
    hallar la fecha con etiqueta cuando la fase 2 no dejó fecha_pedido."""
    o = doc["ocr"]
    cur = {l["divisa"] for l in doc["lineas"] if l.get("divisa")}
    o["moneda_factura"] = next(iter(cur)) if len(cur) == 1 else ""
    o["tipo_cambio"] = None
    if not doc["lineas"] or cur == {dst}:
        return False
    from facturas_json import mark_review
    if len(cur) != 1:
        what = "could not be determined" if not cur else "is not unique (" + ", ".join(sorted(cur)) + ")"
        mark_review(doc, f"the invoice currency {what}: amounts not converted to {dst}; review against the paper")
        return False
    src = o["moneda_factura"]
    day = (parse_date(o.get("fecha_pedido", ""), dayfirst=src != "USD")
           or labeled_date(invoice_text(markdown, o.get("paginas")), dayfirst=src != "USD"))
    when = day or today or date.today()
    found = rate(src, dst, when, log)
    if found is None:
        mark_review(doc, f"amounts in {src} could not be converted to {dst}: no official rate")
        return False
    r, rate_day = found
    o["tipo_cambio"] = {"de": src, "a": dst, "tasa": str(r.quantize(Decimal("0.000001"))), "fecha": rate_day.isoformat(),
                        "fuente": "BCE", "fecha_factura": day.isoformat() if day else ""}
    o["total_original"] = doc["total"]
    doc["total"] = _conv(doc["total"], r)
    for k in ("subtotal_articulos", "envio", "impuestos", "total_antes_impuestos"):
        if k in o:
            o[k] = _conv(o[k], r)
    for k in ("descuentos", "other_charges"):
        for x in o.get(k) or []:
            x["importe"] = _conv(x.get("importe"), r)
    for l in doc["lineas"]:
        lo = l["ocr"]
        lo["importe_original"], lo["precio_unitario_original"] = l["importe_linea"], lo.get("precio_unitario", "")
        l["importe_linea"] = _conv(l["importe_linea"], r)
        lo["precio_unitario"] = _conv(lo.get("precio_unitario", ""), r)
        if l.get("estimacion"):
            for k in ("importe_estimado", "maximo"):
                l["estimacion"][k] = _conv(l["estimacion"][k], r)
        l["divisa"] = dst
    log(f"  factura {doc['id']}: {src} -> {dst} a {o['tipo_cambio']['tasa']} (BCE {rate_day.isoformat()}"
        f"{'' if day else ', sin fecha de factura legible'})")
    return True


def convert_all(out, dst, log, markdown=""):
    for doc in out:
        convert_invoice(doc, dst, log, markdown=markdown)
    return out
