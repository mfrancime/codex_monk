"""Playwright smoke test for the codex_monk war-room UI.

Boots is handled by the caller (a swarm + viz must already be serving on
$VIZ_URL). This script loads the page, waits for live data, asserts the new
per-agent / topology / drawer features render, and writes a screenshot.

Run:
    VIZ_URL=http://127.0.0.1:19288 .venv-pw/bin/python web/verify_ui.py
"""
import os
import sys

from playwright.sync_api import sync_playwright, expect

URL = os.environ.get('VIZ_URL', 'http://127.0.0.1:19288')
SHOT = os.environ.get('SHOT', '/tmp/warroom.png')

failures = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1600, 'height': 950})
    page.goto(URL, wait_until='networkidle')

    # connection should go LIVE within a couple poll cycles
    page.wait_for_function(
        "document.getElementById('conn-text').textContent.includes('LIVE')",
        timeout=8000)
    # agent grid populates
    page.wait_for_selector('#agent-tbody tr', timeout=8000)

    # ── DEFCON banner ───────────────────────────────────────────────────
    defcon = page.get_attribute('body', 'data-defcon')
    lvl = page.inner_text('#defcon-lvl')
    check(f'DEFCON banner shows a level (data-defcon={defcon}, lvl={lvl})',
          defcon in {'1', '2', '3', '4', '5'} and lvl == defcon)

    # ── agent grid: roles + per-agent sev (NOT hardcoded #7) ────────────
    rows = page.query_selector_all('#agent-tbody tr')
    roles = set()
    sevs = set()
    for r in rows:
        tds = r.query_selector_all('td')
        roles.add(tds[2].inner_text().strip())
        sevs.add(tds[5].inner_text().strip())
    print(f"    rows={len(rows)} roles={roles} sevs={sevs}")
    check('grid has multiple agent rows', len(rows) >= 3)
    check('grid shows real roles (probe/sink/gateway)',
          {'probe', 'gateway'} & roles)
    check('grid shows a per-agent severity beyond a single hardcoded agent',
          any(s in {'OK', 'WARN', 'CRITICAL', 'INFO'} for s in sevs))

    # ── topology: agent circles + real VJR links ────────────────────────
    agent_dots = page.query_selector_all('#topo-svg .topo-agent')
    vjr_links = page.query_selector_all('#topo-svg .topo-vjr-link')
    print(f"    topo agents={len(agent_dots)} vjr_links={len(vjr_links)}")
    check('topology drew agent nodes', len(agent_dots) >= 3)
    check('topology drew real VJR gateway links', len(vjr_links) >= 1)

    # ── drawer: click a probe agent, check role + vars table ────────────
    # Use a locator (re-resolved at click time) — the grid re-renders every
    # poll, so a captured ElementHandle would detach.
    probe_row = page.locator(
        '#agent-tbody tr:has(td.cell-role:text-is("probe"))').first
    has_probe = probe_row.count() > 0
    check('found a probe-role agent to inspect', has_probe)
    if has_probe:
        probe_row.click()
        page.wait_for_selector('#drawer[aria-hidden="false"]', timeout=4000)
        page.wait_for_function(
            "document.getElementById('dr-role').textContent.trim() !== '…'",
            timeout=5000)
        role = page.inner_text('#dr-role')
        var_rows = page.query_selector_all('#dr-vars tr')
        genome = page.inner_text('#dr-genome')
        print(f"    drawer role={role!r} var_rows={len(var_rows)} "
              f"genome_len={len(genome)}")
        check('drawer shows role', 'probe' in role)
        check('drawer shows per-agent state vars', len(var_rows) >= 1)

    page.screenshot(path=SHOT, full_page=False)
    print(f"  screenshot → {SHOT}")
    browser.close()

if failures:
    print(f"\nFAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
