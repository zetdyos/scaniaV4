#!/bin/bash
# ExecStartPre de los modelos de SCANIA en .194 (drop-in zz-gpu-presente.conf): el modelo solo arranca si SU tarjeta,
# por bus PCI, está presente y sana en este arranque. Si no, la unidad falla (Restart=on-failure lo reintenta cada
# 10 s) y el modelo queda CAÍDO, nunca «sano» en CPU.
# Motivo (2026-11-02 15:57): la GPU0 se cayó del bus (Xid 79) y los llama-server se reiniciaron sin tarjeta dando
# /health 200. Por bus y no por índice: si la GPU0 desaparece, nvidia-smi/CUDA renumeran la otra como 0 y
# CUDA_VISIBLE_DEVICES=0 llevaría el 27B a la GPU1.
# Uso: scania-gpu-presente.sh 00000000:03:00.0
set -u
bus="${1:?falta el bus PCI}"
corto=$(echo "$bus" | awk -F: '{print $2":"$3}' | cut -d. -f1)   # 00000000:03:00.0 -> 03:00 (el kernel: PCI:0000:03:00)
out=$(timeout 20 nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  echo "scania-gpu-presente: nvidia-smi rc=$rc ($(echo "$out" | head -1)): no se arranca sin GPU" >&2; exit 1
fi
if ! echo "$out" | grep -iqE "^${bus}, *[0-9]+$"; then
  echo "scania-gpu-presente: la tarjeta $bus no está o no está sana: $(echo "$out" | tr '\n' ';')" >&2; exit 1
fi
xid=$(journalctl -k -b --no-pager -q 2>/dev/null | grep -E "NVRM: Xid \(PCI:[0-9a-fA-F]{4}:${corto}\).*(fallen off the bus|Node Reboot Required)" | tail -1)
if [ -n "$xid" ]; then
  echo "scania-gpu-presente: la tarjeta $bus necesita reiniciar el host ($xid)" >&2; exit 1
fi
exit 0
