"""
viz.py — codex_monk war-room viewer.

A single-file stdlib HTTP server that:

  - Auto-discovers fabric files under /dev/shm/ (codex.*.fabric +
    swarm.fabric) and exposes their live state as JSON.
  - Manages boot.py subprocesses by config name (start / stop / status).
  - Lets the operator inspect probe Frames and propose DNA changes from
    the browser.
  - Serves a DEFCON-themed HTML page (see web/) that polls these
    endpoints, renders an animated SVG topology, an agent grid, a log
    tail, and an alert timeline.

Run:
    python viz.py                          # listens on :19200
    python viz.py --port 19200 --bind 0.0.0.0

The viz process itself does NOT spawn or touch any swarm fabric until
the operator clicks Start. It just reads shared memory that other
processes are already writing.
"""

import argparse
import glob
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yaml

from swarm.fabric import (
    Fabric, MAX_AGENTS, MAX_STATE_SLOTS, VERB_NAMES,
    OFF_STATE, SS_SIZE, SS_KEY, SS_VALUE, SS_WRITER, _from_bytes,
    ACB_STATE, ACB_TYPE, ACB_PRIORITY, ACB_HEARTBEAT, ACB_PID,
    S_FREE, S_READY, S_RUNNING, S_BLOCKED, S_ZOMBIE,
)
from swarm.config import is_multiswarm
from swarm import probes, dna_storage


ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, 'web')

_STATE_NAMES = {
    S_FREE: 'free', S_READY: 'ready', S_RUNNING: 'running',
    S_BLOCKED: 'blocked', S_ZOMBIE: 'zombie',
}

_SEV_DEFCON = {
    'CRITICAL': 1,
    'WARN':     3,
    'INFO':     4,
    'OK':       5,
}

_SEV_LEVELS = ['OK', 'INFO', 'WARN', 'CRITICAL']


# ── config metadata: map each fabric to its real state_prefix + topology ────
#
# viz reads every multiswarm config in the project root and builds a
# fabric_path → {state_prefix, gateway peers, per-agent roles} map. This is
# how the war-room knows (a) which short key prefix to strip when reading a
# fabric's state table, (b) the true VJR topology (who routes to whom), and
# (c) each agent's role/probe for labelling — instead of guessing from the
# fabric filename or hardcoding agent ids.

_META_LOCK = threading.Lock()
_META = {'sig': None, 'by_path': {}, 'name2path': {}}


def _role_from_cfg(acfg: dict) -> str:
    """Infer an agent's role from its YAML config block."""
    if acfg.get('mutate_target') is not None or acfg.get('propose_to') is not None:
        return 'mutator'
    if acfg.get('genome'):
        return 'probe'
    if acfg.get('consume_types'):
        return 'sink'
    return 'agent'


def _role_from_state(short_state: dict) -> str:
    """Fallback role inference from the keys an agent actually wrote."""
    keys = short_state.keys()
    if any(k.startswith('gw.') for k in keys):    return 'gateway'
    if any(k.startswith('mut.') for k in keys):   return 'mutator'
    if any(k.startswith('sys.') for k in keys):   return 'probe'
    if any(k.startswith('sink.') for k in keys):  return 'sink'
    return 'agent'


def _config_sig() -> tuple:
    """Cheap change-detector: (path, mtime) of every root *.yaml + sub-yaml."""
    items = []
    for p in sorted(glob.glob(os.path.join(ROOT, '*.yaml')) +
                    glob.glob(os.path.join(ROOT, 'swarms', '*.yaml'))):
        try:
            items.append((p, os.path.getmtime(p)))
        except OSError:
            pass
    return tuple(items)


def _build_meta() -> tuple:
    """Parse every multiswarm config → (by_path, name2path)."""
    by_path = {}
    name2path = {}
    for cfgpath in sorted(glob.glob(os.path.join(ROOT, '*.yaml'))):
        try:
            with open(cfgpath, 'r', encoding='utf-8') as f:
                root = yaml.safe_load(f) or {}
        except Exception:
            continue
        if not is_multiswarm(root):
            continue
        cfgname = os.path.basename(cfgpath)
        for entry in root.get('swarms', []):
            name  = str(entry.get('name', ''))
            fpath = entry.get('fabric_path')
            if not fpath:
                continue
            prefix = str(entry.get('state_prefix', f'swarm.{name}.'))
            gw = entry.get('gateway') or {}
            agents = {}
            inc = entry.get('include')
            if inc:
                ip = inc if os.path.isabs(inc) else os.path.join(ROOT, inc)
                try:
                    with open(ip, 'r', encoding='utf-8') as f:
                        sub = yaml.safe_load(f) or {}
                    for a in sub.get('agents', []):
                        acfg = a.get('config', {}) or {}
                        agents[int(a['id'])] = {
                            'type':  a.get('type', ''),
                            'probe': acfg.get('probe'),
                            'role':  _role_from_cfg(acfg),
                        }
                except Exception:
                    pass
            if gw.get('id') is not None:
                agents[int(gw['id'])] = {
                    'type': 'gateway', 'probe': None, 'role': 'gateway'}
            by_path[fpath] = {
                'swarm_name':  name,
                'config_name': cfgname,
                'state_prefix': prefix,
                'gateway_id':  int(gw['id']) if gw.get('id') is not None else None,
                'peers':       gw.get('peers', []) or [],
                'agents':      agents,
            }
            name2path[name] = fpath
    return by_path, name2path


def _meta():
    """Return (by_path, name2path), rebuilding only when a config changed."""
    sig = _config_sig()
    with _META_LOCK:
        if sig != _META['sig']:
            _META['by_path'], _META['name2path'] = _build_meta()
            _META['sig'] = sig
        return _META['by_path'], _META['name2path']


def _meta_for(path: str) -> dict:
    by_path, _ = _meta()
    return by_path.get(path, {})


def _worst_sev(sevs) -> str:
    worst = 'OK'
    for s in sevs:
        if s in _SEV_LEVELS and _SEV_LEVELS.index(s) > _SEV_LEVELS.index(worst):
            worst = s
    return worst


# ── process control: managed boot.py subprocesses ──────────────────────────

_PROCS_LOCK = threading.Lock()
_PROCS: dict = {}   # config_name → {'pid': int, 'proc': Popen, 'started_at': ts}


def _discover_configs() -> list:
    """List candidate *.yaml configs in the project root. Both single-swarm
    (swarm.yaml) and multiswarm (multiswarm*.yaml) are eligible."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, '*.yaml'))):
        out.append(os.path.basename(path))
    return out


def _start_config(name: str) -> dict:
    cfg_path = os.path.join(ROOT, name)
    if not os.path.isfile(cfg_path):
        return {'ok': False, 'error': f'no such config: {name}'}
    with _PROCS_LOCK:
        existing = _PROCS.get(name)
        if existing and existing['proc'].poll() is None:
            return {'ok': False, 'error': 'already running',
                    'pid': existing['pid']}
        # spawn fresh
        log_dir = os.path.join(ROOT, 'graph')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f'viz_{name}.log')
        log_fh = open(log_path, 'ab')
        env = os.environ.copy()
        env.setdefault('PYTHONUNBUFFERED', '1')
        proc = subprocess.Popen(
            [sys.executable, '-u', os.path.join(ROOT, 'boot.py'),
             '--config', cfg_path],
            cwd=ROOT, stdout=log_fh, stderr=subprocess.STDOUT,
            env=env, preexec_fn=os.setsid)
        _PROCS[name] = {
            'pid': proc.pid, 'proc': proc,
            'started_at': time.time(), 'log_path': log_path,
        }
        return {'ok': True, 'pid': proc.pid, 'log': log_path}


def _stop_config(name: str) -> dict:
    with _PROCS_LOCK:
        entry = _PROCS.get(name)
        if not entry:
            return {'ok': False, 'error': 'not running'}
        proc = entry['proc']
        if proc.poll() is not None:
            del _PROCS[name]
            return {'ok': True, 'exit_code': proc.returncode,
                    'note': 'already exited'}
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        rc = proc.poll()
        del _PROCS[name]
    return {'ok': True, 'exit_code': rc}


def _status_all() -> dict:
    out = {}
    with _PROCS_LOCK:
        for name, entry in list(_PROCS.items()):
            rc = entry['proc'].poll()
            if rc is not None:
                out[name] = {'state': 'exited', 'exit_code': rc,
                             'pid': entry['pid'],
                             'started_at': entry['started_at']}
            else:
                out[name] = {'state': 'running', 'pid': entry['pid'],
                             'started_at': entry['started_at']}
    return out


# ── wargame round runner (EVOLUTION tab "RUN ROUND" button) ─────────────────
#
# Runs one co-evolution round on demand (wargame.py --rounds 1) so the operator
# can advance the Kubernetes Red-vs-Blue arms race from the browser. Guarded:
# refuses to start if a round is already in flight — either one this server
# spawned OR the autonomous wargame_autorun.sh loop's round — so they never race
# on the shared lineage / web/wargame.json.

_WARGAME_LOCK = threading.Lock()
_WARGAME = {'proc': None, 'started_at': None}


def _wargame_busy() -> bool:
    # A round this server spawned is the authoritative signal.
    p = _WARGAME['proc']
    if p is not None and p.poll() is None:
        return True
    # Also detect a round started elsewhere (e.g. wargame_autorun.sh), but match
    # ONLY a real python interpreter running wargame.py — NOT shells that merely
    # mention the string (a stale wait-loop did exactly that and wedged this guard).
    try:
        r = subprocess.run(
            ['pgrep', '-f', r'python[^ ]* [^ ]*wargame\.py --rounds'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _wargame_run(rounds: int = 1, gens: int = 250, lam: int = 24) -> dict:
    with _WARGAME_LOCK:
        if _wargame_busy():
            return {'ok': False, 'running': True,
                    'error': 'a wargame round is already in flight'}
        log_dir = os.path.join(ROOT, 'graph')
        os.makedirs(log_dir, exist_ok=True)
        fh = open(os.path.join(log_dir, 'wargame_ui.log'), 'ab')
        env = os.environ.copy()
        env.setdefault('PYTHONUNBUFFERED', '1')
        proc = subprocess.Popen(
            [sys.executable, '-u', os.path.join(ROOT, 'wargame.py'),
             '--rounds', str(rounds), '--gens', str(gens), '--lam', str(lam)],
            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=env)
        _WARGAME['proc'] = proc
        _WARGAME['started_at'] = time.time()
        return {'ok': True, 'running': True, 'pid': proc.pid,
                'rounds': rounds, 'gens': gens, 'lam': lam}


def _wargame_status() -> dict:
    return {'running': _wargame_busy(), 'started_at': _WARGAME['started_at']}


# ── START WAR: autonomous Red-vs-Blue battle (war_driver.py) ────────────────

_WAR_LOCK = threading.Lock()
_WAR = {'proc': None}


def _war_running() -> bool:
    p = _WAR['proc']
    return p is not None and p.poll() is None


def _war_start(duration: int = 600, gap: float = 7.0) -> dict:
    with _WAR_LOCK:
        if _war_running():
            return {'ok': False, 'running': True, 'error': 'a war is already raging'}
        fh = open(os.path.join(ROOT, 'graph', 'war.log'), 'ab')
        env = os.environ.copy()
        env.setdefault('PYTHONUNBUFFERED', '1')
        proc = subprocess.Popen(
            [sys.executable, '-u', os.path.join(ROOT, 'war_driver.py'),
             str(duration), str(gap)],
            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=env)
        _WAR['proc'] = proc
        return {'ok': True, 'running': True, 'pid': proc.pid, 'duration_s': duration}


def _war_stop() -> dict:
    with _WAR_LOCK:
        p = _WAR['proc']
        if p is None or p.poll() is not None:
            return {'ok': True, 'running': False, 'note': 'no war running'}
        try:
            p.terminate()
        except Exception:
            pass
        return {'ok': True, 'running': False, 'note': 'ceasefire'}


# ── fabric reading ─────────────────────────────────────────────────────────

def _discover_fabrics() -> list:
    """Find all readable fabric files. /dev/shm has the codex_monk live
    fabrics + vajrayana's /dev/shm/swarm.fabric if present."""
    paths = []
    for p in sorted(glob.glob('/dev/shm/codex.*.fabric')):
        paths.append(p)
    if os.path.isfile('/dev/shm/swarm.fabric'):
        paths.append('/dev/shm/swarm.fabric')
    return paths


def _open_fabric(path: str):
    try:
        return Fabric(path=path, create=False)
    except Exception:
        return None


def _read_agent(fab: Fabric, aid: int) -> dict:
    try:
        st  = fab.acb_r(aid, ACB_STATE)
        typ = fab.acb_r(aid, ACB_TYPE)
        pri = fab.acb_r(aid, ACB_PRIORITY)
        hb  = fab.acb_r(aid, ACB_HEARTBEAT, '<Q')
        pid = fab.acb_r(aid, ACB_PID, '<I')
    except Exception:
        return None
    if st == S_FREE and hb == 0:
        return None
    state_name = _STATE_NAMES.get(st, str(st))
    return {
        'id': aid, 'type': typ, 'priority': pri,
        'state': state_name, 'pid': pid,
        'heartbeat': hb,
        'heartbeat_age_s': max(0, int(time.time()) - hb) if hb else None,
    }


def _state_by_writer(fab: Fabric, prefix: str) -> dict:
    """Walk the whole state table once. Group every live key under the agent
    id that wrote it (the per-slot WRITER field), stripping `prefix` from the
    key so callers see canonical keys (sys.sev, mut.best, gw.bind, …).

    Returns {writer_id: {short_key: value}}. This is the real per-agent state
    — the WRITER field is exactly which agent owns each key, so no hardcoded
    'agent #7 is the probe' guessing is needed."""
    out = {}
    for i in range(MAX_STATE_SLOTS):
        off = OFF_STATE + i * SS_SIZE
        kb = bytes(fab.mm[off + SS_KEY: off + SS_KEY + 24]).split(b'\x00')[0]
        if not kb:
            continue
        key = kb.decode('utf-8', 'replace')
        val = _from_bytes(fab.mm[off + SS_VALUE: off + SS_VALUE + 20])
        writer = struct.unpack_from('<H', fab.mm, off + SS_WRITER)[0]
        short = key[len(prefix):] if prefix and key.startswith(prefix) else key
        out.setdefault(writer, {})[short] = val
    return out


def _swarm_snapshot(path: str) -> dict:
    """Build the full live view of a single fabric, with real per-agent
    state attributed by the state table's WRITER field."""
    meta = _meta_for(path)
    prefix = meta.get('state_prefix', '')
    cfg_agents = meta.get('agents', {})

    fab = _open_fabric(path)
    if fab is None:
        return {
            'path': path, 'online': False, 'agents': [],
            'state': {}, 'severity': 'OK', 'defcon': 5,
            'swarm_name': meta.get('swarm_name'),
            'gateway_id': meta.get('gateway_id'),
        }
    try:
        by_writer = _state_by_writer(fab, prefix)

        agents = []
        for aid in range(MAX_AGENTS):
            a = _read_agent(fab, aid)
            if a is None:
                continue
            ast = by_writer.get(aid, {})
            a['vars'] = ast
            a['sev']  = ast.get('sys.sev')
            a['code'] = ast.get('sys.code')
            cfg = cfg_agents.get(aid, {})
            a['role']  = cfg.get('role') or _role_from_state(ast)
            a['probe'] = cfg.get('probe')
            agents.append(a)

        # swarm-level merged view (header, log labels) = union of all writers
        merged_state = {}
        for ast in by_writer.values():
            merged_state.update(ast)

        severity = _worst_sev([a.get('sev') for a in agents] +
                              [merged_state.get('sys.sev')])
        defcon = _SEV_DEFCON.get(severity, 5)
        return {
            'path': path, 'online': True,
            'swarm_name': meta.get('swarm_name'),
            'state_prefix': prefix,
            'gateway_id': meta.get('gateway_id'),
            'agents': agents,
            'state': merged_state,
            'severity': severity,
            'defcon': defcon,
        }
    finally:
        try: fab.close()
        except: pass


def _log_tail(path: str, n: int = 50) -> list:
    fab = _open_fabric(path)
    if fab is None:
        return []
    try:
        head = fab.log_head()
        start = max(1, head - n + 1)
        out = []
        for seq in range(start, head + 1):
            r = fab.log_read(seq)
            if r is None: continue
            r['verb_name'] = VERB_NAMES.get(r.get('verb'), str(r.get('verb')))
            r['ts'] = r.pop('timestamp', 0)
            out.append(r)
        return out
    finally:
        try: fab.close()
        except: pass


def _timeline(n: int = 400) -> dict:
    """Reconstruct each swarm's severity history from its fabric event log.

    The probe role logs an 'edge' entry (value 'SEV:CODE') on every verdict
    change, so the log IS a recorded severity time-series — no extra capture
    needed. This is the substrate the war-room's TIME MACHINE scrubs over:
    per swarm, the ordered (ts, sev, code) edges from all its agents.
    """
    swarms = []
    t_min = None
    t_max = None
    for p in _discover_fabrics():
        fab = _open_fabric(p)
        if fab is None:
            continue
        try:
            head = fab.log_head()
            start = max(1, head - n + 1)
            events = []
            for seq in range(start, head + 1):
                r = fab.log_read(seq)
                if r is None or r.get('key') != 'edge':
                    continue
                sev, _, code = (r.get('value') or '').partition(':')
                ts = r.get('timestamp', 0) / 1_000_000.0   # micros → seconds
                events.append({'seq': r['seq'], 'ts': ts, 'aid': r.get('aid'),
                               'sev': sev or 'OK', 'code': code or 'OK'})
                t_min = ts if t_min is None else min(t_min, ts)
                t_max = ts if t_max is None else max(t_max, ts)
            snap = _swarm_snapshot(p)
            swarms.append({'path': p, 'name': snap.get('swarm_name'),
                           'online': snap.get('online'), 'events': events})
        finally:
            try: fab.close()
            except: pass
    now = time.time()
    return {'swarms': swarms, 't_min': t_min or now, 't_max': t_max or now,
            'now': now}


def _alerts_tail(n: int = 30) -> list:
    """Concatenate the tails of all graph/*.jsonl alert files. Returns the
    last N entries chronologically, newest first."""
    rows = []
    for jp in sorted(glob.glob(os.path.join(ROOT, 'graph', '*.jsonl'))):
        try:
            with open(jp, 'r') as f:
                for line in f.readlines()[-200:]:
                    line = line.strip()
                    if not line: continue
                    try:
                        d = json.loads(line)
                        d['_source'] = os.path.basename(jp)
                        rows.append(d)
                    except ValueError:
                        pass
        except OSError:
            pass
    rows.sort(key=lambda r: r.get('ts', 0), reverse=True)
    return rows[:n]


def _agent_detail(path: str, aid: int) -> dict:
    meta = _meta_for(path)
    prefix = meta.get('state_prefix', '')
    cfg = meta.get('agents', {}).get(aid, {})
    fab = _open_fabric(path)
    if fab is None:
        return {'error': 'fabric not open'}
    try:
        a = _read_agent(fab, aid) or {'id': aid, 'state': 'free'}
        vars_ = _state_by_writer(fab, prefix).get(aid, {})
        role = cfg.get('role') or _role_from_state(vars_)
        a['vars'] = vars_
        a['sev']  = vars_.get('sys.sev')
        a['code'] = vars_.get('sys.code')
        a['role'] = role
        a['probe'] = cfg.get('probe')
        genome = dna_storage.read(fab, aid)
        return {
            'agent': a, 'genome': genome, 'role': role,
            'probe': cfg.get('probe'), 'vars': vars_,
            'swarm_name': meta.get('swarm_name'),
        }
    finally:
        try: fab.close()
        except: pass


def _topology_links(snaps: list) -> list:
    """Derive the real VJR topology from gateway peer config. Each fabric's
    gateway lists named peers; resolve those names to fabric paths and emit
    one undirected link per connected pair (both ends online)."""
    _, name2path = _meta()
    online = {s['path'] for s in snaps if s.get('online')}
    seen = set()
    links = []
    for s in snaps:
        src = s['path']
        meta = _meta_for(src)
        for peer in meta.get('peers', []):
            dst = name2path.get(peer.get('name'))
            if not dst or dst == src:
                continue
            key = tuple(sorted((src, dst)))
            if key in seen:
                continue
            seen.add(key)
            links.append({
                'from': src, 'to': dst,
                'peer': peer.get('name'),
                'online': src in online and dst in online,
            })
    return links


def _frame_for_probe(name: str) -> dict:
    try:
        p = probes.get(name)
    except KeyError as e:
        return {'error': str(e)}
    try:
        return {'probe': name, 'describe': p.describe(),
                'frame': p.sample_all()}
    except Exception as e:
        return {'error': repr(e)}


def _propose_genome(path: str, aid: int, genome: str) -> dict:
    fab = _open_fabric(path)
    if fab is None:
        return {'ok': False, 'error': 'fabric not open'}
    try:
        dna_storage.write(fab, aid, genome, writer=0)
        return {'ok': True, 'wrote': genome, 'aid': aid}
    except Exception as e:
        return {'ok': False, 'error': repr(e)}
    finally:
        try: fab.close()
        except: pass


# ── HTTP handler ───────────────────────────────────────────────────────────

class VizHandler(BaseHTTPRequestHandler):

    def _json(self, body, status=200):
        data = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        # path like '/style.css' → web/style.css
        rel = path.lstrip('/')
        if not rel: rel = 'index.html'
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            '.html': 'text/html; charset=utf-8',
            '.css':  'text/css',
            '.js':   'application/javascript',
            '.json': 'application/json; charset=utf-8',
            '.svg':  'image/svg+xml',
        }.get(ext, 'application/octet-stream')
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get('Content-Length', '0') or '0')
        if length <= 0: return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def log_message(self, fmt, *args):
        # quieter access log; comment out to re-enable
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path.startswith('/api/'):
            return self._dispatch_api(path, qs, body=None, method='GET')
        return self._static(path or '/')

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        body = self._read_body()
        return self._dispatch_api(path, qs, body=body, method='POST')

    def _dispatch_api(self, path, qs, body, method):
        if path == '/api/swarms' and method == 'GET':
            fabrics = _discover_fabrics()
            snaps = [_swarm_snapshot(p) for p in fabrics]
            # roll up swarm-wide DEFCON
            worst_defcon = 5
            for s in snaps:
                if s['defcon'] < worst_defcon:
                    worst_defcon = s['defcon']
            return self._json({
                'ts': time.time(),
                'defcon': worst_defcon,
                'swarms': snaps,
                'links':   _topology_links(snaps),
                'configs': _discover_configs(),
                'procs':   _status_all(),
                'probes':  sorted(probes.list_probes().keys()),
            })
        if path == '/api/log' and method == 'GET':
            fabric_path = (qs.get('path') or [''])[0]
            n = int((qs.get('n') or ['50'])[0])
            return self._json({'entries': _log_tail(fabric_path, n)})
        if path == '/api/alerts' and method == 'GET':
            n = int((qs.get('n') or ['30'])[0])
            return self._json({'entries': _alerts_tail(n)})
        if path == '/api/timeline' and method == 'GET':
            n = int((qs.get('n') or ['400'])[0])
            return self._json(_timeline(n))
        if path == '/api/agent' and method == 'GET':
            fabric_path = (qs.get('path') or [''])[0]
            aid = int((qs.get('id') or ['0'])[0])
            return self._json(_agent_detail(fabric_path, aid))
        if path == '/api/frame' and method == 'GET':
            name = (qs.get('probe') or [''])[0]
            return self._json(_frame_for_probe(name))
        if path == '/api/start' and method == 'POST':
            name = body.get('config', '')
            return self._json(_start_config(name))
        if path == '/api/stop' and method == 'POST':
            name = body.get('config', '')
            return self._json(_stop_config(name))
        if path == '/api/status' and method == 'GET':
            return self._json(_status_all())
        if path == '/api/propose' and method == 'POST':
            return self._json(_propose_genome(
                body.get('path', ''),
                int(body.get('id', 0)),
                body.get('genome', '')))
        if path == '/api/wargame' and method == 'POST':
            return self._json(_wargame_run(
                int(body.get('rounds', 1)),
                int(body.get('gens', 250)),
                int(body.get('lam', 24))))
        if path == '/api/wargame' and method == 'GET':
            return self._json(_wargame_status())
        if path == '/api/war' and method == 'POST':
            action = (body.get('action') or 'start').lower()
            if action == 'stop':
                return self._json(_war_stop())
            return self._json(_war_start(int(body.get('duration', 600)),
                                         float(body.get('gap', 7.0))))
        if path == '/api/war' and method == 'GET':
            return self._json({'running': _war_running()})

        return self._json({'error': 'not found', 'path': path}, status=404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bind', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=19200)
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.bind, args.port), VizHandler)
    print(f'\n  codex_monk viz')
    print(f'  bound {args.bind}:{args.port}')
    print(f'  open  http://{args.bind}:{args.port}/')
    print(f'  Ctrl+C to stop\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  shutting down — stopping any managed swarms')
        for name in list(_PROCS):
            _stop_config(name)
        server.shutdown()


if __name__ == '__main__':
    main()
