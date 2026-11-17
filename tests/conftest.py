import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# the private infra config: the tests use the placeholders of config.example.toml, never the real file
os.environ['TANGENTSWARM_CONFIG'] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 'tangentswarm', 'config.example.toml')


from tangentswarm import deploy as _deploy  # noqa: E402
_REAL_DEPLOY_START = _deploy.start


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep logs, auth db and config out of the real home directory. HOME points at an
    empty temp dir, so a launch in a test can never pre-trust anything in the real
    ~/.claude.json or ~/.codex/config.toml, and nothing watches a real tmux pane."""
    monkeypatch.setenv('TANGENTSWARM_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TANGENTSWARM_CONFIG_DIR', str(tmp_path / 'config'))
    (tmp_path / 'home').mkdir(exist_ok=True)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    for var in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
        monkeypatch.delenv(var, raising=False)
    from tangentswarm import worktrees
    monkeypatch.setattr(worktrees, '_watch_startup',
                        lambda pane, agent, wait=None: {'state': 'unwatched', 'blocked': None})
    for var in list(os.environ):
        if var.startswith(('TANGENTSWARM_AUTH_', 'TANGENTSWARM_API_KEY', 'TANGENTSWARM_AGENT_')):
            monkeypatch.delenv(var, raising=False)
    from tangentswarm import board
    monkeypatch.setattr(board, 'FT_STATE', tmp_path / 'swarm-fasttrack.json')
    drafted = []  # tangentswarm #95: never a real gh pr ready --undo in a test
    monkeypatch.setattr(board, 'REAL_MAKE_DRAFT', board.make_draft, raising=False)
    monkeypatch.setattr(board, 'make_draft', lambda n, repo, sh=None: drafted.append((n, repo)) or True)
    monkeypatch.setattr(board, 'DRAFTED', drafted, raising=False)
    from tangentswarm import moves
    monkeypatch.setattr(moves, 'LOG', tmp_path / 'moves.log')
    monkeypatch.setattr(moves, 'SEEN', tmp_path / 'ready-seen.json')
    monkeypatch.setattr(moves, 'MERGES', tmp_path / 'merge-clock.json')
    monkeypatch.setattr(moves, 'CLOCK_LOG', tmp_path / 'merge-to-move.log')
    from tangentswarm import conflictmap
    monkeypatch.setattr(conflictmap, 'PATH', tmp_path / 'conflict-map.json')
    from tangentswarm import boardcache
    monkeypatch.setattr(boardcache, 'PATH', tmp_path / 'board-snapshot.json')
    monkeypatch.setattr(boardcache, 'DETAILS', tmp_path / 'pr-details.json')
    monkeypatch.setattr(boardcache, 'SYNC_LOG', tmp_path / 'board-sync.log')
    from tangentswarm import gqlmeter
    monkeypatch.setattr(gqlmeter, 'LOG', tmp_path / 'gql.log')
    from tangentswarm import advance, gatelock
    monkeypatch.setattr(advance, 'RERUN', tmp_path / 'swarm-serial.rerun')
    monkeypatch.setattr(advance, 'CASCADE_LOG', tmp_path / 'cascade.log')
    from tangentswarm import flow
    monkeypatch.setattr(flow, 'RELAY_DIR', tmp_path)
    monkeypatch.setattr(flow, 'HELD', tmp_path / 'held.json')
    monkeypatch.setattr(flow, 'head_branch', lambda n: '')  # never a real gh pr view in a test
    monkeypatch.setattr(flow, 'PREMERGE_STATE', tmp_path / 'premerge.json')
    labels_written = []  # tangentswarm #65: never a real minbot label write in a test
    monkeypatch.setattr(flow, 'label_write', lambda n, op, names, repo=flow.REPO: labels_written.append((n, op, list(names))))
    flow.LABELS_WRITTEN = labels_written
    from tangentswarm import stophook
    from tangentswarm import spool
    from tangentswarm import webhook as _wh
    monkeypatch.setattr(_wh, 'LOG', tmp_path / 'webhook.log')
    from tangentswarm import deploy  # tangentswarm #113: never a real deploy unit in a test
    started = []
    monkeypatch.setattr(deploy, 'start', lambda sha, pr=None, **kw: started.append((sha, pr)) or f"started {sha[:8]}")
    monkeypatch.setattr(deploy, 'STARTED', started, raising=False)
    monkeypatch.setattr(deploy, 'REAL_START', _REAL_DEPLOY_START, raising=False)
    monkeypatch.setenv('SWARM_DEPLOY_STATE', str(tmp_path / 'deploy-state'))
    monkeypatch.setenv('SWARM_FLOW_LIVE', str(tmp_path / 'no-flow-live'))
    monkeypatch.setattr(_wh, 'INBOX', tmp_path / 'webhook-inbox')
    monkeypatch.setattr(_wh, 'SEEN', tmp_path / 'webhook-seen.json')
    monkeypatch.setattr(_wh, 'WAKE_URL_FILE', tmp_path / 'wake-url')
    monkeypatch.setattr(_wh, 'WAKE_KEY_FILE', tmp_path / 'wake-key')
    promoted_woken = []  # tangentswarm #67: never a real SIGUSR1 to the live swarm-promoted in a test
    monkeypatch.setattr(_wh, 'REAL_SIGNAL_PROMOTED', _wh.signal_promoted, raising=False)
    monkeypatch.setattr(_wh, 'signal_promoted', lambda: promoted_woken.append(1) or 'woken (test)')
    _wh.PROMOTED_WOKEN = promoted_woken
    from tangentswarm import eventdb
    monkeypatch.setattr(eventdb, 'PATH', str(tmp_path / 'webhook-events.sqlite'))
    monkeypatch.setattr(eventdb, '_store', None)
    from tangentswarm import events as _events
    monkeypatch.setattr(_events, 'EVENTS', tmp_path / 'github-events.jsonl')
    monkeypatch.setattr(_events, 'OFFSET', tmp_path / 'events.offset')
    monkeypatch.setattr(_events, 'MN_PENDING', tmp_path / 'mn-pending')
    monkeypatch.setattr(_events, 'MN_CONFIG', tmp_path / 'mn-workflow-config.json')  # absent: platform only
    monkeypatch.setattr(spool, 'SPOOL', tmp_path / 'spool')
    monkeypatch.setattr(advance, 'check_runs', lambda head, repo=None: [])  # no real GitHub read in a test
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
    from tangentswarm import promoted as _promoted
    monkeypatch.setattr(_promoted, 'BOARD_EVERY', float('inf'))  # no periodic board pass in a test
    from tangentswarm import slots, launches as _launches
    monkeypatch.setattr(_launches, 'OLD_HELD', tmp_path / 'swarm-held.json')  # never the real retired list
    monkeypatch.setattr(_launches, 'pr_of_branch', lambda branch: None)  # no real gh in a test
    monkeypatch.setattr(slots, 'worker_memory', lambda: None)  # never the real systemd slice in a test
    monkeypatch.setattr(slots, 'STATE', tmp_path / 'slots.json')
    monkeypatch.setattr(slots, 'SESSIONS', tmp_path / 'claude-sessions')
    monkeypatch.setattr(slots, 'PROJECTS', tmp_path / 'claude-projects')
    from tangentswarm import alerts, panewatch
    monkeypatch.setattr(alerts, 'STATE', tmp_path / 'watch.json')
    monkeypatch.setattr(alerts, 'ALERT_URL_FILE', tmp_path / 'alert-url')
    monkeypatch.setattr(alerts, 'ALERT_KEY_FILE', tmp_path / 'alert-key')
    monkeypatch.setattr(panewatch, 'REPORT', tmp_path / 'panes.json')
    yield tmp_path
