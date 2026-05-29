"""
test_cgroup_pods.py — synthetic kubepods cgroup tree → Frame aggregation.

We build a fake /sys/fs/cgroup/kubepods.slice/ in a tempdir, point the
probe at it via CODEX_CGROUP_ROOT, and assert the aggregated Frame matches
expectations. Covers:

  1. Empty kubepods → pod.count=0, all maxes=0.
  2. Three pods: one healthy, one pressured, one with rising OOM kills.
     pod.count=3, pod.max.psi_some_avg10 = the pressured one's,
     pod.count_pressured = 1, pod.sum.oom_kills = sum across three,
     and pod.delta.oom_kills_60s = 0 on first tick.
  3. Re-sample after one pod's oom_kill counter increments → delta > 0.
  4. Genome interpret() against the Frame using the cgroup_pods OPCODES
     fires the expected severity (Πo > 0 → CRITICAL).

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_cgroup_pods
"""

import os
import shutil
import sys
import tempfile

# point the probe at our synthetic tree BEFORE import so describe() picks it up
_TMP_ROOT = tempfile.mkdtemp(prefix='codex_cgroup_test_')
os.environ['CODEX_CGROUP_ROOT'] = _TMP_ROOT

from swarm.probes import cgroup_pods
from swarm.probes import get as get_probe
from swarm.genome import interpret


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _make_pod(root, name, *, psi_some=0.0, psi_full=0.0,
              mem_current=0, mem_max=0, oom_kill=0):
    """Build a synthetic pod cgroup directory with the files the probe reads."""
    pod_dir = os.path.join(root, name)
    os.makedirs(pod_dir, exist_ok=True)
    with open(os.path.join(pod_dir, 'memory.pressure'), 'w') as f:
        f.write(f'some avg10={psi_some} avg60=0.00 avg300=0.00 total=0\n')
        f.write(f'full avg10={psi_full} avg60=0.00 avg300=0.00 total=0\n')
    with open(os.path.join(pod_dir, 'memory.current'), 'w') as f:
        f.write(f'{mem_current}\n')
    with open(os.path.join(pod_dir, 'memory.max'), 'w') as f:
        f.write(f'{mem_max if mem_max else "max"}\n')
    with open(os.path.join(pod_dir, 'memory.events'), 'w') as f:
        f.write(f'low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom_kill}\n')


def _set_oom(root, name, oom_kill):
    with open(os.path.join(root, name, 'memory.events'), 'w') as f:
        f.write(f'low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom_kill}\n')


def main():
    print()
    print('== cgroup_pods probe ==')
    print(f'  synthetic root: {_TMP_ROOT}')

    # The probe is already registered — verify it's discoverable through
    # the registry.
    p = get_probe('cgroup_pods')
    _check('probe registered in plugin system', p.name == 'cgroup_pods')
    _check('opcodes include Π load',           'Π' in p.opcodes)
    _check('describe non-empty',               len(p.describe()) > 0)

    # ── case 1: empty tree ───────────────────────────────────────────────
    f = p.sample_all()
    _check('empty: pod.count = 0',              f['pod.count'] == 0)
    _check('empty: max psi.some = 0',           f['pod.max.psi_some_avg10'] == 0.0)
    _check('empty: count_pressured = 0',        f['pod.count_pressured'] == 0)
    _check('empty: oom_kills_60s = 0',          f['pod.delta.oom_kills_60s'] == 0)
    _check('empty: cgroup_root = 1 (path exists)', f['pod.cgroup_root'] == 1)

    # ── case 2: three pods, mixed ────────────────────────────────────────
    _make_pod(_TMP_ROOT, 'kubepods-pod-aaa.slice',
              psi_some=1.0, mem_current=100_000_000, mem_max=500_000_000,
              oom_kill=0)
    _make_pod(_TMP_ROOT, 'kubepods-pod-bbb.slice',
              psi_some=8.0, psi_full=1.5, mem_current=400_000_000,
              mem_max=500_000_000, oom_kill=0)
    _make_pod(_TMP_ROOT, 'kubepods-pod-ccc.slice',
              psi_some=3.0, mem_current=200_000_000, mem_max=400_000_000,
              oom_kill=2)

    f = p.sample_all()
    _check('3-pod: pod.count = 3',            f['pod.count'] == 3)
    _check('3-pod: max psi.some = 8.0',       f['pod.max.psi_some_avg10'] == 8.0)
    _check('3-pod: max psi.full = 1.5',       f['pod.max.psi_full_avg10'] == 1.5)
    _check('3-pod: max mem_pct = 80.0',       f['pod.max.mem_pct'] == 80.0)
    _check('3-pod: count_pressured = 1',      f['pod.count_pressured'] == 1)
    _check('3-pod: sum oom_kills = 2',        f['pod.sum.oom_kills'] == 2)
    # Ring baseline is the EARLIEST entry within the 60s window — which
    # is the empty-tree sample at sum=0. So delta = 2 - 0 = 2.
    _check('3-pod: oom_kills_60s = 2 (vs empty baseline)',
           f['pod.delta.oom_kills_60s'] == 2)

    # ── case 3: oom counter ticks up → delta keeps rising ───────────────
    _set_oom(_TMP_ROOT, 'kubepods-pod-ccc.slice', 5)
    f = p.sample_all()
    _check('after-oom: sum oom_kills = 5',    f['pod.sum.oom_kills'] == 5)
    _check('after-oom: delta_60s = 5 (vs empty baseline)',
           f['pod.delta.oom_kills_60s'] == 5)

    # ── case 4: genome fires CRITICAL on Πo > 0 ─────────────────────────
    # Rules in priority order: oom_kills > 0 → CRIT first (worst-case wins),
    # otherwise psi.some > 5 → WARN.
    genome = 'Πo0>→Cd;Πs5>→Wp;'
    sev, code = interpret(genome, f, p.opcodes)
    _check('genome: oom_kills > 0 fires CRITICAL', sev == 'CRITICAL')
    _check('genome: code = CLUSTER_DEGRADED',     code == 'CLUSTER_DEGRADED')

    # genome on the EMPTY frame should be OK,OK
    sev, code = interpret(genome, {'pod.max.psi_some_avg10': 0,
                                    'pod.delta.oom_kills_60s': 0},
                          p.opcodes)
    _check('genome: silent on empty',             sev == 'OK' and code == 'OK')

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    try:
        main()
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
