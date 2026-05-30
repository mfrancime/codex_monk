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

const $ = (id) => document.getElementById(id);

let STATE = {
  swarms: [],
  links: [],
  procs: {},
  configs: [],
  probes: [],
  defcon: 5,
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

// ── topology (3D WebGL — see scene3d.js) ─────────────────────────────────
//
// The center is now a full-screen Three.js scene. app.js just feeds it the
// polled state; all rendering/animation lives in window.War3D.

let _war3dStarted = false;

function ensureWar3D() {
  if (_war3dStarted || !window.War3D) return;
  const canvas = $('war-canvas');
  if (!canvas) return;
  _war3dStarted = true;   // set first so a WebGL failure can't retry-loop
  try {
    window.War3D.init(canvas, { onAgentClick: openDrawer });
  } catch (e) {
    // No/blocked WebGL — degrade gracefully instead of hanging the UI.
    console.error('War3D init failed:', e);
    const hint = $('topo-meta');
    if (hint) hint.textContent = 'WebGL unavailable — 3D scene disabled';
    window.War3D = null;
  }
}

function renderTopology() {
  ensureWar3D();
  if (window.War3D && _war3dStarted) {
    window.War3D.setDefcon(STATE.defcon);
    window.War3D.update(STATE);
  }
  const swarms = STATE.swarms;
  const online = swarms.filter((s) => s.online).length;
  $('topo-meta').textContent = swarms.length
    ? `${swarms.length} fabric${swarms.length === 1 ? '' : 's'} · ${online} online`
    : 'no fabrics online';
}

// (agent clicks in 3D are handled by War3D's raycaster → openDrawer)

// ── agent grid table ─────────────────────────────────────────────────────

// Reconcile rows in place (keyed by path+id) rather than rebuilding the
// whole <tbody> each poll — keeps rows clickable and preserves scroll while
// the heartbeat age ticks every second.
const _gridRows = new Map();   // key -> <tr>

function renderAgentGrid() {
  const tbody = $('agent-tbody');
  let total = 0;
  const seen = new Set();
  STATE.swarms.forEach((s) => {
    if (!s.online) return;
    const short = s.swarm_name || s.path.split('/').pop()
      .replace('codex.', '').replace('.fabric', '');
    s.agents.forEach((a) => {
      total++;
      const key = s.path + '#' + a.id;
      seen.add(key);
      const sev = a.sev || '—';
      let tr = _gridRows.get(key);
      if (!tr) {
        tr = document.createElement('tr');
        tr.dataset.path = s.path;
        tr.dataset.id = a.id;
        tr.addEventListener('click', () => openDrawer(s.path, a.id));
        for (let i = 0; i < 7; i++) tr.appendChild(document.createElement('td'));
        _gridRows.set(key, tr);
        tbody.appendChild(tr);
      }
      const td = tr.children;
      td[0].textContent = short;
      td[1].textContent = '#' + a.id;
      td[2].textContent = a.role || '—'; td[2].className = 'cell-role';
      td[3].textContent = a.state;       td[3].className = 'cell-state-' + a.state;
      td[4].textContent = (a.heartbeat_age_s ?? '—') + 's';
      td[5].textContent = sev;           td[5].className = 'cell-sev-' + sev;
      td[6].textContent = a.code || '—';
    });
  });
  // drop rows for agents that disappeared
  _gridRows.forEach((tr, key) => {
    if (!seen.has(key)) { tr.remove(); _gridRows.delete(key); }
  });
  $('agent-meta').textContent = total;
}

// ── fabric log tail ──────────────────────────────────────────────────────

async function refreshLog() {
  const sel = $('log-fabric');
  if (!sel.value && STATE.swarms.length) {
    // populate the dropdown
    // short label: codex.k8s_aggregator.fabric → k8s_aggregator, so the
    // native select stays inside its panel (see .log #log-fabric in CSS).
    sel.innerHTML = STATE.swarms.map((s) => {
      const short = s.path.split('/').pop()
        .replace(/^codex\./, '').replace(/\.fabric$/, '');
      return `<option value="${s.path}">${short}</option>`;
    }).join('');
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
