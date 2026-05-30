# CLAUDE.md

Guidance for working in **codex_monk** — an evolvable, declarative agentic OS.

## The design law (read first)

**New behavior comes from a new genome string + new YAML, never a new Python
class.** This is the project's explicit "Karpathy" rule: behavior is data, not
code. There is exactly one agent class (`DeclarativeAgent`) and one genome
interpreter; capabilities are recombined from opcodes and config.

Before writing any `.py`, ask: *can an existing opcode or YAML knob already
reach this?* Only fall back to Python when a genuinely new domain needs a new
**probe module** (a new sensor surface + opcode alphabet). When that's truly
required, name the cost up front and keep it to one probe module — do **not**
add a new agent subclass, and prefer not to add interpreter opcodes.

> Lesson worth remembering: temporal windows and cross-source aggregation
> belong in the **probe** (a Python sampler that exposes a derived scalar Frame
> key), not in new genome opcodes. The stateless RPN genome then gates on the
> scalar with ordinary comparators. (See `fabric_peer`'s 60s query-delta and
> `quorum`'s cross-fabric counts.)

## Architecture

- **`swarm/fabric.py`** — a fixed 512 KB shared-memory file (`/dev/shm/codex.*.fabric`):
  superblock + 32 agent control blocks + 1024 state slots (key≤24B, value≤20B) +
  per-agent inboxes + an event log. Byte-compatible with the `vajrayana` fabric
  it forked from, which is why `fabric_peer`/`quorum` can read sibling swarms.
- **`swarm/genome.py`** — a stack-based RPN interpreter. A genome is a short
  UTF-8 string of opcodes interpreted against a **Frame** (a probe's dict) plus a
  per-domain **opcode table**, yielding a `(sev, code)` verdict. Stateless,
  crash-proof (unknown/garbage opcodes no-op), so mutated DNA can only score
  poorly, never raise. Spine: literals, comparators `> < ≥ ≤ ≡ ≠`, boolean
  `∧ ∨ ¬`, emit `→XY`, rule separator `;`.
- **`swarm/agents/declarative.py`** — `DeclarativeAgent`, the **only** agent
  class. Three config-selected roles: **probe** (has a `genome` — samples, runs
  `interpret`, edge-emits on verdict change), **sink** (has `consume_types` —
  persists alerts), **mutator** (has `mutate_target`/`propose_to` — evolves a
  genome and writes it back, locally or cross-swarm via `MSG_DNA_PROPOSE=701`).
- **Multiswarm** (`boot.py` + `multiswarm*.yaml` + `swarms/*.yaml`) — one OS
  process per sub-swarm, each with its own fabric, bridged by `GatewayAgent`
  over the VJR protocol (`swarm/protocol/vjr.py`, HMAC-tagged length-prefixed
  JSON). Each sub-swarm namespaces its state with a short `state_prefix`
  (`nod.`, `clu.`, `agg.`).
- **`viz.py`** + **`web/`** — live war-room: an HTTP server exposing
  `/api/swarms`, with a full-screen 3D WebGL UI (`web/scene3d.js`).

## Probe registry (`swarm/probes/`)

A probe is a module exposing `sample_all() -> dict` (the Frame), an `OPCODES`
table `{first_char: {sig_char: frame_key}}`, and `describe()`. It registers
itself at import via `register(...)`; `DeclarativeAgent` resolves it by the
`probe:` config name. Domain authors pick disjoint opcode first-chars. Probes
take **no constructor args** — runtime config comes from env vars (resolved at
sample time).

| Probe | Opcode | Domain |
|---|---|---|
| `kernel` | `ψ` `~` `κ` | PSI / swap / memory / cgroup (the original sensor) |
| `cgroup_pods` | `Π` | per-pod cgroup pressure + OOM kills (`/sys/fs/cgroup/kubepods.slice`) |
| `disk_net` | `Δ` `Ν` | disk utilization (`/proc/diskstats`) + network errors (`/proc/net/dev`) |
| `k8s_api` | `K` | control-plane health (apiserver/etcd/scheduler/nodes/events) |
| `fabric_peer` | `Ψ` | introspect **one** sibling fabric's state (sev, PSI, heartbeat, RAG) |
| `quorum` | `Γ` | **the governor's eyes** — correlate **many** sibling fabrics at once |

### `quorum` — the governor probe

Opens every sibling fabric and reads each one's gate verdict + heartbeat
**prefix-agnostically** (it enumerates state slots and matches keys by suffix,
so `nod.sys.sev` / `clu.sys.sev` need no configured prefix). It infers each
peer's role from its fabric basename (`*cluster*`/`*control*`/`*api*` →
control plane, else node) and exposes cluster-wide **counts** as Frame scalars,
so a stateless genome can reason about the whole cluster.

- **Frame keys / opcodes (`Γ`):** `Γt` peers_total, `Γu` peers_present,
  `Γs` peers_stale, `Γn` node_total, `Γp` node_pressured (sev≥WARN),
  `Γc` node_critical (sev≥CRITICAL), `Γo` control_total, `Γk` control_ok
  (all control peers present & ≤INFO), `Γm` max_sev.
- **Discovery (env, sample-time):** `CODEX_QUORUM_PEERS` (explicit comma list,
  wins) or `CODEX_QUORUM_GLOB` (default `/dev/shm/codex.*.fabric`);
  `CODEX_QUORUM_SELF` and any `*aggregat*` basename are excluded;
  `CODEX_QUORUM_STALE_S` (default 30) sets the heartbeat-staleness threshold.
- **Used by** the governor agent (id=2) in `swarms/k8s_aggregator.yaml`, genome
  `Γc0>Γk∧→Cd;Γk0≡→Cg;Γp0>→Wp`: a node CRITICAL while the control plane is
  healthy → `CLUSTER_DEGRADED`; control plane itself down → `GATE_DOWN`
  (dominates); a node merely pressured → `POD_PRESSURE`. Its verdict is routed
  back to the audit sink so it joins the cluster timeline.

To add a probe: write `swarm/probes/<name>.py` calling `register(...)` with a
fresh opcode first-char, add `<name>` to the eager-import list in
`swarm/probes/__init__.py`, then reference it from YAML as `probe: <name>`.

## Running & testing

```bash
# only runtime dependency is pyyaml (see requirements.txt)
python boot.py                              # single swarm (swarm.yaml)
python boot.py --config multiswarm.k8s.yaml # 3-swarm k8s monitor (node/cluster/aggregator)
python viz.py --port 19200                  # live war-room UI on http://127.0.0.1:19200/

python -m eval.test_quorum                  # run one test ("ALL PASS" on success)
for t in eval/test_*.py; do python -m "eval.$(basename $t .py)"; done   # full suite
```

Each `eval/test_*.py` is a standalone script printing `[PASS]/[FAIL]` and
exiting non-zero on failure. New probes get a matching test that builds
synthetic fabrics in `/tmp` and asserts both Frame values and genome verdicts
(see `eval/test_quorum.py`, `eval/test_fabric_peer.py`).

The web UI must be verified in a real browser (`web/verify_ui.py`, Playwright),
not just by curl/import.

## Operational gotchas

- **Forkserver survival:** `multiprocessing` sub-swarm children do **not** die
  cleanly when `boot.py` is interrupted — they keep running and hold their TCP
  ports (`Address already in use` on relaunch). Recover by killing them by PID
  / `pkill -9 -f "boot.py --config <file>"` and `rm -f /dev/shm/codex.*.fabric`.
- **Probe config is env-based**, not YAML — probes can't see agent config, so
  cross-cutting settings (peer paths, thresholds) go through `CODEX_*` env vars.
- `graph/*.jsonl` (alert sinks) are gitignored; live runs won't dirty the tree.
