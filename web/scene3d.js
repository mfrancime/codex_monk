/* scene3d.js — the full-screen WebGL war-room.
 *
 * Three.js r134 (UMD globals: THREE.*). Renders the live swarm as a 3D scene:
 *   - each fabric is a glowing hub sphere wrapped in a slowly-spinning
 *     wireframe icosahedron shell, colored by aggregated severity;
 *   - each agent orbits its hub on a tilted ring, colored by its OWN sev;
 *   - each VJR gateway link is a curved beam with energy pulses streaming
 *     from source hub to peer hub;
 *   - UnrealBloom gives everything the neon war-room glow;
 *   - a starfield + grid floor give depth, and the camera auto-orbits.
 *
 * DEFCON-reactive: as the level drops toward 1 the bloom swells, the scene
 * tints red, hubs pulse harder and pulses fly faster.
 *
 * Public API (window.War3D):
 *   init(canvas, { onAgentClick })   — stand up the scene once
 *   update(state)                    — reconcile to the latest /api/swarms
 *   setDefcon(level)                 — drive global intensity
 */
(function () {
  'use strict';

  const SEV_COLOR = {
    OK: 0x4dff7c, INFO: 0x5fb8ff, WARN: 0xffb300, CRITICAL: 0xff3030,
  };
  const DIM = 0x2da551;
  const GATEWAY = 0x5fb8ff;
  const sevColor = (s) => (s && SEV_COLOR[s] != null) ? SEV_COLOR[s] : DIM;

  let renderer, scene, camera, composer, bloom, controls, clock;
  let hubGroup, starfield;
  let raycaster, pointer, onAgentClick = null;
  let defcon = 5;

  const hubs = {};        // path -> hub record
  let links = [];         // active link records
  const agentPickables = []; // meshes with userData {path,id} for raycasting
  const hubPickables = [];   // hub spheres with userData {path,isHub} for clicks
  let hovered = null;        // currently hover-highlighted mesh
  let replayMode = false;    // true while the TIME MACHINE is driving colors

  // ⚔ battle-of-the-spheres state (fed from web/war.json via setWar)
  let warState = null;
  let _warTurn = -1, _warPhase = '';
  const projectiles = [];

  // ── helpers ───────────────────────────────────────────────────────────

  let _glowTex = null;
  function glowTexture() {
    if (_glowTex) return _glowTex;
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const ctx = c.getContext('2d');
    const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0.0, 'rgba(255,255,255,1)');
    g.addColorStop(0.25, 'rgba(190,255,210,0.9)');
    g.addColorStop(0.6, 'rgba(77,255,124,0.35)');
    g.addColorStop(1.0, 'rgba(77,255,124,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 64, 64);
    _glowTex = new THREE.CanvasTexture(c);
    return _glowTex;
  }

  function makeLabel(text, color) {
    const c = document.createElement('canvas');
    c.width = 256; c.height = 64;
    const ctx = c.getContext('2d');
    ctx.font = 'bold 30px "JetBrains Mono", monospace';
    ctx.fillStyle = '#' + color.toString(16).padStart(6, '0');
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 12;
    ctx.fillText(text.toUpperCase(), 128, 34);
    const tex = new THREE.CanvasTexture(c);
    tex.anisotropy = 4;
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({
      map: tex, transparent: true, depthTest: false,
    }));
    spr.scale.set(16, 4, 1);
    return spr;
  }

  // Team identity from the fabric path, so the 3D spheres read as armies, not
  // anonymous "FABRIC" blobs. Label is team-COLORED (persistent identity); the
  // sphere itself stays severity-colored (live attack state).
  function teamLabel(swarm) {
    const p = swarm.path || '';
    if (/k8s_deployed/.test(p)) return 'BLUE ARMY';
    if (/codex\.red\.|\/red\./.test(p)) return 'RED ARMY';
    if (/aggregat/.test(p)) return 'GOVERNOR';
    if (/evolver/.test(p)) return 'EVOLVER';
    if (/kernel/.test(p)) return 'KERNEL';
    return swarm.swarm_name
      || (p.split('/').pop() || 'fabric').replace(/^codex\./, '').replace(/\.fabric$/, '');
  }
  function teamColor(swarm) {
    const p = swarm.path || '';
    if (/k8s_deployed/.test(p)) return 0x5fb8ff;          // 🔵 blue
    if (/codex\.red\.|\/red\./.test(p)) return 0xff3030;  // 🔴 red
    if (/aggregat/.test(p)) return 0xffb300;              // 🛡️ amber
    return 0x4dff7c;                                       // green
  }
  function isTeam(swarm) {
    return /k8s_deployed|codex\.red\.|\/red\.|aggregat/.test(swarm.path || '');
  }
  // The ARMY spheres are colored by TEAM (blue/red/amber) so you can see who's
  // who; the orbiting agent dots still flash by their own severity (live fire).
  function hubColor(swarm) {
    return isTeam(swarm) ? teamColor(swarm) : sevColor(swarm.severity);
  }

  // small unit tag (front name) that rides above an agent dot as a CHILD sprite,
  // so it follows the dot's orbit for free. Turns anonymous "fairies" into the
  // named soldiers of each army (pods/nodes/apiserver/scheduler defenders).
  function makeUnitTag(text, color) {
    const c = document.createElement('canvas');
    c.width = 128; c.height = 32;
    const ctx = c.getContext('2d');
    ctx.font = 'bold 18px "JetBrains Mono", monospace';
    ctx.fillStyle = '#' + color.toString(16).padStart(6, '0');
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 6;
    ctx.fillText(text.toUpperCase(), 64, 17);
    const tex = new THREE.CanvasTexture(c); tex.anisotropy = 2;
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({
      map: tex, transparent: true, depthTest: false }));
    spr.scale.set(6, 1.5, 1);
    spr.position.set(0, 2.0, 0);
    return spr;
  }

  function makeStarfield() {
    const n = 600, pos = new Float32Array(n * 3);
    // deterministic scatter (no Math.random dependency for repeatability)
    for (let i = 0; i < n; i++) {
      const a = i * 2.3999632;            // golden-angle spiral
      const r = 200 + (i % 400);
      pos[i * 3]     = Math.cos(a) * r * (0.4 + (i % 7) / 10);
      pos[i * 3 + 1] = ((i * 53) % 400) - 160;
      pos[i * 3 + 2] = Math.sin(a) * r * (0.4 + (i % 5) / 10);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    return new THREE.Points(g, new THREE.PointsMaterial({
      color: 0x2da551, size: 0.7, transparent: true, opacity: 0.5,
    }));
  }

  function hubPosition(i, total) {
    if (total <= 1) return new THREE.Vector3(0, 0, 0);
    const a = (i / total) * Math.PI * 2;
    const R = Math.max(22, total * 9);
    return new THREE.Vector3(Math.cos(a) * R, 0, Math.sin(a) * R);
  }

  // ── hub lifecycle ─────────────────────────────────────────────────────

  function buildHub(swarm, pos) {
    const group = new THREE.Group();
    group.position.copy(pos);

    const col = hubColor(swarm);
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(4.2, 32, 32),
      new THREE.MeshStandardMaterial({
        color: col, emissive: col, emissiveIntensity: 1.4,
        roughness: 0.35, metalness: 0.4,
      }));
    sphere.userData = { path: swarm.path, isHub: true };
    group.add(sphere);
    hubPickables.push(sphere);

    const shell = new THREE.Mesh(
      new THREE.IcosahedronGeometry(7, 1),
      new THREE.MeshBasicMaterial({
        color: col, wireframe: true, transparent: true, opacity: 0.35,
      }));
    group.add(shell);

    const label = makeLabel(teamLabel(swarm), teamColor(swarm));
    label.position.set(0, 11, 0);
    group.add(label);

    hubGroup.add(group);
    const rec = { group, sphere, shell, label, pos: pos.clone(),
                  agents: {}, ringR: 13, data: swarm, phase: 0 };
    syncAgents(rec, swarm);
    return rec;
  }

  function syncAgents(rec, swarm) {
    const live = new Set();
    const agents = swarm.agents || [];
    agents.forEach((a, idx) => {
      live.add(a.id);
      const isGw = a.role === 'gateway';
      const col = isGw ? GATEWAY : sevColor(a.sev);
      let m = rec.agents[a.id];
      if (!m) {
        m = new THREE.Mesh(
          new THREE.SphereGeometry(isGw ? 1.5 : 1.15, 18, 18),
          new THREE.MeshStandardMaterial({
            color: col, emissive: col, emissiveIntensity: 1.2,
            roughness: 0.4, metalness: 0.3,
          }));
        m.userData = { path: swarm.path, id: a.id };
        rec.group.add(m);
        rec.agents[a.id] = m;
        agentPickables.push(m);
      }
      m.material.color.setHex(col);
      m.material.emissive.setHex(col);
      // stable orbit slot
      m.userData.baseAngle = (idx / Math.max(1, agents.length)) * Math.PI * 2;
      m.userData.tilt = isGw ? 0.0 : 0.5;
      m.userData.crit = a.sev === 'CRITICAL';
    });
    // remove agents that vanished
    Object.keys(rec.agents).forEach((id) => {
      if (!live.has(parseInt(id, 10)) && !live.has(id)) {
        const m = rec.agents[id];
        rec.group.remove(m);
        const pi = agentPickables.indexOf(m);
        if (pi >= 0) agentPickables.splice(pi, 1);
        delete rec.agents[id];
      }
    });
  }

  function updateHub(rec, swarm) {
    if (replayMode) { rec.data = swarm; return; }  // TIME MACHINE owns the colors
    const col = hubColor(swarm);                    // team identity persists
    rec.sphere.material.color.setHex(col);
    rec.sphere.material.emissive.setHex(col);
    rec.shell.material.color.setHex(col);
    rec.data = swarm;
    syncAgents(rec, swarm);
  }

  function removeHub(path) {
    const rec = hubs[path];
    if (!rec) return;
    Object.values(rec.agents).forEach((m) => {
      const pi = agentPickables.indexOf(m); if (pi >= 0) agentPickables.splice(pi, 1);
    });
    const hi = hubPickables.indexOf(rec.sphere); if (hi >= 0) hubPickables.splice(hi, 1);
    hubGroup.remove(rec.group);
    delete hubs[path];
  }

  // ── links + pulses ────────────────────────────────────────────────────

  function clearLinks() {
    links.forEach((lk) => {
      scene.remove(lk.line);
      lk.pulses.forEach((p) => scene.remove(p));
    });
    links = [];
  }

  function buildLinks(stateLinks) {
    clearLinks();
    (stateLinks || []).forEach((l) => {
      const a = hubs[l.from], b = hubs[l.to];
      if (!a || !b) return;
      const mid = a.pos.clone().add(b.pos).multiplyScalar(0.5);
      mid.y += a.pos.distanceTo(b.pos) * 0.22;     // arc upward
      const curve = new THREE.QuadraticBezierCurve3(a.pos, mid, b.pos);
      const col = l.online ? 0x4dff7c : 0x335544;
      const geo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(40));
      const line = new THREE.Line(geo, new THREE.LineBasicMaterial({
        color: col, transparent: true, opacity: l.online ? 0.55 : 0.2,
      }));
      scene.add(line);
      // energy pulses streaming along the curve
      const pulses = [];
      const nPulse = l.online ? 4 : 0;
      for (let i = 0; i < nPulse; i++) {
        const spr = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowTexture(), color: 0x9dffc0, transparent: true, opacity: 0.95,
          depthWrite: false, blending: THREE.AdditiveBlending,
        }));
        spr.scale.set(3.2, 3.2, 1);
        spr.userData = { t: i / nPulse };
        scene.add(spr);
        pulses.push(spr);
      }
      links.push({ curve, line, pulses, online: l.online });
    });
  }

  // ── main loop ─────────────────────────────────────────────────────────

  let _last = 0;
  const FRAME_MS = 1000 / 30;               // cap at ~30 fps to spare the CPU/GPU

  // ⚔ BATTLE OF THE SPHERES — drive the 3D fight from the live war state.
  const unitFront = {};          // "path#id" -> front (pods/nodes/…)
  let _unitTags = 0;

  function tagUnits(path, color) {
    const rec = path && hubs[path];
    if (!rec) return;
    Object.keys(rec.agents).forEach((id) => {
      const front = unitFront[path + '#' + id];
      const m = rec.agents[id];
      if (front && !m.userData._tag) {
        const tag = makeUnitTag(front, color);
        m.add(tag);
        m.userData._tag = tag;
        _unitTags++;
        window.__war3dUnitTags = _unitTags;
      }
    });
  }

  function setWar(w) {
    warState = w;
    // map each army's agent ids → the front it fights on, so the orbiting dots
    // read as named units. Blue units carry their aid; Red's are PLAYBOOK-ordered.
    if (w && w.armies) {
      const bf = w.blue_fabric, rf = w.red_fabric;
      ((w.armies.blue && w.armies.blue.units) || []).forEach((u) => {
        if (bf && u.aid != null) unitFront[bf + '#' + u.aid] = u.front;
      });
      ((w.armies.red && w.armies.red.units) || []).forEach((u, i) => {
        if (rf) unitFront[rf + '#' + (i + 1)] = u.front;
      });
      if (bf) tagUnits(bf, teamColor({ path: bf }));
      if (rf) tagUnits(rf, teamColor({ path: rf }));
      // territory HP bars on the Blue sphere
      if (bf && hubs[bf] && w.battlefield) {
        ensureFrontBars(hubs[bf], Object.keys(w.battlefield));
        updateFrontBars(hubs[bf], w.battlefield);
      }
    }
  }

  // world position of a Blue front's territory bar (beam aim point), or null
  function frontWorldPos(blue, front) {
    const fb = blue && blue._frontBars && blue._frontBars[front];
    if (!fb) return null;
    const v = new THREE.Vector3();
    fb.holder.getWorldPosition(v);
    return v;
  }

  function spawnProjectile(from, to, phase, front) {
    if (projectiles.length > 20) return;
    const hex = phase === 'stealth' ? 0xb030ff
      : phase === 'preempting' ? 0x5fb8ff : 0xff3030;
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(), color: hex, transparent: true, opacity: 0.95,
      depthTest: false,
    }));
    spr.scale.set(3.4, 3.4, 1);
    spr.position.copy(from);
    spr.userData = { from: from.clone(), to: to.clone(), t: 0, hex, front };
    scene.add(spr);
    projectiles.push(spr);
    window.__war3dProjSpawned = (window.__war3dProjSpawned || 0) + 1;
  }

  function updateProjectiles(dt) {
    for (let i = projectiles.length - 1; i >= 0; i--) {
      const s = projectiles[i];
      s.userData.t += dt * 1.5;
      const tt = s.userData.t;
      if (tt >= 1) {                              // ⚡ the round LANDS
        onImpact(s.userData.to, s.userData.hex, s.userData.front);
        scene.remove(s); projectiles.splice(i, 1); continue;
      }
      s.position.lerpVectors(s.userData.from, s.userData.to, tt);
      s.position.y += Math.sin(tt * Math.PI) * 7;
      s.material.opacity = 0.95 * (1 - tt * 0.4);
    }
  }

  // ⚡ IMPACT JUICE — a beam reaching a front blooms a burst and recoils the
  // struck territory bar. Short-lived additive sprites, capped, so it stays
  // light. This is the hit-feedback that makes the ground war feel physical.
  const bursts = [];
  function spawnBurst(pos, hex) {
    if (bursts.length > 12) return;
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(), color: hex, transparent: true, opacity: 1,
      depthTest: false, blending: THREE.AdditiveBlending,
    }));
    spr.scale.set(2, 2, 1);
    spr.position.copy(pos);
    spr.userData = { t: 0 };
    scene.add(spr);
    bursts.push(spr);
    window.__war3dBursts = (window.__war3dBursts || 0) + 1;
  }
  function updateBursts(dt) {
    for (let i = bursts.length - 1; i >= 0; i--) {
      const s = bursts[i];
      s.userData.t += dt * 3.2;
      const tt = s.userData.t;
      if (tt >= 1) { scene.remove(s); bursts.splice(i, 1); continue; }
      const sc = 2 + tt * 11;
      s.scale.set(sc, sc, 1);
      s.material.opacity = 1 - tt;
    }
  }
  function onImpact(pos, hex, front) {
    spawnBurst(pos, hex);
    const blue = warState && hubs[warState.blue_fabric];
    const fb = blue && blue._frontBars && blue._frontBars[front];
    if (fb) fb.bar.userData.hitT = 0.4;          // recoil this territory bar
  }

  function flashHub(rec, hex) {
    if (rec) {
      rec.flashT = 0.7; rec.flashHex = hex;
      window.__war3dFlashes = (window.__war3dFlashes || 0) + 1;
    }
  }

  // 🗺️ TERRITORY — the 4 k8s fronts as HP bars ringing the Blue sphere. Each
  // drains with its front's health and recolors by holder (blue held → amber
  // contested → red fallen), so the cluster's ground war is legible ON the
  // battlefield, not just in the console. ~8 objects, attached to the blue hub.
  const _frontOrder = ['pods', 'nodes', 'apiserver', 'etcd', 'scheduler'];
  function frontHolderColor(h) {
    return h === 'blue' ? 0x5fb8ff : (h === 'red' ? 0xff3030 : 0xffb300);
  }
  function ensureFrontBars(rec, fronts) {
    if (rec._frontBars) return;
    rec._frontBars = {};
    const order = fronts.slice().sort(
      (a, b) => (_frontOrder.indexOf(a) + 1 || 99) - (_frontOrder.indexOf(b) + 1 || 99));
    order.forEach((f, i) => {
      const a = (i / order.length) * Math.PI * 2;
      const R = 11;
      const holder = new THREE.Group();
      holder.position.set(Math.cos(a) * R, -3, Math.sin(a) * R);
      const bar = new THREE.Mesh(
        new THREE.BoxGeometry(0.8, 6, 0.8),
        new THREE.MeshStandardMaterial({
          color: 0x5fb8ff, emissive: 0x5fb8ff, emissiveIntensity: 0.9,
          roughness: 0.4, metalness: 0.3 }));
      bar.position.y = 3;
      holder.add(bar);
      const lab = makeUnitTag(f, 0x9fd8ff);
      lab.position.set(0, 7.4, 0);
      holder.add(lab);
      rec.group.add(holder);
      rec._frontBars[f] = { holder, bar };
    });
    window.__war3dFrontBars = Object.keys(rec._frontBars).length;
  }
  function updateFrontBars(rec, bf) {
    if (!rec._frontBars) return;
    Object.keys(rec._frontBars).forEach((f) => {
      const c = bf[f]; if (!c) return;
      const { bar } = rec._frontBars[f];
      const h = Math.max(0.05, (c.health || 0) / 100);
      bar.scale.y = h;
      bar.position.y = 3 * h;                 // keep the base anchored at 0
      const col = frontHolderColor(c.holder);
      bar.material.color.setHex(col);
      bar.material.emissive.setHex(col);
    });
  }

  function stepBattle(dt) {
    updateProjectiles(dt);                       // in-flight rounds + impacts
    updateBursts(dt);                            // impact blooms (even post-war)
    if (!warState || !warState.running) return;
    const blue = hubs[warState.blue_fabric];
    const red = hubs[warState.red_fabric];
    const cur = warState.current || {};
    const ph = cur.phase || '';
    if (cur.phase !== _warPhase || warState.turn !== _warTurn) {
      if (blue && red && ['attacking', 'stealth', 'preempting'].includes(ph)) {
        const aim = frontWorldPos(blue, cur.front) || blue.pos;  // hit the front
        spawnProjectile(red.pos, aim, ph, cur.front);
      }
      if (['breached', 'stealth_hit'].includes(ph)) flashHub(blue, 0xff3030);
      else if (['blocked', 'preempted'].includes(ph)) flashHub(blue, 0x4dff7c);
      _warPhase = cur.phase; _warTurn = warState.turn;
    }
    // ⚡ decay territory-bar recoil (set on impact) — a quick bright width-pop
    if (blue && blue._frontBars) {
      Object.values(blue._frontBars).forEach((fb) => {
        const b = fb.bar;
        if (b.userData.hitT > 0) {
          b.userData.hitT = Math.max(0, b.userData.hitT - dt);
          const k = b.userData.hitT / 0.4;
          b.material.emissiveIntensity = 0.9 + k * 3;
          b.scale.x = 1 + k * 0.6; b.scale.z = 1 + k * 0.6;
        } else if (b.scale.x !== 1) {
          b.material.emissiveIntensity = 0.9; b.scale.x = 1; b.scale.z = 1;
        }
      });
    }
  }

  function animate() {
    // freeze hook: lets a test (or a paused tab) stop the rAF loop after
    // rendering one final frame, so a screenshot can capture a stable image.
    if (window.__war3dFreeze) { composer.render(); return; }
    requestAnimationFrame(animate);
    // Don't burn cycles while the tab is hidden — this is the main thing that
    // pegs a machine when the war-room is left open in a background tab.
    if (document.hidden) { _last = 0; return; }
    const now = performance.now();
    if (now - _last < FRAME_MS) return;     // throttle
    const dt = _last ? Math.min((now - _last) / 1000, 0.1) : 0.033;
    _last = now;
    const t = now / 1000;
    const heat = (5 - defcon) / 4;            // 0 calm … 1 DEFCON-1

    // ⚔ drive the battle (projectiles, sphere flashes) from the war state
    stepBattle(dt);

    // hub breathing + shell spin + agent orbits
    Object.values(hubs).forEach((rec) => {
      let pulse = 1.3 + Math.sin(t * 2 + rec.pos.x) * 0.25 + heat * 0.6;
      // ⚔ battle flash: a hit/block momentarily slams the sphere bright + tints it
      if (rec.flashT > 0) {
        rec.flashT = Math.max(0, rec.flashT - dt);
        const k = rec.flashT / 0.7;                 // 1 → 0 over the flash
        pulse += k * 4;
        rec.sphere.material.emissive.setHex(rec.flashHex);
        if (rec.flashT === 0 && rec.data) {         // restore the team/sev color
          rec.sphere.material.emissive.setHex(hubColor(rec.data));
        }
      }
      rec.sphere.material.emissiveIntensity = pulse;
      rec.shell.rotation.y += dt * 0.25;
      rec.shell.rotation.x += dt * 0.12;
      Object.values(rec.agents).forEach((m) => {
        const u = m.userData;
        const ang = u.baseAngle + t * (0.35 + (u.crit ? 0.5 : 0));
        const R = rec.ringR;
        m.position.set(
          Math.cos(ang) * R,
          Math.sin(ang * 1.3) * R * u.tilt * 0.4,
          Math.sin(ang) * R);
        const e = 1.0 + (u.crit ? Math.abs(Math.sin(t * 6)) * 1.5 : 0.2);
        m.material.emissiveIntensity = e;
      });
    });

    // stream pulses along links
    const speed = 0.12 + heat * 0.25;
    links.forEach((lk) => {
      lk.pulses.forEach((spr) => {
        spr.userData.t = (spr.userData.t + dt * speed) % 1;
        spr.position.copy(lk.curve.getPoint(spr.userData.t));
      });
    });

    if (starfield) starfield.rotation.y += dt * 0.01;

    // DEFCON-reactive bloom + fog tint
    bloom.strength = 0.9 + heat * 1.1 + Math.sin(t * 3) * heat * 0.2;
    scene.fog.color.setRGB(0.01 + heat * 0.08, 0.03, 0.024);

    controls.update();
    composer.render();
  }

  function onResize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
    composer.setSize(window.innerWidth, window.innerHeight);
    bloom.setSize(window.innerWidth * 0.5, window.innerHeight * 0.5);
  }

  function _pick(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    // agents first (they orbit in front), then the hub spheres behind them
    return raycaster.intersectObjects(agentPickables.concat(hubPickables), false)[0];
  }

  function handleClick(ev) {
    if (!onAgentClick) return;
    const hit = _pick(ev);
    if (!hit) return;
    const u = hit.object.userData;
    // a hub click carries id=null → app.js shows the whole swarm
    onAgentClick(u.path, u.isHub ? null : u.id);
  }

  function handleHover(ev) {
    const hit = _pick(ev);
    const obj = hit ? hit.object : null;
    if (obj === hovered) return;
    if (hovered) hovered.scale.setScalar(1.0);          // un-highlight previous
    hovered = obj;
    if (hovered) hovered.scale.setScalar(1.18);         // pop the hovered mesh
    renderer.domElement.style.cursor = hovered ? 'pointer' : 'default';
  }

  // ── public API ────────────────────────────────────────────────────────

  function init(canvas, opts) {
    onAgentClick = opts && opts.onAgentClick;
    clock = new THREE.Clock();
    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020806, 0.012);

    camera = new THREE.PerspectiveCamera(
      55, window.innerWidth / window.innerHeight, 0.1, 3000);
    camera.position.set(0, 34, 82);

    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true, preserveDrawingBuffer: true,
      powerPreference: 'high-performance' });
    // pixelRatio 1 (not devicePixelRatio) — on HiDPI/4K a 2× buffer with bloom
    // is 4× the pixels and is what makes the scene hang on weaker GPUs.
    renderer.setPixelRatio(1);
    renderer.setSize(window.innerWidth, window.innerHeight);

    scene.add(new THREE.AmbientLight(0x224433, 0.7));
    const key = new THREE.PointLight(0x4dff7c, 1.0, 600);
    key.position.set(0, 80, 60); scene.add(key);

    const grid = new THREE.GridHelper(600, 80, 0x1a4022, 0x0c1f12);
    grid.position.y = -20; scene.add(grid);

    starfield = makeStarfield(); scene.add(starfield);
    hubGroup = new THREE.Group(); scene.add(hubGroup);

    composer = new THREE.EffectComposer(renderer);
    composer.addPass(new THREE.RenderPass(scene, camera));
    // bloom at half resolution — its blur passes are the heaviest part of the
    // frame; half-res is visually almost identical and ~4× cheaper.
    bloom = new THREE.UnrealBloomPass(
      new THREE.Vector2(window.innerWidth * 0.5, window.innerHeight * 0.5),
      1.0, 0.7, 0.18);
    composer.addPass(bloom);

    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.06;
    controls.autoRotate = true; controls.autoRotateSpeed = 0.45;
    controls.minDistance = 24; controls.maxDistance = 220;
    controls.enablePan = false;

    raycaster = new THREE.Raycaster();
    pointer = new THREE.Vector2();
    renderer.domElement.addEventListener('click', handleClick);
    renderer.domElement.addEventListener('pointermove', handleHover);
    window.addEventListener('resize', onResize);

    animate();
    window.__war3dReady = true;
  }

  function update(state) {
    const swarms = (state.swarms || []).filter((s) => s.online);
    // reconcile hubs
    const livePaths = new Set(swarms.map((s) => s.path));
    Object.keys(hubs).forEach((p) => { if (!livePaths.has(p)) removeHub(p); });
    swarms.forEach((s, i) => {
      const pos = hubPosition(i, swarms.length);
      if (!hubs[s.path]) {
        hubs[s.path] = buildHub(s, pos);
      } else {
        hubs[s.path].pos.copy(pos);
        hubs[s.path].group.position.copy(pos);
        updateHub(hubs[s.path], s);
      }
    });
    buildLinks(state.links);
    window.__war3dHubs = Object.keys(hubs).length;
    window.__war3dAgents = agentPickables.length;
  }

  function setDefcon(level) { defcon = level || 5; }

  // ── TIME MACHINE ───────────────────────────────────────────────────────
  // Drive hub colors from a replayed {path: severity} snapshot instead of
  // live state. While replaying, updateHub() yields color control to us.
  function setReplay(sevByPath) {
    replayMode = true;
    Object.keys(hubs).forEach((path) => {
      const rec = hubs[path];
      const sev = sevByPath[path] || 'OK';
      const col = sevColor(sev);
      rec.sphere.material.color.setHex(col);
      rec.sphere.material.emissive.setHex(col);
      rec.shell.material.color.setHex(col);
    });
  }
  function clearReplay() { replayMode = false; }   // next live update recolors

  window.War3D = { init, update, setDefcon, setReplay, clearReplay, setWar };
})();
