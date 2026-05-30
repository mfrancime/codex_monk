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
    window.War3D.update(STATE);
    // while the TIME MACHINE is armed, it owns hub colors + DEFCON tint;
    // don't let the live poll repaint over the replayed past.
    if (!(window.TM && window.TM.armed)) window.War3D.setDefcon(STATE.defcon);
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

// Clicking a HUB (the big swarm sphere) calls this with id=null → show a
// swarm-level summary built from STATE (no extra API call), reusing the
// drawer's existing fields. Clicking an agent row/mesh keeps the id.
function openSwarm(path) {
  const s = STATE.swarms.find((x) => x.path === path);
  if (!s) return;
  STATE.selected = { path, id: null };
  $('drawer').setAttribute('aria-hidden', 'false');
  const short = s.swarm_name || path.split('/').pop();
  $('drawer-title').textContent = `SWARM · ${short}`;
  $('dr-fabric').textContent = path;
  $('dr-role').textContent = `swarm · ${s.agents.length} agents`;
  $('dr-state').textContent = s.online ? 'online' : 'offline';
  $('dr-sev').textContent = s.severity || '—';
  $('dr-sev').className = s.severity ? 'cell-sev-' + s.severity : '';
  $('dr-pid').textContent = '—';
  $('dr-hb').textContent = '—';
  $('dr-genome').textContent = '(swarm view — click an agent below to inspect its DNA)';
  $('dr-frame').textContent = '';
  $('dr-result').textContent = '';
  // agent roster as the var table
  $('dr-vars').innerHTML = s.agents.map((a) =>
    `<tr><td class="vk">#${a.id} ${a.role || ''}</td>` +
    `<td class="vv cell-sev-${a.sev || '—'}">${a.sev || '—'}` +
    `${a.code ? ' · ' + a.code : ''}</td></tr>`).join('')
    || '<tr><td class="vk empty">no agents</td><td></td></tr>';
}

async function openDrawer(path, id) {
  if (id === null || id === undefined) return openSwarm(path);
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
    if (!(window.TM && window.TM.armed)) setDefcon(STATE.defcon);
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

// ── TIME MACHINE — scrub the swarm's recorded severity history ────────────
//
// The fabric event log already records every 'edge' (verdict change), so
// /api/timeline hands us per-swarm (ts, sev) series. Dragging the scrubber
// reconstructs each hub's color at that instant and drives the scene into
// the past; LIVE snaps back to real time.

const SEV_DEFCON = { CRITICAL: 1, WARN: 3, INFO: 4, OK: 5 };
window.TM = { armed: false, tl: null, t: 0, playing: false };

function _sevAtTime(events, T) {
  let sev = 'OK';
  for (const e of events) { if (e.ts <= T) sev = e.sev; else break; }
  return sev;
}

async function tmLoad() {
  try { window.TM.tl = await jget('/api/timeline?n=500'); }
  catch (e) { window.TM.tl = null; }
  return window.TM.tl;
}

function tmApply(T) {
  const tl = window.TM.tl;
  if (!tl || !window.War3D) return;
  const sevByPath = {};
  let worst = 5;
  tl.swarms.forEach((s) => {
    const sev = _sevAtTime(s.events, T);
    sevByPath[s.path] = sev;
    worst = Math.min(worst, SEV_DEFCON[sev] ?? 5);
  });
  window.War3D.setReplay(sevByPath);
  window.War3D.setDefcon(worst);
  setDefcon(worst);                     // body tint + banner reflect the past
  $('tm-clock').textContent = new Date(T * 1000).toLocaleTimeString();
}

function tmSliderToTime(v) {
  const tl = window.TM.tl;
  if (!tl) return 0;
  const span = Math.max(0.001, tl.t_max - tl.t_min);
  return tl.t_min + (v / 1000) * span;
}

async function tmArm(v) {
  if (!window.TM.tl) await tmLoad();
  if (!window.TM.tl || !window.TM.tl.swarms.length) {
    $('tm-clock').textContent = 'no history'; return;
  }
  window.TM.armed = true;
  $('timemachine').classList.add('armed');
  tmApply(tmSliderToTime(v));
}

function tmLive() {
  window.TM.armed = false;
  window.TM.playing = false;
  $('timemachine').classList.remove('armed');
  $('tm-scrub').value = 1000;
  $('tm-clock').textContent = 'LIVE';
  if (window.War3D) window.War3D.clearReplay();
  refresh();                            // recolor to live immediately
}

$('tm-scrub').addEventListener('input', (e) => {
  window.TM.playing = false;
  tmArm(parseInt(e.target.value, 10));
});
$('tm-live').addEventListener('click', tmLive);
$('tm-rewind').addEventListener('click', () => {
  const s = $('tm-scrub'); s.value = Math.max(0, +s.value - 40); tmArm(+s.value);
});
$('tm-fwd').addEventListener('click', () => {
  const s = $('tm-scrub'); s.value = Math.min(1000, +s.value + 40);
  if (+s.value >= 1000) tmLive(); else tmArm(+s.value);
});
$('tm-play').addEventListener('click', async () => {
  if (!window.TM.tl) await tmLoad();
  window.TM.playing = !window.TM.playing;
  if (window.TM.playing && +$('tm-scrub').value >= 1000) $('tm-scrub').value = 0;
});
setInterval(() => {
  if (!window.TM.playing) return;
  const s = $('tm-scrub');
  const nv = +s.value + 8;
  if (nv >= 1000) { tmLive(); return; }
  s.value = nv; tmArm(nv);
}, 200);

// ── SCORE — gamified uptime: climbs while all-green, streak resets on crit ─
let _score = 0, _streak = 1, _greenSecs = 0;
setInterval(() => {
  if (window.TM.armed) return;                 // don't score the past
  const d = STATE.defcon || 5;
  if (d === 5) {
    _greenSecs++;
    _streak = 1 + Math.floor(_greenSecs / 10); // every 10s green → +1 multiplier
    _score += _streak;
  } else {
    _greenSecs = 0;
    if (d === 1) _streak = 1;                   // CRITICAL breaks the streak
  }
  $('score-val').textContent = _score.toLocaleString();
  $('score-streak').textContent = '×' + _streak;
  $('score-chip').classList.toggle('hot', _streak >= 5);
}, 1000);
