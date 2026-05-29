"""
Probe plugin registry.

A probe is a pure-Python module that exposes:

  - sample_all() -> dict[str, float|int|bool]   the Frame (one tick's reading)
  - OPCODES: dict[str, dict[str, str]]          {first_char: {sig_char: frame_key}}
  - describe() -> str                            short, for boot banner

Probes register themselves at import time by calling `register(name, ...)`.
The DeclarativeAgent looks up its probe by config name (`probe: kernel`,
`probe: cgroup_pods`, etc.) and feeds the resolved (sample_all, OPCODES)
pair into the genome interpreter.

This is the only place codex_monk hard-codes a list of domains, and it
doesn't: domains add themselves by importing their module. New domain =
new probe module + register() call. No substrate edits.
"""

from typing import Callable, Dict, NamedTuple


class Probe(NamedTuple):
    name: str
    sample_all: Callable[[], dict]
    opcodes: Dict[str, Dict[str, str]]
    describe: Callable[[], str]


_REGISTRY: Dict[str, Probe] = {}


def register(name: str,
             sample_all: Callable[[], dict],
             opcodes: Dict[str, Dict[str, str]],
             describe: Callable[[], str] = None) -> None:
    """Add a probe to the registry. Called by each probe module on import."""
    if name in _REGISTRY:
        return                                  # idempotent re-import
    if describe is None:
        describe = lambda: name
    _REGISTRY[name] = Probe(name, sample_all, opcodes, describe)


def get(name: str) -> Probe:
    """Resolve a probe by name. Raises KeyError with the available list so
    boot-time misconfiguration fails loudly."""
    if name not in _REGISTRY:
        raise KeyError(
            f'unknown probe {name!r}; registered: {sorted(_REGISTRY)}')
    return _REGISTRY[name]


def list_probes() -> Dict[str, Probe]:
    return dict(_REGISTRY)


# Eagerly import built-in probes so they register themselves. New probe
# modules added here will be auto-discovered by `probes.list_probes()`.
# Imports happen at the BOTTOM to avoid circular reference (each probe
# module imports `register` from this module). Optional probes are
# wrapped — missing/broken ones don't break the framework.
from swarm.probes import kernel as _kernel  # noqa: F401,E402

for _name in ('cgroup_pods', 'disk_net', 'k8s_api', 'fabric_peer'):
    try:
        __import__(f'swarm.probes.{_name}')
    except ImportError:
        pass
