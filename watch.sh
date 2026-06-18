#!/usr/bin/env bash
# watch.sh — Monitor en tiempo real del harness v6.
# Muestra cada 3s: estado del harness, modelos activos, CPU/GPU, cola de bugs.
#
# USO: ./watch.sh [STATUS_FILE] [LOG_FILE]
#   defaults: /tmp/loop-v6-status.json, /tmp/loop-v6.log

set -euo pipefail

STATUS_FILE="${1:-/tmp/loop-v6-status.json}"
LOG_FILE="${2:-/tmp/loop-v6.log}"
INTERVAL="${INTERVAL:-3}"
HISTORY_WINDOW="${HISTORY_WINDOW:-30}"  # segundos

# Estado del último tick (para diffs).
LAST_FIXES=0
LAST_BUGS=0

# Acumuladores (ventana móvil de HISTORY_WINDOW segundos).
declare -A CALLS_BY_MODEL
declare -A WINDOW_TS

cleanup() {
  echo
  echo "[watch] terminando ($(date '+%H:%M:%S'))"
  exit 0
}
trap cleanup INT TERM

# Colores.
C_DIM=$'\033[2m'
C_BOLD=$'\033[1m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_RED=$'\033[31m'
C_CYAN=$'\033[36m'
C_MAGENTA=$'\033[35m'
C_RESET=$'\033[0m'

while true; do
  clear
  NOW=$(date '+%Y-%m-%d %H:%M:%S')
  HARNESS_PID=$(cat /tmp/loop-v6.pid 2>/dev/null || echo '?')
  MONITOR_PID=$(cat /tmp/loop-monitor.pid 2>/dev/null || echo '?')

  # 1) Estado del harness.
  echo "${C_BOLD}═══ HARNESS v6 — $NOW ═══${C_RESET}"
  echo "  Harness PID: ${C_CYAN}$HARNESS_PID${C_RESET}   Monitor PID: ${C_CYAN}$MONITOR_PID${C_RESET}"
  if [ -f "$STATUS_FILE" ]; then
    HARNESS_DATA=$(python3 -c "
import json
try:
    with open('$STATUS_FILE') as f:
        d = json.load(f)
    s = d.get('stats', {})
    up = d.get('uptime_sec', 0)
    h = up // 3600
    m_ = (up % 3600) // 60
    sec = up % 60
    print(f'{h:02d}:{m_:02d}:{sec:02d}|{s.get(\"rounds\",0)}|{s.get(\"clean_rounds_in_a_row\",0)}|{s.get(\"suspects_produced\",0)}|{s.get(\"suspects_refuted_by_filter\",0)}|{s.get(\"bugs_confirmed\",0)}|{s.get(\"fixes_attempted\",0)}|{s.get(\"fixes_succeeded\",0)}|{s.get(\"fixes_reverted\",0)}|{s.get(\"fixes_to_review\",0)}|{s.get(\"opus_invocations\",0)}')
except Exception as e:
    print(f'ERR|{e}')
" 2>&1)
    if [[ "$HARNESS_DATA" == ERR* ]]; then
      echo "  ${C_RED}Status: error leyendo $STATUS_FILE${C_RESET}"
    else
      IFS='|' read -r UPTIME ROUNDS CLEAN SUSPECTS REFUTED BUGS FIXES_ATT FIXES_OK FIXES_REV TO_REVIEW OPUS <<< "$HARNESS_DATA"
      echo "  Uptime:        ${C_BOLD}$UPTIME${C_RESET}   Rounds: ${C_BOLD}$ROUNDS${C_RESET} (clean: $CLEAN)"
      echo "  Suspects:      ${C_YELLOW}$SUSPECTS${C_RESET} producidos  |  ${C_DIM}$REFUTED refutados por filtro${C_RESET}"
      echo "  Bugs:          ${C_RED}$BUGS${C_RESET} confirmados"
      echo "  Fixes:         ${C_GREEN}$FIXES_OK${C_RESET} succeeded  |  $FIXES_ATT attempted  |  $FIXES_REV reverted  |  ${C_YELLOW}$TO_REVIEW to review${C_RESET}"
      echo "  Opus:          ${C_MAGENTA}$OPUS${C_RESET} invocaciones"

      # 2) Modelos activos en los últimos N segundos (delta sobre tick anterior).
      if [ -f "$LOG_FILE" ]; then
        # Contar calls por modelo en los últimos $HISTORY_WINDOW segundos.
        python3 - "$LOG_FILE" "$HISTORY_WINDOW" << 'PY' 2>/dev/null
import sys, re, time
from collections import Counter
log_file, window = sys.argv[1], int(sys.argv[2])
cutoff = time.time() - window
c = Counter()
active = set()
try:
    with open(log_file) as f:
        for line in f:
            # "[HH:MM:SS]   [call] MODELO 1.2s  in~100  out~200"
            m = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\].*\[call\] (\S+) ', line)
            if not m:
                continue
            h, mi, s = m.group(1), m.group(2), m.group(3)
            # Convertir a epoch aproximado (asumimos hoy).
            now = time.localtime()
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(h), int(mi), int(s), 0, 0, 0))
            if t < cutoff:
                continue
            model = m.group(4)
            c[model] += 1
            active.add(model)
except Exception:
    pass
# Output: modelo|activo(0/1)|calls_en_ventana
for m, n in sorted(c.items(), key=lambda kv: -kv[1]):
    flag = '*' if m in active else ' '
    print(f'{m}|1|{n}')
PY
      fi
    fi
  else
    echo "  ${C_YELLOW}Status: $STATUS_FILE no existe aún${C_RESET}"
  fi

  # 3) CPU y GPU en vivo.
  echo
  echo "${C_BOLD}─── CPU / GPU ───${C_RESET}"
  LOAD=$(uptime | awk -F'load average:' '{print $2}' | xargs)
  echo "  Load avg:  $LOAD"
  CPU_IDLE=$(top -bn1 | grep "Cpu" | awk '{print $8}' | head -1)
  echo "  CPU idle:  ${CPU_IDLE:-?}%   (procesos python3 activos: $(pgrep -c -f "python3.*harness" 2>/dev/null || echo 0))"

  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "  GPUs:"
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
                --format=csv,noheader 2>/dev/null | \
    awk -F', ' '{
      printf "    GPU %s %-30s  %5dMiB / %5dMiB  util=%s%%\n",
        $1, substr($2, 1, 28), $3, $4, $5
    }'
  fi

  # 4) Ollama RAM.
  if command -v curl >/dev/null 2>&1; then
    OLLAMA=$(curl -s --max-time 2 http://host.docker.internal:11434/api/ps 2>/dev/null | \
             python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for m in d.get('models', []):
        print(f\"  {m['name']:30s}  vram={m.get('size_vram',0)/1e9:.1f}GB\")
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$OLLAMA" ]; then
      echo "  Ollama RAM:"
      echo "$OLLAMA"
    fi
  fi

  # 5) Modelos en uso en los últimos 5s (delta activo).
  echo
  echo "${C_BOLD}─── Calls por modelo (ventana ${HISTORY_WINDOW}s) ───${C_RESET}"
  if [ -f "$LOG_FILE" ]; then
    CUTOFF_5S=$(($(date +%s) - 5))
    python3 - "$LOG_FILE" "$CUTOFF_5S" << 'PY' 2>/dev/null
import sys, re, time
from collections import Counter
log_file, cutoff = sys.argv[1], int(sys.argv[2])
c = Counter()
try:
    with open(log_file) as f:
        for line in f:
            m = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\].*\[call\] (\S+) ', line)
            if not m: continue
            now = time.localtime()
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0))
            if t < cutoff: continue
            c[m.group(4)] += 1
except Exception: pass
if not c:
    print(f'  {chr(34) + chr(34)}  (sin actividad en los últimos 5s)')
else:
    for m, n in sorted(c.items(), key=lambda kv: -kv[1]):
        bar = '█' * min(n, 30)
        print(f'  {m:48s}  {n:>3}  {bar}')
PY
  fi

  # 6) Últimas 8 líneas del log.
  echo
  echo "${C_BOLD}─── Log (últimas 8) ───${C_RESET}"
  if [ -f "$LOG_FILE" ]; then
    tail -8 "$LOG_FILE" | sed 's/^/  /'
  fi

  sleep "$INTERVAL"
done
