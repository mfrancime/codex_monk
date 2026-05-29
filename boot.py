"""
Boot — Universal swarm launcher. Reads swarm.yaml config.

Usage:
  python boot.py                       # loads swarm.yaml
  python boot.py swarm_wsl.yaml        # loads swarm.yaml + override
  python boot.py --config my.yaml      # loads specific config
"""

import os
import sys
import multiprocessing

from swarm.config import load, swarm_id, swarm_name, agents, gateway, security
from swarm.kernel import Orchestrator


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    base_cfg = os.path.join(root, 'swarm.yaml')

    # determine override config
    override = None
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '--config':
            base_cfg = sys.argv[2]
        else:
            override = arg
            if not os.path.isabs(override):
                override = os.path.join(root, override)

    cfg = load(base_cfg, override)

    sid = swarm_id(cfg)
    sname = swarm_name(cfg)
    region = cfg['swarm'].get('region', '')

    print()
    print('=' * 50)
    print(f'  CODEX_MONK — agentic OS / monitor swarm')
    print(f'  {sname} (id={sid}) {region}')
    print('=' * 50)

    orch = Orchestrator()

    # spawn agents from config
    for agent in agents(cfg):
        atype = agent['type']
        aid = agent['id']
        priority = agent.get('priority', 2)
        acfg = agent.get('config', {})

        # resolve relative watch_dir
        if 'watch_dir' in acfg:
            wd = acfg['watch_dir']
            if not os.path.isabs(wd):
                wd = os.path.join(root, wd)
            os.makedirs(wd, exist_ok=True)
            acfg['watch_dir'] = wd

        orch.spawn(atype, agent_id=aid, agent_type=aid,
                   priority=priority, **acfg)

    # spawn gateway from config
    gw = gateway(cfg)
    if gw.get('enabled', False):
        sec = security(cfg)
        orch.spawn('gateway',
                   agent_id=gw.get('id', 5),
                   agent_type=gw.get('id', 5),
                   priority=gw.get('priority', 1),
                   mode=gw.get('mode', 'server'),
                   host=gw.get('host', 'localhost'),
                   port=int(gw.get('port', 19100)),
                   swarm_id=sid,
                   swarm_name=sname,
                   psk=sec.get('psk', ''))

    print(f'\n  Swarm ID  : {sid}')
    print(f'  Name      : {sname}')
    print(f'  Agents    : {len(agents(cfg))}' +
          (' + gateway' if gw.get('enabled') else ''))
    print(f'  Fabric    : {orch.fabric.path}')
    if gw.get('enabled'):
        print(f'  Gateway   : {gw["mode"]} {gw.get("host","0.0.0.0")}:{gw.get("port",19100)}')
    print()
    print('  Ctrl+C to shutdown.')
    print()

    orch.run()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
