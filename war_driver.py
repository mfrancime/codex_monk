"""
war_driver.py — autonomous Red-vs-Blue battle (the START WAR engine).

Drives a live game between two teams against the running k8s_deployed swarm
(the Blue champions running as DNA):

  🔴 RED  — auto-cycles attacks across the fronts (deploy_telemetry injects a
            failure state into the synthetic cluster the Blue agents watch).
  🔵 BLUE — the evolved champion genomes; each tick they sense the injected
            telemetry and emit a verdict.

Each turn: Red attacks a front → wait for Blue to tick → read the Blue agent's
verdict from the fabric → score it a BLOCK (Blue caught it) or a BREACH (Red got
through) → stand down to healthy → next front. Live state streams to web/war.json
for the war-room battle view. Eval/orchestration tier; no swarm/ changes.

  python war_driver.py [duration_s] [turn_gap_s]
"""

import json
import os
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
FX = os.environ.get('CODEX_FX_ROOT', '/tmp/codex_k8s_telemetry')
FABRIC = '/dev/shm/codex.k8s_deployed.fabric'
WAR_JSON = os.path.join(ROOT, 'web', 'war.json')
PY = os.path.join(ROOT, '.venv-pw', 'bin', 'python')
if not os.path.exists(PY):
    PY = sys.executable

# Red's playbook: (front, blue agent id, attack label, expected Blue code).
# etcd is omitted — its native signal (latency) isn't injectable via the fake seam.
PLAYBOOK = [
    ('pods',      2, 'OOMKiller storm',        'POD_PRESSURE'),
    ('nodes',     3, 'kubelet goes NotReady',  'NODE_NOT_READY'),
    ('apiserver', 4, 'control-plane decapitation', 'GATE_DOWN'),
    ('scheduler', 6, 'pods unschedulable',     'CLUSTER_DEGRADED'),
]


def _inject(state):
    subprocess.run([PY, os.path.join(ROOT, 'deploy_telemetry.py'), state, FX],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _verdict(agent_id):
    """Read (sev, code) the given Blue agent last wrote to the fabric."""
    try:
        from swarm.fabric import (Fabric, MAX_STATE_SLOTS, OFF_STATE, SS_SIZE,
                                  SS_KEY, SS_VALUE, SS_WRITER, _from_bytes)
        fab = Fabric(path=FABRIC, create=False)
    except Exception:
        return (None, None)
    sev = code = None
    try:
        for i in range(MAX_STATE_SLOTS):
            off = OFF_STATE + i * SS_SIZE
            kb = bytes(fab.mm[off + SS_KEY: off + SS_KEY + 24]).split(b'\x00')[0]
            if not kb:
                continue
            w = struct.unpack_from('<H', fab.mm, off + SS_WRITER)[0]
            if w != agent_id:
                continue
            key = kb.decode('utf-8', 'replace')
            val = _from_bytes(fab.mm[off + SS_VALUE: off + SS_VALUE + 20])
            if key == 'sys.sev':
                sev = val
            elif key == 'sys.code':
                code = val
    finally:
        try: fab.close()
        except Exception: pass
    return (sev, code)


def _write(state):
    state['updated'] = time.time()
    tmp = WAR_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, WAR_JSON)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 7.0
    deadline = time.time() + duration

    fronts = {f: {'blocks': 0, 'breaches': 0, 'last': '—'} for f, _, _, _ in PLAYBOOK}
    score = {'blue': 0, 'red': 0}
    log = []
    turn = 0

    _inject('healthy')
    _write({'running': True, 'turn': 0, 'score': score, 'fronts': fronts,
            'current': {'front': '—', 'attack': 'mustering forces…', 'phase': 'calm'},
            'log': [], 'duration_s': duration, 'ends_at': deadline})
    time.sleep(2)

    i = 0
    while time.time() < deadline:
        turn += 1
        front, aid, label, want_code = PLAYBOOK[i % len(PLAYBOOK)]
        i += 1

        # 🔴 Red attacks
        _write({'running': True, 'turn': turn, 'score': score, 'fronts': fronts,
                'current': {'front': front, 'attack': label, 'phase': 'attacking'},
                'log': log[-24:], 'duration_s': duration, 'ends_at': deadline})
        _inject(front)

        # 🔵 Blue gets up to `gap` seconds to react — POLL its verdict each tick
        # so a correct block is credited the moment it fires (no fixed-wait
        # breach artifacts from reading before the agent ticked).
        sev = code = None
        blocked = False
        t_end = time.time() + max(gap, 8.0)
        while time.time() < t_end:
            time.sleep(1.0)
            sev, code = _verdict(aid)
            if sev not in (None, 'OK') and code == want_code:
                blocked = True
                break
        if blocked:
            score['blue'] += 1
            fronts[front]['blocks'] += 1
            fronts[front]['last'] = 'BLOCKED'
            result, phase = f'🔵 BLOCKED ({sev}:{code})', 'blocked'
        else:
            score['red'] += 1
            fronts[front]['breaches'] += 1
            fronts[front]['last'] = 'BREACH'
            result, phase = f'🔴 BREACH (Blue said {sev}:{code})', 'breached'

        log.append({'turn': turn, 'front': front, 'attack': label, 'result': result})
        _write({'running': True, 'turn': turn, 'score': score, 'fronts': fronts,
                'current': {'front': front, 'attack': label, 'phase': phase,
                            'verdict': f'{sev}:{code}'},
                'log': log[-24:], 'duration_s': duration, 'ends_at': deadline})

        # 🔵 Blue stands the cluster back down
        _inject('healthy')
        time.sleep(max(2.0, gap * 0.6))

    _inject('healthy')
    _write({'running': False, 'turn': turn, 'score': score, 'fronts': fronts,
            'current': {'front': '—', 'attack': 'war over', 'phase': 'calm'},
            'log': log[-24:], 'duration_s': duration, 'ends_at': time.time(),
            'winner': 'BLUE' if score['blue'] >= score['red'] else 'RED'})


if __name__ == '__main__':
    main()
