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

# Red is a REAL swarm: war_driver (Red's command) maintains codex.red.fabric with
# one attacker unit per front — a live agent with an "attack genome" (its vector)
# and live status. The war-room shows it as a swarm sphere you can inspect, just
# like Blue. Symbolic attack DNA (↑ drive up, ↓ drive down the signal it abuses):
RED_FABRIC = '/dev/shm/codex.red.fabric'
ATTACK_DNA = {'pods': 'Πo↑Πm↑→breach', 'nodes': 'Ωr↑→breach',
              'apiserver': 'Ka↓→breach', 'scheduler': 'Kd↑→breach'}

# 🔮 ANTICIPATION — fronts that TELEGRAPH (a leading indicator rises before the
# breach). If a Blue unit detects the precursor it PRE-EMPTS: the breach never
# lands (prevention = negative MTTR), scored above a reactive block. Only fronts
# whose champion gates on a continuous signal can pre-empt; the rest are sudden.
PRECURSOR = {'pods': 'pods_pre', 'scheduler': 'scheduler_pre'}

# 🥷 STEALTH — Red infiltrates UNDER Blue's detection threshold (sub-threshold
# erosion). Evades a default champion entirely; Blue can only catch it by gating
# TIGHTER (the user's intervention), at the risk of decoy false-positives. Red's
# real path to win. Used more as the war drags on (escalating cunning).
STEALTH = {'pods': 'pods_stealth', 'scheduler': 'scheduler_stealth'}

# 🎚️ DIFFICULTY — how lethal the Red swarm is, chosen at START WAR (env-based,
# like the rest of codex_monk's runtime config). Scales the reaction window Blue
# gets, how often Red goes stealth / concentrates on the soft front, and how hard
# each hit erodes ground. RECRUIT = Blue romps; ELITE = Red is a killer.
DIFFICULTY = os.environ.get('CODEX_WAR_DIFFICULTY', 'veteran').lower()
DIFF_CFG = {
    'recruit': {'win_mult': 1.8, 'stealth_cap': 0.25, 'stealth_base': 0.10,
                'soft_p': 0.55, 'erode': 12, 'breach': 12},
    'veteran': {'win_mult': 1.0, 'stealth_cap': 0.75, 'stealth_base': 0.20,
                'soft_p': 0.72, 'erode': 17, 'breach': 18},
    'elite':   {'win_mult': 0.6, 'stealth_cap': 0.92, 'stealth_base': 0.35,
                'soft_p': 0.88, 'erode': 23, 'breach': 24},
}
DC = DIFF_CFG.get(DIFFICULTY, DIFF_CFG['veteran'])


class RedSwarm:
    """Red army as a live fabric of attacker units (no new agent class — the war
    command writes its units' control blocks + state directly)."""

    def __init__(self):
        from swarm.fabric import (Fabric, ACB_TYPE, ACB_STATE, ACB_PID,
                                  ACB_HEARTBEAT, S_RUNNING)
        from swarm import dna_storage
        self._A = (ACB_TYPE, ACB_STATE, ACB_PID, ACB_HEARTBEAT, S_RUNNING)
        try:
            os.remove(RED_FABRIC)
        except OSError:
            pass
        self.fab = Fabric(path=RED_FABRIC, create=True)
        self.units = {}
        for i, (f, *_rest) in enumerate(PLAYBOOK, start=1):
            self.units[f] = i
            self.fab.acb_w(i, ACB_TYPE, 7)
            self.fab.acb_w(i, ACB_STATE, S_RUNNING)
            self.fab.acb_w(i, ACB_PID, os.getpid(), '<I')
            self.fab.acb_w(i, ACB_HEARTBEAT, int(time.time()), '<Q')
            dna_storage.write(self.fab, i, ATTACK_DNA.get(f, '?'), writer=i)
            self.fab.state_set('sys.mode', 'red-attacker', i)
            self.fab.state_set('red.target', f, i)
            self.fab.state_set('red.status', 'ready', i)
            self.fab.state_set('sys.sev', 'OK', i)
            self.fab.state_set('sys.code', 'OK', i)

    def _set(self, front, status, sev, code, **kv):
        i = self.units[front]
        self.fab.acb_w(i, self._A[3], int(time.time()), '<Q')   # ACB_HEARTBEAT
        self.fab.state_set('red.status', status, i)
        self.fab.state_set('sys.sev', sev, i)
        self.fab.state_set('sys.code', code, i)
        for k, v in kv.items():
            self.fab.state_set(k, str(v)[:20], i)

    def beat(self):
        for i in self.units.values():
            self.fab.acb_w(i, self._A[3], int(time.time()), '<Q')

    def attack(self, front, label):
        self._set(front, 'ATTACKING', 'WARN', 'ASSAULT', **{'red.attack': label})

    def result(self, front, breached):
        self._set(front, 'BREACHED' if breached else 'REPELLED',
                  'CRITICAL' if breached else 'OK',
                  'BREACH' if breached else 'REPELLED')

    def idle(self, front):
        self._set(front, 'ready', 'OK', 'OK')

    def close(self):
        try:
            self.fab.close()
        except Exception:
            pass


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
    """Red presses the SOFTEST front (breaches + infiltrations, low reinforcement,
    low health) — but ABANDONS a front it's already taken to go take the next,
    so the assault spreads instead of overkilling one corpse."""
    if rng.random() < DC['soft_p']:
        def soft(p):
            v = fronts[p[0]]
            taken = -60 if v['health'] < 25 else -v['health'] * 0.02
            return v['breaches'] + v['stealth'] * 0.6 - v['defense'] * 0.9 + taken
        return max(PLAYBOOK, key=soft)
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
                  'health': 100, 'verdict': 'OK', 'latency': None, 'preempts': 0,
                  'stealth': 0}
              for f, _, _, _, _ in PLAYBOOK}
    score = {'blue': 0, 'red': 0, 'prevented': 0, 'infiltrated': 0}
    log = []
    turn = 0

    try:
        red = RedSwarm()
    except Exception:
        red = None

    def _r(method, *a):
        if red:
            try:
                getattr(red, method)(*a)
            except Exception:
                pass

    def battlefield(active=None):
        return {f: {'holder': _holder(v['health']), 'health': v['health'],
                    'under_attack': (f == active), 'verdict': v['verdict'],
                    'defense': v['defense'], 'blocks': v['blocks'],
                    'breaches': v['breaches'], 'latency': v['latency'],
                    'preempts': v['preempts'], 'telegraphs': (f in PRECURSOR),
                    'stealth': v['stealth']}
                for f, v in fronts.items()}

    def armies(strategy=''):
        blue_units = [{'front': f, 'aid': AID[f], 'genome': GEN[f],
                       'health': v['health'], 'verdict': v['verdict'],
                       'blocks': v['blocks'], 'breaches': v['breaches'],
                       'preempts': v['preempts'], 'holder': _holder(v['health'])}
                      for f, v in fronts.items()]
        red_units = [{'front': f, 'target': f, 'genome': ATTACK_DNA.get(f, '?'),
                      'breaches': v['breaches'], 'pressure': v['defense']}
                     for f, v in fronts.items()]
        return {
            'blue': {'name': 'BLUE — k8s_deployed champions',
                     'commander': 'governor', 'units': blue_units,
                     'held': sum(1 for v in fronts.values() if v['health'] > 66)},
            'red': {'name': 'RED — chaos engineers', 'units': red_units,
                    'strategy': strategy,
                    'taken': sum(1 for v in fronts.values() if v['health'] < 34),
                    'infiltrations': sum(v['stealth'] for v in fronts.values())},
        }

    def snap(cur, phase, strategy='', active=None):
        wave = turn // WAVE_TURNS
        _write({'running': True, 'turn': turn, 'score': score, 'fronts': fronts,
                'current': cur, 'phase': phase, 'wave': wave + 1,
                'react_window': WAVE_WINDOWS[min(wave, len(WAVE_WINDOWS) - 1)],
                'strategy': strategy, 'battlefield': battlefield(active),
                'armies': armies(strategy), 'governor': _governor(),
                'blue_fabric': FABRIC, 'red_fabric': RED_FABRIC,
                'difficulty': DIFFICULTY,
                'log': log[-24:], 'duration_s': duration, 'ends_at': deadline})

    _inject('healthy')
    snap({'front': '—', 'attack': 'mustering forces…', 'phase': 'calm'}, 'calm',
         strategy='Red is massing for the assault')
    time.sleep(2)

    while time.time() < deadline:
        turn += 1
        wave = turn // WAVE_TURNS
        base = max(1, round(WAVE_WINDOWS[min(wave, len(WAVE_WINDOWS) - 1)]
                            * DC['win_mult']))
        front, aid, label, want_code, _g = _pick_target(fronts, rng)
        defense = fronts[front]['defense']
        window = min(12, base + defense)
        strat = (f'wave {wave + 1}: base squeeze {base}s · storming {front}'
                 + (f' · 🛡️ Blue reinforced +{defense}s' if defense else ''))

        # 🥷 STEALTH — more likely as the war drags on. Red slips UNDER the
        # detector's threshold for silent erosion. Only a tighter genome catches it.
        if front in STEALTH and rng.random() < min(DC['stealth_cap'],
                                                   DC['stealth_base'] * (wave + 1)):
            _r('beat')
            _r('attack', front, 'stealth infiltration')
            snap({'front': front, 'attack': 'going dark', 'phase': 'stealth'},
                 'stealth', strategy=strat + ' · 🥷 sub-threshold', active=front)
            _inject(STEALTH[front])
            caught = False
            ssev = scode = None
            t0 = time.time()
            while time.time() - t0 < max(4, base):
                time.sleep(0.8)
                ssev, scode = _read_state(FABRIC, aid)
                if ssev not in (None, 'OK') and scode == want_code:
                    caught = True
                    break
            if caught:                                  # a tight genome saw it
                score['blue'] += 3
                fronts[front]['blocks'] += 1
                fronts[front]['last'] = 'CAUGHT'
                fronts[front]['verdict'] = f'{ssev}:{scode}'
                result, phase = f'🔵 CAUGHT a stealth attack on {front}! +3', 'blocked'
            else:                                       # slipped under the radar
                score['red'] += 3
                score['infiltrated'] += 1
                fronts[front]['stealth'] += 1
                fronts[front]['last'] = 'STEALTH'
                fronts[front]['health'] = max(0, fronts[front]['health'] - DC['erode'])
                fronts[front]['verdict'] = 'undetected'
                result, phase = f'🥷 RED INFILTRATED {front} +2 — silent erosion', 'stealth_hit'
            log.append({'turn': turn, 'front': front, 'attack': 'stealth', 'result': result})
            snap({'front': front, 'attack': 'infiltration', 'phase': phase,
                  'verdict': fronts[front]['verdict']}, phase, strategy=strat, active=front)
            _inject('healthy')
            _r('idle', front)
            time.sleep(max(1.5, base * 0.4))
            continue

        # 🔮 ANTICIPATION — if this front telegraphs, Red shows the leading edge
        # first and Blue may PRE-EMPT (detect the precursor → breach prevented).
        if front in PRECURSOR:
            _r('beat')
            _r('attack', front, label)
            snap({'front': front, 'attack': '⚠ ' + label + ' brewing', 'phase': 'preempting'},
                 'preempting', strategy=strat + ' · 🔮 telegraphed', active=front)
            _inject(PRECURSOR[front])
            t0 = time.time()
            psev = pcode = None
            plat = None
            while time.time() - t0 < max(5, base + 1):
                time.sleep(0.8)
                psev, pcode = _read_state(FABRIC, aid)
                if psev not in (None, 'OK') and pcode == want_code:
                    plat = round(time.time() - t0, 1)
                    break
            if plat is not None:                       # 🔵 Blue saw it coming
                score['blue'] += 4
                score['prevented'] += 1
                fronts[front]['blocks'] += 1
                fronts[front]['preempts'] += 1
                fronts[front]['last'] = 'PRE-EMPT'
                fronts[front]['health'] = min(100, fronts[front]['health'] + 10)
                fronts[front]['verdict'] = f'{psev}:{pcode}'
                fronts[front]['latency'] = plat
                result = f'🔮 PRE-EMPTED {front} +4 — breach prevented ({psev} in {plat}s)'
                log.append({'turn': turn, 'front': front, 'attack': label, 'result': result})
                snap({'front': front, 'attack': label, 'phase': 'preempted',
                      'verdict': f'{psev}:{pcode}', 'latency': plat}, 'preempted',
                     strategy=strat, active=front)
                _inject('healthy')
                _r('idle', front)
                time.sleep(max(1.5, base * 0.4))
                continue                               # breach never lands

        # 🔴 Red strikes — its attacker unit goes live in codex.red.fabric
        _r('beat')
        _r('attack', front, label)
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
        _r('result', front, team == 'red')
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
            fronts[front]['health'] = max(0, fronts[front]['health'] - DC['breach'])
            result, phase = f'🔴 BREACH {front} +{pts} → 🛡️ Blue reinforces', 'breached'

        log.append({'turn': turn, 'front': front, 'attack': label, 'result': result})
        snap({'front': front, 'attack': label, 'phase': phase,
              'verdict': f'{sev}:{code}', 'latency': latency},
             phase, strategy=strat, active=front)

        _inject('healthy')
        _r('idle', front)
        time.sleep(max(1.5, window * 0.4))

    _inject('healthy')
    _r('beat')
    # Victory is TERRITORIAL: who holds more of the cluster at the end. Stealth
    # erosion can hand Red the ground even while Blue leads on points. Ties break
    # on score (raw blocks/prevents vs breaches/infiltrations).
    blue_holds = sum(1 for v in fronts.values() if v['health'] >= 50)
    red_holds = len(fronts) - blue_holds
    if red_holds > blue_holds:
        winner = 'RED'
    elif blue_holds > red_holds:
        winner = 'BLUE'
    else:
        winner = 'BLUE' if score['blue'] >= score['red'] else 'RED'
    mvp = max(fronts, key=lambda f: fronts[f]['blocks'] + fronts[f]['preempts'])
    fell = [f for f, v in fronts.items() if v['health'] < 50]
    _write({'running': False, 'turn': turn, 'score': score, 'fronts': fronts,
            'current': {'front': '—', 'attack': 'war over', 'phase': 'calm'},
            'wave': turn // WAVE_TURNS + 1, 'battlefield': battlefield(),
            'armies': armies(), 'governor': _governor(),
            'log': log[-24:], 'duration_s': duration, 'ends_at': time.time(),
            'difficulty': DIFFICULTY, 'winner': winner, 'mvp': mvp,
            'summary': (f'🗺️ {blue_holds}/{len(fronts)} fronts held by Blue · '
                        f'🏅 MVP: {mvp} ({fronts[mvp]["blocks"] + fronts[mvp]["preempts"]}) · '
                        + (f'🥷 Red took: {", ".join(fell)}' if fell
                           else 'Blue held the whole cluster'))})


if __name__ == '__main__':
    main()
