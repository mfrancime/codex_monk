#!/usr/bin/env bash
# deploy_champions.sh — run the wargame's evolved champion genomes as LIVE DNA.
#
# Generates synthetic Kubernetes telemetry, points the cgroup_pods + k8s_api
# probes at it via their env seams, and boots k8s_deployed.yaml — a swarm whose
# five Blue probe agents each run an evolved champion genome. The fabric
# (/dev/shm/codex.k8s_deployed.fabric) shows up in the war-room automatically.
#
#   ./deploy_champions.sh                 # boot against a HEALTHY cluster (green)
#
# Inject a live Red attack while it runs (no restart — probes re-read each tick):
#   python deploy_telemetry.py pods       # OOMKill storm   -> Blue id 2 fires
#   python deploy_telemetry.py nodes      # kubelet down    -> Blue id 3 fires
#   python deploy_telemetry.py apiserver  # control-plane   -> Blue id 4 fires
#   python deploy_telemetry.py scheduler  # unschedulable   -> Blue id 6 fires
#   python deploy_telemetry.py healthy    # stand down      -> all green again
set -e
cd "$(dirname "$0")"
FX="${CODEX_FX_ROOT:-/tmp/codex_k8s_telemetry}"
PY=.venv-pw/bin/python

"$PY" deploy_telemetry.py "${1:-healthy}" "$FX"
export CODEX_CGROUP_ROOT="$FX/cgroup"
export CODEX_K8S_API_FAKE_PATH="$FX/apiserver.json"
export CODEX_K8S_API_FAKE_HEALTHY=1
echo "[deploy] champions loaded as live DNA · telemetry=$FX · war-room: http://127.0.0.1:19200"
exec "$PY" boot.py --config k8s_deployed.yaml
