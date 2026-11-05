#!/bin/bash
# Instala (o quita con --quitar) la recuperación ante caída de GPU en .194. Ejecutar con sudo desde esta carpeta:
#   ssh -t techstudi@192.168.1.194 'cd ~/scania-recuperacion && sudo bash instalar.sh'
# NO reinicia ningún modelo: la comprobación previa rige desde el próximo arranque de cada unidad.
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "ejecutar con sudo"; exit 1; }
D=/etc/systemd/system
GPU0="scania-mdl-text"
GPU1="scania-mdl-glm scania-mdl-qwen-vl scania-mdl-ppocr"   # embed y rerank (V3) NO se tocan
if [ "${1:-}" = "--quitar" ]; then
  systemctl disable --now scania-gpu-vigia.timer 2>/dev/null || true
  rm -f $D/scania-gpu-vigia.service $D/scania-gpu-vigia.timer /usr/local/sbin/scania-gpu-vigia.sh /usr/local/sbin/scania-gpu-presente.sh
  for u in $GPU0 $GPU1; do rm -f $D/$u.service.d/zz-gpu-presente.conf; done
  systemctl daemon-reload
  echo "quitado (el estado en /var/lib/scania-gpu-vigia y el registro /var/log/scania-gpu-vigia.log se dejan)"; exit 0
fi
install -m755 scania-gpu-presente.sh scania-gpu-vigia.sh /usr/local/sbin/
install -m644 scania-gpu-vigia.service scania-gpu-vigia.timer $D/
for u in $GPU0; do install -Dm644 zz-gpu-presente-gpu0.conf $D/$u.service.d/zz-gpu-presente.conf; done
for u in $GPU1; do install -Dm644 zz-gpu-presente-gpu1.conf $D/$u.service.d/zz-gpu-presente.conf; done
systemctl daemon-reload
echo "--- comprobación previa por modelo (debe salir 0):"
for u in $GPU0 $GPU1; do
  bus=$(systemctl show -p ExecStartPre $u | grep -oE "scania-gpu-presente.sh [0-9a-fA-F]{8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]" | head -1 | awk '{print $2}')
  if /usr/local/sbin/scania-gpu-presente.sh "$bus"; then echo "  $u ($bus) -> 0"; else echo "  $u ($bus) -> FALLA: revisar antes de seguir"; fi
done
echo "--- vigía en prueba (no reinicia):"
/usr/local/sbin/scania-gpu-vigia.sh --prueba
systemctl enable --now scania-gpu-vigia.timer
systemctl list-timers --no-pager scania-gpu-vigia.timer
echo "instalado. Registro: /var/log/scania-gpu-vigia.log y journalctl -t scania-gpu-vigia"
