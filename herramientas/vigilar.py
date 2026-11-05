"""Vigila la API de SCANIA V4 y los slots de .194 (solo lectura). Una línea por evento."""
import json, time, urllib.request

API = "http://127.0.0.1:8091/api/jobs"
SLOTS = {"GLM": 8086, "27B": 8083}  # el VL no: duerme a los 30 s (--sleep-idle-seconds) y GET /slots lo mantiene despierto
seen = {}
first = True
last_busy_report = 0


JOBS = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "demo_jobs")
DEPLOY = "20261023-2041"  # lote tras ed4e454 (auditoría del flujo): se compara con la corrida anterior del mismo PDF
count = {"docs": 0, "facturas": 0, "revision": 0, "imagen": 0, "estimadas": 0, "sin_estimar": 0, "convertidas": 0, "duplicados": 0, "dup_limpios": 0}  # desde que arranca el vigilante
# El recuento se guarda en disco tras cada trabajo (fuera de git, junto a verdad/27b.jsonl): tras un reinicio de la sesión
# o del PC, VIGILAR_CONTADOR=continuar sigue desde ahí; un JSON sigue ese recuento; sin la variable, desde cero.
CONTADOR_FILE = __import__("pathlib").Path(__file__).resolve().parents[1] / "verdad" / "vigilante_contador.json"
_seed = __import__("os").environ.get("VIGILAR_CONTADOR") or "{}"
if _seed.strip() == "continuar":
    _seed = json.dumps(json.loads(CONTADOR_FILE.read_text(encoding="utf-8"))["contador"]) if CONTADOR_FILE.exists() else "{}"
count.update(json.loads(_seed))  # seguir un recuento anterior
# Último trabajo contado (se guarda con el contador): al seguir un recuento, los trabajos que terminaron con el vigilante
# parado (entre dos monitores) se procesan al arrancar en vez de darse por vistos (2026-11-03: 33680 y 34776 se perdieron).
CATCH_UP = count.get("ultimo_job", "") if _seed.strip() not in ("", "{}") else None
for _k in ("clasif_llamadas", "clasif_no200", "clasif_entregadas", "clasif_segura", "clasif_dudosa", "clasif_no", "clasif_mandadas"):
    count.setdefault(_k, 0)

# ----------------------------------------------------------------------------- clasificaciones (2026-11-02)
# WTCP pide clasificaciones aparte (POST /api/classify, contrato §8) y no son trabajos: no salen en /api/jobs. Se vigilan
# leyendo, sin tocar nada: el registro de la API (llamadas y su código), clasif_jobs/en_curso.json (descripciones pedidas
# y no entregadas: una clave que desaparece es una entregada) y el /api/health y /api/lines/{id} del clasificador :8092.
REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
__import__("sys").path.insert(0, str(REPO))
import re  # noqa: E402
import clasificacion  # noqa: E402  solo para `resultado` (estado de una línea terminada) y las constantes
import gpus  # noqa: E402  caída de GPU en .194 (la misma comprobación con la que la API retiene la cola)
API_LOG = REPO / "logs" / "api.out.log"
EN_CURSO = REPO / "clasif_jobs" / "en_curso.json"
CLASIF_CADA = float(__import__("os").environ.get("VIGILAR_CLASIF_CADA", "1800"))  # resumen cada 30 min
CLASSIFY_RE = re.compile(r'"POST /api/classify(/feedback)? HTTP/[\d.]+" (\d{3})')


def _ventana():
    return {"llamadas": 0, "codigos": {}, "feedback": 0, "nuevas": 0, "mandadas": 0, "entregadas": 0,
            "segura": 0, "dudosa": 0, "no": 0}


cl = {"log_pos": None, "en": None, "linea": {}, "cargadas": None, "arriba": True, "vivo": True, "t": time.time(),
      "ventana": _ventana()}


def _clasif(now):
    w = cl["ventana"]
    # 1) llamadas de WTCP en el registro de la API (se reescribe en cada despliegue: si encoge, desde el principio)
    try:
        size = API_LOG.stat().st_size
        if cl["log_pos"] is None:
            cl["log_pos"] = size
        if size < cl["log_pos"]:
            cl["log_pos"] = 0
        with open(API_LOG, "rb") as f:
            f.seek(cl["log_pos"])
            chunk = f.read().decode("utf-8", "replace")
            cl["log_pos"] = f.tell()
        for m in CLASSIFY_RE.finditer(chunk):
            if m.group(1):
                w["feedback"] += 1
                continue
            code = m.group(2)
            w["llamadas"] += 1
            count["clasif_llamadas"] += 1
            w["codigos"][code] = w["codigos"].get(code, 0) + 1
            if code != "200":
                count["clasif_no200"] += 1
                print(f"{now} CLASIFICACIÓN: POST /api/classify respondió {code} a WTCP", flush=True)
    except OSError:
        pass
    # 2) descripciones en curso: nuevas y entregadas (con el estado que dio el clasificador)
    try:
        en = json.loads(EN_CURSO.read_text(encoding="utf-8")) if EN_CURSO.exists() else {}
    except (OSError, ValueError):
        en = cl["en"] or {}
    for k, v in en.items():
        if v.get("line_id"):
            cl["linea"][k] = v["line_id"]
    if cl["en"] is not None:
        w["nuevas"] += sum(1 for k in en if k not in cl["en"])
        for k, v in en.items():  # un error nuevo del clasificador en una línea
            if v.get("errores", 0) > (cl["en"].get(k) or {}).get("errores", 0):
                print(f"{now} CLASIFICACIÓN: error del clasificador ({v['errores']} de {clasificacion.MAX_INTENTOS}) "
                      f"en «{v['texto'][:60]}»", flush=True)
        for k in [k for k in cl["en"] if k not in en]:
            w["entregadas"] += 1
            count["clasif_entregadas"] += 1
            est = "no"
            lid = cl["linea"].pop(k, None)
            if lid:
                try:
                    r = clasificacion.resultado(get(f"{clasificacion.CLASIFICADOR_URL}/api/lines/{lid}?include=summary", 8))
                    est = {"sugerencia_segura": "segura", "dudosa": "dudosa"}.get(r["estado"], "no")
                except Exception:  # noqa: BLE001
                    est = "no"
            w[est] += 1
            count["clasif_" + est] += 1
    cl["en"] = en
    # 3) el clasificador: arriba, trabajador vivo, líneas cargadas (las que manda SCANIA y las de su propia interfaz)
    try:
        q = get(f"{clasificacion.CLASIFICADOR_URL}/api/health", 5).get("queue") or {}
        if not cl["arriba"]:
            print(f"{now} CLASIFICADOR :8092 responde de nuevo", flush=True)
        cl["arriba"] = True
        if bool(q.get("worker_alive")) != cl["vivo"]:
            print(f"{now} CLASIFICADOR :8092 trabajador {'vivo de nuevo' if q.get('worker_alive') else 'PARADO'}", flush=True)
        cl["vivo"] = bool(q.get("worker_alive"))
        n = q.get("batches_loaded")
        if n is not None:
            if cl["cargadas"] is not None and n >= cl["cargadas"]:
                w["mandadas"] += n - cl["cargadas"]
                count["clasif_mandadas"] += n - cl["cargadas"]
            cl["cargadas"] = n
    except Exception as e:  # noqa: BLE001
        if cl["arriba"]:
            print(f"{now} CLASIFICADOR :8092 NO RESPONDE: {e}", flush=True)
        cl["arriba"] = False
    # 4) resumen cada CLASIF_CADA segundos
    if time.time() - cl["t"] >= CLASIF_CADA:
        mins = round((time.time() - cl["t"]) / 60)
        con_linea = sum(1 for v in en.values() if v.get("line_id"))
        con_error = sum(1 for v in en.values() if v.get("errores"))
        cods = ", ".join(f"{c}: {n}" for c, n in sorted(w["codigos"].items())) or "-"
        print(f"{now} CLASIFICACIÓN (últimos {mins} min): {w['llamadas']} llamadas de WTCP ({cods})"
              + (f", {w['feedback']} correcciones" if w["feedback"] else "")
              + f" | {w['nuevas']} descripciones nuevas pedidas, {w['mandadas']} mandadas al clasificador | "
              f"{w['entregadas']} entregadas (segura {w['segura']}, dudosa {w['dudosa']}, no_clasificado {w['no']}) | "
              f"en curso {len(en)} ({con_linea} en el clasificador o terminadas sin entregar, {con_error} con error) | "
              f"acumulado: {count['clasif_llamadas']} llamadas, {count['clasif_entregadas']} entregadas "
              f"(segura {count['clasif_segura']}, dudosa {count['clasif_dudosa']}, no_clasificado {count['clasif_no']})", flush=True)
        try:
            CONTADOR_FILE.write_text(json.dumps({"guardado": time.strftime("%Y-%m-%d %H:%M:%S"), "contador": count},
                                                ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        cl["t"] = time.time()
        cl["ventana"] = _ventana()


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
        if first and not (CATCH_UP is not None and st in ("done", "error") and j["id"] > CATCH_UP):
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
                count["ultimo_job"] = max(count.get("ultimo_job", ""), j["id"])
                count["facturas"] += len(_r)
                count["revision"] += sum(1 for _i in _r if _i["motivo_revision"])
                # Duplicados: para WTCP van «A revisión - DUPLICADO»; para nosotros, un duplicado bien detectado es una
                # clasificación correcta de SCANIA y cuenta como «No-Revisión» (usuario 2026-10-23; la auditoría comprueba
                # que de verdad sea el mismo pedido).
                for _i in _r:
                    if _i["motivo_revision"] == "DUPLICADO":
                        count["duplicados"] += 1
                        count["dup_limpios"] += 1
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
                _f = count["facturas"] or 1
                _pc = lambda n, d: f"{100 * n / d:.1f} %" if d else "-"
                _l3 = count["estimadas"] + count["sin_estimar"]
                _nr = count["facturas"] - count["revision"] + count["dup_limpios"]
                print(f"{now}   SCANIA (nuestro): no-revisión {_nr} ({_pc(_nr, _f)}) | revisión {count['facturas'] - _nr} "
                      f"({_pc(count['facturas'] - _nr, _f)}) | duplicados {count['duplicados']} (contados como no-revisión)", flush=True)
                print(f"{now}   CONTADOR: {count['docs']} docs, {count['facturas']} facturas | sin revisión "
                      f"{count['facturas'] - count['revision']} ({_pc(count['facturas'] - count['revision'], _f)}) | a revisión "
                      f"{count['revision']} ({_pc(count['revision'], _f)}) | por imagen {count['imagen']} ({_pc(count['imagen'], _f)}) | "
                      f"fase 3: {count['estimadas']} de {_l3} línea(s) sin importe estimadas ({_pc(count['estimadas'], _l3)}) | "
                      f"moneda convertida {count['convertidas']} ({_pc(count['convertidas'], _f)})", flush=True)
                try:
                    CONTADOR_FILE.write_text(json.dumps({"guardado": time.strftime("%Y-%m-%d %H:%M:%S"), "contador": count}, ensure_ascii=False), encoding="utf-8")
                except OSError as _e:
                    print(f"{now}   (no se pudo guardar el contador: {_e})", flush=True)
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
    _clasif(now)
    if first:
        print(f"{now} vigilando: {len(running)} en curso ({', '.join(j['pdf'] for j in running)}), {len(queued)} en cola; "
              f"clasificaciones en curso {len(cl['en'] or {})}, clasificador {'arriba' if cl['arriba'] else 'NO RESPONDE'} "
              f"(resumen cada {CLASIF_CADA / 60:.0f} min)", flush=True)
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
    if time.time() - globals().get("gpu_t", 0) >= 30:  # caída de GPU: aviso al empezar y al terminar
        g_ok, g_motivo = gpus.listos(cache_s=0.0)
        if g_ok != globals().get("gpu_ok", True):
            print(f"{now} " + ("GPU: los modelos vuelven a estar en sus GPU; la API retoma la cola" if g_ok else
                               f"ALERTA GPU: {g_motivo}; la API retiene la cola (y repite el trabajo cortado) y .194 se "
                               "reinicia solo con freno"), flush=True)
        globals().update(gpu_ok=g_ok, gpu_t=time.time())
    if len(busy) > 1:
        print(f"{now} ALERTA: varios modelos procesando a la vez: {busy}", flush=True)
    time.sleep(3)
