import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# the private infra config: the tests use the placeholders of config.example.toml, never the real file
os.environ['TANGENTSWARM_CONFIG'] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 'tangentswarm', 'config.example.toml')


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep logs, auth db and config out of the real home directory."""
    monkeypatch.setenv('TANGENTSWARM_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TANGENTSWARM_CONFIG_DIR', str(tmp_path / 'config'))
    for var in list(os.environ):
        if var.startswith(('TANGENTSWARM_AUTH_', 'TANGENTSWARM_API_KEY', 'TANGENTSWARM_AGENT_')):
            monkeypatch.delenv(var, raising=False)
    from tangentswarm import board
    monkeypatch.setattr(board, 'FT_STATE', tmp_path / 'swarm-fasttrack.json')
    from tangentswarm import moves
    monkeypatch.setattr(moves, 'LOG', tmp_path / 'moves.log')
    monkeypatch.setattr(moves, 'SEEN', tmp_path / 'ready-seen.json')
    from tangentswarm import conflictmap
    monkeypatch.setattr(conflictmap, 'PATH', tmp_path / 'conflict-map.json')
    from tangentswarm import boardcache
    monkeypatch.setattr(boardcache, 'PATH', tmp_path / 'board-snapshot.json')
    monkeypatch.setattr(boardcache, 'DETAILS', tmp_path / 'pr-details.json')
    from tangentswarm import gqlmeter
    monkeypatch.setattr(gqlmeter, 'LOG', tmp_path / 'gql.log')
    from tangentswarm import advance, gatelock
    monkeypatch.setattr(advance, 'RERUN', tmp_path / 'swarm-serial.rerun')
    monkeypatch.setattr(advance, 'CASCADE_LOG', tmp_path / 'cascade.log')
    from tangentswarm import flow
    monkeypatch.setattr(flow, 'RELAY_DIR', tmp_path)
    monkeypatch.setattr(flow, 'HELD', tmp_path / 'held.json')
    from tangentswarm import stophook
    from tangentswarm import spool
    from tangentswarm import webhook as _wh
    monkeypatch.setattr(_wh, 'LOG', tmp_path / 'webhook.log')
    monkeypatch.setattr(_wh, 'INBOX', tmp_path / 'webhook-inbox')
    monkeypatch.setattr(_wh, 'SEEN', tmp_path / 'webhook-seen.json')
    monkeypatch.setattr(_wh, 'WAKE_URL_FILE', tmp_path / 'wake-url')
    monkeypatch.setattr(_wh, 'WAKE_KEY_FILE', tmp_path / 'wake-key')
    from tangentswarm import eventdb
    monkeypatch.setattr(eventdb, 'PATH', str(tmp_path / 'webhook-events.sqlite'))
    monkeypatch.setattr(eventdb, '_store', None)
    from tangentswarm import events as _events
    monkeypatch.setattr(_events, 'EVENTS', tmp_path / 'github-events.jsonl')
    monkeypatch.setattr(_events, 'OFFSET', tmp_path / 'events.offset')
    monkeypatch.setattr(spool, 'SPOOL', tmp_path / 'spool')
    monkeypatch.setattr(advance, 'check_runs', lambda head: [])  # no real GitHub read in a test
    from tangentswarm import tick as _tick
    monkeypatch.setattr(_tick, 'STATE_DIR', tmp_path / 'tick-state')
    monkeypatch.setattr(stophook, 'LOG', tmp_path / 'stop-hook.log')
    monkeypatch.setattr(stophook, 'SEEN', tmp_path / 'stop-hook-seen.json')
    monkeypatch.setattr(stophook, 'LOCKS', tmp_path / 'stop-hook-locks')
    monkeypatch.setattr(flow, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setattr(flow, 'LOG', tmp_path / 'flow.log')
    monkeypatch.setattr(advance, 'schedule', lambda reset, **kw: 'not scheduled (test)')
    # a queued cascade would be run by a daemon test's drain as a REAL ~/bin/swarm-advance
    monkeypatch.setattr(advance, 'queue_cascade', lambda qdir=None: 'not queued (test)')
    monkeypatch.setattr(gatelock, 'QUEUE_DIR', tmp_path / 'gate-queue')
    from tangentswarm import slots
    monkeypatch.setattr(slots, 'worker_memory', lambda: None)  # never the real systemd slice in a test
    monkeypatch.setattr(slots, 'STATE', tmp_path / 'slots.json')
    monkeypatch.setattr(slots, 'SESSIONS', tmp_path / 'claude-sessions')
    monkeypatch.setattr(slots, 'PROJECTS', tmp_path / 'claude-projects')
    yield tmp_path
