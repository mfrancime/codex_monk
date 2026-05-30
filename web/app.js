/* codex_monk war-room — vanilla JS client.
 *
 * Polls /api/swarms every POLL_MS, renders:
 *   - DEFCON banner (driven by aggregated severity)
 *   - control column (configs, running procs, probes)
 *   - SVG topology (one circle per fabric, agents arranged around it,
 *                   pulses on observed message-log entries)
 *   - agent grid (sortable, click to open drawer)
 *   - fabric log tail + alert tail
 *
 * No frameworks. State is two globals + a few DOM updates.
 */

const POLL_MS = 1500;
const LOG_POLL_MS = 1500;
const ALERT_POLL_MS = 2500;
const PULSE_BUDGET = 6;   // max simultaneous pulses on the topology

const $ = (id) => document.getElementById(id);
const SEV_TO_DEFCON = { OK: 5, INFO: 4, WARN: 3, CRITICAL: 1 };
const SEV_TO_CLASS = { OK: 'ok', INFO: 'ok', WARN: 'warn', CRITICAL: 'crit' };

let STATE = {
  swarms: [],
  links: [],
  procs: {},
  configs: [],
  probes: [],
  defcon: 5,
  lastLogSeq: {},   // fabric path → highest seq seen (drives pulse animation)
  selected: null,   // {path, id}
};

// ── clock ────────────────────────────────────────────────────────────────

function tickClock() {
  const d = new Date();
  $('clock').textContent =
    d.getHours().toString().padStart(2, '0') + ':' +
    d.getMinutes().toString().padStart(2, '0') + ':' +
    d.getSeconds().toString().padStart(2, '0');
}
setInterval(tickClock, 1000);
tickClock();

// ── fetching ─────────────────────────────────────────────────────────────

async function jget(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(r.statusText);
  return await r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  return await r.json();
}

// ── DEFCON state on <body> ───────────────────────────────────────────────

function setDefcon(level, severity) {
  document.body.dataset.defcon = String(level);
  $('defcon-lvl').textContent = level;
  $('defcon-state').textContent = ({
    1: 'CRITICAL', 2: 'ELEVATED', 3: 'WARN', 4: 'INFO', 5: 'NOMINAL',
  })[level] || 'NOMINAL';
}

// ── connection dot ───────────────────────────────────────────────────────

function setConn(ok, msg) {
  $('conn-dot').classList.toggle('live', !!ok);
  $('conn-text').textContent = msg || (ok ? 'LIVE' : 'OFFLINE');
}

// ── control column ───────────────────────────────────────────────────────

function renderConfigs() {
  const ul = $('config-list');
  if (!STATE.configs.length) {
    ul.innerHTML = '<li class="empty">no configs</li>';
    return;
  }
  ul.innerHTML = '';
  STATE.configs.forEach((name) => {
    const running = STATE.procs[name] && STATE.procs[name].state === 'running';
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="name">${name}</span>
      ${running
        ? `<button class="btn stop" data-stop="${name}">STOP</button>`
        : `<button class="btn go" data-start="${name}">RUN</button>`}
    `;
    ul.appendChild(li);
  });
}

function renderProcs() {
  const ul = $('proc-list');
  const names = Object.keys(STATE.procs);
  if (!names.length) {
    ul.innerHTML = '<li class="empty">none</li>';
    return;
  }
  ul.innerHTML = '';
  names.forEach((n) => {
    const p = STATE.procs[n];
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="name">${n}</span>
      <span class="pid">${p.state.toUpperCase()} · pid ${p.pid}</span>
    `;
    ul.appendChild(li);
  });
}

function renderProbes() {
  const ul = $('probe-list');
  if (!STATE.probes.length) { ul.innerHTML = ''; return; }
  ul.innerHTML = STATE.probes.map(
    (n) => `<li><span class="pname">${n}</span></li>`
  ).join('');
}

document.body.addEventListener('click', async (e) => {
  const startBtn = e.target.closest('[data-start]');
  const stopBtn = e.target.closest('[data-stop]');
  if (startBtn) {
    const name = startBtn.dataset.start;
    await jpost('/api/start', { config: name });
    setTimeout(refresh, 200);
  } else if (stopBtn) {
    const name = stopBtn.dataset.stop;
    await jpost('/api/stop', { config: name });
    setTimeout(refresh, 200);
  }
});

// ── topology (SVG) ───────────────────────────────────────────────────────

const SVG_NS = 'http://www.w3.org/2000/svg';

function el(tag, attrs = {}, children = []) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  for (const c of children) n.appendChild(c);
  return n;
}

function renderTopology() {
  const svg = $('topo-svg');
  svg.innerHTML = '';
  const W = 1000, H = 600;

  const swarms = STATE.swarms;
  if (!swarms.length) {
    svg.appendChild(el('text', {
      x: W / 2, y: H / 2, class: 'topo-node-label',
    }, [document.createTextNode('NO FABRICS ONLINE')]));
    return;
  }

  // Layout: arrange swarm hubs along the horizontal midline.
  const posByPath = {};
  const positions = swarms.map((s, i) => {
    const slots = swarms.length;
    const x = (W / (slots + 1)) * (i + 1);
    const y = H / 2;
    posByPath[s.path] = { x, y };
    return { swarm: s, x, y };
  });

  // Real VJR topology: one line per gateway peer link (from /api/links,
  // derived from each gateway's configured peers). Active links (both ends
  // online) render solid; configured-but-down links render dimmed.
  STATE.links.forEach((lk) => {
    const a = posByPath[lk.from], b = posByPath[lk.to];
    if (!a || !b) return;
    const line = el('line', {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: 'topo-vjr-link' + (lk.online ? ' online' : ' down'),
    });
    svg.appendChild(line);
    // peer label at the midpoint
    if (lk.peer) {
      svg.appendChild(el('text', {
        x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 6,
        class: 'topo-link-label',
      }, [document.createTextNode('VJR')]));
    }
  });

  positions.forEach(({ swarm, x, y }) => {
    drawFabric(svg, swarm, x, y);
  });

  $('topo-meta').textContent =
    `${swarms.length} fabric${swarms.length === 1 ? '' : 's'} · ` +
    `${swarms.filter((s) => s.online).length} online`;
}

function drawFabric(svg, swarm, cx, cy) {
  const sevClass = SEV_TO_CLASS[swarm.severity] || 'ok';
  const hubR = 60;
  // Hub
  svg.appendChild(el('circle', {
    cx, cy, r: hubR,
    class: 'topo-node-fabric ' + (swarm.online ? 'online' : '') +
           ' ' + (sevClass === 'ok' ? '' : sevClass),
  }));

  // Crosshair grid lines inside hub
  svg.appendChild(el('line', {
    x1: cx - hubR, y1: cy, x2: cx + hubR, y2: cy,
    stroke: '#1a4022', 'stroke-width': 1,
  }));
  svg.appendChild(el('line', {
    x1: cx, y1: cy - hubR, x2: cx, y2: cy + hubR,
    stroke: '#1a4022', 'stroke-width': 1,
  }));

  // Hub label
  const short = swarm.path.split('/').pop()
    .replace('codex.', '').replace('.fabric', '');
  svg.appendChild(el('text', {
    x: cx, y: cy - 5, class: 'topo-node-label',
  }, [document.createTextNode(short.toUpperCase())]));
  svg.appendChild(el('text', {
    x: cx, y: cy + 10, class: 'topo-node-meta',
  }, [document.createTextNode(swarm.severity)]));
  svg.appendChild(el('text', {
    x: cx, y: cy + 22, class: 'topo-node-meta',
  }, [document.createTextNode(
    swarm.agents.length + ' agents')]));

  // Agents arranged around the hub
  const agentR = 110;
  swarm.agents.forEach((a, i) => {
    const angle = (i / swarm.agents.length) * 2 * Math.PI - Math.PI / 2;
    const ax = cx + agentR * Math.cos(angle);
    const ay = cy + agentR * Math.sin(angle);
    const isGateway = a.role === 'gateway';
    const sev = a.sev;   // this agent's OWN severity, by writer attribution
    const cls = ['topo-agent'];
    if (isGateway) cls.push('gateway');
    if (a.state === 'zombie') cls.push('crit');
    if (sev === 'CRITICAL') cls.push('crit');
    else if (sev === 'WARN') cls.push('warn');
    // Link from hub to agent
    svg.appendChild(el('line', {
      x1: cx, y1: cy, x2: ax, y2: ay, class: 'topo-link',
    }));
    svg.appendChild(el('circle', {
      cx: ax, cy: ay, r: 14, class: cls.join(' '),
      'data-path': swarm.path, 'data-id': a.id,
    }));
    svg.appendChild(el('text', {
      x: ax, y: ay + 3, class: 'topo-agent-label',
    }, [document.createTextNode('#' + a.id)]));
  });
}

// click an agent dot to open drawer
$('topo-svg').addEventListener('click', (e) => {
  const c = e.target.closest('[data-path]');
  if (!c) return;
  openDrawer(c.dataset.path, parseInt(c.dataset.id, 10));
});

// ── agent grid table ─────────────────────────────────────────────────────

function renderAgentGrid() {
  const tbody = $('agent-tbody');
  tbody.innerHTML = '';
  let total = 0;
  STATE.swarms.forEach((s) => {
    if (!s.online) return;
    const short = s.swarm_name || s.path.split('/').pop()
      .replace('codex.', '').replace('.fabric', '');
    s.agents.forEach((a) => {
      total++;
      const tr = document.createElement('tr');
      tr.dataset.path = s.path;
      tr.dataset.id = a.id;
      const agentSev = a.sev || '—';
      const agentCode = a.code || '—';
      tr.innerHTML = `
        <td>${short}</td>
        <td>#${a.id}</td>
        <td class="cell-role">${a.role || '—'}</td>
        <td class="cell-state-${a.state}">${a.state}</td>
        <td>${a.heartbeat_age_s ?? '—'}s</td>
        <td class="cell-sev-${agentSev}">${agentSev}</td>
        <td>${agentCode}</td>
      `;
      tr.addEventListener('click', () => openDrawer(s.path, a.id));
      tbody.appendChild(tr);
    });
  });
  $('agent-meta').textContent = total;
}

// ── fabric log tail ──────────────────────────────────────────────────────

async function refreshLog() {
  const sel = $('log-fabric');
  if (!sel.value && STATE.swarms.length) {
    // populate the dropdown
    sel.innerHTML = STATE.swarms.map(
      (s) => `<option value="${s.path}">${s.path.split('/').pop()}</option>`
    ).join('');
  }
  const fabric = sel.value;
  if (!fabric) return;
  let data;
  try { data = await jget(`/api/log?path=${encodeURIComponent(fabric)}&n=60`); }
  catch (e) { return; }
  const wrap = $('log-tbody');
  wrap.innerHTML = '';
  data.entries.forEach((row) => {
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = `
      <span class="seq">#${row.seq}</span>
      <span class="verb ${row.verb_name}">${row.verb_name}</span>
      <span class="key">${row.key || ''}</span>
      <span class="val">${row.value || ''}</span>
    `;
    wrap.appendChild(div);
  });
  // ── pulse topology for new entries since last poll ───────────────────
  const lastSeen = STATE.lastLogSeq[fabric] || 0;
  const newOnes = data.entries.filter((r) => r.seq > lastSeen);
  if (data.entries.length) {
    STATE.lastLogSeq[fabric] = data.entries[data.entries.length - 1].seq;
  }
  schedulePulses(fabric, newOnes);
}

$('log-fabric').addEventListener('change', refreshLog);

// ── alert timeline ───────────────────────────────────────────────────────

async function refreshAlerts() {
  let data;
  try { data = await jget('/api/alerts?n=30'); } catch (e) { return; }
  const wrap = $('alert-tbody');
  wrap.innerHTML = '';
  data.entries.forEach((row) => {
    const ts = new Date((row.ts || 0) * 1000).toLocaleTimeString();
    const div = document.createElement('div');
    div.className = 'alert-entry';
    div.innerHTML = `
      <span class="ts">${ts}</span>
      <span class="type">t=${row.type ?? '?'}</span>
      <span class="src">${row._source || ''}</span>
      <span class="payload">${row.payload || JSON.stringify(row)}</span>
    `;
    wrap.appendChild(div);
  });
  $('alert-meta').textContent = data.entries.length;
}

// ── pulse animation on new log entries ───────────────────────────────────

let _activePulses = 0;
function schedulePulses(fabricPath, entries) {
  // find the SVG center for this fabric
  const swarmIdx = STATE.swarms.findIndex((s) => s.path === fabricPath);
  if (swarmIdx < 0) return;
  const svg = $('topo-svg');
  const W = 1000, H = 600;
  const slots = STATE.swarms.length;
  const cx = (W / (slots + 1)) * (swarmIdx + 1);
  const cy = H / 2;
  const agentR = 110;
  const agents = STATE.swarms[swarmIdx].agents;

  for (const row of entries) {
    if (_activePulses >= PULSE_BUDGET) break;
    const verb = row.verb_name;
    let cls = 'topo-pulse';
    if (verb === 'ERROR') cls += ' crit';
    else if (verb === 'MSG' || verb === 'SIG') cls += ' warn';
    // pulse direction: from a random agent to the hub center
    const i = Math.floor(Math.random() * Math.max(1, agents.length));
    const angle = (i / Math.max(1, agents.length)) * 2 * Math.PI - Math.PI / 2;
    const ax = cx + agentR * Math.cos(angle);
    const ay = cy + agentR * Math.sin(angle);
    const dot = el('circle', { cx: ax, cy: ay, r: 4, class: cls });
    svg.appendChild(dot);
    _activePulses++;
    const anim = el('animate', {
      attributeName: 'cx', from: ax, to: cx, dur: '0.8s',
      fill: 'freeze', begin: '0s',
    });
    const anim2 = el('animate', {
      attributeName: 'cy', from: ay, to: cy, dur: '0.8s',
      fill: 'freeze', begin: '0s',
    });
    const anim3 = el('animate', {
      attributeName: 'opacity', from: 1, to: 0, dur: '0.8s',
      fill: 'freeze', begin: '0s',
    });
    dot.appendChild(anim);
    dot.appendChild(anim2);
    dot.appendChild(anim3);
    setTimeout(() => { dot.remove(); _activePulses--; }, 900);
  }
}

// ── drawer (agent detail / propose / frame inspect) ──────────────────────

async function openDrawer(path, id) {
  STATE.selected = { path, id };
  $('drawer').setAttribute('aria-hidden', 'false');
  $('drawer-title').textContent =
    `AGENT #${id} · ${path.split('/').pop()}`;
  $('dr-fabric').textContent = path;
  $('dr-genome').textContent = 'loading…';
  $('dr-frame').textContent = 'loading…';
  $('dr-result').textContent = '';
  $('dr-result').className = 'dr-result';

  $('dr-role').textContent = '…';
  $('dr-sev').textContent = '—';
  $('dr-vars').innerHTML = '';
  try {
    const data = await jget(
      `/api/agent?path=${encodeURIComponent(path)}&id=${id}`);
    if (data.agent) {
      const role = data.role || data.agent.role || '—';
      $('dr-role').textContent =
        role + (data.probe ? ` · probe:${data.probe}` : '');
      $('dr-state').textContent = data.agent.state;
      const sev = data.agent.sev;
      $('dr-sev').textContent = sev
        ? sev + (data.agent.code ? ` (${data.agent.code})` : '') : '—';
      $('dr-sev').className = sev ? 'cell-sev-' + sev : '';
      $('dr-pid').textContent   = data.agent.pid || '—';
      $('dr-hb').textContent    =
        (data.agent.heartbeat_age_s ?? '—') + 's';
    }
    // per-agent live state vars (everything this agent wrote, by writer id)
    const vars = data.vars || {};
    const keys = Object.keys(vars).filter((k) => !k.startsWith('dna.')
                                              && !k.startsWith('a.')).sort();
    $('dr-vars').innerHTML = keys.length
      ? keys.map((k) =>
          `<tr><td class="vk">${k}</td><td class="vv">${vars[k]}</td></tr>`
        ).join('')
      : '<tr><td class="vk empty">no state written yet</td><td></td></tr>';
    $('dr-genome').textContent = data.genome || '(empty)';
  } catch (e) {
    $('dr-genome').textContent = 'error: ' + e;
  }

  // best-effort frame: try each probe; we don't know which one this
  // agent uses, so show all
  const frameOut = [];
  for (const probe of STATE.probes) {
    try {
      const f = await jget(
        `/api/frame?probe=${encodeURIComponent(probe)}`);
      frameOut.push(`── ${probe} (${f.describe || ''}) ──\n` +
                     JSON.stringify(f.frame || {}, null, 2));
    } catch (e) { /* shrug */ }
  }
  $('dr-frame').textContent = frameOut.join('\n\n') || '(no probes loaded)';
}

$('drawer-close').addEventListener('click', () => {
  $('drawer').setAttribute('aria-hidden', 'true');
  STATE.selected = null;
});

$('dr-propose-btn').addEventListener('click', async () => {
  if (!STATE.selected) return;
  const g = $('dr-propose').value.trim();
  if (!g) { $('dr-result').textContent = 'genome empty'; return; }
  const r = await jpost('/api/propose', {
    path: STATE.selected.path,
    id: STATE.selected.id,
    genome: g,
  });
  if (r.ok) {
    $('dr-result').textContent = `OK: wrote ${r.wrote.length} chars`;
    $('dr-result').className = 'dr-result ok';
    setTimeout(() => openDrawer(STATE.selected.path, STATE.selected.id), 500);
  } else {
    $('dr-result').textContent = 'ERR: ' + (r.error || 'unknown');
    $('dr-result').className = 'dr-result err';
  }
});

// ── main poll loop ───────────────────────────────────────────────────────

async function refresh() {
  try {
    const data = await jget('/api/swarms');
    STATE.swarms = data.swarms || [];
    STATE.links = data.links || [];
    STATE.procs = data.procs || {};
    STATE.configs = data.configs || [];
    STATE.probes = data.probes || [];
    STATE.defcon = data.defcon || 5;

    setConn(true, 'LIVE');
    setDefcon(STATE.defcon);
    renderConfigs();
    renderProcs();
    renderProbes();
    renderTopology();
    renderAgentGrid();
  } catch (e) {
    setConn(false, 'OFFLINE');
  }
}

refresh();
setInterval(refresh, POLL_MS);
setInterval(refreshLog, LOG_POLL_MS);
setInterval(refreshAlerts, ALERT_POLL_MS);
refreshAlerts();
