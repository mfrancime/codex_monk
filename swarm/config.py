"""
Config — Load and resolve swarm configuration from YAML.

Supports:
  - Environment variable substitution: ${VAR:default}
  - Config inheritance: base config + override
  - Validation of required fields
"""

import os
import re
import copy
import yaml


_ENV_RE = re.compile(r'\$\{(\w+)(?::([^}]*))?\}')


def _resolve_env(value):
    """Replace ${VAR:default} with environment variable or default."""
    if not isinstance(value, str):
        return value
    def _sub(m):
        var = m.group(1)
        default = m.group(2) or ''
        return os.environ.get(var, default)
    return _ENV_RE.sub(_sub, value)


def _resolve_deep(obj):
    """Recursively resolve env vars in a config dict."""
    if isinstance(obj, dict):
        return {k: _resolve_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_deep(v) for v in obj]
    return _resolve_env(obj)


def _deep_merge(base, override):
    """Merge override into base. Override wins for scalars, merges dicts."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def load(config_path, override_path=None):
    """
    Load swarm config from YAML.

    Args:
        config_path: path to base swarm.yaml
        override_path: optional path to override yaml (merged on top)

    Returns:
        resolved config dict
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    if override_path and os.path.exists(override_path):
        with open(override_path, 'r', encoding='utf-8') as f:
            override = yaml.safe_load(f)
        if override:
            cfg = _deep_merge(cfg, override)

    cfg = _resolve_deep(cfg)
    _validate(cfg)
    return cfg


def _validate(cfg):
    """Basic validation."""
    assert 'swarm' in cfg, 'missing swarm section'
    assert 'id' in cfg['swarm'], 'missing swarm.id'
    assert 'name' in cfg['swarm'], 'missing swarm.name'
    assert 'agents' in cfg, 'missing agents section'
    ids = set()
    for agent in cfg['agents']:
        assert 'type' in agent, f'agent missing type: {agent}'
        assert 'id' in agent, f'agent missing id: {agent}'
        aid = agent['id']
        assert aid not in ids, f'duplicate agent id: {aid}'
        ids.add(aid)


# ── Multiswarm loader ─────────────────────────────────

def is_multiswarm(cfg):
    """True if the config file is a multiswarm composition (root key
    `multiswarm:` + `swarms:` list of sub-swarms). False if it's a single
    swarm config."""
    return 'multiswarm' in cfg and 'swarms' in cfg


def load_multiswarm(config_path):
    """Load a multiswarm composition and return a list of fully-resolved
    sub-swarm configs. Each entry is the same shape `load()` returns for
    a single swarm, plus `state_prefix` + `fabric_path` injected, and a
    gateway agent appended if the sub-swarm declared one.

    Schema:
        multiswarm: { id, name }
        security: { psk: "${VJR_PSK:default}" }
        swarms:
          - name: kernel
            include: swarms/kernel.yaml
            state_prefix: "swarm.kernel."
            fabric_path: "/dev/shm/codex.kernel.fabric"
            gateway:
              id: 9
              bind: "127.0.0.1:19101"
              peers: [{name, addr}, ...]
              routes: [{type, peer, agent}, ...]
    """
    with open(config_path, 'r', encoding='utf-8') as f:
        root = yaml.safe_load(f)
    root = _resolve_deep(root)

    assert 'multiswarm' in root, 'missing multiswarm: section'
    assert 'swarms' in root, 'missing swarms: list'
    assert isinstance(root['swarms'], list), 'swarms: must be a list'

    ms_meta = root['multiswarm']
    multiswarm_id   = int(ms_meta.get('id', 1))
    multiswarm_name = str(ms_meta.get('name', 'multiswarm'))

    psk = root.get('security', {}).get('psk', '')

    base_dir = os.path.dirname(os.path.abspath(config_path))

    sub_swarms = []
    for entry in root['swarms']:
        name         = str(entry['name'])
        include_path = entry['include']
        state_prefix = str(entry.get('state_prefix', f'swarm.{name}.'))
        fabric_path  = entry.get('fabric_path')

        if not os.path.isabs(include_path):
            include_path = os.path.join(base_dir, include_path)
        with open(include_path, 'r', encoding='utf-8') as f:
            sub = yaml.safe_load(f) or {}
        sub = _resolve_deep(sub)

        agents_list = list(sub.get('agents', []))

        # Inject state_prefix into every agent's config so DeclarativeAgent
        # / GatewayAgent see it at construction.
        for a in agents_list:
            a.setdefault('config', {})
            a['config']['state_prefix'] = state_prefix

        # Optional inline gateway agent declared by the multiswarm entry.
        gw = entry.get('gateway')
        if gw is not None:
            gw_cfg = {
                'swarm_name': name,
                'bind':       gw.get('bind', '127.0.0.1:0'),
                'peers':      gw.get('peers', []),
                'routes':     gw.get('routes', []),
                'psk':        psk,
                'state_prefix': state_prefix,
            }
            agents_list.append({
                'type': 'gateway',
                'id':   int(gw['id']),
                'priority': int(gw.get('priority', 1)),
                'config': gw_cfg,
            })

        sub_cfg = {
            'swarm': {
                'id':     int(sub.get('swarm', {}).get('id', multiswarm_id)),
                'name':   name,
                'region': sub.get('swarm', {}).get(
                    'region', ms_meta.get('region', '')),
            },
            'agents':       agents_list,
            'state_prefix': state_prefix,
            'fabric_path':  fabric_path,
            'security':     {'psk': psk},
        }
        _validate(sub_cfg)
        sub_swarms.append(sub_cfg)

    return {
        'multiswarm': {
            'id': multiswarm_id, 'name': multiswarm_name,
        },
        'swarms': sub_swarms,
    }


# ── Convenience accessors ─────────────────────

def swarm_id(cfg):
    return cfg['swarm']['id']

def swarm_name(cfg):
    return cfg['swarm']['name']

def agents(cfg):
    return cfg.get('agents', [])

def gateway(cfg):
    return cfg.get('gateway', {})

def security(cfg):
    return cfg.get('security', {})

def fabric_cfg(cfg):
    return cfg.get('fabric', {})

def graph_cfg(cfg):
    return cfg.get('graph', {})
