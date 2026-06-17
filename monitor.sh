#!/usr/bin/env bash
# Monitor del harness v6 — observa el status file cada 30s y reporta.
#
# Muestra:
#   - Estado de colas (suspectq, bugq).
#   - Stats: rounds, clean_rounds, bugs confirmed, fixes succeeded.
#   - Gasto del run actual (local, no facturado).
#   - Memoria: total_rounds, fixes, open_bugs.
#   - Stall detection: si no hay avance en N minutos, avisa.
#
# USO: ./monitor.sh [STATUS_FILE]
#   STATUS_FILE default: /tmp/loop-v6-status.json

set -euo pipefail

STATUS_FILE="${1:-/tmp/loop-v6-status.json}"
LOG_FILE="${LOG_FILE:-/tmp/loop-v6.log}"
HARNESS_PID_FILE="${HARNESS_PID_FILE:-/tmp/loop-v6.pid}"

INTERVAL="${INTERVAL:-30}"
STALL_SECS="${STALL_SECS:-300}"   # 5 min sin stats.json = alerta

mkdir -p "$(dirname "$LOG_FILE")"

LAST_FIXES_SUCCEEDED=0
LAST_ROUNDS=0
LAST_BUGS_CONFIRMED=0
LAST_E2E_FINDINGS=0
STALL_START=""

# Header
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  MONITOR HARNESS v6 — refresh cada ${INTERVAL}s"
echo "  Status file:  $STATUS_FILE"
echo "  Log file:     $LOG_FILE"
echo "  Started:      $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo

# Estado inicial
HARNESS_RUNNING=true

cleanup() {
  echo
  echo "[monitor] terminando ($(date '+%H:%M:%S'))"
  exit 0
}
trap cleanup INT TERM

while $HARNESS_RUNNING; do
  if [ ! -f "$STATUS_FILE" ]; then
    echo "[$(date '+%H:%M:%S')] [monitor] ⚠ $STATUS_FILE no existe aún — esperando..."
    sleep $INTERVAL
    continue
  fi

  # Leer el status
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  STATS=$(python3 -c "
import json, sys
try:
    with open('$STATUS_FILE') as f:
        d = json.load(f)
except Exception as e:
    print(f'ERROR: {e}')
    sys.exit(1)

s = d.get('stats', {})
up = d.get('uptime_sec', 0)
q = d.get('quota', {})
m = d.get('memory', {})

h = up // 3600
m_ = (up % 3600) // 60
s_ = up % 60
uptime = f'{h:02d}:{m_:02d}:{s_:02d}'

# Calcular gasto total del run.
total_eff = sum(v.get('eff', 0) for v in q.values())
total_calls = sum(v.get('calls', 0) for v in q.values())

# Top modelos por uso.
top = sorted(q.items(), key=lambda kv: -kv[1].get('eff', 0))[:5]
top_str = ', '.join(f\"{k.split(':')[-1]}={v.get('eff', 0):,}\" for k, v in top)

print(f'  uptime:        {uptime}')
print(f'  rounds:        {s.get(\"rounds\", 0)}   clean_in_a_row: {s.get(\"clean_rounds_in_a_row\", 0)}')
print(f'  suspects:      {s.get(\"suspects_produced\", 0)}   '
      f'filter_refuted: {s.get(\"suspects_refuted_by_filter\", 0)}   '
      f'fp_filtered: {s.get(\"suspects_filtered_as_known_fp\", 0)}')
print(f'  bugs:          confirmed={s.get(\"bugs_confirmed\", 0)}   '
      f'e2e_findings: {s.get(\"e2e_findings\", 0)}')
print(f'  fixes:         attempted={s.get(\"fixes_attempted\", 0)}   '
      f'succeeded={s.get(\"fixes_succeeded\", 0)}   '
      f'reverted={s.get(\"fixes_reverted\", 0)}   '
      f'to_review={s.get(\"fixes_to_review\", 0)}')
print(f'  opus:           invocations={s.get(\"opus_invocations\", 0)}')
print(f'  memory:         rounds={m.get(\"total_rounds\", 0)}   '
      f'fixes={m.get(\"total_fixes_applied\", 0)}   '
      f'open_bugs={m.get(\"open_bugs\", 0)}   '
      f'hardened_zones={m.get(\"hardened_zones\", 0)}')
print(f'  spend_local:   calls={total_calls}   eff_tokens={total_eff:,}')
if top_str:
    print(f'  top_models:     {top_str}')
" 2>&1)

  echo "── $TS ──"
  echo "$STATS"

  # Stall detection.
  CURRENT_ROUNDS=$(python3 -c "import json; print(json.load(open('$STATUS_FILE'))['stats'].get('rounds', 0))" 2>/dev/null || echo 0)
  CURRENT_FIXES=$(python3 -c "import json; print(json.load(open('$STATUS_FILE'))['stats'].get('fixes_succeeded', 0))" 2>/dev/null || echo 0)

  if [ "$CURRENT_ROUNDS" = "$LAST_ROUNDS" ] && [ "$CURRENT_FIXES" = "$LAST_FIXES_SUCCEEDED" ]; then
    if [ -z "$STALL_START" ]; then
      STALL_START=$(date +%s)
    else
      NOW=$(date +%s)
      ELAPSED=$((NOW - STALL_START))
      if [ "$ELAPSED" -gt "$STALL_SECS" ]; then
        echo "  ⚠ STALL: $((ELAPSED/60)) min sin avance (rounds=$CURRENT_ROUNDS fixes=$CURRENT_FIXES)"
      fi
    fi
  else
    STALL_START=""
    LAST_ROUNDS=$CURRENT_ROUNDS
    LAST_FIXES_SUCCEEDED=$CURRENT_FIXES
  fi

  # Verificar si el harness sigue vivo.
  if [ -f "$HARNESS_PID_FILE" ]; then
    HARNESS_PID=$(cat "$HARNESS_PID_FILE")
    if ! kill -0 "$HARNESS_PID" 2>/dev/null; then
      echo
      echo "── HARNESS TERMINATED (pid $HARNESS_PID ya no existe) ──"
      HARNESS_RUNNING=false
      break
    fi
  fi

  sleep "$INTERVAL"
done

# Reporte final.
echo
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  REPORTE FINAL"
echo "═══════════════════════════════════════════════════════════════════════════════"
if [ -f "$STATUS_FILE" ]; then
  python3 -c "
import json
with open('$STATUS_FILE') as f:
    d = json.load(f)
s = d.get('stats', {})
m = d.get('memory', {})
q = d.get('quota', {})
print(json.dumps(d, indent=2, default=str))
print()
print('─── Review file (/tmp/loop-needs-review.json) ───')
try:
    with open('/tmp/loop-needs-review.json') as f:
        items = json.load(f)
    print(f'Total: {len(items)} items')
    for it in items[:5]:
        print(f\"  - {it['file']}:{it['line']}  {it['why'][:60]}  ({it['model']})\")
except FileNotFoundError:
    print('(sin items)')
print()
print('─── Last 30 lines of harness log ───')
"
fi
tail -30 "$LOG_FILE" 2>/dev/null || true
