"""
dna_storage.py — genomes live in the shared fabric as a chain of state slots.

This is the substrate for in-fabric Borg mutation: every agent's DNA is
readable AND writable by any other agent in the swarm via the same
shared-memory primitives. An optimizer agent rewrites another agent's
genome by calling `write(fabric, target_id, new_genome)`; the target sees
the new DNA on its next on_tick via `read(fabric, self.id)`.

Why a chain of slots: fabric state values cap at 20 bytes (a hard contract
inherited from vajrayana). A non-trivial genome can be 60–160 bytes. So we
fan out:

    dna.{id}.0    first ≤20 UTF-8 bytes, codepoint-aligned
    dna.{id}.1    next ≤20 bytes
    ...
    dna.{id}.7    last possible chunk (8 * 20 = 160 byte ceiling)

Reading concatenates until the first empty slot. Writing splits codepoint-
aware so no chunk is mid-codepoint (which would round-trip through the
fabric's `errors='replace'` decode as U+FFFD and corrupt the genome). Slot
clears past the genome length are explicit — a shorter new genome must not
leave stale tail bytes from a longer predecessor.

Atomicity: each `state_set` is locked, but the multi-slot write is NOT
atomic. A reader hitting the chain mid-write may see a mix of old and new
chunks. The genome interpreter is robust to arbitrary garbage (unknown
opcodes are silent no-ops; stack underflow pushes 0), so the worst-case
outcome of a torn read is one tick of wrong verdict. Acceptable at v1; an
atomic generation counter is the obvious next step if it becomes a real
problem.
"""

MAX_CHUNKS = 8           # 8 × 20 = 160-byte ceiling per agent
CHUNK_BYTES = 20         # must match the fabric's _to_bytes truncation


def split(genome):
    """Split a UTF-8 str into chunks of ≤CHUNK_BYTES bytes each, never
    splitting mid-codepoint. Returns a list of valid UTF-8 strs. Truncates
    silently past MAX_CHUNKS × CHUNK_BYTES bytes."""
    encoded = genome.encode('utf-8')
    chunks = []
    i, n = 0, len(encoded)
    while i < n and len(chunks) < MAX_CHUNKS:
        end = min(i + CHUNK_BYTES, n)
        # UTF-8 continuation bytes are 10xxxxxx — back up off any we'd cut
        while end > i and end < n and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[i:end].decode('utf-8'))
        i = end
    return chunks


def write(fabric, aid, genome, writer=None):
    """Write `genome` to dna.{aid}.0..{MAX_CHUNKS-1}. Any slots past the
    genome length are CLEARED so stale tail bytes from a longer predecessor
    cannot leak into the next read. `writer` is the agent id stamped on the
    state version; defaults to `aid` (the agent owns its own DNA)."""
    if writer is None:
        writer = aid
    chunks = split(genome)
    for k in range(MAX_CHUNKS):
        v = chunks[k] if k < len(chunks) else ''
        fabric.state_set(f'dna.{aid}.{k}', v, writer)


def read(fabric, aid):
    """Read dna.{aid}.0..N until the first empty slot. Returns '' if no
    slots have been seeded."""
    chunks = []
    for k in range(MAX_CHUNKS):
        v, _ = fabric.state_get(f'dna.{aid}.{k}')
        if not v:
            break
        chunks.append(v)
    return ''.join(chunks)
