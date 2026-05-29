"""
Agent DNA — The atomic building block every agent shares.

Every agent in the swarm inherits this exact same structure.
Same lifecycle. Same interface. Same capabilities.
Only the behavior (on_tick, on_message, on_signal) differs.

Lifecycle:  spawn → attach → run(tick/signal/message loop) → die
"""

import os
import time

from swarm.fabric import (
    Fabric,
    S_READY, S_RUNNING, S_BLOCKED, S_ZOMBIE,
    SIG_KILL, SIG_STOP, SIG_MSG,
    VERB_SPAWN, VERB_EXIT, VERB_STATE, VERB_MSG, VERB_ERROR,
    ACB_ID, ACB_TYPE, ACB_PRIORITY, ACB_STATE,
    ACB_HEARTBEAT, ACB_PID, ACB_WATCHDOG, ACB_RUNTIME,
)


class Agent:
    """
    The DNA.  Every agent is this.

    Subclasses override three methods:
        on_tick()    → do one unit of work, return True if more pending
        on_message() → handle an inbox message
        on_signal()  → handle non-system signals
    """

    def __init__(self, agent_id, agent_type, priority, state_prefix=''):
        self.id           = agent_id
        self.type         = agent_type
        self.priority     = priority
        self.state_prefix = state_prefix
        self.fabric       = None
        self.alive        = False

    # ── lifecycle ─────────────────────────────────────

    def attach(self, fabric):
        """Connect to the fabric (called inside the agent's own process)."""
        self.fabric = fabric
        self._register()
        self.alive = True

    def _register(self):
        f = self.fabric
        f.acb_w(self.id, ACB_ID,        self.id,       '<H')
        f.acb_w(self.id, ACB_TYPE,       self.type)
        f.acb_w(self.id, ACB_PRIORITY,   self.priority)
        f.acb_w(self.id, ACB_STATE,      S_READY)
        f.acb_w(self.id, ACB_PID,        os.getpid(),   '<I')
        f.acb_w(self.id, ACB_WATCHDOG,   30,            '<I')
        f.acb_w(self.id, ACB_HEARTBEAT,  int(time.time()), '<Q')
        f.log_append(self.id, VERB_SPAWN,
                     f'agent.{self.id}', f'type={self.type}')
        f.state_set(f'a.{self.id}.state', 'alive', self.id)

    def run(self):
        """Main loop — this IS the agent's life."""
        f  = self.fabric
        ev = f'Swarm_{self.id}'

        while self.alive:
            # ── heartbeat ────────────────────────────
            f.acb_w(self.id, ACB_HEARTBEAT, int(time.time()), '<Q')

            # ── signals ──────────────────────────────
            sigs = f.sig_recv(self.id)
            if sigs:
                if sigs & (1 << SIG_KILL):
                    self._die('killed')
                    return
                if sigs & (1 << SIG_STOP):
                    f.acb_w(self.id, ACB_STATE, S_BLOCKED)
                    f.evt_wait(ev, timeout_ms=5000)
                    f.acb_w(self.id, ACB_STATE, S_READY)
                    continue
                # mask system signals before passing to user handler
                user_sigs = sigs & ~((1 << SIG_KILL) | (1 << SIG_STOP) | (1 << SIG_MSG))
                if user_sigs:
                    self.on_signal(user_sigs)

            # ── drain inbox ──────────────────────────
            for _ in range(64):                  # cap per cycle
                msg = f.inbox_recv(self.id)
                if msg is None:
                    break
                try:
                    self.on_message(msg)
                except Exception as e:
                    f.log_append(self.id, VERB_ERROR,
                                 f'a.{self.id}.msg', str(e)[:20])

            # ── work ─────────────────────────────────
            f.acb_w(self.id, ACB_STATE, S_RUNNING)
            t0 = time.perf_counter()
            has_work = False
            try:
                has_work = self.on_tick()
            except Exception as e:
                f.log_append(self.id, VERB_ERROR,
                             f'a.{self.id}.tick', str(e)[:20])
            elapsed = int((time.perf_counter() - t0) * 1_000_000)

            # update cumulative runtime
            rt = f.acb_r(self.id, ACB_RUNTIME, '<Q')
            f.acb_w(self.id, ACB_RUNTIME, rt + elapsed, '<Q')

            if has_work:
                f.acb_w(self.id, ACB_STATE, S_READY)
                time.sleep(0.01)               # brief yield
            else:
                f.acb_w(self.id, ACB_STATE, S_BLOCKED)
                f.evt_wait(ev, timeout_ms=3000) # sleep until woken
                f.acb_w(self.id, ACB_STATE, S_READY)

    def _die(self, reason='exit'):
        self.alive = False
        f = self.fabric
        f.acb_w(self.id, ACB_STATE, S_ZOMBIE)
        f.state_set(f'a.{self.id}.state', 'dead', self.id)
        f.log_append(self.id, VERB_EXIT, f'agent.{self.id}', reason)

    # ── OVERRIDE THESE (the "gene expression") ───────

    def on_tick(self) -> bool:
        """One unit of work.  Return True if more work pending."""
        return False

    def on_signal(self, signals: int):
        """Handle non-system signals."""
        pass

    def on_message(self, msg: dict):
        """Handle an inbox message."""
        pass

    # ── STANDARD CAPABILITIES (every agent gets these) ─

    def read_state(self, key):
        return self.fabric.state_get(self.state_prefix + key)

    def write_state(self, key, value):
        return self.fabric.state_set(self.state_prefix + key, value, self.id)

    def read_state_raw(self, key):
        return self.fabric.state_get(key)

    def send_msg(self, to_id, msg_type, payload=''):
        ok = self.fabric.inbox_send(self.id, to_id, msg_type, payload)
        if ok:
            self.fabric.log_append(
                self.id, VERB_MSG,
                f'{self.id}>{to_id}', payload[:20])
        return ok

    def emit_signal(self, target_id, bit):
        self.fabric.sig_send(target_id, bit)

    def log(self, key, value=''):
        self.fabric.log_append(self.id, VERB_STATE, key, value)


# ══════════════════════════════════════════════════════
#  PROCESS ENTRY POINT (called by multiprocessing)
# ══════════════════════════════════════════════════════

def agent_entry(cls_name, agent_id, agent_type, priority,
                fabric_path, config):
    """Top-level function that runs inside the child process."""
    from swarm.agents import create_agent

    fabric = Fabric(fabric_path)
    agent  = create_agent(cls_name, agent_id, agent_type,
                          priority, config)
    agent.attach(fabric)

    try:
        agent.run()
    except KeyboardInterrupt:
        agent._die('interrupted')
    except Exception as e:
        fabric.log_append(agent_id, VERB_ERROR,
                          f'agent.{agent_id}', str(e)[:20])
        agent._die('crashed')
    finally:
        fabric.close()
