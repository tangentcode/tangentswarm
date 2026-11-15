"""Private infra config: ~/.config/tangentswarm/config.toml (or $TANGENTSWARM_CONFIG).

Project node ids, the Status option ids, staging hosts and secret file paths are not in
the code (Michal 2026-11-15 2:04 PM ET). config.example.toml in the repo has the shape with
placeholders (tangentswarm/config.example.toml, package data); the tests run with it. With no config file the example is used and a warning
goes to stderr once, so a missing file shows instead of failing in silence.
"""
import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the tomli backport (a dependency there only)
    import tomli as tomllib

EXAMPLE = Path(__file__).resolve().parent / 'config.example.toml'  # package data: in the wheel too
_cache = {}


def path():
    env = os.environ.get('TANGENTSWARM_CONFIG')
    if env:
        return Path(env).expanduser()
    from . import paths
    return paths.config_dir() / 'config.toml'


def load():
    p = path()
    if p in _cache:
        return _cache[p]
    src = p if p.exists() else EXAMPLE
    if src == EXAMPLE and p != EXAMPLE:
        print(f"tangentswarm: no config at {p}: using the placeholders in {EXAMPLE.name}", file=sys.stderr)
    with open(src, 'rb') as f:
        _cache[p] = tomllib.load(f)
    return _cache[p]


def get(*keys, default=None):
    d = load()
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def files(*keys):
    """A list of paths from the config, with "~" expanded."""
    return [Path(os.path.expanduser(str(x))) for x in get(*keys, default=[]) or []]
