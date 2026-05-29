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
