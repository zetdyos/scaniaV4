"""Comprobación tras un reinicio (de .194, de este PC o de la sesión): SOLO LEE, no arranca ni cambia nada.

Nació el 2026-11-02: la GPU0 de .194 se cayó del bus (Xid 79), los llama-server se reiniciaron SIN tarjeta y seguían
dando /health 200, y la métrica decía PP-OCRv6 en GPU0 porque el inventario del exportador estaba viejo. Se tardó una hora
en verlo. Aquí está todo lo que hubo que mirar a mano, con lo que debe dar:

  - .194: las dos GPU en las métricas; cada modelo con memoria gráfica (el VL puede estar dormido, ~300 MiB) y en su
    tarjeta (con --ssh, la tarjeta real de nvidia-smi por bus PCI; sin ssh, la etiqueta del inventario del exportador);
    /health de 8083, 8086, 8088, 8089; el 27B con n_ctx 16384 y visión.
  - Este PC: API :8091 sana y sin la marca .no_aceptar; clasificador :8092 con su trabajador vivo; restos de tuberías de
    monitores viejos (Git usr\\bin) y vigilantes duplicados; clasificaciones en curso; contador guardado del vigilante.

Uso: .venv/Scripts/python.exe herramientas/chequeo_arranque.py [--ssh]
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
HOST = "192.168.1.194"
# Tarjeta esperada por modelo (CLAUDE.md, regla 2): GPU0 = solo el 27B; el resto en GPU1. Bus PCI de nvidia-smi en .194.
ESPERADO = {"text": 0, "glm-194": 1, "qwen-vl": 1, "ppocr": 1, "embed": 1, "rerank": 1}
BUS = {"00000000:03:00.0": 0, "00000000:04:00.0": 1}
PUERTOS = {"27B": 8083, "GLM-OCR": 8086, "Qwen2.5-VL": 8088, "PP-OCRv6": 8089}
MIN_VRAM = {"qwen-vl": 200, "ppocr": 300}  # VL dormido (--sleep-idle-seconds 30): ~300 MiB; PP-OCRv6 recién
# arrancado 430-900 MiB (crece con el uso hasta su tope de 2560); los demás, cargados, mucho más
fallos = []


def ok(cond, texto, detalle=""):
    print(("  OK    " if cond else "  FALLO ") + texto + (f"  ({detalle})" if detalle else ""))
    if not cond:
        fallos.append(texto)


def get(url, t=8):
    with urllib.request.urlopen(url, timeout=t) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main(con_ssh):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "comprobación de arranque (solo lectura)")
    print(f"Host de modelos {HOST}:")
    try:
        _, m = get(f"http://{HOST}:9100/metrics")
    except Exception as e:  # noqa: BLE001
        m = ""
        ok(False, "exportador :9100 responde", type(e).__name__)
    gpus = {ln.split('gpu="')[1].split('"')[0] for ln in m.splitlines() if ln.startswith("DCGM_FI_DEV_GPU_TEMP{")}
    ok(gpus == {"0", "1"}, "las dos GPU aparecen en las métricas", f"vistas: {sorted(gpus) or 'ninguna'}")
    vram = {}
    for ln in m.splitlines():
        if ln.startswith("llm_modelo_vram_mib{"):
            lab = dict(kv.split("=") for kv in ln[ln.index("{") + 1:ln.index("}")].replace('"', "").split(","))
            vram[lab["modelo"]] = (int(lab.get("gpu", -1)), float(ln.rsplit(" ", 1)[1]))
    reales = {}
    if con_ssh:
        try:
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"techstudi@{HOST}",
                                  "nvidia-smi --query-compute-apps=gpu_bus_id,pid --format=csv,noheader;"
                                  " for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader); do"
                                  " echo \"PID $p $(ps -o args= -p $p)\"; done"],
                                 capture_output=True, text=True, timeout=30).stdout
            pid_bus = {ln.split(",")[1].strip(): BUS.get(ln.split(",")[0].strip()) for ln in out.splitlines() if "," in ln}
            for ln in out.splitlines():
                if ln.startswith("PID "):
                    _, pid, args = ln.split(" ", 2)
                    puerto = next((p for p in ("8083", "8084", "8085", "8086", "8088", "8089") if p in args), None)
                    alias = {"8083": "text", "8084": "embed", "8085": "rerank", "8086": "glm-194", "8088": "qwen-vl",
                             "8089": "ppocr"}.get(puerto)
                    if alias:
                        reales[alias] = pid_bus.get(pid)
        except Exception as e:  # noqa: BLE001
            ok(False, "nvidia-smi por ssh", type(e).__name__)
    for alias, gpu in ESPERADO.items():
        g, mib = vram.get(alias, (None, 0.0))
        ok(mib >= MIN_VRAM.get(alias, 1000), f"{alias}: con memoria gráfica (no en CPU)", f"{mib:.0f} MiB")
        if con_ssh:
            ok(reales.get(alias) == gpu, f"{alias}: en GPU{gpu} según nvidia-smi", f"real: GPU{reales.get(alias)}")
            ok(g == reales.get(alias), f"{alias}: la métrica dice la misma tarjeta que nvidia-smi",
               f"métrica GPU{g}; si no coincide, corregir inventario.json en SCANIA-HW y en ~/ de .194")
        else:
            ok(g == gpu, f"{alias}: en GPU{gpu} según la métrica", f"métrica GPU{g} (la real: --ssh)")
    for nombre, puerto in PUERTOS.items():
        try:
            st, _ = get(f"http://{HOST}:{puerto}/health")
        except Exception as e:  # noqa: BLE001
            st = type(e).__name__
        ok(st == 200, f"{nombre} :{puerto} /health", str(st))
    try:
        p = json.loads(get(f"http://{HOST}:8083/props")[1])
        n_ctx = (p.get("default_generation_settings") or {}).get("n_ctx")
        ok(n_ctx == 16384, "27B con contexto 16384", str(n_ctx))
        ok(bool((p.get("modalities") or {}).get("vision")), "27B con visión (proyector cargado)")
    except Exception as e:  # noqa: BLE001
        ok(False, "27B /props", type(e).__name__)

    print("Este PC:")
    try:
        h = json.loads(get("http://127.0.0.1:8091/api/health", 20)[1])
        malos = [x["name"] for x in h["models"] if not x["ok"]]
        ok(not malos, "API :8091 /api/health: todo ok", ", ".join(malos) or
           f"en curso {h['jobs_running']}, en cola {h['jobs_queued']}")
    except Exception as e:  # noqa: BLE001
        ok(False, "API :8091 responde", type(e).__name__)
    ok(not (HERE / ".no_aceptar").exists(), "sin la marca .no_aceptar (si está, la API da 503 a las subidas)")
    try:
        q = json.loads(get("http://127.0.0.1:8092/api/health")[1]).get("queue") or {}
        ok(bool(q.get("worker_alive")), "clasificador :8092 con trabajador vivo",
           f"lotes en curso {q.get('running_batches')}, pendientes {q.get('pending_items')}")
    except Exception as e:  # noqa: BLE001
        ok(False, "clasificador :8092 responde", type(e).__name__)
    if sys.platform == "win32":
        ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)`t$($_.CommandLine)\" }"],
                            capture_output=True, text=True, timeout=60).stdout.splitlines()
        restos = [ln for ln in ps if len(ln.split("\t")) > 1 and "\\Git\\usr\\bin\\" in ln.split("\t")[1]]
        vigias = [ln for ln in ps if "vigilar.py" in ln and "\\.venv\\" in ln]
        print(f"  INFO  restos de tuberías (Git usr\\bin: tail/grep/cut/sed…): {len(restos)}; vigilantes vivos: {len(vigias)}"
              " — los de un monitor caducado se cierran antes de rearmar")
    en = HERE / "clasif_jobs" / "en_curso.json"
    if en.exists():
        d = json.loads(en.read_text(encoding="utf-8"))
        sin = sum(1 for v in d.values() if not v.get("line_id"))
        print(f"  INFO  clasificaciones en curso: {len(d)} ({sin} sin enviar al clasificador: se reenvían cuando WTCP las"
              " vuelva a pedir)")
    c = HERE / "verdad" / "vigilante_contador.json"
    if c.exists():
        g = json.loads(c.read_text(encoding="utf-8"))
        print(f"  INFO  contador del vigilante guardado el {g['guardado']}: {g['contador']['docs']} docs, "
              f"{g['contador']['facturas']} facturas — rearmar con VIGILAR_CONTADOR=continuar")
    print("RESULTADO:", "todo bien" if not fallos else f"{len(fallos)} fallo(s): " + "; ".join(fallos))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main("--ssh" in sys.argv))
