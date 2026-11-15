"""swarm-events: the console viewer of the event DB (Michal 2026-11-15 1:56 PM ET)."""
import io
import json
import sqlite3
from datetime import datetime

from tangentswarm import eventdb, events_view as V

REPO = {'full_name': 'tangentcode/platform'}


def make(tmp_path):
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    p = {'action': 'submitted', 'repository': REPO, 'pull_request': {'number': 537}, 'sender': {'login': 'codex'}}
    st.add_received('d1', 'pull_request_review', p, {}, json.dumps(p).encode(), True, decisions=['codex'])
    st.add_received('d1', 'pull_request_review', p, {}, b'{}', True, decisions=['codex'])
    st.add_received('d2', 'push', {'repository': REPO}, {}, b'{}', True, reason='log-only', decisions=['log-only'])
    st.add('handled', 'd1', 'pull_request_review', p, decisions=['codex'], notes=['#537 -> Ready'], error='gh: boom')
    return st, str(tmp_path / 'e.sqlite')


def test_lines_color_errors_red_and_dim_duplicates_and_log_only(tmp_path):
    st, path = make(tmp_path)
    out = io.StringIO()
    assert V.run(path, once=True, color=True, out=out) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 4
    assert 'pull_request_review/submitted' in lines[0] and 'tangentcode/platform#537' in lines[0]
    assert 'codex -> queued' in lines[0] and 'sig ok' in lines[0] and '\033' not in lines[0]
    assert lines[1].startswith(V.DIM) and '(duplicate)' in lines[1] and 'dropped as duplicate' in lines[1]
    assert lines[2].startswith(V.DIM) and 'log-only' in lines[2]
    assert f'{V.RED}ERROR: gh: boom' in lines[3] and 'error, moved card to Ready' in lines[3]


def test_follow_prints_only_new_rows_and_v_prints_the_json(tmp_path):
    st, path = make(tmp_path)
    out = io.StringIO()
    added = []

    def sleep(s):
        if not added:
            added.append(st.add('handled', 'd9', 'issues', {'action': 'opened', 'repository': REPO,
                                                          'issue': {'number': 9}}))
    V.run(path, verbose=True, color=False, out=out, sleep=sleep, polls=2)
    text = out.getvalue()
    assert text.count('issues/opened') == 1 and text.count('#537 ') >= 3
    assert '"body": {' in text and '"action": "submitted"' in text


def test_the_viewer_never_writes(tmp_path):
    _, path = make(tmp_path)
    db = V.connect(path)
    try:
        db.execute("INSERT INTO events(stage) VALUES('x')")
        assert False, 'wrote'
    except sqlite3.OperationalError as e:
        assert 'readonly' in str(e)


def test_since(tmp_path):
    now = datetime(2026, 11, 15, 13, 0, tzinfo=eventdb.ET)
    assert V.since_iso('today', now) == '2026-11-15T00:00:00-05:00'
    assert V.since_iso('all', now) == ''
    assert V.since_iso('2026-11-14', now) == '2026-11-14T00:00:00-05:00'
    _, path = make(tmp_path)
    out = io.StringIO()
    V.run(path, since='2099-01-01', once=True, color=False, out=out)
    assert out.getvalue() == ''


def test_the_bin_wrapper_runs_this_checkout(tmp_path):
    import os
    import subprocess
    import sys
    _, path = make(tmp_path)
    wrapper = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin', 'swarm-events')
    r = subprocess.run([wrapper, '--once', '--no-color', '--db', path], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, TANGENTSWARM_PYTHON=sys.executable))
    assert r.returncode == 0 and r.stdout.count('\n') == 4 and 'tangentcode/platform#537' in r.stdout, r.stderr
