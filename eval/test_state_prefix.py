"""
test_state_prefix.py — state_prefix is the multiswarm's auditability lever.

Each sub-swarm in a multiswarm declares a `state_prefix: "swarm.<name>."`.
Every Agent.write_state / read_state call transparently prefixes that key,
so the kernel swarm's `sys.psi.some10` lives at `swarm.kernel.sys.psi.some10`
and the evolver swarm's at `swarm.evolver.sys.psi.some10`. They can share
nothing else (separate fabrics) but a future governor can introspect both
just by looking at the prefixed keys.

Two things must hold:

  1. PREFIXED: an agent created with state_prefix='swarm.kernel.' writes
     to the prefixed key and NOT the bare key. Genome-driven state writes
     (sys.sev, sys.code, sys.psi.some10, ...) all get the prefix.

  2. BACKWARD-COMPATIBLE: an agent created without state_prefix (default
     '') writes exactly where every previous test expected.

If 1 fails, audit data from different sub-swarms collides. If 2 fails,
every existing test breaks.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_state_prefix
"""

import os
import sys
import tempfile

from swarm.fabric import Fabric
from swarm import template
import swarm.agents.declarative as declarative


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _frame(psi_some, psi_full, used_pct, swap_present):
    total = 100_000
    avail = int(total * (1.0 - used_pct / 100.0))
    swap_kb = 1_048_576 if swap_present else 0
    return {
        'ts':                  0.0,
        'psi.available':       True,
        'psi.some.avg10':      psi_some,
        'psi.some.avg60':      0.0,
        'psi.full.avg10':      psi_full,
        'psi.full.avg60':      0.0,
        'mem.total_kb':        total,
        'mem.available_kb':    avail,
        'mem.used_pct':        used_pct,
        'mem.avail_pct':       100.0 - used_pct,
        'mem.swap_total_kb':   swap_kb,
        'mem.swap_present':    swap_present,
        'mem.swap_total_mb':   swap_kb / 1024.0,
        'cgroup.available':    False,
        'cgroup.current_bytes': 0,
        'cgroup.oom_kills':    0,
    }


class FrameStub:
    def __init__(self):
        self.current = _frame(0, 0, 30, True)
    def __call__(self):
        return self.current


def _fabric_path(suffix):
    return os.path.join(tempfile.gettempdir(),
                        f'codex_monk_test_prefix_{suffix}.fabric')


def _make_probe(state_prefix=''):
    cfg = {
        'genome': 'ψs‡10>→Ww;',
        'narrator_id': 1,
        'calm_interval': 0,
        'alert_interval': 0,
        'state_prefix': state_prefix,
    }
    return template.create_agent('declarative', 7, 7, 1, cfg)


def _drive(agent, frame, stub):
    stub.current = frame
    agent._next_due = 0.0
    agent.on_tick()


def main():
    print()
    print('== state_prefix ==')

    stub = FrameStub()
    declarative.sample_all = stub

    spike = _frame(psi_some=12, psi_full=0, used_pct=50, swap_present=True)

    # ── case 1: with prefix ────────────────────────────────────────────────
    path = _fabric_path('prefixed')
    if os.path.exists(path): os.remove(path)
    fabric = Fabric(path=path, create=True)
    try:
        sensor = _make_probe(state_prefix='swarm.kernel.')
        sensor.attach(fabric)
        _drive(sensor, spike, stub)

        # The probe writes sys.sev, sys.code, sys.psi.some10, etc.
        # With state_prefix='swarm.kernel.', those land at the prefixed keys.
        sev_prefixed, _   = fabric.state_get('swarm.kernel.sys.sev')
        sev_bare, _       = fabric.state_get('sys.sev')
        some_prefixed, _  = fabric.state_get('swarm.kernel.sys.psi.some10')
        some_bare, _      = fabric.state_get('sys.psi.some10')

        # The agent's read_state(key) must read from the SAME prefixed slot.
        sev_via_read, _   = sensor.read_state('sys.sev')

        # read_state_raw bypasses the prefix — for cross-swarm introspection.
        sev_raw, _        = sensor.read_state_raw('swarm.kernel.sys.sev')

        print()
        print(f"  [with prefix 'swarm.kernel.']")
        print(f"    swarm.kernel.sys.sev = {sev_prefixed!r}")
        print(f"    sys.sev (bare)       = {sev_bare!r}")
        print(f"    swarm.kernel.sys.psi.some10 = {some_prefixed!r}")
        print(f"    agent.read_state('sys.sev')      = {sev_via_read!r}")
        print(f"    agent.read_state_raw(full key)   = {sev_raw!r}")

        _check('write_state lands at prefixed key',  sev_prefixed == 'WARN')
        _check('write_state does NOT land at bare key', sev_bare in (None, ''))
        _check('multi-segment keys also prefix',     some_prefixed == '12.00')
        _check('bare psi key empty',                 some_bare in (None, ''))
        _check('read_state reads through prefix',    sev_via_read == 'WARN')
        _check('read_state_raw bypasses prefix',     sev_raw == 'WARN')
    finally:
        fabric.close()
        if os.path.exists(path): os.remove(path)

    # ── case 2: no prefix (default — backward compatible) ─────────────────
    path = _fabric_path('bare')
    if os.path.exists(path): os.remove(path)
    fabric = Fabric(path=path, create=True)
    try:
        sensor = _make_probe(state_prefix='')
        sensor.attach(fabric)
        _drive(sensor, spike, stub)

        sev_bare, _ = fabric.state_get('sys.sev')
        sev_via_read, _ = sensor.read_state('sys.sev')

        print()
        print(f"  [no prefix — default]")
        print(f"    sys.sev              = {sev_bare!r}")
        print(f"    agent.read_state('sys.sev') = {sev_via_read!r}")

        _check('default prefix writes bare key',   sev_bare == 'WARN')
        _check('default prefix reads bare key',    sev_via_read == 'WARN')
    finally:
        fabric.close()
        if os.path.exists(path): os.remove(path)

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
