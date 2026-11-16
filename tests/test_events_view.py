"""swarm-events (tangentswarm #34 Step 1): one row per event, skips hidden, plain outcomes,
signature failures highlighted, age shading."""
import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

from tangentswarm import eventdb, events_view as V

REPO = {'full_name': 'tangentcode/platform'}
NOW = 1_800_000_000.0


def make(tmp_path):
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    merged = {'action': 'closed', 'repository': REPO, 'pull_request': {'number': 513}, 'sender': {'login': 'tangentstorm'}}
    st.add_received('d1', 'pull_request', merged, {}, json.dumps(merged).encode(), True, decisions=['merged'], now=NOW - 30)
    st.add('handled', 'd1', 'pull_request', merged, decisions=['merged'], pr=513, now=NOW - 29,
           notes=['cascade queued for swarm-promoted', '#513 -> Done', '#491 -> Done', '#513 merged: cascade, Done'])
    st.add_received('d1', 'pull_request', merged, {}, b'{}', True, decisions=['merged'], now=NOW - 20)  # redelivery
    ping = {'zen': 'x'}
    st.add_received('d2', 'ping', ping, {}, b'{}', True, decisions=['skip'], now=NOW - 25)
    st.add('handled', 'd2', 'ping', ping, decisions=['skip'], notes=['skip: ping'], now=NOW - 25)
    st.add_received('d3', 'push', {'repository': REPO}, {}, b'{}', True, reason='log-only', decisions=['log-only'],
                    now=NOW - 24)
    st.add_received('d4', 'pull_request', None, {}, b'x', False, reason='bad signature', now=NOW - 600)
    review = {'action': 'submitted', 'repository': REPO, 'pull_request': {'number': 537},
              'sender': {'login': 'chatgpt-codex-connector[bot]'}}
    st.add_received('d5', 'pull_request_review', review, {}, b'{}', True, decisions=['codex'], now=NOW - 10)
    return st, str(tmp_path / 'e.sqlite')


def test_one_row_per_event_skips_and_redeliveries_hidden(tmp_path):
    st, path = make(tmp_path)
    evs = V.events(st.rows())
    assert [(e['event'], e['status']) for e in evs] == [
        ('pull_request/closed', 'handled'), ('pull_request', 'failed'), ('pull_request_review/submitted', 'queued')]
    merged = evs[0]
    assert merged['pr'] == 513 and merged['actor'] == 'tangentstorm'
    assert merged['outcome'] == 'cascade; moved #513 to Done; moved #491 to Done; #513 merged; card Done; issues closed'


def test_the_signature_shows_only_when_it_failed(tmp_path):
    st, path = make(tmp_path)
    out = io.StringIO()
    V.run(path, once=True, color=False, out=out, clock=lambda: NOW)
    lines = out.getvalue().splitlines()
    assert len(lines) == 3 and not any('sig ok' in l or 'sig -' in l for l in lines)
    assert lines[1].endswith('SIGNATURE FAILED') and 'SIGNATURE' not in lines[0] + lines[2]
    colored = io.StringIO()
    V.run(path, once=True, color=True, out=colored, clock=lambda: NOW)
    assert V.SIG_BAD + ' SIGNATURE FAILED ' in colored.getvalue().splitlines()[1]


def test_age_shading_bright_then_one_gray_step_per_minute():
    assert V.shade(0) is None and V.shade(119) is None
    assert V.shade(120) == V.GRAY_START and V.shade(180) == V.GRAY_START - 1
    assert V.shade(10 * 3600) == V.GRAY_END
    e = dict(at='2026-11-15T13:00:00-05:00', t=NOW - 30, status='handled', event='x', pr=1, actor='a', outcome='o')
    assert V.line(e, NOW).startswith(V.BOLD)
    assert V.line(dict(e, t=NOW - 300), NOW).startswith(f'\033[38;5;{V.GRAY_START - 3}m')
    assert V.line(dict(e, status='failed', error='boom'), NOW).startswith(V.RED)


def test_a_terminal_is_redrawn_for_the_shading(tmp_path):
    st, path = make(tmp_path)
    out, t = io.StringIO(), [NOW]

    def sleep(s):
        t[0] += 11
    V.run(path, color=True, out=out, sleep=sleep, polls=3, redraw=10, clock=lambda: t[0], tty=True)
    assert out.getvalue().count(V.CLEAR) == 3  # first draw, then every 10 s with no new event


def test_a_pipe_reprints_a_queued_event_once_it_is_handled(tmp_path):
    st, path = make(tmp_path)
    out, added = io.StringIO(), []

    def sleep(s):
        if not added:
            added.append(st.add('handled', 'd5', 'pull_request_review', None, decisions=['codex'], pr=537,
                                notes=['#537 -> In Progress', '#537 relayed to its worker (sent)'], now=NOW - 5))
    V.run(path, color=False, out=out, sleep=sleep, polls=2, clock=lambda: NOW)
    lines = out.getvalue().splitlines()
    assert 'queued' in lines[2] and 'handled' in lines[-1]
    assert lines[-1].endswith('moved #537 to In Progress; relayed to #537 worker')


def test_server_action_rows_have_no_empty_columns():
    r = dict(stage='event', source='slots', event='park', pr=547, actor='mn-547-x', received=NOW,
             received_at='2026-11-15T13:00:00-05:00', notes='["its PR is in Ready"]')
    (e,) = V.events([r])
    assert V.line(e, NOW, color=False) == ('13:00:00         slots park                   #547           mn-547-x             '
                                           'its PR is in Ready')


def test_the_viewer_never_writes(tmp_path):
    _, path = make(tmp_path)
    db = V.connect(path)
    try:
        db.execute("INSERT INTO events(stage) VALUES('x')")
        assert False, 'wrote'
    except sqlite3.OperationalError as e:
        assert 'readonly' in str(e)


def test_since():
    now = datetime(2026, 11, 15, 13, 0, tzinfo=eventdb.ET)
    assert V.since_iso('today', now) == '2026-11-15T00:00:00-05:00'
    assert V.since_iso('all', now) == ''
    assert V.since_iso('2026-11-14', now) == '2026-11-14T00:00:00-05:00'


def test_the_bin_wrapper_runs_this_checkout(tmp_path):
    _, path = make(tmp_path)
    wrapper = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin', 'swarm-events')
    r = subprocess.run([wrapper, '--once', '--no-color', '--since', 'all', '--db', path], capture_output=True,
                       text=True, timeout=30, env=dict(os.environ, TANGENTSWARM_PYTHON=sys.executable))
    assert r.returncode == 0 and r.stdout.count('\n') == 3 and '#513' in r.stdout, r.stderr


def test_a_cascade_that_did_nothing_is_hidden_and_commands_read_plainly():
    idle = dict(stage='event', source='cascade', event='cascade', received=NOW, received_at='2026-11-15T13:00:00-05:00',
                notes=json.dumps(['cascade: start; budget 780s', 'promote: Human Review 2/2',
                                  'fill: Staging 2, target 2: no pull needed', 'signal: no Backlog issue without a PR']))
    busy = dict(idle, notes=json.dumps(['promote: Human Review 2/2', 'park: #547 parked; session s kept']))
    cmd = dict(stage='event', source='gh', event='command', action='issue close 491', pr=491, received=NOW,
               received_at='2026-11-15T13:00:01-05:00', notes='["gh issue close 491 -R o/r"]')
    evs = V.events([idle, busy, cmd])
    assert [(e['event'], e['outcome']) for e in evs] == [('cascade', 'parked #547'), ('gh issue close 491', '')]


def test_replays_stay_separate_and_a_request_without_a_delivery_id_shows(tmp_path):
    """Codex on PR #36."""
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    st.add('replay', 'replay-537', 'replay', decisions=['codex'], pr=537, error='codex #537: RuntimeError: x', now=NOW)
    st.add('replay', 'replay-537', 'replay', decisions=['codex'], pr=537, notes=['queued the MinBot check of #537'],
           now=NOW + 60)
    st.add_received('', 'pull_request', None, {}, None, None, reason='bad length', error='Content-Length 0', now=NOW + 61)
    evs = V.events(st.rows())
    assert [(e['event'], e['status']) for e in evs] == [('replay', 'failed'), ('replay', 'handled'),
                                                         ('pull_request', 'failed')]
    assert evs[1]['outcome'] == 'MinBot check #537' and not evs[1]['error']
    assert 'Content-Length 0' in evs[2]['error']


def test_a_verbose_pipe_reprints_only_the_changed_event(tmp_path):
    """Codex on PR #36: by position, one handled row reprinted the rest of the day."""
    st, path = make(tmp_path)
    out, added = io.StringIO(), []
    first = io.StringIO()
    V.run(path, verbose=True, color=False, out=first, once=True, clock=lambda: NOW)

    def sleep(s):
        if not added:
            added.append(st.add('handled', 'd5', 'pull_request_review', None, decisions=['codex'], pr=537,
                                notes=['#537 -> In Progress'], now=NOW - 5))
    V.run(path, verbose=True, color=False, out=out, sleep=sleep, polls=2, clock=lambda: NOW)
    assert out.getvalue().startswith(first.getvalue())
    tail = out.getvalue()[len(first.getvalue()):]
    assert tail.count('\n') > 0 and '#513' not in tail and 'moved #537 to In Progress' in tail


def test_a_rejected_attempt_stays_apart_from_the_accepted_redelivery(tmp_path):
    """Codex on PR #36: the accepted redelivery was shown as a signature failure."""
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    p = {'action': 'opened', 'repository': REPO, 'issue': {'number': 9}, 'sender': {'login': 'tangentstorm'}}
    st.add_received('d9', 'issues', None, {}, b'x', False, reason='bad signature', now=NOW)
    st.add_received('d9', 'issues', p, {}, b'{}', True, decisions=['card'], now=NOW + 5)
    st.add('handled', 'd9', 'issues', p, decisions=['card'], notes=['#9 -> Triage'], now=NOW + 6)
    evs = V.events(st.rows())
    assert [(e['status'], e['sig_bad']) for e in evs] == [('failed', True), ('handled', False)]
    assert evs[1]['actor'] == 'tangentstorm' and evs[1]['outcome'] == 'moved #9 to Triage'


def test_a_colored_pipe_does_not_reprint_for_a_new_shade(tmp_path):
    """Codex on PR #36: the gray level changed with age and reprinted unchanged events."""
    st, path = make(tmp_path)
    out, t = io.StringIO(), [NOW]

    def sleep(s):
        t[0] += 120
    V.run(path, color=True, out=out, sleep=sleep, polls=3, clock=lambda: t[0])
    assert len(out.getvalue().splitlines()) == 3


def test_handling_after_midnight_shows_when_the_receipt_was_before(tmp_path):
    """Codex on PR #36: a duplicate receipt today hid today's handled row of yesterday's event."""
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    p = {'action': 'opened', 'repository': REPO, 'issue': {'number': 9}, 'sender': {'login': 'tangentstorm'}}
    st.add_received('d9', 'issues', p, {}, b'{}', True, decisions=['card'], now=NOW)        # before --since
    st.add_received('d9', 'issues', p, {}, b'{}', True, decisions=['card'], now=NOW + 2)    # duplicate
    st.add('handled', 'd9', 'issues', p, decisions=['card'], notes=['#9 -> Triage'], now=NOW + 3)
    today = [r for r in st.rows() if r['received'] > NOW + 1]
    (e,) = V.events(today)
    assert e['status'] == 'handled' and e['outcome'] == 'moved #9 to Triage' and e['pr'] == 9
    assert V.events([today[0]]) == []  # only the duplicate: nothing to show
