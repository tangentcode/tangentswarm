"""tangentswarm #32 (Michal 2026-11-15 2:43 PM ET): one events DB for everything the swarm does."""
import json
import sqlite3
import subprocess

from tangentswarm import board, eventdb, events_view as V, proc


def rows(**where):
    return [r for r in eventdb.store().rows() if all(r.get(k) == v for k, v in where.items())]


def test_the_old_webhook_db_is_renamed_once_and_its_rows_are_webhook_rows(tmp_path):
    old, new = tmp_path / 'webhook-events.sqlite', tmp_path / 'events.sqlite'
    st = eventdb.EventStore(old)
    st.add('handled', 'd1', 'pull_request')
    st._db.close()
    s2 = eventdb.EventStore(new, old=str(old))
    assert not old.exists() and new.exists()
    (r,) = s2.rows()
    assert r['delivery'] == 'd1' and r['source'] == 'webhook' and r['actor'] is None
    s2.add_event('launcher', 'launch', pr=601, actor='b', notes=['opened draft PR #601'])
    assert s2.rows("source='launcher'")[0]['pr'] == 601
    db = sqlite3.connect(new)
    try:
        db.execute("DELETE FROM events")
        assert False
    except sqlite3.DatabaseError as e:
        assert 'append-only' in str(e)


def test_log_event_never_raises(monkeypatch):
    monkeypatch.setattr(eventdb, 'store', lambda: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    assert eventdb.log_event('x', source='mcp') is None


def test_minbot_calls_and_gh_writes_are_logged_and_reads_are_not():
    k = eventdb.command_kind
    assert k(['minbot', '--apply', 'evidence', '7']) == 'minbot' and k(['minbot', 'api', 'graphql']) is None
    assert k(['gh', 'pr', 'comment', '7', '--body', 'x']) == 'gh' and k(['gh', 'pr', 'view', '7']) is None
    assert k(['gh', 'api', '-X', 'PATCH', 'repos/o/r/pulls/7']) == 'gh' and k(['gh', 'api', 'repos/o/r']) is None
    assert k(['gh', 'project', 'item-edit', '--id', 'I']) is None  # board.move_card logs moves
    fake = lambda a, **kw: subprocess.CompletedProcess(a, 0, '', '')  # noqa: E731
    proc.sh('minbot', '--apply', 'evidence', '537', '--title', 'MinBot pass', who='swarm-advance', run=fake)
    proc.sh('gh', 'pr', 'view', '537', run=fake)
    (r,) = rows(source='minbot')
    assert r['pr'] == 537 and r['action'] == 'evidence 537 MinBot pass' and r['actor'] == 'swarm-advance'
    assert rows(source='gh') == []


def test_a_card_move_is_logged_with_its_pr():
    calls = []
    board.move_card({'project_id': lambda: 'P', 'STATUS_FIELD': 'F'}, 'I7', board.READY, calls.append, n=7)
    (r,) = rows(source='board')
    assert r['event'] == 'move' and r['action'] == board.READY and r['pr'] == 7


def test_a_stop_hook_run_is_one_row_with_its_decision(tmp_path, monkeypatch):
    from tangentswarm import slots, stophook
    wt = tmp_path / 'platform.x'
    wt.mkdir()
    monkeypatch.setattr(slots, 'is_platform_worktree', lambda c: True)
    monkeypatch.setattr(stophook, '_run', lambda *a: stophook.say('#7 head abc pushed; nothing to do'))
    monkeypatch.setattr(stophook, 'LOCKS', tmp_path / 'locks')
    stophook.run(wt)
    (r,) = rows(source='stop-hook')
    assert r['pr'] == 7 and r['actor'] == 'platform.x' and 'nothing to do' in json.loads(r['notes'])[0]


def test_a_worker_mcp_call_is_logged(monkeypatch, tmp_path):
    from tangentswarm import spool, worker_mcp
    monkeypatch.setattr(worker_mcp, 'worktree', lambda: str(tmp_path / 'platform.y'))
    monkeypatch.setattr(spool, 'submit', lambda op, wt, wait=None: {'ok': True, 'pr': 9, 'note': 'handed off'})
    assert worker_mcp.via_daemon('handoff')['ok']
    (r,) = rows(source='mcp')
    assert r['action'] == 'handoff' and r['pr'] == 9 and r['actor'] == 'platform.y' and r['error'] is None


def test_the_viewer_filters_by_source(tmp_path):
    import io
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    st.add('handled', 'd1', 'pull_request')
    st.add_event('launcher', 'launch', pr=601, actor='b', notes=['opened draft PR #601'])
    out = io.StringIO()
    V.run(str(tmp_path / 'e.sqlite'), once=True, color=False, out=out, sources=('launcher',))
    (line,) = out.getvalue().splitlines()
    assert 'launcher' in line and '#601' in line and 'opened draft PR #601' in line
    out = io.StringIO()
    V.run(str(tmp_path / 'e.sqlite'), once=True, color=False, out=out)
    assert len(out.getvalue().splitlines()) == 2
