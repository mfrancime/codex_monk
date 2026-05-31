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
// ⚔ war console + battle-of-the-spheres run always (not just when the tab is open)
evoWarPoll();
setInterval(evoWarPoll, 1500);

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

// ── EVOLUTION / WARGAME tab — Kubernetes Red-vs-Blue co-evolution ─────────
//
// Reads /wargame.json (regenerated by wargame.py each round) and renders the
// arms race: per front, the rung ladder, the reigning Blue champion genome,
// its per-attack verdict, and the lineage of champions over rounds. Pure DOM,
// no WebGL — polls only while the panel is open (lightweight, per design).

const EVO = { open: false, timer: null, last: null };

function evoStatusClass(s) {
  return ({ HIT: 'ok', MISS: 'bad', FP: 'bad', NEAR: 'warn', HALF: 'warn' })[s] || '';
}

// colorize a genome string: emit tokens (→XY) get highlighted so the evolved
// structure is readable at a glance.
function evoGenomeHTML(g) {
  if (!g) return '<span class="evo-gene-empty">∅ (empty)</span>';
  const esc = g.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc.replace(/→(..)/g, '<span class="evo-emit">→$1</span>');
}

// the higher-dimensional champion: same arms race searched in R^(8·L)
// embedding space, decoded back to a present-time genome.
function evoVecRow(fr) {
  if (!fr.champion_vec && fr.score_vec === null) return '';
  const vf = fr.feasible_vec;
  const vs = fr.score_vec === null || fr.score_vec === undefined
    ? '—' : Number(fr.score_vec).toFixed(0);
  return `<div class="evo-champ vec">
    <span class="evo-champ-lbl" title="evolved in continuous embedding space, R^8 per token">ℝ⁸·L${fr.vec_dim ? ' =' + fr.vec_dim + 'D' : ''}</span>
    <code class="evo-gene">${evoGenomeHTML(fr.champion_vec)}</code>
    <span class="evo-badge ${vf ? 'ok' : 'bad'}">${vf ? 'FEASIBLE' : 'INFEASIBLE'}</span>
    <span class="evo-score">score ${vs}</span>
  </div>`;
}

// 🏆 LEADERBOARD — rank the Blue defenders by a battle score (win-rate, Red
// escalations survived, speed-to-master). Directly shows the teaming + standings.
function evoLeaderboard(fronts, names) {
  const rows = names.map((n) => {
    const f = fronts[n], s = f.stats || {};
    const holding = f.champion_feasible && (f.champion_score ?? -1) >= -0.001;
    const battle = Math.round((s.win_rate || 0) * 100)
      + (s.breaks_survived || 0) * 8
      - (s.first_master_round || 99)
      + (holding ? 25 : 0);
    return { n, f, s, battle, holding };
  }).sort((a, b) => b.battle - a.battle);
  const medal = ['🥇', '🥈', '🥉'];
  const liveFronts = (WAR.record && WAR.record.fronts) || {};
  const liveBadge = (name) => {
    const fb = liveFronts[name];
    if (!fb || (fb.held + fb.lost) === 0) {
      return '<span class="lb-live none" title="no live START WAR battles fought on this front yet">🗺️ —</span>';
    }
    const total = fb.held + fb.lost;
    const rate = Math.round((fb.held / total) * 100);
    return `<span class="lb-live ${fb.held >= fb.lost ? 'ok' : 'bad'}" title="live START WAR record across matches: ${fb.held} held / ${fb.lost} lost (${rate}% hold)${fb.infil ? ' · ' + fb.infil + ' stealth infiltrations suffered' : ''}">🗺️ ${fb.held}–${fb.lost}${fb.infil ? ' 🥷' + fb.infil : ''}</span>`;
  };
  const body = rows.map((r, i) => `
    <div class="lb-row ${r.holding ? 'hold' : 'breach'}">
      <span class="lb-rank">${medal[i] || '#' + (i + 1)}</span>
      <span class="lb-name">${(r.f.flavor || r.n).replace(/\s*—.*/, '')}</span>
      <code class="lb-gene">${evoGenomeHTML(r.f.champion)}</code>
      <span class="lb-stat" title="rounds won (score 0)">${Math.round((r.s.win_rate || 0) * 100)}%</span>
      <span class="lb-stat" title="Red escalations survived → re-mastered">⚔️${r.s.breaks_survived || 0}</span>
      <span class="lb-stat" title="DNA edit-distance churn — how much it evolved">🧬${r.s.dna_churn || 0}</span>
      ${liveBadge(r.n)}
      <span class="lb-badge ${r.holding ? 'ok' : 'bad'}">${r.holding ? 'HOLDING' : 'BREACHED'}</span>
    </div>`).join('');
  const blue = rows.filter((r) => r.holding).length;
  const matches = (WAR.record.red || 0) + (WAR.record.blue || 0);
  const liveNote = matches
    ? ` · 🗺️ ${matches} live battle${matches === 1 ? '' : 's'} fought`
    : ' · 🗺️ no live battles yet';
  return `<div class="evo-lb">
    <div class="evo-lb-head">🏆 LEADERBOARD · 🔵 Blue holds ${blue}/${rows.length} fronts${liveNote}</div>
    ${body}</div>`;
}

// ⚙️ LIVE FORCES — surface the architectural roles from the running fabrics so
// gateway / governor / mutator / probe / sink are identifiable (answers "where
// is the gateway/governor"), and label which fabric is which team.
function evoForces() {
  const sw = (STATE.swarms || []).filter((s) => s.online);
  if (!sw.length) return '<div class="evo-forces"><div class="evo-forces-head">⚙️ LIVE FORCES — no fabrics online</div></div>';
  const roles = { gateway: 0, governor: 0, mutator: 0, probe: 0, sink: 0 };
  const fabrics = [];
  let govVerdict = '';
  sw.forEach((s) => {
    const short = s.swarm_name || s.path.split('/').pop()
      .replace(/^codex\./, '').replace(/\.fabric$/, '');
    let purpose = 'live fabric';
    if (/k8s_deployed/.test(s.path)) purpose = '🔵 BLUE — champions as live DNA';
    else if (/evolver/.test(s.path)) purpose = '🧬 live evolver — mutates DNA';
    else if (/kernel/.test(s.path)) purpose = '📡 sensor swarm';
    else if (/aggregat/.test(s.path)) purpose = '🛡️ GOVERNOR — cluster oversight';
    (s.agents || []).forEach((a) => {
      if (a.probe === 'quorum') {
        roles.governor++;
        if (a.sev) govVerdict = a.sev + (a.code && a.code !== 'OK' ? ':' + a.code : '');
      } else if (roles[a.role] !== undefined) roles[a.role]++;
    });
    fabrics.push(`<span class="force-fab"><b>${short}</b> · ${purpose}</span>`);
  });
  const chip = (icon, label, n) => n
    ? `<span class="force-chip" title="${label}">${icon} ${label} ×${n}</span>`
    : `<span class="force-chip off" title="${label} — not running">${icon} ${label} ×0</span>`;
  const govChip = roles.governor
    ? `<span class="force-chip gov ${govVerdict && govVerdict !== 'OK' ? 'alert' : ''}" title="the cluster governor's live verdict">🛡️ governor ⟨${govVerdict || '…'}⟩</span>`
    : `<span class="force-chip off" title="governor not booted — run ./deploy_governor.sh">🛡️ governor ×0</span>`;
  return `<div class="evo-forces">
    <div class="evo-forces-head">⚙️ LIVE FORCES (running fabrics)</div>
    <div class="force-roles">
      ${chip('🚪', 'gateway', roles.gateway)}
      ${govChip}
      ${chip('🧬', 'mutator', roles.mutator)}
      ${chip('📡', 'probe', roles.probe)}
      ${chip('🗄️', 'sink', roles.sink)}
    </div>
    <div class="force-fabrics">${fabrics.join('')}</div>
  </div>`;
}

function evoRenderFront(name, fr) {
  const total = fr.rungs_total;
  const mastered = (fr.mastered_rung ?? -1) + 1;
  const cur = (fr.current_rung ?? 0) + 1;
  const pips = Array.from({ length: total }, (_, i) =>
    `<span class="evo-pip ${i < mastered ? 'done' : (i + 1 === cur ? 'cur' : '')}"></span>`
  ).join('');
  const feas = fr.champion_feasible;
  const scoreTxt = fr.champion_score === null || fr.champion_score === undefined
    ? '—' : Number(fr.champion_score).toFixed(0);
  const st = fr.stats || {};
  const stat = (label, val, title) =>
    `<span class="evo-stat" title="${title || ''}"><span class="sv">${val}</span><span class="sl">${label}</span></span>`;
  const statsHTML = st.rounds ? `<div class="evo-stats">
    ${stat('ROUNDS', st.rounds, 'rounds this team has fought')}
    ${stat('WIN%', Math.round((st.win_rate || 0) * 100), 'fraction of rounds at score 0')}
    ${stat('BROKEN', st.breaks_survived, 'times Red broke the champion (decoy/escalation) and it re-mastered')}
    ${stat('DNA Δ', st.dna_churn, 'total genome edit-distance across the lineage — how much it evolved')}
    ${stat('LEN', `${st.len_now}${st.len_trimmed > 0 ? '<small>↓' + st.len_trimmed + '</small>' : ''}`, 'current genome length (↓ = chars parsimony trimmed from peak)')}
    ${stat('→0 @r', st.first_master_round ?? '—', 'round it first reached a perfect detector')}
  </div>` : '';
  const per = (fr.per || []).map((p) => {
    const lab = p.status === 'HIT' ? `HIT ${p.latency}t` : p.status;
    return `<span class="evo-chip ${evoStatusClass(p.status)}" title="${p.scn} · ${p.phase}">${p.scn.replace(/^k8s_/, '').replace(/\.yaml$/, '')}: ${lab}</span>`;
  }).join('');
  // RED team's arsenal at the current rung (the attacks Blue must defend)
  const curRung = (fr.ladder || [])[fr.current_rung];
  const redAttacks = ((curRung && curRung.attacks) || [])
    .map((a) => a.replace(/^k8s_/, '').replace(/\.yaml$/, '').replace(/_decoy$/, ' (decoy)'))
    .join(' · ') || '—';
  const hist = (fr.history || []).slice(-10).reverse().map((r) => {
    const tag = r.mastered ? '✔ MASTERED' : `grind#${r.attempt}`;
    return `<div class="evo-lin ${r.mastered ? 'won' : ''}">
      <span class="evo-lin-rd">r${r.round}</span>
      <span class="evo-lin-rung">rung ${r.rung + 1}/${r.rungs_total}</span>
      <span class="evo-lin-tag">${tag}</span>
      <span class="evo-lin-score">${Number(r.score).toFixed(0)}</span>
      <span class="evo-lin-gene">${evoGenomeHTML(r.champion)}</span>
    </div>`;
  }).join('') || '<div class="evo-lin evo-empty">no rounds yet</div>';

  return `<div class="evo-front">
    <div class="evo-front-head">
      <span class="evo-flavor">${fr.flavor || name}</span>
      <span class="evo-domain">probe: ${fr.domain}</span>
    </div>
    <div class="evo-rungs">
      <span class="evo-rungs-lbl">RUNG ${cur}/${total} · MASTERED ${mastered}/${total}</span>
      <span class="evo-pips">${pips}</span>
    </div>
    <div class="evo-teamline red"><span class="evo-team red">🔴 RED TEAM</span>
      <span class="evo-teamdesc">attacks · rung ${cur}/${total}: ${redAttacks}</span></div>
    <div class="evo-per">${per}</div>

    <div class="evo-teamline blue"><span class="evo-team blue">🔵 BLUE TEAM</span>
      <span class="evo-teamdesc">evolved detector genome (its live DNA)</span></div>
    <div class="evo-champ">
      <span class="evo-champ-lbl">1-D</span>
      <code class="evo-gene">${evoGenomeHTML(fr.champion)}</code>
      <span class="evo-badge ${feas ? 'ok' : 'bad'}">${feas ? 'HOLDING' : 'BREACHED'}</span>
      <span class="evo-score">score ${scoreTxt}</span>
    </div>
    ${evoVecRow(fr)}
    ${statsHTML}
    <div class="evo-lineage">${hist}</div>
  </div>`;
}

async function evoRefresh() {
  let doc;
  try { doc = await jget('/wargame.json?_=' + Date.now()); }
  catch (e) {
    $('evo-body').innerHTML =
      '<div class="evo-empty">no wargame data yet — wargame.py has not produced web/wargame.json.</div>';
    return;
  }
  EVO.last = doc;
  const fronts = doc.fronts || {};
  const fnames = Object.keys(fronts);
  const holding = fnames.filter(
    (n) => fronts[n].champion_feasible && (fronts[n].champion_score ?? -1) >= -0.001).length;
  $('evo-sub').innerHTML =
    `<span class="evo-hold">🔵 Blue holding <b>${holding}/${fnames.length}</b> fronts</span> · ` +
    `${doc.rounds_total || 0} rounds · ${fnames.length} fronts · ` +
    `updated ${new Date((doc.updated || 0) * 1000).toLocaleTimeString()}`;
  const order = ['pods', 'nodes', 'apiserver', 'etcd', 'scheduler',
                 'etcd_native', 'nodes_native'];
  const names = Object.keys(fronts).sort(
    (a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  $('evo-body').innerHTML =
    evoLeaderboard(fronts, names) +
    evoForces() +
    names.map((n) => evoRenderFront(n, fronts[n])).join('')
    || '<div class="evo-empty">no fronts</div>';
}

function evoToggle(force) {
  EVO.open = force === undefined ? !EVO.open : force;
  $('evopanel').setAttribute('aria-hidden', String(!EVO.open));
  if (EVO.open) {
    evoRefresh();
    evoWarPoll();
    if (!EVO.timer) EVO.timer = setInterval(evoRefresh, 4000);
  } else {
    if (EVO.timer) { clearInterval(EVO.timer); EVO.timer = null; }
  }
}

// "RUN ROUND" — trigger one co-evolution round on the server, poll until it
// finishes, then refresh the tab. Guarded server-side against racing the
// autonomous loop (a busy response just polls until the in-flight round ends).
async function evoRunRound() {
  const btn = $('evo-run');
  if (btn.disabled) return;
  btn.disabled = true;
  let r;
  try { r = await jpost('/api/wargame', { rounds: 1 }); } catch (e) { r = { ok: false }; }
  btn.textContent = r.ok ? '▶ running…' : (r.running ? '▶ busy…' : '▶ retry');
  const poll = setInterval(async () => {
    let s;
    try { s = await jget('/api/wargame'); } catch (e) { return; }
    if (!s.running) {
      clearInterval(poll);
      btn.textContent = '✓ round done';
      evoRefresh();
      setTimeout(() => { btn.disabled = false; btn.textContent = '▶ RUN ROUND'; }, 1800);
    }
  }, 2000);
}

// ⚔ START WAR — autonomous Red-vs-Blue STRATEGY battle. Click START → the two
// armies fight on the Kubernetes battlefield with NO further input; your only
// lever is to drill into a front and deploy a custom genome to that Blue unit.
const WAR = { timer: null, last: null, selected: null };
const BF_ORDER = ['pods', 'nodes', 'apiserver', 'etcd', 'scheduler'];

// ── 🏆 SERIES RECORD — persistent best-of-N standings across matches ─────────
// Lives in localStorage so the rivalry survives reloads. A match result is
// recorded exactly once (off the same running→ended hook as the victory screen);
// when a team reaches the series majority it clinches and a fresh series begins.
const WAR_REC_KEY = 'codex_war_record_v1';
function loadRecord() {
  let r = null;
  try { r = JSON.parse(localStorage.getItem(WAR_REC_KEY) || 'null'); } catch (e) { /* ignore */ }
  if (!r || typeof r !== 'object') r = {};
  const rec = Object.assign(
    { blue: 0, red: 0, sBlue: 0, sRed: 0, bestOf: 5, streakTeam: null, streak: 0,
      fronts: {} }, r);
  if (!rec.fronts || typeof rec.fronts !== 'object') rec.fronts = {};
  return rec;
}
function saveRecord(r) {
  try { localStorage.setItem(WAR_REC_KEY, JSON.stringify(r)); } catch (e) { /* ignore */ }
}
function seriesNeed(r) { return Math.ceil((r.bestOf || 5) / 2); }
WAR.record = loadRecord();

function recordResult(winner) {
  const r = WAR.record;
  const team = (winner || '').toUpperCase() === 'RED' ? 'RED' : 'BLUE';
  if (team === 'RED') { r.red++; r.sRed++; } else { r.blue++; r.sBlue++; }
  if (r.streakTeam === team) r.streak++; else { r.streakTeam = team; r.streak = 1; }
  const need = seriesNeed(r);
  const clinch = r.sRed >= need ? 'RED' : (r.sBlue >= need ? 'BLUE' : null);
  saveRecord(r);
  return clinch;        // non-null → series won; caller resets for a new series
}

// Per-front territorial outcome of a finished match (final holder by health),
// accumulated across matches so the leaderboard can show who actually wins the
// ground war — not just who evolves a clean detector in the lab.
function recordFronts(w) {
  const r = WAR.record;
  if (!r.fronts) r.fronts = {};
  const bf = w.battlefield || {};
  Object.keys(bf).forEach((f) => {
    const c = bf[f];
    const fr = r.fronts[f] || { held: 0, lost: 0, infil: 0, preempt: 0 };
    if ((c.health || 0) >= 50) fr.held++; else fr.lost++;   // war_driver's 50hp line
    fr.infil += c.stealth || 0;
    fr.preempt += c.preempts || 0;
    r.fronts[f] = fr;
  });
  saveRecord(r);
}
function renderSeries() {
  const el = $('wc-series');
  if (!el) return;
  const r = WAR.record;
  const need = seriesNeed(r);
  const streak = r.streak > 1
    ? ` · <span class="${r.streakTeam === 'RED' ? 'red' : 'blue'}">${r.streakTeam} streak ×${r.streak}</span>` : '';
  el.innerHTML =
    `<span class="wc-series-lbl">SERIES · best of ${r.bestOf} (first to ${need})</span>` +
    `<span class="wc-series-sc">🔴 <b>${r.sRed}</b> — <b>${r.sBlue}</b> 🔵</span>` +
    `<span class="wc-series-life">lifetime 🔴 ${r.red} · 🔵 ${r.blue}${streak}</span>`;
}

function evoWarBanner(w) {
  if (!w) return '';
  const s = w.score || { blue: 0, red: 0 };
  const cur = w.current || {};
  const phase = cur.phase || 'calm';
  const gov = w.governor || {};
  const bf = w.battlefield || {};
  const armies = w.armies || {};
  const phaseText = ({
    stealth: `🥷 RED infiltrates <b>${cur.front}</b> — going dark, under Blue's radar`,
    stealth_hit: `🥷 RED INFILTRATED <b>${cur.front}</b> — undetected, silent erosion`,
    preempting: `🔮 RED telegraphs <b>${cur.front}</b> — ${cur.attack}… Blue reads the signs`,
    preempted: `🔮 BLUE PRE-EMPTED <b>${cur.front}</b> — breach PREVENTED${cur.latency != null ? ' (' + cur.latency + 's)' : ''}`,
    attacking: `🔴 RED storms <b>${cur.front}</b> — ${cur.attack}`,
    blocked: `🔵 BLUE HELD <b>${cur.front}</b> · ${cur.verdict || ''}${cur.latency != null ? ' in ' + cur.latency + 's' : ''}`,
    breached: `🔴 RED BREACHED <b>${cur.front}</b> — ground lost`,
    calm: w.running ? 'standing down…' : `⚑ WAR OVER — winner: <b>${w.winner || '—'}</b>`,
  })[phase] || '';
  const govTxt = gov.present
    ? `🛡️ governor: <b class="${gov.sev && gov.sev !== 'OK' ? 'gov-alert' : ''}">${gov.sev}${gov.code && gov.code !== 'OK' ? ':' + gov.code : ''}</b> overseeing`
    : '🛡️ governor offline (./deploy_governor.sh)';
  const summary = (!w.running && w.summary) ? `<div class="war-summary">${w.summary}</div>` : '';
  const log = (w.log || []).slice(-5).reverse().map((l) =>
    `<div class="war-log-row">t${l.turn} · ${l.result}</div>`).join('');
  const blueHeld = armies.blue ? armies.blue.held : 0;
  const redTook = armies.red ? armies.red.taken : 0;
  return `<div class="evo-war-box ${w.running ? 'live' : 'over'}">
    <div class="war-score">
      <span class="war-side red">🔴 RED <b>${s.red}</b></span>
      <span class="war-vs">${w.running ? '⚔ LIVE · turn ' + (w.turn || 0) + ' · wave ' + (w.wave || 1) : 'CEASEFIRE'}</span>
      <span class="war-side blue">🔵 BLUE <b>${s.blue}</b></span>
    </div>
    <div class="war-gov">${govTxt}</div>
    <div class="war-phase ${phase}">${phaseText}</div>
    <div class="war-armies">🔵 holds <b>${blueHeld}</b>/${Object.keys(bf).length} · 🔴 took <b>${redTook}</b> · 🔮 <b>${s.prevented || 0}</b> prevented · 🥷 <b>${(armies.red && armies.red.infiltrations) || 0}</b> infiltrated</div>
    ${w.strategy ? `<div class="war-strat">🔴 ${w.strategy}</div>` : ''}
    ${summary}
    <div class="war-log">${log}</div>
  </div>`;
}

// Reconcile the battlefield cells IN PLACE (keyed by front) so they never detach
// mid-click and don't flicker — the grid lives in its own persistent container.
const _bfCells = new Map();
function evoWarCells(w) {
  const grid = $('evo-war-bf');
  if (!grid) return;
  const bf = (w && w.battlefield) || {};
  const cur = (w && w.current) || {};
  const fronts = Object.keys(bf).sort((a, b) => BF_ORDER.indexOf(a) - BF_ORDER.indexOf(b));
  if (!fronts.length) { grid.innerHTML = ''; _bfCells.clear(); return; }
  fronts.forEach((f) => {
    const c = bf[f];
    let cell = _bfCells.get(f);
    if (!cell) {
      cell = document.createElement('div');
      cell.dataset.front = f;
      cell.title = 'click to inspect / deploy a genome';
      cell.innerHTML = '<div class="bf-name"></div><div class="bf-bar"><span></span></div><div class="bf-meta"></div>';
      _bfCells.set(f, cell);
      grid.appendChild(cell);
    }
    let phaseCls = '';
    if (c.under_attack && w.running) {
      phaseCls = ' ' + ({ preempting: 'preempting', preempted: 'preempted',
        stealth: 'stealth', stealth_hit: 'stealth' }[cur.phase] || 'under-attack');
    }
    cell.className = 'bf-cell ' + c.holder + phaseCls + (WAR.selected === f ? ' selected' : '');
    cell.children[0].textContent = (c.telegraphs ? '🔮 ' : '') + f + (c.defense ? ' 🛡️' + c.defense : '');
    const bar = cell.children[1].firstChild;
    bar.style.width = c.health + '%';
    bar.className = c.holder;
    cell.children[2].textContent = c.holder + ' · ' + c.health + 'hp'
      + (c.preempts ? ' · 🔮' + c.preempts : '') + (c.stealth ? ' · 🥷' + c.stealth : '');
  });
}

// 🔍 drill-down — click a front to inspect it and DEPLOY A CUSTOM GENOME to its
// live Blue unit (the user's only intervention). Rendered in a separate element
// so polling never wipes the genome you're typing.
function evoWarSelect(front) {
  WAR.selected = front;
  const el = $('evo-war-detail');
  const w = WAR.last;
  if (!el) return;
  if (!w || !w.battlefield || !w.battlefield[front]) { el.innerHTML = ''; return; }
  const c = w.battlefield[front];
  const bu = ((w.armies && w.armies.blue && w.armies.blue.units) || []).find((u) => u.front === front) || {};
  const ru = ((w.armies && w.armies.red && w.armies.red.units) || []).find((u) => u.front === front) || {};
  const cur = w.current || {};
  const atk = (cur.front === front && w.running) ? cur.attack : 'probing for an opening';
  el.innerHTML = `<div class="bf-detail">
    <div class="bf-detail-head"><span>🔍 ${front} — <b class="${c.holder}">${(c.holder || '').toUpperCase()}</b> · ${c.health}hp</span>
      <button class="bf-close" type="button">×</button></div>
    <div class="bf-row"><span class="bf-lbl blue">🔵 DEFENDER</span> <code class="bf-gene">${evoGenomeHTML(bu.genome || '')}</code></div>
    <div class="bf-row"><span class="bf-lbl">status</span> ${c.verdict} · holds ${c.blocks} · breaches ${c.breaches} · 🛡️ +${c.defense || 0}s · 🔮 ${c.preempts || 0} pre-empted · 🥷 ${c.stealth || 0} infiltrated</div>
    <div class="bf-row"><span class="bf-lbl red">🔴 ATTACKER</span> ${atk}${ru.genome ? ' · <code class="bf-gene">' + ru.genome + '</code>' : ''}</div>
    <div class="bf-inject">
      <input class="bf-genome-in" id="bf-gin" placeholder="custom genome for ${front}…" spellcheck="false">
      <button class="bf-deploy" type="button" data-aid="${bu.aid}">⚡ DEPLOY</button>
    </div>
    <div class="bf-hint">your intervention: re-arm this live Blue unit's DNA — it fights with your genome on its next tick</div>
    <div class="bf-result" id="bf-res"></div>
  </div>`;
}

async function evoWarDeploy(aid) {
  const w = WAR.last;
  const inp = $('bf-gin');
  const res = $('bf-res');
  const g = ((inp && inp.value) || '').trim();
  if (!g) { if (res) res.textContent = 'enter a genome string first'; return; }
  try {
    const r = await jpost('/api/propose', { path: w.blue_fabric, id: aid, genome: g });
    if (res) {
      res.innerHTML = r.ok
        ? `✅ deployed — <b>${WAR.selected}</b> now defends with «${g}»`
        : 'ERR: ' + (r.error || 'unknown');
      res.className = 'bf-result ' + (r.ok ? 'ok' : 'err');
    }
  } catch (e) { if (res) res.textContent = 'deploy failed: ' + e; }
}

async function evoWarPoll() {
  let w = null;
  try { w = await jget('/war.json?_=' + Date.now()); } catch (e) { /* no war yet */ }
  if (w && w.running && w.updated && (Date.now() / 1000 - w.updated) > 25) {
    w.running = false;
    if (w.current) w.current.phase = 'calm';
  }
  WAR.last = w;
  // ⚔ always-on: drive the battle-of-the-spheres + the right SIGINT console
  if (window.War3D && window.War3D.setWar) window.War3D.setWar(w);
  renderWarConsole(w);
  maybeShowVictory(w);
  // tab-only rendering (the WARGAME overlay)
  if (EVO.open) {
    const el = $('evo-war-banner');
    if (el) el.innerHTML = w ? evoWarBanner(w) : '';
    evoWarCells(w);
  }
  const btn = $('evo-war');
  if (btn && w) {
    btn.textContent = w.running ? '■ STOP WAR' : '⚔ START WAR';
    btn.classList.toggle('live', !!w.running);
  }
}

// ── ⚔ CYBERWAR CONSOLE (right column) — live SIGINT: status, strategies of
// each army, and the autonomous event/hack feed. Always on, fed by war.json.
function wcFeedClass(r) {
  if (/INFILTRAT|STEALTH|🥷/.test(r)) return 'stealth';
  if (/BREACH/.test(r)) return 'red';
  if (/PRE-EMPT|🔮/.test(r)) return 'cyan';
  if (/HELD|CAUGHT|BLOCK|REPELLED/.test(r)) return 'green';
  return '';
}

function renderWarConsole(w) {
  const liveEl = $('wc-live'), scoreEl = $('wc-score');
  const teamsEl = $('wc-teams'), feedEl = $('wc-feed');
  if (!feedEl) return;
  renderSeries();                       // standings stay visible even pre-war
  if (!w) {
    if (liveEl) { liveEl.textContent = '○ STANDBY'; liveEl.className = 'wc-live'; }
    return;
  }
  const s = w.score || { blue: 0, red: 0 };
  const armies = w.armies || {}, gov = w.governor || {};
  const running = !!w.running;
  if (liveEl) {
    liveEl.textContent = running ? '● LIVE'
      : (w.winner ? '⚑ WINNER ' + w.winner : '⚑ CEASEFIRE');
    liveEl.className = 'wc-live ' + (running ? 'on' : 'off');
  }
  if (scoreEl) {
    scoreEl.innerHTML =
      `<span class="wc-side red">🔴 RED <b>${s.red}</b></span>` +
      `<span class="wc-vs">${running ? 'turn ' + (w.turn || 0) + ' · wave ' + (w.wave || 1) : 'ceasefire'}</span>` +
      `<span class="wc-side blue">🔵 BLUE <b>${s.blue}</b></span>`;
  }
  if (teamsEl) {
    const blue = armies.blue || {}, red = armies.red || {};
    const govTxt = gov.present
      ? `<span class="${gov.sev && gov.sev !== 'OK' ? 'gov-alert' : ''}">${gov.sev}${gov.code && gov.code !== 'OK' ? ':' + gov.code : ''}</span>`
      : 'offline';
    const unit = (u, holder) =>
      `<div class="wc-unit"><span class="wc-front ${holder ? u.holder : 'red'}">${u.front}</span><code>${evoGenomeHTML(u.genome)}</code></div>`;
    const diff = (w.difficulty || 'veteran').toLowerCase();
    const diffBadge = `<span class="wc-diff ${diff}">${diff.toUpperCase()}</span>`;
    const strat = (w.strategy_preset || 'balanced').toLowerCase();
    const stratIcon = { balanced: '⚖', blitz: '⚡', stealth: '🥷', feint: '🎭' }[strat] || '⚖';
    const stratBadge = strat === 'balanced' ? ''
      : ` <span class="wc-strat ${strat}">${stratIcon} ${strat.toUpperCase()}</span>`;
    teamsEl.innerHTML =
      `<div class="wc-team red">
        <div class="wc-team-head">🔴 RED ARMY ${diffBadge}${stratBadge} · took <b>${red.taken || 0}</b>/5 · 🥷 <b>${red.infiltrations || 0}</b></div>
        <div class="wc-strat">${w.strategy || red.strategy || 'probing'}</div>
        ${(red.units || []).map((u) => unit(u, false)).join('')}
      </div>
      <div class="wc-team blue">
        <div class="wc-team-head">🔵 BLUE ARMY · holds <b>${blue.held || 0}</b>/5 · 🛡️ ${govTxt}</div>
        ${(blue.units || []).map((u) => unit(u, true)).join('')}
      </div>`;
  }
  const log = w.log || [];
  if (!log.length) {
    feedEl.innerHTML = `<div class="wc-idle">${running ? 'mustering forces…' : 'no events yet'}</div>`;
    return;
  }
  const atBottom = feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 60;
  let html = log.map((l) =>
    `<div class="wc-line ${wcFeedClass(l.result)}"><span class="wc-t">t${String(l.turn).padStart(2, '0')}</span> ${l.result}</div>`).join('');
  if (w.summary && !running) html += `<div class="wc-line summary">⚑ ${w.summary}</div>`;
  feedEl.innerHTML = html;
  if (atBottom) feedEl.scrollTop = feedEl.scrollHeight;
}

// ⚑ VICTORY SCREEN — pop once when a war we watched running comes to an end,
// with the final tally, MVP, and a one-click rematch. Stays out of the way on
// page load (only fires on a live running→ended transition, not a stale file).
function showVictory(w, clinch) {
  const s = w.score || { blue: 0, red: 0 };
  const armies = w.armies || {};
  const blueHeld = (armies.blue && armies.blue.held) || 0;
  const redTook = (armies.red && armies.red.taken) || 0;
  const nFronts = Object.keys(w.battlefield || {}).length || 5;
  const win = (w.winner || '—').toUpperCase();
  const banner = $('wv-banner');
  if (clinch) {
    banner.textContent = clinch === 'RED'
      ? '🏆 RED WINS THE SERIES' : '🏆 BLUE WINS THE SERIES';
    banner.className = 'wv-banner clinch ' + (clinch === 'RED' ? 'red' : 'blue');
  } else {
    banner.textContent = win === 'RED' ? '⚑ RED ARMY WINS' : '🛡️ BLUE HOLDS THE CLUSTER';
    banner.className = 'wv-banner ' + (win === 'RED' ? 'red' : 'blue');
  }
  const r = WAR.record;
  $('wv-series').innerHTML =
    `series (best of ${r.bestOf}) · 🔴 <b>${r.sRed}</b> — <b>${r.sBlue}</b> 🔵` +
    ` · lifetime 🔴 ${r.red} · 🔵 ${r.blue}` +
    (clinch ? ` · <b>${clinch} takes it — new series</b>` : '');
  $('wv-score').innerHTML =
    `<span class="wv-side red">🔴 RED <b>${s.red}</b></span>` +
    `<span class="wv-x">—</span>` +
    `<span class="wv-side blue">🔵 BLUE <b>${s.blue}</b></span>`;
  const stat = (icon, val, lbl) =>
    `<span class="wv-stat"><span class="wv-sv">${icon} ${val}</span><span class="wv-sl">${lbl}</span></span>`;
  $('wv-stats').innerHTML =
    stat('🛡️', `${blueHeld}/${nFronts}`, 'fronts held') +
    stat('🏅', w.mvp || '—', 'MVP front') +
    stat('🔮', s.prevented || 0, 'pre-empted') +
    stat('🥷', (armies.red && armies.red.infiltrations) || 0, 'infiltrated');
  $('wv-summary').textContent = w.summary || '';
  $('war-victory').setAttribute('aria-hidden', 'false');
}
function hideVictory() { $('war-victory').setAttribute('aria-hidden', 'true'); }
function maybeShowVictory(w) {
  const running = !!(w && w.running);
  if (running) { WAR.wasRunning = true; WAR.dismissed = false; hideVictory(); return; }
  if (w && w.winner && WAR.wasRunning && !WAR.dismissed) {
    const clinch = recordResult(w.winner);   // tally the result once per match
    recordFronts(w);                         // + per-front territorial outcome
    showVictory(w, clinch);
    if (clinch) { WAR.record.sBlue = 0; WAR.record.sRed = 0; saveRecord(WAR.record); }
    renderSeries();
    WAR.wasRunning = false;           // show exactly once per finished war
  }
}
$('wv-dismiss').addEventListener('click', () => { WAR.dismissed = true; hideVictory(); });
$('wv-rematch').addEventListener('click', async () => {
  hideVictory(); WAR.dismissed = true;
  await jpost('/api/war', {
    action: 'start', duration: 600, gap: 7,
    difficulty: warDifficulty(), strategy: warStrategy() });
  setTimeout(evoWarPoll, 400);
});

function warDifficulty() {
  const sel = $('evo-diff');
  return (sel && sel.value) || 'veteran';
}
function warStrategy() {
  const sel = $('evo-strategy');
  return (sel && sel.value) || 'balanced';
}

async function evoWarToggle() {
  const btn = $('evo-war');
  const live = btn.classList.contains('live');
  btn.textContent = live ? '…stopping' : '…starting';
  if (live) await jpost('/api/war', { action: 'stop' });
  else await jpost('/api/war', {
    action: 'start', duration: 600, gap: 7,
    difficulty: warDifficulty(), strategy: warStrategy() });
  setTimeout(evoWarPoll, 400);
}

$('evo-toggle').addEventListener('click', () => evoToggle());
$('evo-close').addEventListener('click', () => evoToggle(false));
$('evo-run').addEventListener('click', evoRunRound);
$('evo-war').addEventListener('click', evoWarToggle);
// best-of series length — init from the saved record, reset the series on change
(() => {
  const sel = $('evo-bestof');
  if (!sel) return;
  sel.value = String(WAR.record.bestOf);
  sel.addEventListener('change', (e) => {
    WAR.record.bestOf = parseInt(e.target.value, 10) || 5;
    WAR.record.sBlue = 0; WAR.record.sRed = 0;   // new length → fresh series
    saveRecord(WAR.record); renderSeries();
  });
})();
renderSeries();
// battlefield cell → drill-down (delegated on the persistent reconciled grid)
$('evo-war-bf').addEventListener('click', (e) => {
  const cell = e.target.closest('.bf-cell');
  if (cell) evoWarSelect(cell.dataset.front);
});
$('evo-war-detail').addEventListener('click', (e) => {
  if (e.target.closest('.bf-close')) { WAR.selected = null; $('evo-war-detail').innerHTML = ''; return; }
  const dep = e.target.closest('.bf-deploy');
  if (dep) evoWarDeploy(parseInt(dep.dataset.aid, 10));
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && EVO.open) evoToggle(false); });

// ── ? WHAT IS THIS — collapsible docs side panel (lazy-embeds /docs.html) ────
(() => {
  const drawer = $('docs-drawer'), frame = $('docs-frame'), toggle = $('docs-toggle');
  if (!drawer || !toggle) return;
  let loaded = false;
  const isOpen = () => drawer.getAttribute('aria-hidden') === 'false';
  function setDocs(open) {
    if (open && !loaded) { frame.src = '/docs.html?embed=1'; loaded = true; }
    drawer.setAttribute('aria-hidden', String(!open));
    toggle.classList.toggle('active', open);
  }
  toggle.addEventListener('click', () => setDocs(!isOpen()));
  const close = $('docs-close');
  if (close) close.addEventListener('click', () => setDocs(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) setDocs(false);
  });
})();

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
