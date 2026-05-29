"""
vjr.py — Vajrayana wire protocol v1.

Inter-swarm transport for codex_monk. Each gateway agent uses this to ship
inbox messages between fabrics over TCP. The protocol is intentionally
minimal so the gateway can be the last big Python commit and never grow:

  Frame on the wire:
    [4 bytes  big-endian payload length N]
    [32 bytes HMAC-SHA256(psk, payload)]
    [N bytes  payload — JSON envelope]

  Envelope (JSON, all keys required):
    {
      "ver":        1,
      "src_swarm":  str,
      "dst_swarm":  str,
      "src_agent":  int,
      "dst_agent":  int,
      "type":       int,
      "ts":         float,    # producer-side time (informational only)
      "payload":    str       # ≤48 bytes — fabric inbox carries no more
    }

Defensive: any malformed frame, bad HMAC, oversize length, or JSON parse
failure causes `unpack_one` to return None. The caller treats None as
"drop this frame, log it, keep reading the stream." A torn read (fewer
bytes than expected) returns None as well; the caller retries when more
bytes arrive on the socket.
"""

import hashlib
import hmac
import json
import struct


VER = 1
HMAC_LEN = 32                 # SHA-256
MAX_PAYLOAD = 4096            # 4 KB cap per envelope — far above 48-byte inbox
LEN_HDR = 4                   # 4-byte big-endian length prefix


class Envelope:
    """A VJR message — what the gateway packs into a frame and what
    arrives at the other side after unpacking. Keep small + boring."""

    __slots__ = ('ver', 'src_swarm', 'dst_swarm', 'src_agent',
                 'dst_agent', 'type', 'ts', 'payload')

    def __init__(self, src_swarm, dst_swarm, src_agent, dst_agent,
                 type, payload, ts=0.0, ver=VER):
        self.ver = ver
        self.src_swarm = src_swarm
        self.dst_swarm = dst_swarm
        self.src_agent = int(src_agent)
        self.dst_agent = int(dst_agent)
        self.type = int(type)
        self.ts = float(ts)
        self.payload = payload or ''

    def to_dict(self):
        return {
            'ver': self.ver,
            'src_swarm': self.src_swarm,
            'dst_swarm': self.dst_swarm,
            'src_agent': self.src_agent,
            'dst_agent': self.dst_agent,
            'type': self.type,
            'ts': self.ts,
            'payload': self.payload,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            src_swarm=str(d['src_swarm']),
            dst_swarm=str(d['dst_swarm']),
            src_agent=int(d['src_agent']),
            dst_agent=int(d['dst_agent']),
            type=int(d['type']),
            payload=str(d.get('payload', '')),
            ts=float(d.get('ts', 0.0)),
            ver=int(d.get('ver', VER)),
        )


def _hmac(psk, payload_bytes):
    key = psk.encode('utf-8') if isinstance(psk, str) else psk
    return hmac.new(key, payload_bytes, hashlib.sha256).digest()


def pack(envelope, psk):
    """Serialize Envelope → bytes ready for socket.sendall."""
    body = json.dumps(envelope.to_dict(), separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_PAYLOAD:
        raise ValueError(f'VJR payload too large: {len(body)} > {MAX_PAYLOAD}')
    tag = _hmac(psk, body)
    return struct.pack('>I', len(body)) + tag + body


def unpack_one(buf, psk):
    """Try to consume one frame from the head of `buf` (a bytes object).

    Returns (Envelope, consumed_bytes) on success. Returns (None, 0) if
    `buf` doesn't yet hold a full frame (caller reads more and retries).
    Returns (None, skip) where skip > 0 if a frame was structurally read
    but failed HMAC / JSON / version — the caller should drop those bytes
    and continue. This three-state contract keeps the gateway's read loop
    simple.
    """
    n = len(buf)
    if n < LEN_HDR:
        return None, 0
    (payload_len,) = struct.unpack('>I', buf[:LEN_HDR])
    if payload_len <= 0 or payload_len > MAX_PAYLOAD:
        # garbage length — skip just the length header so the loop can
        # resynchronize on the next read
        return None, LEN_HDR
    frame_total = LEN_HDR + HMAC_LEN + payload_len
    if n < frame_total:
        return None, 0
    tag = buf[LEN_HDR:LEN_HDR + HMAC_LEN]
    body = buf[LEN_HDR + HMAC_LEN:frame_total]
    expected = _hmac(psk, body)
    if not hmac.compare_digest(tag, expected):
        return None, frame_total            # bad HMAC: drop whole frame
    try:
        d = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, frame_total
    if not isinstance(d, dict) or d.get('ver') != VER:
        return None, frame_total
    try:
        env = Envelope.from_dict(d)
    except (KeyError, ValueError, TypeError):
        return None, frame_total
    return env, frame_total


def drain(buf, psk):
    """Consume all complete frames from `buf`. Returns (envelopes, leftover).
    `envelopes` may include Nones — these are frames that parsed structurally
    but failed validation; the caller logs them. Leftover bytes are kept for
    the next socket read."""
    envs = []
    while True:
        env, consumed = unpack_one(buf, psk)
        if env is None and consumed == 0:
            break                            # need more bytes
        envs.append(env)                     # None on validation failure
        buf = buf[consumed:]
    return envs, buf
