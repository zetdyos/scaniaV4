# Recuperación ante una caída de GPU en .194 (2026-11-04)

Origen: el 2026-11-02 a las 15:57 la GPU0 se cayó del bus (`Xid 79`; `Xid 154 Node Reboot Required` en las dos). Los
llama-server se reiniciaron **sin tarjeta** y seguían dando `/health 200`. La API de SCANIA decía «ok», WTCP siguió
subiendo, tres trabajos acabaron en `INTERNAL` y el host necesitó un reinicio a mano a las 16:27.

Decisiones del usuario (2026-11-04): reinicio automático con freno, y en SCANIA «retener y repetir».

## En .194 (esta carpeta)

| Pieza | Qué hace |
|---|---|
| `scania-gpu-presente.sh BUS` + `zz-gpu-presente.conf` (`ExecStartPre=+…`) en `scania-mdl-text` (bus 03:00, GPU0), `-glm`, `-qwen-vl` y `-ppocr` (bus 04:00, GPU1) | El modelo solo arranca si **su** tarjeta, por bus PCI, está en `nvidia-smi` con memoria legible y sin Xid 79/154 en este arranque. Si no, la unidad falla y `Restart=on-failure` la reintenta cada 10 s: el modelo queda caído, nunca «sano» en CPU. Va por bus y no por índice porque, si la GPU0 desaparece, CUDA renumera la otra como 0 y `CUDA_VISIBLE_DEVICES=0` llevaría el 27B a la GPU1. `embed` y `rerank` (V3) no se tocan. |
| `scania-gpu-vigia.sh` + `.service` + `.timer` (cada 30 s) | Xid 79/154 en el registro del kernel → reinicia el host al momento. `nvidia-smi` que falla o tarjeta que falta → 3 comprobaciones seguidas (~90 s) y reinicia. Nada en los primeros 5 min tras arrancar. El reinicio es `systemctl reboot --force` (un proceso colgado en la GPU perdida puede no terminar) y, si a los 3 min sigue vivo, sysrq. |
| Freno | Un solo reinicio automático. Si la falla sigue tras reiniciar (fuente caída), o vuelve antes de 60 min, **no reinicia más**: los modelos quedan parados y espera a una persona. El freno (`/var/lib/scania-gpu-vigia/frenado`) se quita solo cuando el vigía ve las dos tarjetas sanas. |

El reinicio afecta a todo .194, también a lo de la V3 (embed, rerank, LiteLLM, :8000, expand_catalog).

Instalar (con la contraseña de sudo de `techstudi`, que no se guarda en ningún sitio):

```bash
scp deploy/host-modelos/recuperacion/* techstudi@192.168.1.194:scania-recuperacion/
ssh -t techstudi@192.168.1.194 'cd ~/scania-recuperacion && sudo bash instalar.sh'   # --quitar para deshacer
```

No reinicia ningún modelo: la comprobación previa rige desde el próximo arranque de cada unidad.
**Instalado el 2026-11-04 a las 19:17** (los cuatro modelos → 0, vigía «GPU sanas», `chequeo_arranque.py --ssh`: todo bien).
Registro: `/var/log/scania-gpu-vigia.log` y `journalctl -t scania-gpu-vigia`. Prueba sin reiniciar:
`sudo /usr/local/sbin/scania-gpu-vigia.sh --prueba`.

Probado el 2026-11-04: 25 casos con `nvidia-smi`, `journalctl` y `systemctl` simulados en Linux (WSL), y los dos
guiones en modo prueba en .194 contra el `nvidia-smi` y el registro reales (las dos tarjetas → 0; un bus inexistente →
1; las 3 líneas Xid del 2026-11-02 en `journalctl -k -b -1` casan con la expresión). `nvidia-smi` tarda 25 ms.

## En SCANIA (`gpus.py`, `demo_api.py`)

- `/api/health`: un modelo con `/health 200` pero sin memoria gráfica, o con una tarjeta de menos en el exportador
  :9100, da `ok: false`, y WTCP no sube nada (§1 del contrato).
- La cola única no empieza una tarea mientras haya una caída de GPU o el host no conteste: un análisis sigue en
  `queued` con el motivo en `step`; las clasificaciones esperan al 27B.
- Un análisis que falla a mitad con una caída de GPU espera a que vuelvan los modelos y se repite desde el principio,
  como máximo 2 veces, y sigue en `running`: WTCP espera sin límite (§3) y no reenvía el archivo. Sin caída de GPU, el
  error sigue siendo `INTERNAL`, como antes.
- Solo cuenta la evidencia de GPU. Un modelo que se reinicia con su tarjeta presente (PP-OCRv6 se repara solo) no detiene
  nada, y un fallo del propio exportador tampoco.

## Pendiente (no hecho)

- **No hay perro guardián por hardware** (`/dev/watchdog` no existe). Si el host entero se cuelga, como pasó en parte el
  2026-11-02 (el registro se corta a las 16:19 sin secuencia de apagado), ningún software lo reinicia. Opción:
  cargar `iTCO_wdt` (si la placa Dell lo tiene) y poner `RuntimeWatchdogSec=60` en `/etc/systemd/system.conf`.
