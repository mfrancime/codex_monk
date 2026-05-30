"""Playwright check for the EVOLUTION / WARGAME tab.

A viz server must be serving on $VIZ_URL with a web/wargame.json present.
Opens the page, toggles the WARGAME panel, asserts the per-front cards render
with both the discrete (1-D) and higher-dimensional (vector) champions, and
screenshots.

Run:
    VIZ_URL=http://127.0.0.1:19200 .venv-pw/bin/python web/verify_wargame.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get('VIZ_URL', 'http://127.0.0.1:19200')
SHOT = os.environ.get('SHOT', '/tmp/wargame_tab.png')

GL_ARGS = ['--use-gl=angle', '--use-angle=swiftshader',
           '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist',
           '--disable-gpu-sandbox']

failures = []
console_errors = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch(args=GL_ARGS)
    page = browser.new_page(viewport={'width': 1500, 'height': 950})
    page.on('console', lambda m: console_errors.append(m.text)
            if m.type == 'error' else None)
    page.on('pageerror', lambda e: console_errors.append(str(e)))
    page.goto(URL, wait_until='domcontentloaded')

    # the WARGAME toggle exists in the HUD
    toggle = page.query_selector('#evo-toggle')
    check('WARGAME HUD toggle present', toggle is not None)

    page.click('#evo-toggle')
    page.wait_for_function(
        "document.getElementById('evopanel').getAttribute('aria-hidden') === 'false'",
        timeout=10000)
    check('WARGAME panel opens', True)

    # fronts render
    page.wait_for_selector('.evo-front', timeout=10000)
    fronts = page.query_selector_all('.evo-front')
    print(f"    fronts rendered: {len(fronts)}")
    check('at least one front card renders', len(fronts) >= 1)

    # discrete + higher-dimensional champion rows both present
    disc = page.query_selector_all('.evo-champ:not(.vec)')
    vec = page.query_selector_all('.evo-champ.vec')
    print(f"    discrete champion rows: {len(disc)} · vector(ℝⁿ) rows: {len(vec)}")
    check('discrete (1-D) champion rows render', len(disc) >= 1)
    check('higher-dimensional (vector) champion rows render', len(vec) >= 1)

    # at least one genome string actually shows
    gene = page.query_selector('.evo-gene')
    gene_txt = gene.inner_text() if gene else ''
    print(f"    sample champion genome: «{gene_txt}»")
    check('a champion genome string is shown', bool(gene_txt.strip()))

    # rung progress + lineage present
    check('rung progress pips render', len(page.query_selector_all('.evo-pip')) >= 1)
    check('lineage rows render', len(page.query_selector_all('.evo-lin')) >= 1)

    # per-team stats strip
    stats = page.query_selector_all('.evo-stats')
    statcells = page.query_selector_all('.evo-stat')
    print(f"    stats strips: {len(stats)} · stat cells: {len(statcells)}")
    check('per-team stats strip renders', len(stats) >= 1)
    check('stat cells render (ROUNDS/WIN%/BROKEN/DNA/LEN/...)', len(statcells) >= 6)

    # RUN ROUND button present + wires up (click → state change to running/busy)
    runbtn = page.query_selector('#evo-run')
    check('RUN ROUND button present', runbtn is not None)
    before = runbtn.inner_text()
    page.click('#evo-run')
    page.wait_for_timeout(1200)
    after = page.inner_text('#evo-run')
    print(f"    RUN ROUND: '{before}' -> '{after}'")
    check('RUN ROUND click triggers a round (running/busy state)',
          after != before and ('running' in after.lower() or 'busy' in after.lower()))

    # sub-header reflects rounds
    sub = page.inner_text('#evo-sub')
    print(f"    sub-header: {sub}")
    check('sub-header shows rounds/fronts', 'front' in sub.lower())

    page.screenshot(path=SHOT, full_page=False)
    print(f"  screenshot → {SHOT}")
    browser.close()

if console_errors:
    print("  console errors:")
    for e in console_errors[:10]:
        print("    -", e)

print()
if failures:
    print(f"WARGAME-TAB VERIFY: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("WARGAME-TAB VERIFY: ALL PASS")
