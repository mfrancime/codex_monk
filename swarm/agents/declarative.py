"""
DeclarativeAgent — the only agent class in codex_monk.

New behaviors come from new genome strings + new YAML, not from new Python
files. This is the user's stated design law for the agentic OS.

Three roles, all selected by config — never by subclass:

  PROBE role (has a genome):
    on_tick → reads live genome from fabric (dna.{id}.0..N), samples kernel
    telemetry, runs `interpret(genome, frame)`, edge-emits on (sev, code)
    change. The genome can be REWRITTEN at runtime by another agent (see
    MUTATOR role); the probe sees the new DNA on its very next tick.

  SINK role (has consume_types):
    on_message → if msg.type matches, persist + audit. Replaces EchoAgent.

  MUTATOR role (has mutate_target):
    on_tick → reads target's genome from fabric (dna.{target}.*), scores it
    against a labeled scenario, generates λ candidates, writes the best
    back IF it strictly improves. This is the in-fabric Borg dynamic — the
    swarm rewrites its own DNA in shared memory while running.

Hard contracts inherited from the OS:
  - Gate decisions NEVER reach an LLM (VAJ Law 2).
  - State values ≤20 bytes; the genome is chunked across dna.{id}.0..N
    slots so a multi-rule genome (~60+ bytes) fits without losing
    codepoint integrity.
  - The genome interpreter is robust to garbage (mutated DNA can never
    crash the agent — worst case is a wrong verdict for one tick).
  - Persist failures (sink role) leave a VERB_ERROR trail in the fabric
    event log, never silently swallowed.
"""

import json
import os
import random
import time

from swarm import dna_storage
from swarm.dna import Agent
from swarm.fabric import VERB_ERROR
from swarm.fitness import load_scenario, score
from swarm.genome import interpret
from swarm.probes.kernel import sample_all, CAPS


MSG_SYS_ALERT = 700
_FAST_CADENCE_PSI_SOME = 5.0


class DeclarativeAgent(Agent):

    def __init__(self, agent_id, agent_type, priority,
                 # PROBE
                 genome='', narrator_id=None,
                 calm_interval=10, alert_interval=1,
                 # SINK
                 consume_types=None, persist_path=None,
                 # MUTATOR
                 mutate_target=None, mutation_interval=30,
                 mutation_lambda=4, fitness_scenario=None,
                 mutation_seed=0,
                 # MULTISWARM
                 state_prefix=''):
        super().__init__(agent_id, agent_type, priority,
                         state_prefix=state_prefix)
        # probe
        self.genome = genome or ''
        self.narrator_id = int(narrator_id) if narrator_id is not None else None
        self.calm_interval = float(calm_interval)
        self.alert_interval = float(alert_interval)
        # sink
        self.consume_types = set(int(t) for t in (consume_types or []))
        self.persist_path = persist_path
        # mutator
        self.mutate_target = int(mutate_target) if mutate_target is not None else None
        self.mutation_interval = float(mutation_interval)
        self.mutation_lambda = int(mutation_lambda)
        self._scenario = load_scenario(fitness_scenario) if fitness_scenario else None
        self._rng = random.Random(int(mutation_seed))

        self._last_sev = None
        self._last_code = None
        self._next_due = 0.0
        self._announced = False
        self._consumed = 0
        self._mut_cycles = 0

    # ── role dispatch ──────────────────────────────────────────────────────
    def on_tick(self):
        if self.mutate_target is not None:
            return self._tick_mutator()
        # PROBE or pure SINK
        return self._tick_probe()

    # ── PROBE role ─────────────────────────────────────────────────────────
    def _tick_probe(self):
        # Read live genome from fabric. On the very first tick where the
        # chain is empty, seed it from the constructor genome — this is how
        # the YAML-declared starting DNA enters the fabric.
        live = dna_storage.read(self.fabric, self.id)
        if not live and self.genome:
            dna_storage.write(self.fabric, self.id, self.genome)
            live = self.genome
        if not live:
            return False    # pure sink, no probing work

        now = time.time()
        if now < self._next_due:
            return (self._next_due - now) < self.calm_interval

        frame = sample_all()

        if not self._announced:
            mode = 'psi' if CAPS.psi_memory else 'fallback_level'
            self.write_state('sys.mode', mode)
            self.write_state('sys.psi.on', '1' if CAPS.psi_memory else '0')
            self.log('agent.boot', mode)
            self._announced = True

        # compact dumb numbers
        self.write_state('sys.ts',          str(int(frame.ts)))
        self.write_state('sys.mem.availmb', str(int(frame.mem.available_kb / 1024)))
        self.write_state('sys.mem.usedpct', f'{frame.mem.used_pct:.1f}')
        self.write_state('sys.swap.mb',     str(int(frame.mem.swap_total_mb)))
        self.write_state('sys.psi.some10',  f'{frame.psi_mem.some.avg10:.2f}')
        self.write_state('sys.psi.full10', f'{frame.psi_mem.full.avg10:.2f}')

        sev, code = interpret(live, frame)
        self.write_state('sys.sev',  sev[:20])
        self.write_state('sys.code', code[:20])

        rising = (sev != 'OK'
                  or (frame.psi_mem.available
                      and frame.psi_mem.some.avg10 >= _FAST_CADENCE_PSI_SOME))
        self._next_due = now + (self.alert_interval if rising else self.calm_interval)

        if (sev, code) != (self._last_sev, self._last_code):
            if self.narrator_id is not None:
                payload = f'{sev}:{code}'[:48]
                self.send_msg(self.narrator_id, MSG_SYS_ALERT, payload)
            self.log('edge', f'{sev}:{code}'[:20])
            self._last_sev, self._last_code = sev, code

        return rising

    # ── MUTATOR role ──────────────────────────────────────────────────────
    def _tick_mutator(self):
        if self._scenario is None:
            return False
        now = time.time()
        if now < self._next_due:
            return False
        self._next_due = now + self.mutation_interval

        current = dna_storage.read(self.fabric, self.mutate_target)
        if not current:
            return False    # target hasn't seeded its DNA yet

        # local import to avoid the circular swarm.evolve -> swarm.fitness
        # -> swarm.agents.declarative path during module load.
        from swarm.evolve import mutate as mutate_genome

        cur_score = score(current, self._scenario)
        best_score, best_genome = cur_score, current
        for _ in range(self.mutation_lambda):
            cand = mutate_genome(current, self._rng)
            cs = score(cand, self._scenario)
            improved = (cs['score'] > best_score['score']
                        or (cs['score'] == best_score['score']
                            and len(cand) < len(best_genome)))
            if improved:
                best_score, best_genome = cs, cand

        self._mut_cycles += 1
        self.write_state('mut.cycles', str(self._mut_cycles))
        self.write_state('mut.best', f'{best_score["score"]:.0f}'[:20])

        if best_genome != current:
            dna_storage.write(self.fabric, self.mutate_target,
                              best_genome, writer=self.id)
            delta = best_score['score'] - cur_score['score']
            self.log('mut.adopt', f'+{delta:.0f}'[:20])

        return True

    # ── SINK role ─────────────────────────────────────────────────────────
    def on_message(self, msg):
        mtype = msg.get('type')
        if mtype not in self.consume_types:
            return

        self._consumed += 1
        payload = (msg.get('payload') or '')[:48]

        self.log('sink.recv', payload[:20])
        self.write_state('sink.last', payload[:20])
        self.write_state('sink.n', str(self._consumed))

        if self.persist_path:
            rec = {
                'ts': time.time(),
                'from': msg.get('sender'),
                'type': mtype,
                'payload': payload,
            }
            try:
                d = os.path.dirname(self.persist_path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.persist_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec) + '\n')
            except OSError as e:
                self.fabric.log_append(self.id, VERB_ERROR,
                                       'sink.persist', str(e)[:20])
