"""
fabric_peer.py — introspect a SIBLING fabric (any vajrayana-DNA swarm).

codex_monk's fabric format is byte-identical with the vajrayana fabric it
forked from (same FABRIC_SIZE/MAX_AGENTS/offsets/ACB layout/state slot
shape/inbox format). That means `Fabric(path=<peer_path>).state_get(key)`
just works against any vajrayana-derived process's shared memory — no
new protocol, no over-the-wire indirection.

This probe is the demonstration: monitor a peer swarm by reading its
shared-memory state, expose the readings as Frame keys, let codex_monk
genomes gate on operational signals (gate score, query latency, OOM
risk, agent heartbeats, etc.).

Operationally interesting peer state — sampled each tick:

  agent lifecycle:
    a.{aid}.state    → 'alive' / 'dead' / 'crashed' (per agent)

  vajrayana RAG agent writes (rag_agent.py):
    rag.status       → 'ready' / 'indexing' / 'searching'
    rag.docs         → indexed doc count
    rag.q            → current query text
    rag.r.doc        → last retrieved doc id
    rag.r.score      → last raw BM25 score (FLOAT — gate signal)
    rag.r.passage    → last passage offset
    rag.r.count      → retrieval count cumulative

  sensor / narrator / kernel (sys.*):
    sys.sev          → 'OK' / 'INFO' / 'WARN' / 'CRITICAL' (encoded → int)
    sys.code         → ≤20 char code (informational only)
    sys.ts           → unix timestamp of last sample (heartbeat)
    sys.mem.usedpct  → memory %
    sys.psi.some10   → PSI some.avg10
    sys.psi.full10   → PSI full.avg10
    sys.mode         → 'psi' / 'fallback_level'

  gateway agent writes (gateway_agent.py):
    gw.status        → 'connected' / 'error' / etc.

Frame keys (this probe's contract):
  peer.available           — 1 if the fabric file opens cleanly
  peer.sys.sev_int         — OK=0, INFO=1, WARN=2, CRITICAL=3
  peer.sys.psi_some10
  peer.sys.psi_full10
  peer.sys.mem_usedpct
  peer.sys.heartbeat_age_s — now - sys.ts (large = peer hung)
  peer.rag.docs            — int
  peer.rag.last_score      — float
  peer.rag.query_count     — int (rag.r.count)
  peer.gw.status_ok        — 1 if gw.status looks healthy
  peer.alive_agent_count   — count of a.*.state == 'alive'
  peer.delta.queries_60s   — newly-observed query count in last ~60s

Opcodes (Ψ — uppercase psi):
  Ψa → peer.available
  Ψv → peer.sys.sev_int
  Ψp → peer.sys.psi_some10
  Ψf → peer.sys.psi_full10
  Ψm → peer.sys.mem_usedpct
  Ψh → peer.sys.heartbeat_age_s
  Ψr → peer.rag.docs
  Ψs → peer.rag.last_score
  Ψq → peer.rag.query_count
  Ψg → peer.gw.status_ok
  Ψn → peer.alive_agent_count
  Ψd → peer.delta.queries_60s

Configuration:
  CODEX_PEER_FABRIC_PATH  — path to peer fabric file. Default `/dev/shm/swarm.fabric`.
                            For Windows-side vajrayana from WSL, point at
                            `/mnt/c/Users/<user>/AppData/Local/Temp/swarm.fabric`.

Robustness:
  - Peer file missing or not yet created → peer.available=0, all else 0.
  - Per-tick open/close: we don't hold the mmap. Cheap (a few ms) and
    survives peer restart. The Fabric class is built for this.
  - All state values come back as raw strings; this probe parses with
    lenient float() and tolerates anything.
"""

import os
import time

from swarm.fabric import Fabric, FABRIC_SIZE, MAX_AGENTS, ACB_STATE
from swarm.probes import register


_SEV_INT = {
    'OK':       0,
    'INFO':     1,
    'WARN':     2,
    'CRITICAL': 3,
}

_GW_STATUS_HEALTHY = {
    'connected', 'ready', 'idle', 'ok', 'open', 'serving',
}


def _peer_path() -> str:
    """Resolved at sample time so env changes are observed."""
    return os.environ.get('CODEX_PEER_FABRIC_PATH', '/dev/shm/swarm.fabric')


def _try_open(path: str) -> Fabric:
    """Best-effort open. Returns None if the peer file doesn't exist or
    isn't fabric-sized yet (vajrayana mid-startup, etc.)."""
    if not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        if st.st_size < FABRIC_SIZE:
            return None
    except OSError:
        return None
    try:
        return Fabric(path=path, create=False)
    except Exception:
        return None


def _float(s, default=0.0):
    if s is None:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _int(s, default=0):
    if s is None:
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _read_state(fab: Fabric, key: str):
    """state_get returns (value, version). We only need the value here."""
    try:
        v, _ver = fab.state_get(key)
    except Exception:
        return None
    return v


def _alive_count(fab: Fabric) -> int:
    """Count agents with ACB_STATE != S_FREE/S_ZOMBIE. The fabric exposes
    ACB_STATE as a uint8 — we treat anything other than 0 (FREE) or the
    zombie sentinel as 'live'. This is intentionally coarse — the goal is
    'is there life on the peer' not exact lifecycle accounting."""
    n = 0
    for aid in range(MAX_AGENTS):
        try:
            st = fab.acb_r(aid, ACB_STATE)
        except Exception:
            continue
        # 0 = S_FREE; common zombie sentinel = high value. Treat 1..3 as alive
        # (S_READY/S_RUNNING/S_BLOCKED). Anything else → not counted.
        if 1 <= st <= 3:
            n += 1
    return n


# ── stateful: query-count delta over a small ring ─────────────────────────

_RING_WINDOW_S = 60.0
_RING_CAP = 32
_QUERY_RING: list = []   # [(ts, query_count), ...]


def _record_query_count(now: float, qc: int) -> int:
    _QUERY_RING.append((now, qc))
    if len(_QUERY_RING) > _RING_CAP:
        del _QUERY_RING[0]
    cutoff = now - _RING_WINDOW_S
    baseline = qc
    for ts, val in _QUERY_RING:
        if ts >= cutoff:
            baseline = val
            break
    return max(0, qc - baseline)


def sample_all() -> dict:
    now = time.time()
    path = _peer_path()
    fab = _try_open(path)

    if fab is None:
        # peer not up. Frame is mostly zeros; genome can detect via Ψa.
        return {
            'ts':                       now,
            'peer.available':           0,
            'peer.sys.sev_int':         0,
            'peer.sys.psi_some10':      0.0,
            'peer.sys.psi_full10':      0.0,
            'peer.sys.mem_usedpct':     0.0,
            'peer.sys.heartbeat_age_s': 9999.0,   # explicit "very stale"
            'peer.rag.docs':            0,
            'peer.rag.last_score':      0.0,
            'peer.rag.query_count':     0,
            'peer.gw.status_ok':        0,
            'peer.alive_agent_count':   0,
            'peer.delta.queries_60s':   0,
        }

    try:
        sev_str = _read_state(fab, 'sys.sev') or 'OK'
        sev_int = _SEV_INT.get(sev_str, 0)
        psi_some = _float(_read_state(fab, 'sys.psi.some10'))
        psi_full = _float(_read_state(fab, 'sys.psi.full10'))
        mem_pct  = _float(_read_state(fab, 'sys.mem.usedpct'))
        sys_ts   = _float(_read_state(fab, 'sys.ts'))
        heartbeat_age = max(0.0, now - sys_ts) if sys_ts > 0 else 9999.0

        rag_docs   = _int(_read_state(fab, 'rag.docs'))
        rag_score  = _float(_read_state(fab, 'rag.r.score'))
        rag_qcount = _int(_read_state(fab, 'rag.r.count'))

        gw_status = (_read_state(fab, 'gw.status') or '').strip().lower()
        gw_ok = 1 if gw_status in _GW_STATUS_HEALTHY else 0

        alive = _alive_count(fab)

        delta_q = _record_query_count(now, rag_qcount)

        return {
            'ts':                       now,
            'peer.available':           1,
            'peer.sys.sev_int':         sev_int,
            'peer.sys.psi_some10':      psi_some,
            'peer.sys.psi_full10':      psi_full,
            'peer.sys.mem_usedpct':     mem_pct,
            'peer.sys.heartbeat_age_s': heartbeat_age,
            'peer.rag.docs':            rag_docs,
            'peer.rag.last_score':      rag_score,
            'peer.rag.query_count':     rag_qcount,
            'peer.gw.status_ok':        gw_ok,
            'peer.alive_agent_count':   alive,
            'peer.delta.queries_60s':   delta_q,
        }
    finally:
        try:
            fab.close()
        except Exception:
            pass


def describe() -> str:
    path = _peer_path()
    present = os.path.isfile(path)
    return f'fabric_peer ({"open" if present else "absent"}: {path})'


OPCODES = {
    'Ψ': {
        'a': 'peer.available',
        'v': 'peer.sys.sev_int',
        'p': 'peer.sys.psi_some10',
        'f': 'peer.sys.psi_full10',
        'm': 'peer.sys.mem_usedpct',
        'h': 'peer.sys.heartbeat_age_s',
        'r': 'peer.rag.docs',
        's': 'peer.rag.last_score',
        'q': 'peer.rag.query_count',
        'g': 'peer.gw.status_ok',
        'n': 'peer.alive_agent_count',
        'd': 'peer.delta.queries_60s',
    },
}


register('fabric_peer', sample_all, OPCODES, describe)


if __name__ == "__main__":
    import json
    print(describe())
    print(json.dumps(sample_all(), indent=2))
