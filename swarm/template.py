"""
Agent Template — Declarative spec for any agent.

codex_monk has ONE registered agent class: `DeclarativeAgent`. New behaviors
are NOT new Python classes — they are new genome strings + new YAML config
blocks. If you find yourself adding a second template here, stop: that's
probably a new opcode or a new probe.kind, not a new agent class.

Usage in swarm.yaml:

  agents:
    - type: declarative
      id: 7
      priority: 1
      config:
        genome: "ψs‡10>→Ww;"        # the gate, as alien-RPN
        narrator_id: 1
        calm_interval: 10
        alert_interval: 1

    - type: declarative
      id: 1
      priority: 2
      config:
        consume_types: [700]
        persist_path: graph/alerts.jsonl
"""

from swarm.fabric import SIG_USR1, SIG_USR2


class AgentTemplate:
    """Declarative spec for an agent type."""

    __slots__ = (
        'name', 'description',
        'accepts', 'emits', 'signals',
        'state_keys', 'config_schema',
        'capabilities', 'priority_default',
    )

    def __init__(self, name, description='',
                 accepts=None, emits=None, signals=None,
                 state_keys=None, config_schema=None,
                 capabilities=None, priority_default=2):
        self.name = name
        self.description = description
        self.accepts = accepts or []
        self.emits = emits or []
        self.signals = signals or []
        self.state_keys = state_keys or []
        self.config_schema = config_schema or {}
        self.capabilities = capabilities or []
        self.priority_default = priority_default

    def validate_config(self, config):
        errors = []
        for key, spec in self.config_schema.items():
            required = spec.get('required', False)
            default = spec.get('default')
            if key not in config:
                if required:
                    errors.append(f'missing required config: {key}')
                elif default is not None:
                    config[key] = default
        return errors

    def to_dict(self):
        return {
            'name': self.name,
            'description': self.description,
            'accepts': self.accepts,
            'emits': self.emits,
            'signals': self.signals,
            'state_keys': self.state_keys,
            'config_schema': self.config_schema,
            'capabilities': self.capabilities,
        }


# ══════════════════════════════════════════════
#  THE ONLY TEMPLATE
# ══════════════════════════════════════════════

DECLARATIVE_TEMPLATE = AgentTemplate(
    name='declarative',
    description='The only agent in codex_monk. Behavior is its genome + config; '
                'gates, sinks, side-effects all live in YAML.',
    accepts=[100, 700],
    emits=[700],
    signals=[SIG_USR1, SIG_USR2],
    state_keys=['sys.*', 'sink.*', 'dna.*'],
    config_schema={
        'genome':         {'type': 'str',  'default': ''},
        'narrator_id':    {'type': 'int',  'default': None},
        'calm_interval':  {'type': 'int',  'default': 10},
        'alert_interval': {'type': 'int',  'default': 1},
        'consume_types':  {'type': 'list', 'default': []},
        'persist_path':   {'type': 'str',  'default': None},
    },
    capabilities=['telemetry', 'psi', 'gate', 'sink', 'declarative'],
    priority_default=1,
)


# ══════════════════════════════════════════════
#  REGISTRY
# ══════════════════════════════════════════════

_REGISTRY = {}


def register(template, agent_class):
    _REGISTRY[template.name] = {'template': template, 'class': agent_class}


def get_template(name):
    entry = _REGISTRY.get(name)
    return entry['template'] if entry else None


def get_class(name):
    entry = _REGISTRY.get(name)
    return entry['class'] if entry else None


def list_types():
    return {name: entry['template'].to_dict()
            for name, entry in _REGISTRY.items()}


def create_agent(cls_name, agent_id, agent_type, priority, config):
    entry = _REGISTRY.get(cls_name)
    if not entry:
        raise ValueError(f'Unknown agent type: {cls_name}. '
                         f'Registered: {list(_REGISTRY.keys())}')

    template = entry['template']
    cls = entry['class']

    errors = template.validate_config(config)
    if errors:
        raise ValueError(f'Agent {cls_name} config errors: {errors}')

    import inspect
    sig = inspect.signature(cls.__init__)
    params = set(sig.parameters.keys()) - {'self'}
    kwargs = {k: v for k, v in config.items() if k in params}

    return cls(agent_id, agent_type, priority, **kwargs)


def _register_builtins():
    from swarm.agents.declarative import DeclarativeAgent
    register(DECLARATIVE_TEMPLATE, DeclarativeAgent)


_register_builtins()
