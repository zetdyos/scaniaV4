#!/bin/bash
# Vigía de las GPU de .194 (scania-gpu-vigia.timer, cada 30 s): si una tarjeta se cae (Xid 79 «fallen off the bus»,
# 154 «Node Reboot Required», nvidia-smi que falla o tarjeta que falta), reinicia el host, CON FRENO:
#   - una sola vez: si la falla sigue tras el reinicio, o vuelve antes de 60 min, NO reinicia más; los modelos de SCANIA
#     quedan parados (scania-gpu-presente.sh) y espera a una persona. El freno se quita solo cuando ve las dos tarjetas
#     sanas (una persona arregló la fuente o reinició a mano).
#   - Xid en el registro del kernel: definitivo (el driver exige reiniciar). nvidia-smi que falla o tarjeta que falta:
#     tres comprobaciones seguidas (~90 s), para no reiniciar por un nvidia-smi lento en plena carga.
#   - nada en los primeros 5 min tras arrancar (driver y persistencia).
# Decisión del usuario 2026-11-04 («reinicio automático con freno»). Afecta a todo .194 (también lo de la V3).
# Prueba sin reiniciar: scania-gpu-vigia.sh --prueba
set -u
ESPERADAS="00000000:03:00.0 00000000:04:00.0"
FRENO_S=3600
ARRANQUE_S=300
B="${SCANIA_VIGIA_RAIZ:-}"   # solo para pruebas: raíz de los archivos de estado
LIB=$B/var/lib/scania-gpu-vigia
RUN=$B/run/scania-gpu-vigia
LOG=$B/var/log/scania-gpu-vigia.log
PRUEBA=0; [ "${1:-}" = "--prueba" ] && PRUEBA=1
mkdir -p "$LIB" "$RUN" "$(dirname "$LOG")"
log() { echo "$(date -Is) $*" >> "$LOG"; logger -t scania-gpu-vigia "$*"; [ $PRUEBA = 1 ] && echo "$*"; }
boot=${SCANIA_VIGIA_BOOT:-$(cat /proc/sys/kernel/random/boot_id)}
now=${SCANIA_VIGIA_AHORA:-$(date +%s)}
up=${SCANIA_VIGIA_UPTIME:-$(cut -d. -f1 /proc/uptime)}

motivo=""; definitivo=0
out=$(timeout 20 nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  motivo="nvidia-smi rc=$rc: $(echo "$out" | head -1)"
else
  for b in $ESPERADAS; do
    echo "$out" | grep -iqE "^${b}, *[0-9]+$" || motivo="$motivo falta o no sana $b;"
  done
fi
xid=$(journalctl -k -b --no-pager -q 2>/dev/null | grep -E "NVRM: Xid .*(fallen off the bus|Node Reboot Required)" | tail -1)
if [ -n "$xid" ]; then motivo="$motivo kernel: $xid"; definitivo=1; fi

if [ -z "$motivo" ]; then
  rm -f "$RUN/seguidas"
  if [ -f "$LIB/frenado" ]; then log "las dos GPU sanas de nuevo: se quita el freno"; rm -f "$LIB/frenado"; fi
  if [ -f "$RUN/fallo" ]; then log "las dos GPU sanas de nuevo"; rm -f "$RUN/fallo"; fi
  [ $PRUEBA = 1 ] && echo "GPU sanas: nada que hacer"
  exit 0
fi

[ -f "$RUN/fallo" ] || log "FALLO de GPU: $motivo"
echo "$motivo" > "$RUN/fallo"
if [ "$up" -lt $ARRANQUE_S ]; then [ $PRUEBA = 1 ] && echo "recién arrancado (${up} s): se espera"; exit 0; fi
# contador ligado a este arranque (por si /run no se vaciara)
read -r sb sn < "$RUN/seguidas" 2>/dev/null || { sb=""; sn=0; }
[ "$sb" = "$boot" ] || sn=0
seguidas=$((sn + 1)); echo "$boot $seguidas" > "$RUN/seguidas"
if [ $definitivo = 0 ] && [ $seguidas -lt 3 ]; then [ $PRUEBA = 1 ] && echo "comprobación $seguidas con fallo: se confirma con 3 seguidas"; exit 0; fi

if [ -f "$LIB/frenado" ]; then
  [ "$(cat "$LIB/frenado")" = "$boot" ] || { log "FRENO activo: no se reinicia; los modelos de SCANIA quedan parados hasta que una persona lo mire"; echo "$boot" > "$LIB/frenado"; }
  exit 0
fi
ultimo=$(cat "$LIB/ultimo_reinicio" 2>/dev/null || echo 0)
if [ $((now - ultimo)) -lt $FRENO_S ]; then
  log "FRENO: hubo un reinicio automático hace $(( (now - ultimo) / 60 )) min y la falla sigue o volvió; no se reinicia más; los modelos de SCANIA quedan parados hasta que una persona lo mire"
  echo "$boot" > "$LIB/frenado"
  exit 0
fi
if [ $PRUEBA = 1 ]; then echo "PRUEBA: aquí se reiniciaría el host ($motivo)"; exit 0; fi
log "REINICIO AUTOMÁTICO del host: $motivo"
echo "$now" > "$LIB/ultimo_reinicio"
sync
# --force: sin parar unidad por unidad (un proceso colgado en una GPU perdida puede no terminar nunca). Si a los 3 min
# seguimos vivos, reinicio por sysrq (s = sync, b = reboot).
systemctl reboot --force
sleep "${SCANIA_VIGIA_SYSRQ_S:-180}"
log "el reinicio no terminó en 3 min: sysrq"
echo s > "$B/proc/sysrq-trigger"; sleep 5; echo b > "$B/proc/sysrq-trigger"
