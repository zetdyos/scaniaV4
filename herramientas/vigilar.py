"""Vigila la API de SCANIA V4 y los slots de .194 (solo lectura). Una línea por evento."""
import json, time, urllib.request

API = "http://127.0.0.1:8091/api/jobs"
SLOTS = {"GLM": 8086, "27B": 8083}  # el VL no: duerme a los 30 s (--sleep-idle-seconds) y GET /slots lo mantiene despierto
seen = {}
first = True
last_busy_report = 0


JOBS = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "demo_jobs")
DEPLOY = "20261023-0020"  # despliegue de deb751b (fase 3 y moneda del cliente): los trabajos anteriores son la referencia
count = {"docs": 0, "facturas": 0, "revision": 0, "imagen": 0, "estimadas": 0, "sin_estimar": 0, "convertidas": 0}  # desde que arranca el vigilante
count.update(json.loads(__import__("os").environ.get("VIGILAR_CONTADOR") or "{}"))  # seguir un recuento anterior


def summary(job_id):
    from pathlib import Path
    r = json.loads((Path(JOBS) / job_id / "result.json").read_text(encoding="utf-8"))
    return [(i["motivo_revision"] or "ok", i["ocr"].get("envio", ""), i["ocr"].get("impuestos", ""), i["mismatch"][:90]) for i in r]


def compare(j):
    """Resultado nuevo frente a la última vez que se procesó el mismo PDF antes del despliegue."""
    from pathlib import Path
    try:
        new = summary(j["id"])
        prev = [d.name for d in Path(JOBS).iterdir() if d.name < DEPLOY and (d / "result.json").exists()
                and json.loads((d / "meta.json").read_text(encoding="utf-8")).get("pdf") == j["pdf"]]
        if not prev:
            return " | sin referencia previa"
        old = summary(max(prev))
        txt = []
        for k, n in enumerate(new):
            o = old[k] if k < len(old) else None
            mark = "IGUAL" if o and o[0] == n[0] else ("MEJORA" if o and o[0] != "ok" and n[0] == "ok" else "EMPEORA" if o and o[0] == "ok" else "CAMBIA")
            txt.append(f"#{k} {o[0] if o else '?'}->{n[0]} {mark} env={n[1]} imp={n[2]}" + (f" [{n[3]}]" if n[0] != "ok" else ""))
        if len(old) != len(new):
            txt.append(f"facturas {len(old)}->{len(new)}")
        return " | " + "; ".join(txt)
    except Exception as e:  # noqa: BLE001
        return f" | comparación falló: {e}"


def get(url, t=4):
    with urllib.request.urlopen(url, timeout=t) as r:
        return json.load(r)


while True:
    now = time.strftime("%H:%M:%S")
    try:
        jobs = get(API)
    except Exception as e:  # noqa: BLE001
        print(f"{now} API NO RESPONDE: {e}", flush=True)
        time.sleep(10)
        continue
    running = [j for j in jobs if j["state"] == "running"]
    queued = [j for j in jobs if j["state"] == "queued"]
    if len(running) > 1:
        print(f"{now} ALERTA: {len(running)} trabajos en running a la vez: {[j['id'] for j in running]}", flush=True)
    for j in jobs:
        prev = seen.get(j["id"])
        st = j["state"]
        if first:
            seen[j["id"]] = st
            continue
        if prev == st:
            continue
        seen[j["id"]] = st
        if st == "queued":
            print(f"{now} NUEVO {j['pdf']} ({j['id']}) en cola; cola={len(queued)}", flush=True)
        elif st == "running":
            print(f"{now} EMPIEZA {j['pdf']}; cola={len(queued)}", flush=True)
        elif st == "done":
            print(f"{now} LISTO {j['pdf']}: {j['n_pages']} pág, {j['n_invoices']} fact, {j['n_lines']} lín, "
                  f"{j['n_review']} a revisar, {j['seconds']} s; cola={len(queued)}{compare(j)}", flush=True)
            try:  # regla nueva: cantidad del 27B que lo impreso no respalda (se deja la del 27B)
                from pathlib import Path as _P
                _d = _P(JOBS) / j["id"]
                for _l in json.loads((_d / "meta.json").read_text(encoding="utf-8")).get("log", []):
                    if "se deja la del 27B" in _l:
                        print(f"{now}   REGLA CANTIDAD {j['pdf']}: {_l.strip()}", flush=True)
                for _inv in json.loads((_d / "result.json").read_text(encoding="utf-8")):
                    for _ln in _inv["lineas"]:
                        if _ln.get("line_kind") == "goods" and _ln.get("ocr", {}).get("evidencia", {}).get("cantidad") == "modelo":
                            print(f"{now}   CANTIDAD SIN RESPALDO {j['pdf']}#{_inv['id']}: {_ln['cantidad']} x «{_ln['descripcion'][:50]}» "
                                  f"(importe {_ln['importe_linea']})", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{now}   (no se pudo revisar la regla de cantidad: {e})", flush=True)
            try:
                from pathlib import Path as _P
                _r = json.loads((_P(JOBS) / j["id"] / "result.json").read_text(encoding="utf-8"))
                count["docs"] += 1
                count["facturas"] += len(_r)
                count["revision"] += sum(1 for _i in _r if _i["motivo_revision"])
                for _i in _r:
                    for _l in _i["lineas"]:  # fase 3: líneas sin importe con su estimación (o sin ella)
                        _e = _l.get("estimacion")
                        if _e:
                            count["estimadas" if _e["importe_estimado"] else "sin_estimar"] += 1
                            print(f"{now}   FASE 3 {j['pdf']}#{_i['id']} línea {_l['seq']}: "
                                  f"{_e['importe_estimado'] or 'sin estimación'} ({_e['metodo'] or '-'}, tope {_e['maximo'] or '-'}) "
                                  f"«{_l['descripcion'][:50]}»", flush=True)
                    _tc = _i["ocr"].get("tipo_cambio")
                    if _tc:
                        count["convertidas"] += 1
                        print(f"{now}   MONEDA {j['pdf']}#{_i['id']}: {_tc['de']}->{_tc['a']} a {_tc['tasa']} (BCE {_tc['fecha']}, "
                              f"factura {_tc.get('fecha_factura') or 'sin fecha'}); total {_i['ocr'].get('total_original')} -> {_i['total']}", flush=True)
                    if _i["ocr"].get("origen_lineas") == "llm+imagen":
                        count["imagen"] += 1
                        print(f"{now}   CON IMAGEN {j['pdf']}#{_i['id']}: total {_i['total']}, "
                              f"{sum(1 for _l in _i['lineas'] if _l.get('line_kind') == 'goods')} artículos, sin revisión", flush=True)
                _f = count["facturas"]
                print(f"{now}   CONTADOR: {count['docs']} docs, {_f} facturas, {count['revision']} a revisión, "
                      f"{_f - count['revision']} sin revisión ({100 * (_f - count['revision']) / _f:.1f} %), "
                      f"{count['imagen']} por imagen; fase 3: {count['estimadas']} línea(s) estimadas, "
                      f"{count['sin_estimar']} sin estimar; {count['convertidas']} convertidas de moneda", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{now}   (contador: {e})", flush=True)
            if j.get("n_review"):
                try:
                    from pathlib import Path as _P
                    for inv in json.loads((_P(JOBS) / j["id"] / "result.json").read_text(encoding="utf-8")):
                        if inv["motivo_revision"]:
                            print(f"{now}   REVISAR {j['pdf']}#{inv['id']} ({inv['motivo_revision']}, total {inv['total']}): {inv['mismatch']}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"{now}   REVISAR {j['pdf']}: no se pudo leer el motivo ({e})", flush=True)
        elif st == "error":
            print(f"{now} ERROR {j['pdf']}: {j.get('error_code')} {j.get('error')}", flush=True)
    if first:
        print(f"{now} vigilando: {len(running)} en curso ({', '.join(j['pdf'] for j in running)}), {len(queued)} en cola", flush=True)
        first = False
    busy = []
    for name, port in SLOTS.items():
        try:
            busy += [name for s in get(f"http://192.168.1.194:{port}/slots") if s.get("is_processing")]
        except Exception as e:  # noqa: BLE001
            print(f"{now} MODELO {name}:{port} NO RESPONDE: {e}", flush=True)
    try:
        ok = get("http://192.168.1.194:8089/health").get("status") == "ok"
    except Exception:  # noqa: BLE001
        ok = False
    if ok != globals().get("ppocr_ok", True):
        print(f"{now} PP-OCRv6 :8089 {'RESPONDE de nuevo' if ok else 'NO RESPONDE (sin comprobación de saltos de página)'}", flush=True)
    globals()["ppocr_ok"] = ok
    if len(busy) > 1:
        print(f"{now} ALERTA: varios modelos procesando a la vez: {busy}", flush=True)
    time.sleep(3)
