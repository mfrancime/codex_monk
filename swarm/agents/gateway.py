"""
GatewayAgent — the bridge between sub-swarm fabrics.

In codex_monk's multiswarm model every sub-swarm runs its own isolated
mmap fabric. They cannot read each other's state or inboxes directly.
The gateway is the single agent in each sub-swarm responsible for ferrying
inbox messages to peers over the VJR wire protocol.

Routing model — fully declarative:

  agent emits → local fabric inbox (target = local gateway id)
              → gateway.on_message inspects msg.type
              → routes[type] → (peer_name, remote_agent_id)
              → pack VJR envelope, enqueue on peer's send queue
              → client thread sends over persistent TCP
              → peer gateway's server thread reads VJR
              → peer gateway posts to LOCAL inbox of remote_agent_id
              → remote agent picks it up via normal inbox semantics

The genome never knows the route exists. It just emits to the configured
`narrator_id`, which the YAML happens to point at the local gateway.
Re-routing a sub-swarm to a different peer is one YAML edit, no code,
no genome change.

Threading:
  - one server thread: bind, accept, per-conn reader loop
  - one client thread per peer: persistent connect + send-queue drain
  - on_message runs on the kernel's main-loop thread (the agent run loop),
    enqueues to the right peer's queue and returns. No blocking I/O on
    the kernel thread.

Failure modes are loud:
  - unroutable type        → VERB_ERROR 'gw.unroutable'
  - bad HMAC on inbound    → VERB_ERROR 'gw.badhmac'
  - JSON parse failure     → VERB_ERROR 'gw.badjson'
  - socket reset           → reconnect with 1s backoff, log 'gw.reconnect'
  - inbox full on delivery → VERB_ERROR 'gw.inboxfull', message dropped

v1 scope: localhost-quality. No TLS, no dead-letter, single send-queue
per peer with unbounded growth (the local agent is rate-limited by the
declarative cadence — backpressure is implicit). Production hardening
is intentionally deferred.
"""

import socket
import threading
import time

try:
    import queue
except ImportError:                      # pragma: no cover
    import Queue as queue                # py2 leftover

from swarm.dna import Agent
from swarm.fabric import VERB_ERROR, VERB_STATE
from swarm.protocol.vjr import Envelope, pack, drain


SOCKET_RECV = 4096
RECONNECT_BACKOFF_S = 1.0
SEND_QUEUE_GET_TIMEOUT_S = 0.5


class GatewayAgent(Agent):

    def __init__(self, agent_id, agent_type, priority,
                 swarm_name='',
                 bind='127.0.0.1:0',
                 peers=None,
                 routes=None,
                 psk='',
                 state_prefix=''):
        super().__init__(agent_id, agent_type, priority,
                         state_prefix=state_prefix)
        self.swarm_name = swarm_name
        self.bind_str = bind
        self.peers = list(peers or [])           # [{name, addr}]
        self.routes = list(routes or [])         # [{type, peer, agent}]
        self.psk = psk

        # derived: type → (peer_name, remote_agent_id)
        self._route_table = {}
        for r in self.routes:
            self._route_table[int(r['type'])] = (
                str(r['peer']), int(r['agent']))

        # derived: peer_name → 'host:port'
        self._peer_addr = {str(p['name']): str(p['addr']) for p in self.peers}

        self._send_queues = {n: queue.Queue() for n in self._peer_addr}
        self._client_threads = {}
        self._server_thread = None
        self._server_sock = None
        self._actual_bind = None         # ('host', port) after listen
        self._stop = threading.Event()
        self._started = False
        self._stats = {'sent': 0, 'recvd': 0, 'unroutable': 0,
                       'badhmac': 0, 'badjson': 0, 'inboxfull': 0}

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_tick(self):
        if not self._started:
            self._start()
            self._started = True
            self.write_state('gw.bind',
                             f'{self._actual_bind[0]}:{self._actual_bind[1]}'
                             [:20])
            self.write_state('gw.peers', str(len(self._peer_addr))[:20])
            self.log('gw.up', self.swarm_name[:20])
        # publish counters for audit
        self.write_state('gw.sent',  str(self._stats['sent'])[:20])
        self.write_state('gw.recvd', str(self._stats['recvd'])[:20])
        return False

    def on_message(self, msg):
        t = msg.get('type')
        if t not in self._route_table:
            self._stats['unroutable'] += 1
            self.fabric.log_append(self.id, VERB_ERROR,
                                   'gw.unroutable', f'type={t}'[:20])
            return
        peer_name, remote_agent = self._route_table[t]
        env = Envelope(
            src_swarm=self.swarm_name,
            dst_swarm=peer_name,
            src_agent=int(msg.get('sender', 0)),
            dst_agent=remote_agent,
            type=int(t),
            payload=str(msg.get('payload', '')),
            ts=time.time(),
        )
        try:
            self._send_queues[peer_name].put_nowait(env)
        except Exception as e:                  # pragma: no cover
            self.fabric.log_append(self.id, VERB_ERROR,
                                   'gw.enqueue', str(e)[:20])

    def on_signal(self, signals):
        # currently no user signals; reserved
        pass

    def shutdown(self):
        self._stop.set()
        try:
            if self._server_sock is not None:
                self._server_sock.close()
        except OSError:
            pass

    # ── threads ─────────────────────────────────────────────────────────────

    def _start(self):
        host, port_str = self.bind_str.split(':')
        port = int(port_str)
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((host, port))
        self._server_sock.listen(8)
        self._actual_bind = self._server_sock.getsockname()

        self._server_thread = threading.Thread(
            target=self._server_loop, name=f'gw-{self.swarm_name}-srv',
            daemon=True)
        self._server_thread.start()

        for peer_name in self._peer_addr:
            t = threading.Thread(
                target=self._client_loop, args=(peer_name,),
                name=f'gw-{self.swarm_name}-cli-{peer_name}', daemon=True)
            t.start()
            self._client_threads[peer_name] = t

    def _server_loop(self):
        srv = self._server_sock
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._conn_reader, args=(conn,),
                name=f'gw-{self.swarm_name}-rd', daemon=True)
            t.start()

    def _conn_reader(self, conn):
        buf = b''
        conn.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(SOCKET_RECV)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                envs, buf = drain(buf, self.psk)
                for env in envs:
                    if env is None:
                        # drain returns None for any frame that structurally
                        # parsed but failed validation (HMAC / JSON / version).
                        # We can't distinguish the cause here without
                        # threading state through drain; log as a single
                        # bucketed counter.
                        self._stats['badhmac'] += 1
                        self.fabric.log_append(self.id, VERB_ERROR,
                                               'gw.badframe', '')
                        continue
                    self._deliver_inbound(env)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _deliver_inbound(self, env):
        ok = self.fabric.inbox_send(
            self.id, env.dst_agent, env.type, env.payload)
        if not ok:
            self._stats['inboxfull'] += 1
            self.fabric.log_append(self.id, VERB_ERROR, 'gw.inboxfull',
                                   f'to={env.dst_agent}'[:20])
            return
        self._stats['recvd'] += 1
        self.fabric.log_append(self.id, VERB_STATE, 'gw.recvd',
                               f'{env.src_swarm}>{env.dst_agent}'[:20])

    def _client_loop(self, peer_name):
        addr_str = self._peer_addr[peer_name]
        host, port_str = addr_str.split(':')
        port = int(port_str)
        q = self._send_queues[peer_name]
        sock = None
        pending = None       # envelope held across reconnect attempts

        while not self._stop.is_set():
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect((host, port))
                    self.fabric.log_append(self.id, VERB_STATE,
                                           'gw.connect', peer_name[:20])
                except OSError:
                    sock = None
                    if self._stop.wait(RECONNECT_BACKOFF_S):
                        return
                    continue

            env = pending
            pending = None
            if env is None:
                try:
                    env = q.get(timeout=SEND_QUEUE_GET_TIMEOUT_S)
                except queue.Empty:
                    continue

            try:
                sock.sendall(pack(env, self.psk))
                self._stats['sent'] += 1
            except OSError:
                pending = env                      # retry after reconnect
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                self.fabric.log_append(self.id, VERB_ERROR,
                                       'gw.reconnect', peer_name[:20])

        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
