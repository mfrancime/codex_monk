"""
test_gateway.py — prove the gateway actually moves messages across fabrics.

The multiswarm thesis stands or falls on this round-trip:

  swarm-A agent emits → A's fabric inbox of gateway_A
                      → gateway_A.on_message → VJR over TCP
                      → gateway_B server thread reads VJR
                      → gateway_B posts to B's fabric inbox of dst_agent
                      → swarm-B agent reads the message

If THIS doesn't work, no amount of YAML composition will save us.

The harness builds two fabrics in /tmp, two gateways on ephemeral
127.0.0.1 ports, then drives the message manually (no kernel run loop —
just construct, attach, tick once to start network threads, then push a
message through).

Negative checks ensure the gateway is safe under failure: a wrong-PSK
peer cannot deliver, an unroutable message type doesn't crash the
gateway and is logged.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_gateway
"""

import os
import sys
import tempfile
import time

from swarm.fabric import Fabric, VERB_ERROR
from swarm import template


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _fabric_path(suffix):
    return os.path.join(tempfile.gettempdir(),
                        f'codex_monk_test_gw_{suffix}.fabric')


def _wait_for_inbox(fabric, aid, timeout_s=3.0):
    """Poll a fabric inbox until something arrives or timeout. Returns the
    message dict, or None on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = fabric.inbox_recv(aid)
        if m is not None:
            return m
        time.sleep(0.02)
    return None


def _fresh_fabric(suffix):
    p = _fabric_path(suffix)
    if os.path.exists(p):
        os.remove(p)
    return Fabric(path=p, create=True), p


def _make_gateway(aid, swarm_name, bind, peer_name, peer_addr,
                  route_type, route_remote_agent, psk):
    cfg = {
        'swarm_name': swarm_name,
        'bind': bind,
        'peers': [{'name': peer_name, 'addr': peer_addr}],
        'routes': [{'type': route_type, 'peer': peer_name,
                    'agent': route_remote_agent}],
        'psk': psk,
    }
    gw = template.create_agent('gateway', aid, aid, 1, cfg)
    return gw


def _drive_inbox(gw):
    """Pump every message currently in the gateway's inbox through
    on_message. Mirrors what the Agent.run loop would do."""
    while True:
        m = gw.fabric.inbox_recv(gw.id)
        if m is None:
            return
        gw.on_message(m)


PSK = 'gw-test-psk'
WRONG_PSK = 'gw-wrong-psk'
TYPE = 700


def _count_verb_errors(fabric, aid, key_match=None):
    """Walk the immutable log and count VERB_ERROR rows from this agent
    matching an optional key substring. Seqs are 1..head inclusive."""
    n = 0
    head = fabric.log_head()
    for seq in range(1, head + 1):
        rec = fabric.log_read(seq)
        if not rec:
            continue
        if rec.get('agent') == aid and rec.get('verb') == VERB_ERROR:
            if key_match is None or key_match in (rec.get('key') or ''):
                n += 1
    return n


def main():
    print()
    print('== gateway round-trip ==')

    fab_a, path_a = _fresh_fabric('a')
    fab_b, path_b = _fresh_fabric('b')

    try:
        # bind both gateways on ephemeral ports; resolve actual ports
        # after .on_tick() starts the server sockets.
        gw_a = _make_gateway(
            aid=9, swarm_name='alpha', bind='127.0.0.1:0',
            peer_name='beta', peer_addr='127.0.0.1:0',  # patched below
            route_type=TYPE, route_remote_agent=1, psk=PSK)
        gw_b = _make_gateway(
            aid=9, swarm_name='beta', bind='127.0.0.1:0',
            peer_name='alpha', peer_addr='127.0.0.1:0',  # patched below
            route_type=TYPE, route_remote_agent=1, psk=PSK)

        gw_a.attach(fab_a)
        gw_b.attach(fab_b)

        # tick once to start B's server first, so when we point A at it
        # the connect succeeds.
        gw_b.on_tick()
        _, b_port = gw_b._actual_bind

        # patch A's peer addr to B's real port, then start A.
        gw_a._peer_addr['beta'] = f'127.0.0.1:{b_port}'
        gw_a.on_tick()
        _, a_port = gw_a._actual_bind

        # (B's peer-A addr is unused in this single-direction test; leave
        # it on the placeholder port.)

        print(f'    gw-alpha bound 127.0.0.1:{a_port}  → peer beta @ {b_port}')
        print(f'    gw-beta  bound 127.0.0.1:{b_port}')

        # ── happy path: send via A's local inbox → expect arrival on B's
        # local inbox of agent 1 (the route remote_agent).
        fab_a.inbox_send(from_id=7, to_id=gw_a.id,
                         msg_type=TYPE, payload='WARN:MEM_PSI_WARN')
        _drive_inbox(gw_a)

        msg = _wait_for_inbox(fab_b, aid=1, timeout_s=3.0)
        _check('cross-swarm message arrived in B/inbox(1)', msg is not None)
        if msg is not None:
            _check('type preserved across VJR',           msg['type'] == TYPE)
            _check('payload preserved across VJR',
                   msg['payload'] == 'WARN:MEM_PSI_WARN')
            _check('sender on B side is gw_b.id (relay)', msg['sender'] == gw_b.id)

        # ── unroutable type: send a type with no route → no inbox on B,
        # error logged on A.
        before = _count_verb_errors(fab_a, gw_a.id, 'gw.unroutable')
        fab_a.inbox_send(from_id=7, to_id=gw_a.id,
                         msg_type=999, payload='nope')
        _drive_inbox(gw_a)
        time.sleep(0.1)
        after = _count_verb_errors(fab_a, gw_a.id, 'gw.unroutable')
        _check('unroutable type logged via VERB_ERROR',  after > before)

        # ── bad PSK: build a third gateway with the WRONG psk pointed at
        # B. Send a message; expect it to NEVER arrive on B (HMAC reject).
        # We use a fresh fabric C so there's no cross-talk.
        fab_c, path_c = _fresh_fabric('c')
        try:
            gw_c = _make_gateway(
                aid=9, swarm_name='gamma',
                bind='127.0.0.1:0',
                peer_name='beta', peer_addr=f'127.0.0.1:{b_port}',
                route_type=TYPE, route_remote_agent=2,   # different slot to disambiguate
                psk=WRONG_PSK)
            gw_c.attach(fab_c)
            gw_c.on_tick()

            fab_c.inbox_send(from_id=7, to_id=gw_c.id,
                             msg_type=TYPE, payload='IMPOSTER')
            _drive_inbox(gw_c)

            # poll B/inbox(2) — should NEVER receive
            bad = _wait_for_inbox(fab_b, aid=2, timeout_s=0.8)
            _check('wrong-PSK message did NOT arrive on B', bad is None)
            # B should have logged at least one badframe
            bad_logged = _count_verb_errors(fab_b, gw_b.id, 'gw.badframe')
            _check('B logged gw.badframe for wrong-PSK frame', bad_logged > 0)

            gw_c.shutdown()
        finally:
            fab_c.close()
            if os.path.exists(path_c):
                os.remove(path_c)

        gw_a.shutdown()
        gw_b.shutdown()
        # let threads notice the stop event
        time.sleep(0.6)

    finally:
        fab_a.close()
        fab_b.close()
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.remove(p)

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
