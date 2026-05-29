"""
disk_net.py — node disk + network kernel telemetry for codex_monk.

Reads /proc/diskstats and /proc/net/dev each tick, diffs against the
previous tick's counters, and exposes aggregated rate Frame keys.

/proc/diskstats line format (kernel docs `Documentation/iostats.txt`):
   major minor name reads_completed reads_merged sectors_read read_ms
   writes_completed writes_merged sectors_written write_ms in_flight
   io_ticks_ms weighted_io_ticks_ms discard_completed discard_merged
   sectors_discarded discard_ms flush_completed flush_ms

/proc/net/dev line format:
   <iface>: rx_bytes rx_packets rx_errs rx_drop rx_fifo rx_frame rx_compressed
            rx_multicast tx_bytes tx_packets tx_errs tx_drop tx_fifo tx_colls
            tx_carrier tx_compressed

Frame keys:
  disk.max.util_pct        — io_ticks delta / wall time delta * 100, max across devices
  disk.sum.read_iops       — sum of (reads_completed delta) / wall delta
  disk.sum.write_iops      — sum of (writes_completed delta) / wall delta
  disk.max.await_ms        — max ((read_ms + write_ms) delta / (reads + writes) delta)
  net.sum.errors           — sum across interfaces of (rx_errs + tx_errs) delta rate
  net.sum.drops            — sum across interfaces of (rx_drop + tx_drop) delta rate
  net.link_down_count      — interfaces with rx+tx bytes==0 over the tick (heuristic; cheap)
  net.iface_count          — total interfaces seen

Opcodes:
  Δu → disk.max.util_pct
  Δr → disk.sum.read_iops
  Δw → disk.sum.write_iops
  Δa → disk.max.await_ms
  Νe → net.sum.errors
  Νd → net.sum.drops
  Νl → net.link_down_count
  Νn → net.iface_count

Test-friendly: paths overridable via CODEX_DISKSTATS_PATH / CODEX_NETDEV_PATH
env vars so tests point at fixture files.
"""

import os
import time

from swarm.probes import register


def _diskstats_path() -> str:
    return os.environ.get('CODEX_DISKSTATS_PATH', '/proc/diskstats')


def _netdev_path() -> str:
    return os.environ.get('CODEX_NETDEV_PATH', '/proc/net/dev')


# ── stateful prior-tick snapshot — used for delta/rate computation ─────────

_PREV: dict = {
    'ts': None,
    'disks': {},     # name → (reads, writes, read_ms, write_ms, io_ticks)
    'nets':  {},     # name → (rx_bytes, rx_errs, rx_drop, tx_bytes, tx_errs, tx_drop)
}


def _parse_diskstats(path: str) -> dict:
    """Parse /proc/diskstats. Returns name → tuple of cumulative counters
    we care about. Skips loop/ram devices."""
    out = {}
    try:
        with open(path, 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                if name.startswith(('loop', 'ram')):
                    continue
                try:
                    reads     = int(parts[3])
                    read_ms   = int(parts[6])
                    writes    = int(parts[7])
                    write_ms  = int(parts[10])
                    io_ticks  = int(parts[12])    # ms spent doing I/O
                except (IndexError, ValueError):
                    continue
                out[name] = (reads, writes, read_ms, write_ms, io_ticks)
    except OSError:
        pass
    return out


def _parse_netdev(path: str) -> dict:
    """Parse /proc/net/dev. Returns iface → cumulative counters tuple."""
    out = {}
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
    except OSError:
        return out
    # first 2 lines are headers
    for line in lines[2:]:
        if ':' not in line:
            continue
        head, _, tail = line.partition(':')
        name = head.strip()
        if name == 'lo':
            continue
        cols = tail.split()
        if len(cols) < 16:
            continue
        try:
            rx_bytes = int(cols[0])
            rx_errs  = int(cols[2])
            rx_drop  = int(cols[3])
            tx_bytes = int(cols[8])
            tx_errs  = int(cols[10])
            tx_drop  = int(cols[11])
        except (IndexError, ValueError):
            continue
        out[name] = (rx_bytes, rx_errs, rx_drop, tx_bytes, tx_errs, tx_drop)
    return out


def sample_all() -> dict:
    now = time.time()
    disks = _parse_diskstats(_diskstats_path())
    nets  = _parse_netdev(_netdev_path())

    prev_ts = _PREV['ts']
    dt = (now - prev_ts) if prev_ts is not None else 1.0
    if dt <= 0:
        dt = 1.0

    max_util = 0.0
    sum_riops = 0.0
    sum_wiops = 0.0
    max_await = 0.0

    for name, (r, w, rms, wms, iot) in disks.items():
        pr = _PREV['disks'].get(name)
        if pr is None:
            continue
        d_r = r - pr[0]
        d_w = w - pr[1]
        d_rms = rms - pr[2]
        d_wms = wms - pr[3]
        d_iot = iot - pr[4]
        if d_r >= 0 and d_w >= 0:
            sum_riops += d_r / dt
            sum_wiops += d_w / dt
        if d_iot >= 0:
            util = 100.0 * (d_iot / (dt * 1000.0))    # io_ticks is ms
            if util > max_util:
                max_util = util
        ops = d_r + d_w
        if ops > 0:
            await_ms = (d_rms + d_wms) / ops
            if await_ms > max_await:
                max_await = await_ms

    sum_errors = 0.0
    sum_drops  = 0.0
    link_down  = 0
    iface_count = len(nets)
    for name, (rxb, rxe, rxd, txb, txe, txd) in nets.items():
        pn = _PREV['nets'].get(name)
        if pn is None:
            continue
        d_rxb = rxb - pn[0]
        d_txb = txb - pn[3]
        sum_errors += max(0, (rxe - pn[1]) + (txe - pn[4])) / dt
        sum_drops  += max(0, (rxd - pn[2]) + (txd - pn[5])) / dt
        if d_rxb == 0 and d_txb == 0:
            link_down += 1

    # update state for next tick
    _PREV['ts']    = now
    _PREV['disks'] = disks
    _PREV['nets']  = nets

    return {
        'ts':                  now,
        'disk.max.util_pct':   max_util,
        'disk.sum.read_iops':  sum_riops,
        'disk.sum.write_iops': sum_wiops,
        'disk.max.await_ms':   max_await,
        'net.sum.errors':      sum_errors,
        'net.sum.drops':       sum_drops,
        'net.link_down_count': link_down,
        'net.iface_count':     iface_count,
    }


def describe() -> str:
    return f'disk_net ({_diskstats_path()} + {_netdev_path()})'


OPCODES = {
    'Δ': {
        'u': 'disk.max.util_pct',
        'r': 'disk.sum.read_iops',
        'w': 'disk.sum.write_iops',
        'a': 'disk.max.await_ms',
    },
    'Ν': {
        'e': 'net.sum.errors',
        'd': 'net.sum.drops',
        'l': 'net.link_down_count',
        'n': 'net.iface_count',
    },
}


register('disk_net', sample_all, OPCODES, describe)


if __name__ == "__main__":
    import json
    print(json.dumps(sample_all(), indent=2))
    time.sleep(1)
    print(json.dumps(sample_all(), indent=2))
