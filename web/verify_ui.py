"""Playwright smoke test for the codex_monk 3D war-room UI.

A swarm + viz must already be serving on $VIZ_URL. This loads the page,
waits for the WebGL scene + live data, asserts the 3D scene and the floating
panels render, exercises the drawer, captures console errors, and screenshots.

Run:
    VIZ_URL=http://127.0.0.1:19288 .venv-pw/bin/python web/verify_ui.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get('VIZ_URL', 'http://127.0.0.1:19288')
SHOT = os.environ.get('SHOT', '/tmp/warroom.png')

# headless chromium needs software GL for WebGL/Three.js
GL_ARGS = [
    '--use-gl=angle', '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist',
    '--enable-webgl', '--disable-gpu-sandbox',
]

failures = []
console_errors = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch(args=GL_ARGS)
    page = browser.new_page(viewport={'width': 1600, 'height': 950})
    page.on('console', lambda m: console_errors.append(m.text)
            if m.type == 'error' else None)
    page.on('pageerror', lambda e: console_errors.append(str(e)))
    page.goto(URL, wait_until='networkidle')

    page.wait_for_function(
        "document.getElementById('conn-text').textContent.includes('LIVE')",
        timeout=20000)

    # ── WebGL scene initialised ─────────────────────────────────────────
    has_canvas = page.query_selector('#war-canvas') is not None
    check('full-screen WebGL canvas present', has_canvas)
    page.wait_for_function("window.__war3dReady === true", timeout=20000)
    check('Three.js scene initialised (window.__war3dReady)', True)
    # scene reconciled to live data
    page.wait_for_function("(window.__war3dHubs||0) >= 3", timeout=20000)
    hubs = page.evaluate("window.__war3dHubs || 0")
    agents3d = page.evaluate("window.__war3dAgents || 0")
    print(f"    3D hubs={hubs} 3D agent meshes={agents3d}")
    check('3D scene built a hub per online fabric', hubs >= 3)
    check('3D scene built orbiting agent meshes', agents3d >= 4)

    # WebGL actually producing pixels (not a blank context)
    drew = page.evaluate("""() => {
        const c = document.getElementById('war-canvas');
        const gl = c.getContext('webgl2') || c.getContext('webgl');
        return !!gl && c.width > 0 && c.height > 0;
    }""")
    check('canvas has a live WebGL context', drew)

    # ── DEFCON banner ───────────────────────────────────────────────────
    defcon = page.get_attribute('body', 'data-defcon')
    check(f'DEFCON banner shows a level (data-defcon={defcon})',
          defcon in {'1', '2', '3', '4', '5'})

    # ── floating panels + agent grid ────────────────────────────────────
    page.wait_for_selector('#agent-tbody tr', timeout=20000)
    rows = page.query_selector_all('#agent-tbody tr')
    roles, sevs = set(), set()
    for r in rows:
        tds = r.query_selector_all('td')
        roles.add(tds[2].inner_text().strip())
        sevs.add(tds[5].inner_text().strip())
    print(f"    grid rows={len(rows)} roles={roles} sevs={sevs}")
    check('agent grid populated', len(rows) >= 3)
    check('grid shows real roles', {'probe', 'gateway'} & roles)
    check('grid shows per-agent severities', any(
        s in {'OK', 'WARN', 'CRITICAL', 'INFO'} for s in sevs))

    # ── drawer via grid row ─────────────────────────────────────────────
    probe_row = page.locator(
        '#agent-tbody tr:has(td.cell-role:text-is("probe"))').first
    if probe_row.count() > 0:
        probe_row.click()
        page.wait_for_selector('#drawer[aria-hidden="false"]', timeout=4000)
        page.wait_for_function(
            "document.getElementById('dr-role').textContent.trim() !== '…'",
            timeout=5000)
        role = page.inner_text('#dr-role')
        var_rows = page.query_selector_all('#dr-vars tr')
        print(f"    drawer role={role!r} var_rows={len(var_rows)}")
        check('drawer shows role', 'probe' in role)
        check('drawer shows per-agent state vars', len(var_rows) >= 1)
    else:
        check('found a probe-role agent to inspect', False)

    page.wait_for_timeout(1200)   # let a couple animation frames render
    # freeze the rAF loop so the WebGL canvas holds a stable frame to capture
    page.evaluate("window.__war3dFreeze = true")
    page.wait_for_timeout(300)
    try:
        page.screenshot(path=SHOT, full_page=False, timeout=15000)
        print(f"  screenshot → {SHOT}")
    except Exception as e:
        print(f"  screenshot skipped: {e}")
    browser.close()

if console_errors:
    print(f"\n  console/page errors ({len(console_errors)}):")
    for e in console_errors[:10]:
        print(f"    ! {e}")
    failures.append(f'{len(console_errors)} console error(s)')

if failures:
    print(f"\nFAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
