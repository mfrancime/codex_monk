"""
Boot — Universal swarm launcher.

Two shapes are accepted:

  1. SINGLE SWARM (legacy, `swarm.yaml`):
       swarm: {id, name, region}
       agents: [...]
       gateway: {...}        # optional, vajrayana-style

  2. MULTISWARM (new, `multiswarm.yaml`):
       multiswarm: {id, name}
       security: {psk}
       swarms:
         - name: kernel
           include: swarms/kernel.yaml
           state_prefix: "swarm.kernel."
           fabric_path: "/dev/shm/codex.kernel.fabric"
           gateway: {id, bind, peers, routes}
         - name: evolver
           ...

In multiswarm mode, boot.py is a thin launcher: it spawns one
`multiprocessing.Process` per sub-swarm running `_run_one_swarm(sub_cfg)`.
Each child owns its own `Fabric` (at `fabric_path`) and `Orchestrator`,
which in turn spawns agent subprocesses against that fabric. The gateway
agent in each sub-swarm bridges the rest over VJR.

Usage:
  python boot.py                           # loads swarm.yaml (single)
  python boot.py swarm_wsl.yaml            # base swarm.yaml + override
  python boot.py --config my.yaml          # specific single-swarm config
  python boot.py --config multiswarm.yaml  # multiswarm mode
"""

import os
import sys
import multiprocessing

import yaml

from swarm.config import (
    load, load_multiswarm, is_multiswarm,
    swarm_id, swarm_name, agents, gateway, security,
)
from swarm.kernel import Orchestrator


def _run_one_swarm(cfg, label='swarm'):
    """The body of a single Orchestrator. Lives in this module so a child
    process can `multiprocessing.Process(target=_run_one_swarm, args=(cfg,))`
    against it cleanly. Same logic as legacy single-swarm boot; just made
    callable with a pre-built cfg dict instead of a yaml path."""

    sid    = swarm_id(cfg)
    sname  = swarm_name(cfg)
    region = cfg['swarm'].get('region', '')

    fabric_path = cfg.get('fabric_path')

    print()
    print('=' * 50)
    print(f'  CODEX_MONK — {label}')
    print(f'  {sname} (id={sid}) {region}')
    print('=' * 50)

    orch = Orchestrator(fabric_path=fabric_path)

    root = os.path.dirname(os.path.abspath(__file__))
    for agent in agents(cfg):
        atype    = agent['type']
        aid      = agent['id']
        priority = agent.get('priority', 2)
        acfg     = dict(agent.get('config', {}) or {})

        # resolve relative watch_dir
        if 'watch_dir' in acfg:
            wd = acfg['watch_dir']
            if not os.path.isabs(wd):
                wd = os.path.join(root, wd)
            os.makedirs(wd, exist_ok=True)
            acfg['watch_dir'] = wd

        # resolve relative persist_path
        if acfg.get('persist_path'):
            pp = acfg['persist_path']
            if not os.path.isabs(pp):
                pp = os.path.join(root, pp)
            d = os.path.dirname(pp)
            if d:
                os.makedirs(d, exist_ok=True)
            acfg['persist_path'] = pp

        orch.spawn(atype, agent_id=aid, agent_type=aid,
                   priority=priority, **acfg)

    # Legacy single-swarm `gateway:` block (vajrayana shape). In multiswarm
    # mode the gateway is already an entry in agents:, so this is skipped.
    gw = gateway(cfg)
    if gw.get('enabled', False):
        sec = security(cfg)
        orch.spawn('gateway',
                   agent_id=gw.get('id', 5),
                   agent_type=gw.get('id', 5),
                   priority=gw.get('priority', 1),
                   bind=f"{gw.get('host','127.0.0.1')}:{int(gw.get('port',19100))}",
                   psk=sec.get('psk', ''),
                   swarm_name=sname)

    print(f'\n  Swarm ID  : {sid}')
    print(f'  Name      : {sname}')
    print(f'  Agents    : {len(agents(cfg))}')
    print(f'  Fabric    : {orch.fabric.path}')
    print()
    print(f'  Ctrl+C to shutdown ({label}).')
    print()

    orch.run()


def _main_single(base_cfg, override):
    cfg = load(base_cfg, override)
    _run_one_swarm(cfg, label='single-swarm')


def _main_multi(cfg_path):
    ms = load_multiswarm(cfg_path)

    print()
    print('=' * 50)
    print(f'  CODEX_MONK — multiswarm')
    print(f'  {ms["multiswarm"]["name"]} (id={ms["multiswarm"]["id"]})')
    print(f'  Sub-swarms: {[s["swarm"]["name"] for s in ms["swarms"]]}')
    print('=' * 50)

    procs = []
    for sub in ms['swarms']:
        name = sub['swarm']['name']
        p = multiprocessing.Process(
            target=_run_one_swarm, args=(sub, f'swarm:{name}'),
            name=f'codex-{name}', daemon=False)
        p.start()
        procs.append((name, p))
        print(f'  [boot] launched sub-swarm {name!r} pid={p.pid}')

    try:
        for _, p in procs:
            p.join()
    except KeyboardInterrupt:
        print('\n  [boot] interrupt — stopping sub-swarms')
        for name, p in procs:
            if p.is_alive():
                p.terminate()
        for _, p in procs:
            p.join(timeout=5)


def _peek_kind(path):
    """Cheap pre-read: figure out if `path` is a single-swarm or multiswarm
    config without going through the validating loader."""
    with open(path, 'r', encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}
    return 'multiswarm' if is_multiswarm(raw) else 'single'


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    base_cfg = os.path.join(root, 'swarm.yaml')
    override = None

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '--config':
            base_cfg = sys.argv[2]
        else:
            override = arg
            if not os.path.isabs(override):
                override = os.path.join(root, override)

    if not os.path.isabs(base_cfg):
        base_cfg = os.path.join(root, base_cfg)

    if not os.path.exists(base_cfg):
        print(f'  [boot] config not found: {base_cfg}', file=sys.stderr)
        sys.exit(2)

    kind = _peek_kind(base_cfg)
    if kind == 'multiswarm':
        if override is not None:
            print('  [boot] note: override yaml is ignored in multiswarm mode',
                  file=sys.stderr)
        _main_multi(base_cfg)
    else:
        _main_single(base_cfg, override)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
