"""The private infra config (Michal 2026-11-15 2:04 PM ET)."""
import builtins
import importlib
import sys
import tomllib
import types
from pathlib import Path

from tangentswarm import config


def test_the_example_is_package_data_and_has_every_column():
    from tangentswarm import board
    assert config.EXAMPLE.parent == Path(config.__file__).resolve().parent  # inside the package
    pyproject = tomllib.loads((Path(__file__).parent.parent / 'pyproject.toml').read_text())
    assert 'config.example.toml' in pyproject['tool']['setuptools']['package-data']['tangentswarm']
    assert all(board.OPTION[c] for c in board.COLUMNS) and len(set(board.OPTION.values())) == len(board.COLUMNS)


def test_a_missing_config_falls_back_to_the_example(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('TANGENTSWARM_CONFIG', str(tmp_path / 'none.toml'))
    assert config.get('project', 'id') == 'PVT_example'
    assert 'no config at' in capsys.readouterr().err


def test_python_310_uses_the_tomli_backport(monkeypatch):
    """Codex on PR #26: on Python 3.10 (supported) `import tomllib` failed at import time."""
    real_import = builtins.__import__

    def no_tomllib(name, *a, **kw):
        if name == 'tomllib':
            raise ModuleNotFoundError("No module named 'tomllib'")
        return real_import(name, *a, **kw)
    monkeypatch.setitem(sys.modules, 'tomli', types.SimpleNamespace(load=tomllib.load, loads=tomllib.loads))
    monkeypatch.delitem(sys.modules, 'tomllib')
    monkeypatch.setattr(builtins, '__import__', no_tomllib)
    try:
        mod = importlib.reload(config)
        assert mod.tomllib is sys.modules['tomli']
    finally:
        monkeypatch.undo()
        importlib.reload(config)
