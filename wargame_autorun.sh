#!/usr/bin/env bash
# wargame_autorun.sh — autonomous Kubernetes-wargame evolution driver.
#
# Runs the co-evolution arms race (wargame.py) on a fixed wall-clock budget,
# committing + pushing the evolution progress (lineage + champions + web tab
# data) after every tick. Resilient: a failing round or push never stops the
# loop. Eval/orchestration tier — no swarm behavior lives here.
#
# Usage:  ./wargame_autorun.sh [DURATION_S] [GAP_S] [GENS] [LAM]
#   defaults: 8h budget, 900s between ticks, 350 gens, 40 lambda
set -u
cd "$(dirname "$0")"

BRANCH=wargame-k8s-evolution
DUR=${1:-28800}      # 8 hours
GAP=${2:-900}        # 15 min between ticks
GENS=${3:-350}
LAM=${4:-40}
PY=.venv-pw/bin/python
LOG=graph/wargame_autorun.log

mkdir -p graph
END=$(( $(date +%s) + DUR ))
TICK=0
echo "[autorun] START $(date -u +%FT%TZ) dur=${DUR}s gap=${GAP}s gens=$GENS lam=$LAM branch=$BRANCH" >> "$LOG"

while [ "$(date +%s)" -lt "$END" ]; do
  TICK=$((TICK + 1))
  echo "[autorun] --- tick $TICK $(date -u +%FT%TZ) ---" >> "$LOG"

  "$PY" wargame.py --rounds 1 --gens "$GENS" --lam "$LAM" >> "$LOG" 2>&1 \
    || echo "[autorun] round error (continuing)" >> "$LOG"

  SUMMARY=$("$PY" - <<'PY' 2>/dev/null
import json
try:
    d = json.load(open('web/wargame.json'))
    parts = [f"{n}:r{f['current_rung']+1}/{f['rungs_total']}m{f['mastered_rung']+1}"
             for n, f in d['fronts'].items()]
    print(f"rounds={d['rounds_total']} " + " ".join(parts))
except Exception:
    print("summary-unavailable")
PY
)
  git add wargame/lineage.jsonl wargame/CHAMPIONS.md web/wargame.json >> "$LOG" 2>&1
  if git commit -q -m "wargame autorun tick $TICK — $SUMMARY" >> "$LOG" 2>&1; then
    git push -q origin "$BRANCH" >> "$LOG" 2>&1 \
      && echo "[autorun] pushed tick $TICK: $SUMMARY" >> "$LOG" \
      || echo "[autorun] push FAILED tick $TICK" >> "$LOG"
  else
    echo "[autorun] nothing to commit tick $TICK" >> "$LOG"
  fi

  REMAIN=$(( END - $(date +%s) ))
  [ "$REMAIN" -le 5 ] && break
  SLEEP=$GAP; [ "$GAP" -gt "$REMAIN" ] && SLEEP=$REMAIN
  sleep "$SLEEP"
done

echo "[autorun] DONE $(date -u +%FT%TZ) ticks=$TICK" >> "$LOG"
