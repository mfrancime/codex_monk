"""
test_k8s_api.py — k8s_api probe against synthetic apiserver responses.

We sidestep real urllib by using the probe's CODEX_K8S_API_FAKE_PATH hook:
write a JSON file mapping endpoint → canned response, and the probe reads
from it instead of hitting an apiserver. Then assert the Frame keys are
populated as expected from the parsed response.

Three scenarios:
  1. Healthy cluster, all nodes Ready, no warnings, no degraded deployments.
  2. One node NotReady, two warning events, one degraded deployment.
  3. Apiserver unhealthy → all metrics zeroed, healthy=0.

Plus a genome interpret() check that fires CRITICAL when apiserver is down.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_k8s_api
"""

import json
import os
import sys
import tempfile

# Force the probe into the no-auth path so the resolved auth is empty AND
# the test responses come from our fake-table file.
os.environ['K8S_API_TOKEN'] = ''
os.environ['K8S_API_HOST']  = ''
os.environ.pop('KUBERNETES_SERVICE_HOST', None)

from swarm.probes import k8s_api    # noqa: E402
from swarm.probes import get as get_probe   # noqa: E402
from swarm.genome import interpret   # noqa: E402


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _write_fake_table(fake_path, **kwargs):
    """Write canned responses to fake_path. Keys are endpoint paths."""
    with open(fake_path, 'w') as f:
        json.dump(kwargs, f)


_EVENTS_PATH = '/api/v1/events?fieldSelector=type%3DWarning&limit=50'


def main():
    print()
    print('== k8s_api probe ==')

    p = get_probe('k8s_api')
    _check('probe registered',  p.name == 'k8s_api')
    _check('K opcodes present', 'K' in p.opcodes)
    # auth missing because we cleared env above
    _check('auth_present = 0 when no creds',
           p.sample_all()['k8s.api.auth_present'] == 0)

    # ── case 1: healthy ─────────────────────────────────────────────────
    fake = tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                        delete=False)
    fake.close()
    _write_fake_table(
        fake.name,
        **{
            '/healthz': {'status': 'ok'},
            '/api/v1/nodes': {'items': [
                {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
                {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
                {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
            ]},
            _EVENTS_PATH: {'items': []},
            '/apis/apps/v1/deployments': {'items': [
                {'status': {'replicas': 3, 'readyReplicas': 3}},
            ]},
        }
    )
    os.environ['CODEX_K8S_API_FAKE_PATH'] = fake.name
    os.environ['CODEX_K8S_API_FAKE_HEALTHY'] = '1'

    f = p.sample_all()
    _check('healthy: k8s.api.healthy = 1',          f['k8s.api.healthy'] == 1)
    _check('healthy: nodes.total = 3',              f['k8s.nodes.total'] == 3)
    _check('healthy: nodes.not_ready = 0',          f['k8s.nodes.not_ready'] == 0)
    _check('healthy: events.warnings_60s = 0',      f['k8s.events.warnings_60s'] == 0)
    _check('healthy: deployments.degraded = 0',     f['k8s.deployments.degraded'] == 0)

    # ── case 2: one node NotReady, warnings, degraded ───────────────────
    _write_fake_table(
        fake.name,
        **{
            '/healthz': {'status': 'ok'},
            '/api/v1/nodes': {'items': [
                {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
                {'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}},
                {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
            ]},
            _EVENTS_PATH: {'items': [
                {'type': 'Warning'},
                {'type': 'Warning'},
                {'type': 'Normal'},   # ignored — not a warning
            ]},
            '/apis/apps/v1/deployments': {'items': [
                {'status': {'replicas': 3, 'readyReplicas': 1}},   # degraded
                {'status': {'replicas': 2, 'readyReplicas': 2}},   # fine
            ]},
        }
    )
    f = p.sample_all()
    _check('degraded: nodes.not_ready = 1',         f['k8s.nodes.not_ready'] == 1)
    _check('degraded: warnings_60s = 2',            f['k8s.events.warnings_60s'] == 2)
    _check('degraded: deployments.degraded = 1',    f['k8s.deployments.degraded'] == 1)

    # genome: apiserver healthy AND any not_ready → CRIT
    genome = 'Ka0>Kx0>∧→Cd;'
    sev, code = interpret(genome, f, p.opcodes)
    _check('genome: healthy+not_ready → CRIT',      sev == 'CRITICAL')

    # ── case 3: unhealthy apiserver ─────────────────────────────────────
    os.environ['CODEX_K8S_API_FAKE_HEALTHY'] = '0'
    f = p.sample_all()
    _check('unhealthy: api.healthy = 0',            f['k8s.api.healthy'] == 0)
    _check('unhealthy: nodes.total = 0 (skipped)',  f['k8s.nodes.total'] == 0)

    # genome: apiserver unhealthy → CRIT GATE_DOWN. RPN pushes Ka, 1, <
    # → pop b=1, a=Ka → a<b means healthy<1, true when 0.
    genome = 'Ka1<→Cg;'
    sev, code = interpret(genome, f, p.opcodes)
    _check('genome: unhealthy fires GATE_DOWN',     sev == 'CRITICAL' and code == 'GATE_DOWN')

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    try:
        main()
    finally:
        fake_path = os.environ.get('CODEX_K8S_API_FAKE_PATH')
        if fake_path and os.path.exists(fake_path):
            os.unlink(fake_path)
