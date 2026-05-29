"""
test_vjr_protocol.py — VJR pack/unpack round-trip + tamper resistance.

Three things must hold for the wire protocol to be trustworthy enough to
sit between sub-swarm fabrics:

  1. ROUND-TRIP   : pack(env, psk) → bytes → unpack_one → same Envelope.
  2. INTEGRITY    : flip a payload byte → HMAC mismatch → drop frame.
  3. AUTHENTICITY : pack with PSK_A, unpack with PSK_B → drop frame.

Plus stream behavior (split reads, concatenated frames, garbage prefix).
If any of these fail, gateway cross-swarm messaging is unsafe.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_vjr_protocol
"""

import sys

from swarm.protocol.vjr import (
    Envelope, pack, unpack_one, drain, HMAC_LEN, LEN_HDR,
)


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


PSK = 'unit-test-psk-1'
WRONG_PSK = 'unit-test-psk-2'


def _make_env(payload='WARN:MEM_PSI_WARN'):
    return Envelope(src_swarm='kernel', dst_swarm='evolver',
                    src_agent=107, dst_agent=901, type=700,
                    payload=payload, ts=1.0)


def main():
    print()
    print('== VJR wire protocol ==')

    # 1. round-trip
    env = _make_env()
    frame = pack(env, PSK)
    got, n = unpack_one(frame, PSK)
    _check('round-trip: unpacked one frame',     got is not None)
    _check('round-trip: consumed full frame',    n == len(frame))
    _check('round-trip: src_swarm preserved',    got.src_swarm == 'kernel')
    _check('round-trip: dst_swarm preserved',    got.dst_swarm == 'evolver')
    _check('round-trip: src_agent preserved',    got.src_agent == 107)
    _check('round-trip: dst_agent preserved',    got.dst_agent == 901)
    _check('round-trip: type preserved',         got.type == 700)
    _check('round-trip: payload preserved',      got.payload == 'WARN:MEM_PSI_WARN')

    # 2. tampered payload → HMAC fail → drop frame
    body_start = LEN_HDR + HMAC_LEN
    tampered = (frame[:body_start]
                + bytes([frame[body_start] ^ 0x20])
                + frame[body_start + 1:])
    got_t, n_t = unpack_one(tampered, PSK)
    _check('tamper: payload-flipped → drop',     got_t is None)
    _check('tamper: full frame still consumed',  n_t == len(frame))

    # 3. wrong PSK → HMAC fail → drop frame
    got_w, n_w = unpack_one(frame, WRONG_PSK)
    _check('wrong PSK: → drop',                  got_w is None)
    _check('wrong PSK: full frame still consumed', n_w == len(frame))

    # 4. truncated stream → wait-for-more
    half = frame[:len(frame) // 2]
    got_h, n_h = unpack_one(half, PSK)
    _check('truncated: → (None, 0)',             got_h is None and n_h == 0)

    # 5. two frames concatenated → drain yields both
    a = pack(_make_env('A'), PSK)
    b = pack(_make_env('B'), PSK)
    envs, leftover = drain(a + b, PSK)
    _check('drain: two frames returned',         len(envs) == 2)
    _check('drain: order preserved (A first)',
           envs[0] is not None and envs[0].payload == 'A')
    _check('drain: order preserved (B second)',
           envs[1] is not None and envs[1].payload == 'B')
    _check('drain: no leftover bytes',           leftover == b''[:0])

    # 6. garbage length header → resync (skip 4 bytes, do not deadlock)
    junk = b'\xff\xff\xff\xff' + frame
    envs2, leftover2 = drain(junk, PSK)
    _check('garbage length: skipped',            len(envs2) >= 1)
    _check('garbage length: real frame still parsed',
           any(e is not None and e.payload == 'WARN:MEM_PSI_WARN' for e in envs2))

    # 7. partial second frame after a good first → first delivered, second held
    partial = a + b[:5]
    envs3, leftover3 = drain(partial, PSK)
    _check('partial-tail: first frame parsed',
           len(envs3) == 1 and envs3[0] is not None and envs3[0].payload == 'A')
    _check('partial-tail: tail held for next read', leftover3 == b[:5])

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
