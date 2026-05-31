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
import random
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


# Red escalates in WAVES — each shrinks Blue's reaction window. Blue agents tick
# ~every 5s, so once the window drops below that, late attacks slip through and
# the score becomes genuinely contested instead of a walkover.
# Windows fall BELOW Blue's ~5s calm tick cadence in the later waves, so a
# fast/stealthy strike outruns Blue's polling and breaches — Red's path to win.
WAVE_WINDOWS = [9, 6, 4, 3, 2, 2]
WAVE_TURNS = 3


def _pick_target(fronts, rng):
    """Red strategy: 70% press the SOFTEST front (most net breaches, but LOW Blue
    reinforcement), else feint. Red seeks gaps and steers AROUND reinforced
    fronts — so Blue's reinforcement actually redirects the assault."""
    if rng.random() < 0.70:
        return max(PLAYBOOK, key=lambda p:
                   fronts[p[0]]['breaches'] - fronts[p[0]]['defense'] * 0.9
                   - fronts[p[0]]['blocks'] * 0.1)
    return rng.choice(PLAYBOOK)


def _award(latency, window):
    """Latency-banded scoring: fast blocks worth more; a breach hands Red points
    scaled by how hard the squeeze was."""
    if latency is None:
        return ('red', 2 if window <= 5 else 1)
    if latency <= 2:
        return ('blue', 3)
    if latency <= window * 0.6:
        return ('blue', 2)
    return ('blue', 1)


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else int(time.time())
    rng = random.Random(seed)
    deadline = time.time() + duration

    fronts = {f: {'blocks': 0, 'breaches': 0, 'last': '—', 'defense': 0}
              for f, _, _, _ in PLAYBOOK}
    score = {'blue': 0, 'red': 0}
    log = []
    turn = 0

    def snap(cur, phase, **extra):
        d = {'running': True, 'turn': turn, 'score': score, 'fronts': fronts,
             'current': cur, 'phase': phase,
             'log': log[-24:], 'duration_s': duration, 'ends_at': deadline}
        d.update(extra)
        _write(d)

    _inject('healthy')
    snap({'front': '—', 'attack': 'mustering forces…', 'phase': 'calm'}, 'calm',
         wave=1, react_window=WAVE_WINDOWS[0], strategy='Red is massing for the assault')
    time.sleep(2)

    while time.time() < deadline:
        turn += 1
        wave = turn // WAVE_TURNS
        base = WAVE_WINDOWS[min(wave, len(WAVE_WINDOWS) - 1)]
        front, aid, label, want_code = _pick_target(fronts, rng)
        defense = fronts[front]['defense']
        window = min(12, base + defense)        # 🔵 reinforcements widen the window
        soft = min(fronts, key=lambda f: fronts[f]['defense'])
        strat = (f'wave {wave + 1}: base squeeze {base}s · pressing {front}'
                 + (f' · 🛡️ Blue reinforced +{defense}s here' if defense
                    else (f' · seeking soft spot' if any(v['defense'] for v in fronts.values()) else '')))

        # 🔴 Red strikes
        snap({'front': front, 'attack': label, 'phase': 'attacking'}, 'attacking',
             wave=wave + 1, react_window=window, strategy=strat)
        _inject(front)

        # 🔵 Blue gets `window` seconds (base squeeze + its reinforcement here)
        t0 = time.time()
        sev = code = None
        latency = None
        while time.time() - t0 < window:
            time.sleep(0.8)
            sev, code = _verdict(aid)
            if sev not in (None, 'OK') and code == want_code:
                latency = round(time.time() - t0, 1)
                break

        team, pts = _award(latency, window)
        # reinforcements redeploy from the quiet fronts each turn
        for f in fronts:
            if f != front:
                fronts[f]['defense'] = max(0, fronts[f]['defense'] - 1)
        if team == 'blue':
            score['blue'] += pts
            fronts[front]['blocks'] += 1
            fronts[front]['last'] = 'BLOCKED'
            fronts[front]['defense'] = max(0, defense - 1)        # threat passed, ease off
            result, phase = f'🔵 BLOCK +{pts} ({sev}:{code} in {latency}s)', 'blocked'
        else:
            score['red'] += pts
            fronts[front]['breaches'] += 1
            fronts[front]['last'] = 'BREACH'
            fronts[front]['defense'] = min(8, defense + 3)        # 🛡️ Blue rushes reinforcements
            result, phase = f'🔴 BREACH +{pts} → 🛡️ Blue reinforces {front}', 'breached'

        log.append({'turn': turn, 'front': front, 'attack': label, 'result': result})
        snap({'front': front, 'attack': label, 'phase': phase,
              'verdict': f'{sev}:{code}', 'latency': latency}, phase,
             wave=wave + 1, react_window=window, strategy=strat)

        _inject('healthy')
        time.sleep(max(1.5, window * 0.4))

    _inject('healthy')
    winner = 'BLUE' if score['blue'] > score['red'] else (
        'RED' if score['red'] > score['blue'] else 'DRAW')
    mvp = max(fronts, key=lambda f: fronts[f]['blocks']) if fronts else None
    weakest = max(fronts, key=lambda f: fronts[f]['breaches']) if fronts else None
    _write({'running': False, 'turn': turn, 'score': score, 'fronts': fronts,
            'current': {'front': '—', 'attack': 'war over', 'phase': 'calm'},
            'wave': turn // WAVE_TURNS + 1, 'log': log[-24:], 'duration_s': duration,
            'ends_at': time.time(), 'winner': winner,
            'mvp': mvp, 'weakest': weakest,
            'summary': (f'🏅 MVP defender: {mvp} ({fronts[mvp]["blocks"]} blocks) · '
                        f'softest front: {weakest} ({fronts[weakest]["breaches"]} breaches)')
                       if mvp else ''})


if __name__ == '__main__':
    main()
