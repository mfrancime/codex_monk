"""
war_driver.py — the START WAR engine: an autonomous Red-vs-Blue strategy battle.

THE STRATEGY GAME (the model the war-room renders):
  🗺️ Battlefield  — the Kubernetes cluster: 5 fronts (territory), each with a
                    HEALTH bar and a HOLDER (blue=held, contested, red=fallen).
  🔵 Blue army    — the live k8s_deployed champions; one unit defends each front
                    with its evolved genome (its DNA). Real swarm.
  🔴 Red army     — attacker units, one per front, each with a target + strategy.
                    Red escalates in waves and concentrates on the weak spot.
  🛡️ Governor     — Blue's commander (quorum agent): oversees every front and
                    raises the cluster alarm; its live verdict rides on the map.

Each turn: Red strikes a front → Blue's unit gets `window` seconds (base squeeze
+ its reinforcement) to detect → BLOCK (front health recovers, Blue scores by
speed) or BREACH (front health falls, Red gains ground; Blue rushes reinforcements
there). Holder flips by health. Streams the whole game state to web/war.json.

Eval/orchestration tier — no swarm/ changes.
  python war_driver.py [duration_s] [_gap] [seed]
"""

import json
import os
import random
import signal
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
FX = os.environ.get('CODEX_FX_ROOT', '/tmp/codex_k8s_telemetry')
FABRIC = '/dev/shm/codex.k8s_deployed.fabric'
GOV_FABRIC = '/dev/shm/codex.k8s_aggregator.fabric'
WAR_JSON = os.path.join(ROOT, 'web', 'war.json')
PY = os.path.join(ROOT, '.venv-pw', 'bin', 'python')
if not os.path.exists(PY):
    PY = sys.executable

# (front, blue agent id, attack label, expected Blue code, Blue champion genome)
PLAYBOOK = [
    ('pods',      2, 'OOMKiller storm',            'POD_PRESSURE',     'Πm‡83≥Πp∨Πo→Cp→Wp'),
    ('nodes',     3, 'kubelet goes NotReady',      'NODE_NOT_READY',   'Kx1>→CxKx→Wx'),
    ('apiserver', 4, 'control-plane decapitation', 'GATE_DOWN',        'Ka≥→Cg'),
    ('scheduler', 6, 'pods unschedulable',         'CLUSTER_DEGRADED', 'KdKd5>→Cd2>→Wd'),
]

WAVE_WINDOWS = [9, 6, 4, 3, 2, 2]
WAVE_TURNS = 3


def _inject(state):
    subprocess.run([PY, os.path.join(ROOT, 'deploy_telemetry.py'), state, FX],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _read_state(fabric, agent_id):
    """(sev, code) the given agent last wrote to a fabric, or (None, None)."""
    try:
        from swarm.fabric import (Fabric, MAX_STATE_SLOTS, OFF_STATE, SS_SIZE,
                                  SS_KEY, SS_VALUE, SS_WRITER, _from_bytes)
        fab = Fabric(path=fabric, create=False)
    except Exception:
        return (None, None)
    sev = code = None
    try:
        for i in range(MAX_STATE_SLOTS):
            off = OFF_STATE + i * SS_SIZE
            kb = bytes(fab.mm[off + SS_KEY: off + SS_KEY + 24]).split(b'\x00')[0]
            if not kb:
                continue
            if struct.unpack_from('<H', fab.mm, off + SS_WRITER)[0] != agent_id:
                continue
            key = kb.decode('utf-8', 'replace')
            if key.endswith('sys.sev'):
                sev = _from_bytes(fab.mm[off + SS_VALUE: off + SS_VALUE + 20])
            elif key.endswith('sys.code'):
                code = _from_bytes(fab.mm[off + SS_VALUE: off + SS_VALUE + 20])
    finally:
        try: fab.close()
        except Exception: pass
    return (sev, code)


def _governor():
    if not os.path.exists(GOV_FABRIC):
        return {'present': False, 'sev': None, 'code': None}
    sev, code = _read_state(GOV_FABRIC, 2)
    return {'present': True, 'sev': sev or 'OK', 'code': code or 'OK'}


def _write(state):
    state['updated'] = time.time()
    tmp = WAR_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, WAR_JSON)


def _holder(health):
    return 'blue' if health > 66 else ('red' if health < 34 else 'contested')


def _pick_target(fronts, rng):
    """Red presses the SOFTEST front (most net breaches, LOW reinforcement,
    LOW health), else feints — and steers around reinforced fronts."""
    if rng.random() < 0.70:
        return max(PLAYBOOK, key=lambda p:
                   fronts[p[0]]['breaches'] - fronts[p[0]]['defense'] * 0.9
                   - fronts[p[0]]['health'] * 0.02)
    return rng.choice(PLAYBOOK)


def _award(latency, window):
    if latency is None:
        return ('red', 2 if window <= 5 else 1)
    if latency <= 2:
        return ('blue', 3)
    if latency <= window * 0.6:
        return ('blue', 2)
    return ('blue', 1)


def _on_term(*_):
    try:
        with open(WAR_JSON) as f:
            w = json.load(f)
        w['running'] = False
        (w.get('current') or {}).update(phase='calm')
        with open(WAR_JSON, 'w') as f:
            json.dump(w, f)
    except Exception:
        pass
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _on_term)
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else int(time.time())
    rng = random.Random(seed)
    deadline = time.time() + duration

    GEN = {f: g for f, _, _, _, g in PLAYBOOK}
    AID = {f: aid for f, aid, _, _, _ in PLAYBOOK}
    fronts = {f: {'blocks': 0, 'breaches': 0, 'last': '—', 'defense': 0,
                  'health': 100, 'verdict': 'OK', 'latency': None}
              for f, _, _, _, _ in PLAYBOOK}
    score = {'blue': 0, 'red': 0}
    log = []
    turn = 0

    def battlefield(active=None):
        return {f: {'holder': _holder(v['health']), 'health': v['health'],
                    'under_attack': (f == active), 'verdict': v['verdict'],
                    'defense': v['defense'], 'blocks': v['blocks'],
                    'breaches': v['breaches'], 'latency': v['latency']}
                for f, v in fronts.items()}

    def armies(strategy=''):
        blue_units = [{'front': f, 'aid': AID[f], 'genome': GEN[f],
                       'health': v['health'], 'verdict': v['verdict'],
                       'blocks': v['blocks'], 'breaches': v['breaches'],
                       'holder': _holder(v['health'])}
                      for f, v in fronts.items()]
        red_units = [{'front': f, 'target': f, 'breaches': v['breaches'],
                      'pressure': v['defense']} for f, v in fronts.items()]
        return {
            'blue': {'name': 'BLUE — k8s_deployed champions',
                     'commander': 'governor', 'units': blue_units,
                     'held': sum(1 for v in fronts.values() if v['health'] > 66)},
            'red': {'name': 'RED — chaos engineers', 'units': red_units,
                    'strategy': strategy,
                    'taken': sum(1 for v in fronts.values() if v['health'] < 34)},
        }

    def snap(cur, phase, strategy='', active=None):
        wave = turn // WAVE_TURNS
        _write({'running': True, 'turn': turn, 'score': score, 'fronts': fronts,
                'current': cur, 'phase': phase, 'wave': wave + 1,
                'react_window': WAVE_WINDOWS[min(wave, len(WAVE_WINDOWS) - 1)],
                'strategy': strategy, 'battlefield': battlefield(active),
                'armies': armies(strategy), 'governor': _governor(),
                'blue_fabric': FABRIC,
                'log': log[-24:], 'duration_s': duration, 'ends_at': deadline})

    _inject('healthy')
    snap({'front': '—', 'attack': 'mustering forces…', 'phase': 'calm'}, 'calm',
         strategy='Red is massing for the assault')
    time.sleep(2)

    while time.time() < deadline:
        turn += 1
        wave = turn // WAVE_TURNS
        base = WAVE_WINDOWS[min(wave, len(WAVE_WINDOWS) - 1)]
        front, aid, label, want_code, _g = _pick_target(fronts, rng)
        defense = fronts[front]['defense']
        window = min(12, base + defense)
        strat = (f'wave {wave + 1}: base squeeze {base}s · storming {front}'
                 + (f' · 🛡️ Blue reinforced +{defense}s' if defense else ''))

        # 🔴 Red strikes
        snap({'front': front, 'attack': label, 'phase': 'attacking'},
             'attacking', strategy=strat, active=front)
        _inject(front)

        t0 = time.time()
        sev = code = None
        latency = None
        while time.time() - t0 < window:
            time.sleep(0.8)
            sev, code = _read_state(FABRIC, aid)
            if sev not in (None, 'OK') and code == want_code:
                latency = round(time.time() - t0, 1)
                break

        team, pts = _award(latency, window)
        for f in fronts:
            if f != front:
                fronts[f]['defense'] = max(0, fronts[f]['defense'] - 1)
                fronts[f]['health'] = min(100, fronts[f]['health'] + 2)   # quiet fronts recover
        fronts[front]['verdict'] = f'{sev or "OK"}:{code or "OK"}'
        fronts[front]['latency'] = latency
        if team == 'blue':
            score['blue'] += pts
            fronts[front]['blocks'] += 1
            fronts[front]['last'] = 'BLOCKED'
            fronts[front]['defense'] = max(0, defense - 1)
            fronts[front]['health'] = min(100, fronts[front]['health'] + 6)
            result, phase = f'🔵 HELD {front} +{pts} ({sev}:{code} in {latency}s)', 'blocked'
        else:
            score['red'] += pts
            fronts[front]['breaches'] += 1
            fronts[front]['last'] = 'BREACH'
            fronts[front]['defense'] = min(8, defense + 3)
            fronts[front]['health'] = max(0, fronts[front]['health'] - 18)
            result, phase = f'🔴 BREACH {front} +{pts} → 🛡️ Blue reinforces', 'breached'

        log.append({'turn': turn, 'front': front, 'attack': label, 'result': result})
        snap({'front': front, 'attack': label, 'phase': phase,
              'verdict': f'{sev}:{code}', 'latency': latency},
             phase, strategy=strat, active=front)

        _inject('healthy')
        time.sleep(max(1.5, window * 0.4))

    _inject('healthy')
    winner = 'BLUE' if score['blue'] > score['red'] else (
        'RED' if score['red'] > score['blue'] else 'DRAW')
    mvp = max(fronts, key=lambda f: fronts[f]['blocks'])
    fell = [f for f, v in fronts.items() if v['health'] < 34]
    _write({'running': False, 'turn': turn, 'score': score, 'fronts': fronts,
            'current': {'front': '—', 'attack': 'war over', 'phase': 'calm'},
            'wave': turn // WAVE_TURNS + 1, 'battlefield': battlefield(),
            'armies': armies(), 'governor': _governor(),
            'log': log[-24:], 'duration_s': duration, 'ends_at': time.time(),
            'winner': winner, 'mvp': mvp,
            'summary': (f'🏅 MVP defender: {mvp} ({fronts[mvp]["blocks"]} holds) · '
                        + (f'fronts fallen: {", ".join(fell)}' if fell
                           else 'Blue held the whole cluster'))})


if __name__ == '__main__':
    main()
