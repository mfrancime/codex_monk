"""
test_disk_net.py — synthetic /proc/diskstats and /proc/net/dev → Frame rates.

We write two consecutive ticks of fixture content to temp files, point the
probe at them via CODEX_DISKSTATS_PATH / CODEX_NETDEV_PATH, sample twice
(first establishes baseline, second produces deltas), and assert.

Five-line genome verifies the opcode wiring end-to-end on the resulting
Frame.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_disk_net
"""

import os
import sys
import tempfile
import time

# Set env BEFORE the probe module is imported so describe() is right.
_DISK_FILE = tempfile.NamedTemporaryFile(mode='w', suffix='.diskstats',
                                          delete=False)
_NET_FILE  = tempfile.NamedTemporaryFile(mode='w', suffix='.netdev',
                                          delete=False)
_DISK_FILE.close(); _NET_FILE.close()
os.environ['CODEX_DISKSTATS_PATH'] = _DISK_FILE.name
os.environ['CODEX_NETDEV_PATH']    = _NET_FILE.name

from swarm.probes import disk_net   # noqa: E402
from swarm.probes import get as get_probe   # noqa: E402
from swarm.genome import interpret   # noqa: E402


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


# /proc/diskstats fields: maj min name reads_completed reads_merged
#  sectors_read read_ms writes_completed writes_merged sectors_written
#  write_ms in_flight io_ticks_ms weighted_io_ticks_ms ...
def _write_diskstats(path, sda_reads, sda_writes,
                     sda_read_ms, sda_write_ms, sda_io_ticks):
    with open(path, 'w') as f:
        f.write(
            f'   8       0 sda {sda_reads} 0 0 {sda_read_ms} '
            f'{sda_writes} 0 0 {sda_write_ms} 0 {sda_io_ticks} 0 0 0 0 0 0\n'
        )
        # ignored device — loop0
        f.write('   7       0 loop0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n')


# /proc/net/dev columns after `name:`:
#  rx_bytes rx_packets rx_errs rx_drop rx_fifo rx_frame rx_compressed
#  rx_multicast tx_bytes tx_packets tx_errs tx_drop tx_fifo tx_colls
#  tx_carrier tx_compressed
def _write_netdev(path, eth0_rx_bytes, eth0_rx_errs, eth0_rx_drops,
                  eth0_tx_bytes, eth0_tx_errs, eth0_tx_drops,
                  eth1_rx_bytes=0, eth1_tx_bytes=0):
    with open(path, 'w') as f:
        f.write('Inter-|   Receive                              |  Transmit\n')
        f.write(' face |bytes packets errs drop fifo frame compressed multicast'
                '|bytes packets errs drop fifo colls carrier compressed\n')
        f.write(
            f'  eth0: {eth0_rx_bytes} 0 {eth0_rx_errs} {eth0_rx_drops} '
            f'0 0 0 0 {eth0_tx_bytes} 0 {eth0_tx_errs} {eth0_tx_drops} '
            f'0 0 0 0\n'
        )
        # eth1 — link-down candidate: 0 bytes both directions
        f.write(
            f'  eth1: {eth1_rx_bytes} 0 0 0 0 0 0 0 '
            f'{eth1_tx_bytes} 0 0 0 0 0 0 0\n'
        )
        # lo — should be ignored
        f.write('    lo: 99 0 0 0 0 0 0 0 99 0 0 0 0 0 0 0\n')


def main():
    print()
    print('== disk_net probe ==')
    print(f'  diskstats: {_DISK_FILE.name}')
    print(f'  netdev:    {_NET_FILE.name}')

    p = get_probe('disk_net')
    _check('probe registered',  p.name == 'disk_net')
    _check('Δ opcodes present', 'Δ' in p.opcodes)
    _check('Ν opcodes present', 'Ν' in p.opcodes)

    # ── tick 1: baseline. Both rate buckets should be 0 because there's
    #    no prior tick to diff against.
    _write_diskstats(_DISK_FILE.name, sda_reads=100, sda_writes=50,
                     sda_read_ms=2000, sda_write_ms=1500, sda_io_ticks=3000)
    _write_netdev(_NET_FILE.name,
                  eth0_rx_bytes=1000, eth0_rx_errs=0, eth0_rx_drops=0,
                  eth0_tx_bytes=500,  eth0_tx_errs=0, eth0_tx_drops=0)
    f1 = p.sample_all()
    _check('tick1: read_iops = 0 (no baseline)',  f1['disk.sum.read_iops'] == 0.0)
    _check('tick1: write_iops = 0',               f1['disk.sum.write_iops'] == 0.0)
    _check('tick1: errors = 0',                   f1['net.sum.errors'] == 0.0)
    _check('tick1: iface_count = 2',              f1['net.iface_count'] == 2)

    # ── tick 2: simulate ~1 second of activity. Sleep a real second so the
    #    probe's wall-clock dt is non-trivial; deltas land in the rates.
    time.sleep(1.0)
    _write_diskstats(_DISK_FILE.name, sda_reads=150, sda_writes=80,
                     sda_read_ms=2500, sda_write_ms=2000,
                     sda_io_ticks=3500)              # +500 io_ticks ms
    _write_netdev(_NET_FILE.name,
                  eth0_rx_bytes=2000, eth0_rx_errs=3, eth0_rx_drops=2,
                  eth0_tx_bytes=1500, eth0_tx_errs=1, eth0_tx_drops=0,
                  eth1_rx_bytes=0, eth1_tx_bytes=0)  # eth1 stays at 0 → link down
    f2 = p.sample_all()
    _check('tick2: read_iops > 0',                 f2['disk.sum.read_iops'] > 0)
    _check('tick2: write_iops > 0',                f2['disk.sum.write_iops'] > 0)
    _check('tick2: read_iops approx 50/s',         abs(f2['disk.sum.read_iops'] - 50.0) < 10.0)
    _check('tick2: write_iops approx 30/s',        abs(f2['disk.sum.write_iops'] - 30.0) < 10.0)
    _check('tick2: util_pct > 0',                  f2['disk.max.util_pct'] > 0)
    _check('tick2: await_ms > 0',                  f2['disk.max.await_ms'] > 0)
    _check('tick2: errors = 4/s approx',           abs(f2['net.sum.errors'] - 4.0) < 2.0)
    _check('tick2: drops = 2/s approx',            abs(f2['net.sum.drops'] - 2.0) < 2.0)
    _check('tick2: link_down_count = 1 (eth1)',    f2['net.link_down_count'] == 1)

    # ── genome: emit CRITICAL if errors > 0
    genome = 'Νe0>→Cd;Δu‡80>→Wp;'
    sev, code = interpret(genome, f2, p.opcodes)
    _check('genome on errors: CRITICAL',           sev == 'CRITICAL')
    _check('genome on errors: code CLUSTER_DEGRADED', code == 'CLUSTER_DEGRADED')

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    try:
        main()
    finally:
        os.unlink(_DISK_FILE.name)
        os.unlink(_NET_FILE.name)
