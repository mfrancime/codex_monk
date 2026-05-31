"""
deploy_telemetry.py — synthetic Kubernetes telemetry for the deployed-champion
exhibit (eval/demo tooling; touches NO swarm/ code).

Writes a cgroup_pods tree (for CODEX_CGROUP_ROOT) and a fake-apiserver JSON
(for CODEX_K8S_API_FAKE_PATH) describing a cluster in a chosen state. The
deployed swarm's probe agents — which now run the wargame's evolved CHAMPION
genomes as their live DNA — sample this telemetry every tick and emit verdicts.

Re-run with a different state WHILE the swarm is up to inject an attack live;
the probes re-read these files each tick, so the matching champion lights up
with no restart (this is the probes' built-in test-injection seam, reused).

  python deploy_telemetry.py <state> [root]
  states: healthy | pods | nodes | apiserver | scheduler
  root defaults to /tmp/codex_k8s_telemetry
"""

import json
import os
import shutil
import sys


def _healthy_pods():
    # 3 pods at ~5% of their limit, no pressure, no OOMKills
    return [dict(current=100_000_000, max=2_000_000_000, some=0.3, full=0.0, oom=0)
            for _ in range(3)]


def _write_cgroup(root, pods):
    cg = os.path.join(root, 'cgroup')
    shutil.rmtree(cg, ignore_errors=True)
    os.makedirs(cg)
    for i, p in enumerate(pods):
        d = os.path.join(cg, f'kubepods-pod{i}.slice')
        os.makedirs(d)
        with open(os.path.join(d, 'memory.current'), 'w') as f:
            f.write(str(p['current']))
        with open(os.path.join(d, 'memory.max'), 'w') as f:
            f.write(str(p['max']))
        with open(os.path.join(d, 'memory.pressure'), 'w') as f:
            f.write(f"some avg10={p['some']:.2f} avg60=0.00 avg300=0.00 total=0\n"
                    f"full avg10={p['full']:.2f} avg60=0.00 avg300=0.00 total=0\n")
        with open(os.path.join(d, 'memory.events'), 'w') as f:
            f.write(f"oom_kill {p['oom']}\n")


def _write_api(root, healthy=True, total=5, not_ready=0, degraded=0, warnings=0):
    nodes = [{'status': {'conditions': [
        {'type': 'Ready', 'status': ('False' if i < not_ready else 'True')}]}}
        for i in range(total)]
    deps = [{'status': {'replicas': 3, 'readyReplicas': 3}} for _ in range(2)]
    deps += [{'status': {'replicas': 3, 'readyReplicas': 1}} for _ in range(degraded)]
    evs = {'items': [{'type': 'Warning', 'lastTimestamp': '2026-05-30T00:00:00Z'}
                     for _ in range(warnings)]}
    table = {
        '/api/v1/nodes': {'items': nodes},
        '/api/v1/events?fieldSelector=type%3DWarning&limit=50': evs,
        '/apis/apps/v1/deployments': {'items': deps},
    }
    # omitting /healthz makes the probe read the apiserver as DOWN (healthy=0)
    if healthy:
        table['/healthz'] = {'status': 'ok'}
    with open(os.path.join(root, 'apiserver.json'), 'w') as f:
        json.dump(table, f)


# Each state = (pods telemetry, apiserver kwargs). Attacks target exactly one
# front so you can watch that champion — and only that one — fire.
STATES = {
    'healthy':   (_healthy_pods, dict()),
    'pods':      (lambda: [dict(current=1_900_000_000, max=2_000_000_000,
                                some=22.0, full=8.0, oom=5)] + _healthy_pods()[1:],
                  dict()),                              # OOMKill storm on one pod
    'nodes':     (_healthy_pods, dict(not_ready=1)),    # one kubelet NotReady
    'apiserver': (_healthy_pods, dict(healthy=False)),  # control-plane down
    'scheduler': (_healthy_pods, dict(degraded=6, warnings=12)),  # pods can't schedule
    # ── PRECURSORS: the leading edge BEFORE the breach (anticipation layer) ──
    # memory climbing toward the limit (mem_pct 88%) but no OOMKill yet — the
    # tell a mem_pct-gating champion can pre-empt on.
    'pods_pre':  (lambda: [dict(current=1_760_000_000, max=2_000_000_000,
                                some=4.0, full=1.0, oom=0)] + _healthy_pods()[1:],
                  dict()),
    # deployments degrading (4) on the way to the unschedulable storm (12).
    'scheduler_pre': (_healthy_pods, dict(degraded=4, warnings=8)),
}


def main():
    state = sys.argv[1] if len(sys.argv) > 1 else 'healthy'
    root = sys.argv[2] if len(sys.argv) > 2 else '/tmp/codex_k8s_telemetry'
    if state not in STATES:
        print(f"unknown state {state!r}; choose from {list(STATES)}")
        sys.exit(2)
    os.makedirs(root, exist_ok=True)
    pods_fn, api_kw = STATES[state]
    _write_cgroup(root, pods_fn())
    _write_api(root, **api_kw)
    print(f"telemetry[{state}] -> {root}  "
          f"(cgroup/ + apiserver.json)")


if __name__ == '__main__':
    main()
