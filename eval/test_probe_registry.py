"""
test_probe_registry.py — the probe plugin contract.

What this proves:
  1. The four built-in probes (kernel, cgroup_pods, disk_net, k8s_api)
     all register themselves at import time.
  2. Each exposes the (sample_all, opcodes, describe) contract correctly.
  3. Opcode first-chars are disjoint — no domain steps on another's
     alphabet. This is a soft invariant for the framework; if a future
     domain reuses ψ or Π, agents that load both probes will see a
     surprise. The test enforces it now so the surprise is loud.
  4. `probes.get('nope')` raises KeyError with the registered list,
     not silently returns None.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_probe_registry
"""

import sys

from swarm import probes


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def main():
    print()
    print('== probe registry ==')

    reg = probes.list_probes()
    print(f'  registered: {sorted(reg)}')

    expected = {'kernel', 'cgroup_pods', 'disk_net', 'k8s_api'}
    _check('all four built-ins registered', expected.issubset(reg.keys()))

    for name in sorted(expected):
        p = probes.get(name)
        _check(f'{name}: name field matches',     p.name == name)
        _check(f'{name}: sample_all callable',    callable(p.sample_all))
        _check(f'{name}: opcodes is dict',        isinstance(p.opcodes, dict))
        _check(f'{name}: opcodes has ≥ 1 entry',  len(p.opcodes) >= 1)
        _check(f'{name}: describe returns str',   isinstance(p.describe(), str))
        # opcode shape: {first_char: {sig_char: frame_key}}
        for first, sub in p.opcodes.items():
            _check(f'{name}: opcode key {first!r} is 1-char',
                   isinstance(first, str) and len(first) == 1)
            _check(f'{name}: opcode {first!r} maps to dict',
                   isinstance(sub, dict))

    # disjoint first-chars
    seen = {}
    collisions = []
    for name, p in reg.items():
        for first in p.opcodes:
            if first in seen and seen[first] != name:
                collisions.append((first, seen[first], name))
            else:
                seen[first] = name
    _check(f'opcode first-chars are disjoint (got {len(collisions)} collisions)',
           not collisions)

    # unknown probe raises with the registered list visible
    try:
        probes.get('definitely-not-a-probe')
        ok = False
        err = 'no KeyError raised'
    except KeyError as e:
        ok = 'registered' in str(e)
        err = str(e)
    _check('unknown probe raises KeyError with registered list', ok)

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
