"""
Kernel — The swarm orchestrator.

Spawns agents as separate OS processes, monitors health,
manages lifecycle, advances the global tick.
"""

import os
import time
import multiprocessing

from swarm.fabric import (
    Fabric, OFF_SUPER,
    S_FREE, S_ZOMBIE, SB_TICK,
    ACB_STATE, ACB_HEARTBEAT, ACB_WATCHDOG, ACB_PID,
    SIG_KILL, VERB_EXIT, MAX_AGENTS,
)
from swarm.dna import agent_entry


class Orchestrator:
    """The kernel.  One per swarm."""

    def __init__(self, fabric_path=None):
        self.fabric     = Fabric(path=fabric_path, create=True)
        self._procs     = {}   # agent_id → Process
        self._stopping  = False

    # ── agent management ──────────────────────────────

    def spawn(self, cls_name, agent_id, agent_type=0,
              priority=2, **config):
        p = multiprocessing.Process(
            target=agent_entry,
            args=(cls_name, agent_id, agent_type, priority,
                  self.fabric.path, config),
            daemon=True,
        )
        p.start()
        self._procs[agent_id] = p
        print(f'  [kernel] spawned {cls_name} '
              f'(id={agent_id} pid={p.pid})')
        return agent_id

    def kill(self, agent_id):
        self.fabric.sig_send(agent_id, SIG_KILL)

    # ── health ────────────────────────────────────────

    def _health(self):
        now = int(time.time())
        for aid in list(self._procs):
            p = self._procs[aid]
            if not p.is_alive():
                st = self.fabric.acb_r(aid, ACB_STATE)
                if st not in (S_ZOMBIE, S_FREE):
                    self.fabric.acb_w(aid, ACB_STATE, S_ZOMBIE)
                    self.fabric.state_set(f'a.{aid}.state', 'crashed', 0)
                    self.fabric.log_append(0, VERB_EXIT,
                                           f'agent.{aid}', 'crashed')
                    print(f'  [kernel] agent {aid} crashed')
                del self._procs[aid]
                continue
            hb = self.fabric.acb_r(aid, ACB_HEARTBEAT, '<Q')
            wd = self.fabric.acb_r(aid, ACB_WATCHDOG,  '<I')
            if wd and hb and (now - hb) > wd:
                print(f'  [kernel] agent {aid} heartbeat timeout')
                self.kill(aid)

    # ── shutdown ──────────────────────────────────────

    def _shutdown(self):
        self._stopping = True
        print('\n  [kernel] shutting down...')
        for aid in list(self._procs):
            self.fabric.sig_send(aid, SIG_KILL)
        deadline = time.time() + 5
        while self._procs and time.time() < deadline:
            for aid in list(self._procs):
                if not self._procs[aid].is_alive():
                    del self._procs[aid]
            time.sleep(0.1)
        for aid, p in list(self._procs.items()):
            if p.is_alive():
                p.terminate()
                print(f'  [kernel] force-killed agent {aid}')
        self.fabric.close()
        print('  [kernel] offline')

    # ── main loop ─────────────────────────────────────

    def run(self):
        print('  [kernel] online\n')
        tick = 0
        try:
            while not self._stopping:
                if tick % 50 == 0:
                    self._health()
                tick += 1
                self.fabric.w64(OFF_SUPER + SB_TICK, tick)
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
