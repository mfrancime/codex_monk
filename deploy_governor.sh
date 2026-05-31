#!/usr/bin/env bash
# Boot the GOVERNOR — a quorum-probe agent that correlates the health of every
# live codex.*.fabric into one cluster verdict, and oversees a START WAR battle.
# With no control-plane peer present, quorum.control_ok is vacuously 1, so there
# is NO false GATE_DOWN. Appears in the war-room LIVE FORCES as 🛡️ governor.
set -e
cd "$(dirname "$0")"
rm -f /dev/shm/codex.k8s_aggregator.fabric 2>/dev/null
export CODEX_QUORUM_SELF=/dev/shm/codex.k8s_aggregator.fabric
echo "[governor] overseeing the cluster · war-room: http://127.0.0.1:19200"
exec .venv-pw/bin/python boot.py --config multiswarm.governor.yaml
