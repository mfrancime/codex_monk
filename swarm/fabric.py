"""
Shared Memory Fabric — The nervous system of the swarm.

One memory-mapped file. Every agent maps it into its own address space.
Provides: state table, event log, inboxes, signals, named events.
Cross-platform: Windows (native) and Linux (mmap + futex fallback).
"""

import mmap
import struct
import os
import sys
import time
import ctypes

# ══════════════════════════════════════════════════════
#  LAYOUT
# ══════════════════════════════════════════════════════

FABRIC_MAGIC   = 0xFAB71C00
FABRIC_VERSION = 1
FABRIC_SIZE    = 512 * 1024   # 512KB fixed

MAX_AGENTS          = 32
MAX_STATE_SLOTS     = 1024
MAX_LOG_ENTRIES     = 4096
MAX_INBOX_PER_AGENT = 64

# Region offsets (byte-aligned, non-overlapping)
OFF_SUPER   = 0x00000    # 256 B    superblock
OFF_AGENTS  = 0x00100    # 4 KB     32 agents × 128 B
OFF_STATE   = 0x01100    # 64 KB    1024 slots × 64 B
OFF_INBOXES = 0x11100    # 128 KB   32 agents × 64 msgs × 64 B
OFF_LOG     = 0x31100    # 256 KB   4096 entries × 64 B
# End:        0x71100    ≈ 455 KB  < 512 KB ✓

# ── Superblock fields (offsets from OFF_SUPER) ───────
SB_MAGIC      = 0     # uint32
SB_VERSION    = 4     # uint32
SB_TICK       = 8     # uint64
SB_BOOT_TIME  = 16    # uint64
SB_AGENT_CNT  = 24    # uint32
SB_LOG_HEAD   = 28    # uint32  (next seq to write)

# ── ACB fields (offsets within 128-byte slot) ────────
ACB_SIZE       = 128
ACB_ID         = 0     # uint16
ACB_TYPE       = 2     # uint8
ACB_PRIORITY   = 3     # uint8
ACB_STATE      = 4     # uint8
ACB_FLAGS      = 6     # uint16
ACB_ERRNO      = 8     # uint16
ACB_RUNTIME    = 16    # uint64  (cumulative μs)
ACB_LAST_SCHED = 24    # uint64
ACB_VRUNTIME   = 32    # uint64
ACB_INBOX_HEAD = 64    # uint32
ACB_INBOX_TAIL = 68    # uint32
ACB_SIGNALS    = 72    # uint64  (bitmask)
ACB_SIG_MASK   = 80    # uint64
ACB_PARENT     = 88    # uint16
ACB_GROUP      = 90    # uint16
ACB_HEARTBEAT  = 96    # uint64  (unix ts)
ACB_WATCHDOG   = 104   # uint32  (seconds)
ACB_PID        = 108   # uint32
ACB_LOCK       = 112   # uint32  (spinlock)

# ── State-slot layout (64 bytes) ─────────────────────
SS_SIZE      = 64
SS_LOCK      = 0     # uint32
SS_VERSION   = 4     # uint32
SS_WRITER    = 8     # uint16
SS_PAD       = 10    # uint16
SS_TIMESTAMP = 12    # uint64
SS_KEY       = 20    # 24 bytes
SS_VALUE     = 44    # 20 bytes
SS_FMT       = '<I I H H Q 24s 20s'

# ── Log-entry layout (64 bytes) ──────────────────────
LE_SIZE      = 64
LE_FMT       = '<Q Q H B B 24s 20s'
#               seq ts  aid verb pad key   value

# ── Inbox-message layout (64 bytes) ──────────────────
IM_SIZE      = 64
IM_FMT       = '<H H I Q 48s'
#               from type pad ts payload

# ── Agent states ─────────────────────────────────────
S_FREE    = 0
S_READY   = 1
S_RUNNING = 2
S_BLOCKED = 3
S_ZOMBIE  = 4

STATE_NAMES = {0: 'FREE', 1: 'READY', 2: 'RUNNING', 3: 'BLOCKED', 4: 'ZOMBIE'}

# ── Priority classes ─────────────────────────────────
P_REALTIME = 0
P_HIGH     = 1
P_NORMAL   = 2
P_LOW      = 3
P_IDLE     = 4

PRIO_NAMES = {0: 'RT', 1: 'HIGH', 2: 'NORM', 3: 'LOW', 4: 'IDLE'}

# ── Signals (bit positions) ─────────────────────────
SIG_WAKE  = 0
SIG_STOP  = 1
SIG_KILL  = 3
SIG_USR1  = 4
SIG_USR2  = 5
SIG_CHILD = 6
SIG_ALARM = 7
SIG_MSG   = 8

# ── Log verbs ────────────────────────────────────────
VERB_STATE  = 1
VERB_SIGNAL = 2
VERB_SPAWN  = 3
VERB_EXIT   = 4
VERB_MSG    = 5
VERB_ERROR  = 6

VERB_NAMES = {1: 'STATE', 2: 'SIG', 3: 'SPAWN', 4: 'EXIT', 5: 'MSG', 6: 'ERROR'}

# ══════════════════════════════════════════════════════
#  PLATFORM LAYER
# ══════════════════════════════════════════════════════

IS_WINDOWS = sys.platform == 'win32'

if IS_WINDOWS:
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)

    # — named mutexes (for cross-process locking) ────
    _k32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
    ]
    _k32.CreateMutexW.restype = ctypes.c_void_p

    _k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    _k32.ReleaseMutex.restype  = ctypes.c_bool

    # — named events (for cross-process wake) ────────
    _k32.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p,
    ]
    _k32.CreateEventW.restype = ctypes.c_void_p

    _k32.SetEvent.argtypes = [ctypes.c_void_p]
    _k32.SetEvent.restype  = ctypes.c_bool

    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _k32.WaitForSingleObject.restype  = ctypes.c_ulong

    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype  = ctypes.c_bool

    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT  = 258


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════

def _default_path():
    if IS_WINDOWS:
        return os.path.join(os.environ.get('TEMP', '.'), 'swarm.fabric')
    shm = '/dev/shm'
    return os.path.join(shm if os.path.isdir(shm) else '/tmp', 'swarm.fabric')


def _to_bytes(s, size):
    if isinstance(s, bytes):
        b = s
    else:
        b = str(s).encode('utf-8')
    return b[:size].ljust(size, b'\x00')


def _from_bytes(b):
    return b.rstrip(b'\x00').decode('utf-8', errors='replace')


def _djb2(key_bytes, cap):
    h = 5381
    for b in key_bytes:
        h = ((h * 33) + b) & 0xFFFFFFFF
    return h % cap


# ══════════════════════════════════════════════════════
#  FABRIC
# ══════════════════════════════════════════════════════

class Fabric:
    """
    Shared-memory fabric backed by a single mmap'd file.

    Usage:
        creator:  Fabric(create=True)
        joiner:   Fabric()               # opens existing
    """

    def __init__(self, path=None, create=False):
        self.path = path or _default_path()
        self._evt = {}          # name → handle (Windows events)
        self._mtx = {}          # name → handle (Windows mutexes)

        if create:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            with open(self.path, 'wb') as f:
                f.write(b'\x00' * FABRIC_SIZE)

        self._fh = open(self.path, 'r+b')
        self.mm = mmap.mmap(self._fh.fileno(), FABRIC_SIZE)

        # raw pointer for atomics
        self._buf  = (ctypes.c_char * FABRIC_SIZE).from_buffer(self.mm)
        self._base = ctypes.addressof(self._buf)

        if create:
            self._init_super()

    # ── lifecycle ─────────────────────────────────────

    def _init_super(self):
        struct.pack_into('<I I Q Q I I', self.mm, OFF_SUPER,
                         FABRIC_MAGIC, FABRIC_VERSION, 0,
                         int(time.time()), 0, 0)

    def close(self):
        for h in self._evt.values():
            if IS_WINDOWS and h:
                _k32.CloseHandle(h)
        for h in self._mtx.values():
            if IS_WINDOWS and h:
                _k32.CloseHandle(h)
        self._evt.clear()
        self._mtx.clear()
        del self._buf           # release ctypes pointer before mmap close
        self._buf = None
        self._base = 0
        self.mm.close()
        self._fh.close()

    # ── raw memory ────────────────────────────────────

    def r8(self, off):
        return self.mm[off]

    def r16(self, off):
        return struct.unpack_from('<H', self.mm, off)[0]

    def r32(self, off):
        return struct.unpack_from('<I', self.mm, off)[0]

    def r64(self, off):
        return struct.unpack_from('<Q', self.mm, off)[0]

    def w8(self, off, v):
        struct.pack_into('<B', self.mm, off, v)

    def w16(self, off, v):
        struct.pack_into('<H', self.mm, off, v)

    def w32(self, off, v):
        struct.pack_into('<I', self.mm, off, v)

    def w64(self, off, v):
        struct.pack_into('<Q', self.mm, off, v)

    # ── locking (named mutex for cross-process safety) ──

    def _mutex(self, name):
        """Get or create a named mutex."""
        if name not in self._mtx:
            if IS_WINDOWS:
                h = _k32.CreateMutexW(None, False, name)
                self._mtx[name] = h
            else:
                self._mtx[name] = None
        return self._mtx[name]

    def lock(self, off, owner=1):
        """Acquire a named mutex keyed by offset."""
        name = f'SwarmLock_{off}'
        if IS_WINDOWS:
            h = self._mutex(name)
            if h:
                rc = _k32.WaitForSingleObject(h, 5000)  # 5s timeout
                return rc == _WAIT_OBJECT_0
        # fallback: spin on byte (single-machine POC)
        spins = 0
        while True:
            cur = self.r32(off)
            if cur == 0:
                self.w32(off, owner)
                return True
            spins += 1
            if spins > 10_000:
                time.sleep(0.0001)
            if spins > 200_000:
                return False
        return True

    def unlock(self, off):
        """Release the named mutex keyed by offset."""
        self.w32(off, 0)
        name = f'SwarmLock_{off}'
        if IS_WINDOWS and name in self._mtx:
            h = self._mtx[name]
            if h:
                _k32.ReleaseMutex(h)

    def inc32(self, off):
        """Increment a 32-bit value (mutex-protected)."""
        name = f'SwarmInc_{off}'
        if IS_WINDOWS:
            h = self._mutex(name)
            if h:
                _k32.WaitForSingleObject(h, 5000)
        v = self.r32(off) + 1
        self.w32(off, v)
        if IS_WINDOWS and name in self._mtx:
            _k32.ReleaseMutex(self._mtx[name])
        return v

    # ── named events (cross-process wake) ─────────────

    def _evt_handle(self, name):
        if name not in self._evt:
            if IS_WINDOWS:
                h = _k32.CreateEventW(None, False, False, name)
                if not h:
                    return None
                self._evt[name] = h
            else:
                self._evt[name] = None
        return self._evt[name]

    def evt_signal(self, name):
        if IS_WINDOWS:
            h = self._evt_handle(name)
            if h:
                _k32.SetEvent(h)

    def evt_wait(self, name, timeout_ms=3000):
        if IS_WINDOWS:
            h = self._evt_handle(name)
            if h:
                rc = _k32.WaitForSingleObject(h, timeout_ms)
                return rc == _WAIT_OBJECT_0
        time.sleep(timeout_ms / 1000.0)
        return False

    # ── ACB helpers ───────────────────────────────────

    def acb(self, aid):
        """Base offset for agent's ACB."""
        return OFF_AGENTS + aid * ACB_SIZE

    def acb_r(self, aid, field, fmt='<B'):
        return struct.unpack_from(fmt, self.mm, self.acb(aid) + field)[0]

    def acb_w(self, aid, field, val, fmt='<B'):
        struct.pack_into(fmt, self.mm, self.acb(aid) + field, val)

    # ── state table ───────────────────────────────────

    def _slot(self, key):
        """Resolve key → (offset, key_bytes) via linear probe."""
        kb = _to_bytes(key, 24)
        empty = b'\x00' * 24
        h = _djb2(kb, MAX_STATE_SLOTS)
        for i in range(MAX_STATE_SLOTS):
            idx = (h + i) % MAX_STATE_SLOTS
            off = OFF_STATE + idx * SS_SIZE
            stored = self.mm[off + SS_KEY : off + SS_KEY + 24]
            if stored == kb or stored == empty:
                return off, kb
        return -1, kb

    def state_get(self, key):
        """Read → (value_str, version) or (None, 0)."""
        off, kb = self._slot(key)
        if off < 0:
            return None, 0
        stored = self.mm[off + SS_KEY : off + SS_KEY + 24]
        if stored == b'\x00' * 24:
            return None, 0
        val = _from_bytes(self.mm[off + SS_VALUE : off + SS_VALUE + 20])
        ver = self.r32(off + SS_VERSION)
        return val, ver

    def state_set(self, key, value, writer=0):
        """Write → version (or -1 on failure)."""
        off, kb = self._slot(key)
        if off < 0:
            return -1
        if not self.lock(off + SS_LOCK, writer or 1):
            return -1
        ver = self.r32(off + SS_VERSION) + 1
        vb  = _to_bytes(value, 20)
        ts  = int(time.time())
        # pack the whole slot (lock=0 releases it atomically enough for POC)
        struct.pack_into(SS_FMT, self.mm, off,
                         0, ver, writer, 0, ts, kb, vb)
        return ver

    # ── event log ─────────────────────────────────────

    def log_append(self, aid, verb, key, value=''):
        seq = self.inc32(OFF_SUPER + SB_LOG_HEAD)
        slot = (seq - 1) % MAX_LOG_ENTRIES
        off  = OFF_LOG + slot * LE_SIZE
        now  = int(time.time() * 1_000_000)
        struct.pack_into(LE_FMT, self.mm, off,
                         seq, now, aid, verb, 0,
                         _to_bytes(key, 24), _to_bytes(value, 20))
        return seq

    def log_read(self, seq):
        if seq < 1:
            return None
        slot = (seq - 1) % MAX_LOG_ENTRIES
        off  = OFF_LOG + slot * LE_SIZE
        r = struct.unpack_from(LE_FMT, self.mm, off)
        if r[0] != seq:
            return None
        return dict(seq=r[0], timestamp=r[1], agent=r[2],
                    verb=r[3], key=_from_bytes(r[5]), value=_from_bytes(r[6]))

    def log_head(self):
        return self.r32(OFF_SUPER + SB_LOG_HEAD)

    # ── signals ───────────────────────────────────────

    def sig_send(self, target, bit):
        off = self.acb(target) + ACB_SIGNALS
        cur = self.r64(off)
        self.w64(off, cur | (1 << bit))
        self.evt_signal(f'Swarm_{target}')

    def sig_recv(self, aid):
        off = self.acb(aid) + ACB_SIGNALS
        v = self.r64(off)
        if v:
            self.w64(off, 0)
        return v

    # ── inbox ─────────────────────────────────────────

    def _inbox(self, aid):
        return OFF_INBOXES + aid * MAX_INBOX_PER_AGENT * IM_SIZE

    def inbox_send(self, from_id, to_id, msg_type, payload=''):
        a = self.acb(to_id)
        head = self.r32(a + ACB_INBOX_HEAD)
        tail = self.r32(a + ACB_INBOX_TAIL)
        if (tail - head) >= MAX_INBOX_PER_AGENT:
            return False
        slot = tail % MAX_INBOX_PER_AGENT
        off  = self._inbox(to_id) + slot * IM_SIZE
        now  = int(time.time() * 1_000_000)
        struct.pack_into(IM_FMT, self.mm, off,
                         from_id, msg_type, 0, now, _to_bytes(payload, 48))
        self.w32(a + ACB_INBOX_TAIL, tail + 1)
        self.sig_send(to_id, SIG_MSG)
        return True

    def inbox_recv(self, aid):
        a = self.acb(aid)
        head = self.r32(a + ACB_INBOX_HEAD)
        tail = self.r32(a + ACB_INBOX_TAIL)
        if head >= tail:
            return None
        slot = head % MAX_INBOX_PER_AGENT
        off  = self._inbox(aid) + slot * IM_SIZE
        r = struct.unpack_from(IM_FMT, self.mm, off)
        self.w32(a + ACB_INBOX_HEAD, head + 1)
        return dict(sender=r[0], type=r[1], timestamp=r[3],
                    payload=_from_bytes(r[4]))

    # ── tick ──────────────────────────────────────────

    def tick(self):
        return self.r64(OFF_SUPER + SB_TICK)

    def tick_advance(self):
        t = self.tick() + 1
        self.w64(OFF_SUPER + SB_TICK, t)
        return t
