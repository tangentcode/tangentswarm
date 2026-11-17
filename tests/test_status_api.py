"""swarm-api, the Swarm status API (tangentswarm #24). GitHub, the hub and the gate lock are fakes;
nothing here reaches the network or a live DB."""
import json
import os
import threading
import time
import urllib.request

import pytest

import boardfake
from tangentswarm import auth as A
from tangentswarm import auth_cli, board, eventdb
from tangentswarm import status_api as S

SECRET = 's' * 40
HUMAN = {'X-Swarm-User': 'tangentstorm', 'X-Swarm-Proxy-Secret': SECRET}
JSON = {'Content-Type': 'application/json'}


def node(n, col, kind='PullRequest', repo='tangentcode/platform', title='', labels=(), draft=False, item=None,
         board_opts=None):
    opts = board_opts or board.DEFAULT.option
    c = {'__typename': kind, 'number': n, 'title': title or f't{n}', 'state': 'OPEN',
         'repository': {'nameWithOwner': repo}, 'labels': {'nodes': [{'name': l} for l in labels]}}
    if kind == 'PullRequest':
        c.update(isDraft=draft, headRefOid='a' * 40, headRefName=f'b{n}', updatedAt='2026-11-17T00:00:00Z')
    return {'id': item or f'item-{n}', 'fieldValueByName': {'name': col, 'optionId': opts[col]}, 'content': c}


class FakeHub:
    def __init__(self, msgs=()):
        self.msgs = [dict(m) for m in msgs]
        self.sent, self.acked = [], []

    def read_messages(self, reader, recipient=None, since=None, unread_only=False, limit=100, hub_url=None):
        return [m for m in self.msgs if m['id'] > (since or 0)][:limit]

    def send_message(self, sender, recipient, type, body, context=None, hub_url=None):
        self.sent.append(dict(sender=sender, recipient=recipient, type=type, body=body, context=context))
        return {'id': 100 + len(self.sent)}

    def ack(self, mid, hub_url=None):
        self.acked.append(mid)
        return True


class Env:
    """An Api over fakes: board 4 (platform) and board 6 nodes, a recorded gh, a recorded gate move."""

    def __init__(self, tmp_path, nodes4=(), nodes6=(), msgs=(), auth=None):
        self.nodes4, self.nodes6 = list(nodes4), list(nodes6)
        self.ran, self.moved, self.gql_calls = [], [], []
        self.closes = {}     # PR number -> [(issue, repo, latest comment body)]
        self.hub = FakeHub(msgs)
        self.store = eventdb.EventStore(str(tmp_path / 'events.sqlite'))
        self.lock = tmp_path / 'swarm-serial.lock'
        # hr-gate's namespace: its own move() for its columns, else board.move_card's gh path
        gate = {'move': lambda item, col: self.moved.append((item, col)), 'OPT': board.HR_GATE_COLS,
                'project_id': lambda: board.PROJECT_ID, 'STATUS_FIELD': board.STATUS_FIELD}
        src = S.Sources(gql=self.gql, gate=lambda: gate, sh=self.sh, events=lambda: self.store, hub=self.hub,
                        lock=self.lock, queued=lambda: [])
        self.auth = auth or S.AuthConfig(proxy_secret=SECRET,
                                         store_factory=lambda: A.AuthStore(tmp_path / 'auth.db'))
        self.api = S.Api(src, self.auth)

    def gql(self, q, **v):
        self.gql_calls.append(q)
        if 'closingIssuesReferences(first:20)' in q:
            nodes = [{'number': i, 'repository': {'nameWithOwner': r}, 'comments': {'nodes': [{'body': b}]}}
                     for i, r, b in self.closes.get(v['n'], [])]
            return {'repository': {'pullRequest': {'closingIssuesReferences': {'nodes': nodes}}}}
        if 'projectV2(number:4)' in q:
            return boardfake.answer(self.nodes4)
        assert v.get('n') == 6
        return {'organization': {'projectV2': {'items': {'nodes': self.nodes6,
                                                         'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}

    def sh(self, argv):
        self.ran.append([str(a) for a in argv])
        return ''

    def get(self, path, headers=HUMAN):
        st, h, obj = self.api.handle('GET', path, dict(headers))
        return st, obj

    def post(self, path, body, headers=None):
        hd = dict(HUMAN, **JSON) if headers is None else dict(headers)
        st, h, obj = self.api.handle('POST', path, hd, json.dumps(body).encode())
        return st, obj


@pytest.fixture(autouse=True)
def no_real_events(tmp_path, monkeypatch):
    """eventdb.log_event writes to a temp DB, never the live one."""
    st = eventdb.EventStore(str(tmp_path / 'log.sqlite'))
    monkeypatch.setattr(eventdb, '_store', st)
    monkeypatch.setattr(eventdb, 'SOURCE', 'api')   # as main() sets it; another test may leave its own
    return st


# -- auth ------------------------------------------------------------------------------------

def test_health_needs_no_auth_and_everything_else_does(tmp_path):
    e = Env(tmp_path)
    assert e.get('/api/health', {}) == (200, {'ok': True})
    for p in ('/api/whoami', '/api/swarms', '/api/swarms/4/places', '/api/swarms/4/events'):
        st, obj = e.get(p, {})
        assert st == 401 and 'work login' in obj['error']
    # the user header without the proxy secret, or with a wrong one, is nobody
    assert e.get('/api/whoami', {'X-Swarm-User': 'tangentstorm'})[0] == 401
    assert e.get('/api/whoami', {'X-Swarm-User': 'tangentstorm', 'X-Swarm-Proxy-Secret': 'x' * 40})[0] == 401
    assert e.get('/api/whoami', {'X-Swarm-Proxy-Secret': SECRET})[0] == 401   # no user
    st, who = e.get('/api/whoami')
    assert st == 200 and who['kind'] == 'human' and who['human'] == 'tangentstorm' and who['can_write']


def test_no_proxy_secret_means_no_browser_logins(tmp_path):
    e = Env(tmp_path, auth=S.AuthConfig(proxy_secret=None,
                                         store_factory=lambda: A.AuthStore(tmp_path / 'auth.db')))
    assert e.get('/api/whoami', {'X-Swarm-User': 'x', 'X-Swarm-Proxy-Secret': ''})[0] == 401
    assert e.get('/api/whoami')[0] == 401


def test_only_api_paths_are_served(tmp_path):
    e = Env(tmp_path)
    for p in ('/', '/mcp', '/login', '/token', '/authorize', '/register', '/revoke',
              '/.well-known/oauth-authorization-server', '/index.html'):
        assert e.get(p)[0] == 404
    assert e.get('/api/nope')[0] == 404
    st, h, obj = e.api.handle('GET', '/api/swarms/4/cards/1/triage', HUMAN)
    assert st == 405 and h['Allow'] == 'POST'


def test_machine_tokens_read_and_act_only_for_a_human(tmp_path):
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue')])
    store = A.AuthStore(tmp_path / 'auth.db')
    read, *_ = store.issue_token('panel', [A.SCOPE_STATUS], 3600, subject='panel')
    act, *_ = store.issue_token('bot', [A.SCOPE_STATUS, A.SCOPE_ACT], 3600, subject='tangentstorm')
    mcp, *_ = store.issue_token('mcp', [A.SCOPE_READ, A.SCOPE_SHELL], 3600)
    other, *_ = store.issue_token('x', [A.SCOPE_STATUS], 3600, resource='https://h/mcp')
    gone, gid, _ = store.issue_token('y', [A.SCOPE_STATUS], 3600)
    store.revoke_token(token_id=gid)
    old, *_ = store.issue_token('z', [A.SCOPE_STATUS], -10)

    def bearer(t):
        return {'Authorization': f'Bearer {t}'}
    st, who = e.get('/api/whoami', bearer(read))
    assert st == 200 and who == {'kind': 'token', 'name': 'panel', 'human': None, 'scopes': [A.SCOPE_STATUS],
                                 'can_write': False}
    for t in (mcp, other, gone, old, 'tsw_nope'):
        assert e.get('/api/whoami', bearer(t))[0] == 401
    assert e.get('/api/swarms/4/places', bearer(read))[0] == 200
    st, obj = e.post('/api/swarms/4/cards/7/triage', {'action': 'approve'}, dict(bearer(read), **JSON))
    assert st == 403 and A.SCOPE_ACT in obj['error'] and not e.moved
    st, obj = e.post('/api/swarms/4/cards/7/triage', {'action': 'approve'}, dict(bearer(act), **JSON))
    assert st == 200 and e.moved == [('item-7', board.BACKLOG)]


def test_humans_list_limits_who_writes(tmp_path):
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue')],
            auth=S.AuthConfig(proxy_secret=SECRET, humans=('tangentstorm',)))
    other = dict(HUMAN, **{'X-Swarm-User': 'someone'})
    assert e.get('/api/whoami', other)[1]['can_write'] is False
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'}, dict(other, **JSON))[0] == 403
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'})[0] == 200


def test_browser_writes_must_be_same_origin_json(tmp_path):
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue')])
    form = dict(HUMAN, **{'Content-Type': 'application/x-www-form-urlencoded'})
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'}, form)[0] == 415
    cross = dict(HUMAN, **JSON, Origin='https://evil.example', Host='swarm.example')
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'}, cross)[0] == 403
    opaque = dict(HUMAN, **JSON, Origin='null', Host='swarm.example')
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'}, opaque)[0] == 403
    assert not e.moved and not e.ran
    same = dict(HUMAN, **JSON, Origin='https://swarm.example', Host='127.0.0.1:8779',
                **{'X-Forwarded-Host': 'swarm.example'})
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'active'}, same)[0] == 200
    assert len(e.ran) == 1 and e.ran[0][-1] == board.DEFAULT.option[board.ACTIVE]
    st, h, obj = e.api.handle('POST', '/api/swarms/4/cards/7/triage', dict(HUMAN, **JSON), b'not json')
    assert st == 400


def test_proxy_secret_file_must_be_private_and_long(tmp_path):
    p = tmp_path / 'secret'
    p.write_text(SECRET + '\n')
    os.chmod(p, 0o644)
    with pytest.raises(S.ApiError, match='chmod 600'):
        S.AuthConfig.load({'TANGENTSWARM_API_PROXY_SECRET_FILE': str(p)})
    os.chmod(p, 0o600)
    cfg = S.AuthConfig.load({'TANGENTSWARM_API_PROXY_SECRET_FILE': str(p), 'TANGENTSWARM_API_HUMANS': 'a, b'})
    assert cfg.proxy_secret == SECRET and cfg.humans == ('a', 'b')
    p.write_text('short')
    with pytest.raises(S.ApiError, match='shorter'):
        S.AuthConfig.load({'TANGENTSWARM_API_PROXY_SECRET_FILE': str(p)})
    assert S.AuthConfig.load({'TANGENTSWARM_API_PROXY_SECRET_FILE': str(tmp_path / 'none')}).proxy_secret is None


def test_cli_issues_api_tokens_and_act_needs_a_subject(tmp_path, capsys):
    with pytest.raises(SystemExit, match='--subject'):
        auth_cli.main(['token', 'issue', '--client', 'bot', '--scopes', A.SCOPE_ACT])
    auth_cli.main(['token', 'issue', '--client', 'bot', '--scopes', A.SCOPE_ACT, '--subject', 'tangentstorm'])
    token = [l.split()[1] for l in capsys.readouterr().out.splitlines() if l.startswith('token:')][0]
    row = A.AuthStore().lookup_token(token)
    assert row['scopes'].split() == [A.SCOPE_STATUS, A.SCOPE_ACT] and row['subject'] == 'tangentstorm'
    # a confidential client never gets an API scope (only a pre-issued token does)
    with pytest.raises(SystemExit, match='unknown scope'):
        auth_cli.main(['client', 'add', '--name', 'c', '--scopes', A.SCOPE_ACT])
    assert A.SCOPE_STATUS not in A.ISSUABLE_SCOPES and A.SCOPE_ACT not in A.ISSUABLE_SCOPES


# -- reads -----------------------------------------------------------------------------------

def test_swarms_and_places_nest_cards_with_counts(tmp_path):
    e = Env(tmp_path, nodes4=[node(1, board.TRIAGE, 'Issue', title='an idea'),
                              node(2, board.HUMAN_REVIEW), node(3, board.HUMAN_REVIEW, labels=['fast-track']),
                              node(4, board.STAGING), node(5, board.READY, repo='tangentcode/other')],
            nodes6=[node(9, board.READY, repo='tangentcode/tangentswarm', board_opts=board.BOARDS[1].option),
                    node(8, board.TRIAGE, 'Issue', repo='tangentcode/methodology',
                         board_opts=board.BOARDS[1].option),
                    node(7, board.READY, repo='tangentcode/elsewhere', board_opts=board.BOARDS[1].option)])
    st, obj = e.get('/api/swarms')
    assert st == 200 and [s['board'] for s in obj['swarms']] == [4, 6]
    st, obj = e.get('/api/swarms/4/places')
    assert st == 200
    by = {p['key']: p for p in obj['places']}
    assert [p['key'] for p in obj['places']] == list(board.COLUMNS)
    assert by[board.STAGING]['name'] == 'Staging' and by[board.STAGING]['count'] == 1
    hr = by[board.HUMAN_REVIEW]
    assert hr['count'] == 2 and [c['number'] for c in hr['cards']] == [3, 2]   # fast-track first
    assert hr['cards'][0]['ft'] and hr['cards'][0]['kind'] == 'pr'
    t = by[board.TRIAGE]['cards'][0]
    assert t['kind'] == 'issue' and t['title'] == 'an idea' and t['url'].endswith('/issues/1')
    assert by[board.READY]['count'] == 0    # another repo's PR is not this swarm's card
    st, obj = e.get('/api/swarms/6/places')
    by = {p['key']: p for p in obj['places']}
    assert board.STAGING not in by       # no Staging place on a board without staging
    assert [c['number'] for c in by[board.READY]['cards']] == [9] and by[board.TRIAGE]['count'] == 1
    assert e.get('/api/swarms/5/places')[0] == 404
    # board 6 reads are kept a short while; a human action reads it fresh
    n6 = sum('projectV2(number:4)' not in q for q in e.gql_calls)
    e.get('/api/swarms/6/places')
    assert sum('projectV2(number:4)' not in q for q in e.gql_calls) == n6
    e.post('/api/swarms/6/cards/8/triage', {'action': 'active'})
    assert sum('projectV2(number:4)' not in q for q in e.gql_calls) == n6 + 1


def test_transitions_name_rules_limits_and_human_actions(tmp_path):
    from tangentswarm import flow
    e = Env(tmp_path)
    st, obj = e.get('/api/swarms/4/transitions')
    assert st == 200 and obj['limits'][board.HUMAN_REVIEW] == {'target': flow.HR_TARGET}
    assert obj['limits'][board.STAGING]['max'] == flow.STAGING_MAX
    names = {t['name']: t for t in obj['transitions']}
    assert names['promote']['from'] == [board.STAGING] and names['pull']['to'] == board.STAGING
    human = {t['name'] for t in obj['transitions'] if t['human']}
    assert {'triage: approve', 'triage: backlog', 'triage: active', 'triage: discuss', 'answer', 'reject'} <= human
    assert names['triage: active']['action'].endswith('{"action": "active"}')
    assert board.ACTIVE not in names['triage: active']['from']
    st, obj = e.get('/api/swarms/6/transitions')
    names = {t['name']: t for t in obj['transitions']}
    assert 'pull' not in names and names['promote']['from'] == [board.READY]
    assert board.STAGING not in obj['limits'] and obj['limits'][board.HUMAN_REVIEW] == {'target': 2}


def test_events_audit_marks_handled_unhandled_dropped_and_errors(tmp_path):
    e = Env(tmp_path)
    s, now = e.store, time.time()
    pay = {'repository': {'full_name': 'tangentcode/platform'}, 'pull_request': {'number': 5}}
    s.add_received('d-ok', 'pull_request', pay, {'X-Hub-Signature-256': 'sha256=x'}, b'{"secret": 1}', True,
                   now=now - 1000)
    s.add('handled', 'd-ok', 'pull_request', pay, notes=['#5 -> Auto Review'], now=now - 999)
    s.add_received('d-lost', 'issues', pay, {}, b'{}', True, now=now - 1000)
    s.add_received('d-new', 'issues', pay, {}, b'{}', True, now=now - 5)
    s.add_received('d-bad', 'issues', pay, {}, b'{}', False, reason='bad signature', now=now - 900)
    s.add_received('d-ok', 'pull_request', pay, {}, b'{}', True, now=now - 800)   # a redelivery
    s.add_event('board', 'move', action='Ready', pr=5, detail={'item': 'i5'}, now=now - 700)
    s.add_event('gh', 'command', error='rc=1: boom', now=now - 600)
    st, obj = e.get('/api/swarms/4/events?limit=50')
    assert st == 200
    rows = obj['events']
    state = [(r['stage'], r['delivery'], r['state']) for r in rows]
    assert state == [('received', 'd-ok', 'handled'), ('handled', 'd-ok', 'handled'),
                     ('received', 'd-lost', 'unhandled'), ('received', 'd-new', 'pending'),
                     ('received', 'd-bad', 'dropped'), ('received', 'd-ok', 'dropped'),
                     ('event', None, 'handled'), ('event', None, 'error')]
    assert rows[5]['why'] == 'duplicate' and rows[6]['detail'] == {'item': 'i5'}
    assert rows[1]['done'] == ['moved card to Auto Review']
    assert all('body' not in r and 'headers' not in r for r in rows)   # never the raw delivery
    assert 'secret' not in json.dumps(obj)
    # paging: since, limit, source and pr filters
    st, obj = e.get(f"/api/swarms/4/events?since={rows[1]['id']}&limit=2")
    assert [r['delivery'] for r in obj['events']] == ['d-lost', 'd-new'] and obj['next'] == rows[3]['id']
    st, obj = e.get('/api/swarms/4/events?limit=1')
    assert [r['state'] for r in obj['events']] == ['error']        # the latest page
    st, obj = e.get('/api/swarms/4/events?source=board')
    assert [r['event'] for r in obj['events']] == ['move']
    st, obj = e.get(f"/api/swarms/4/events?since={rows[0]['id']}&source=board&limit=5")
    assert [r['event'] for r in obj['events']] == ['move']
    assert e.get('/api/swarms/4/events?pr=x')[0] == 400


def test_messages_are_marked_handled_when_read(tmp_path):
    e = Env(tmp_path, msgs=[dict(id=1, sender='w1', recipient='michal', type='question', body='q?', read=0,
                                 ts=1, context=None),
                            dict(id=2, sender='w2', recipient='michal', type='done', body='ok', read=1, ts=2,
                                 context=None)])
    st, obj = e.get('/api/swarms/4/messages')
    assert st == 200 and [m['state'] for m in obj['messages']] == ['unhandled', 'handled'] and obj['next'] == 2
    assert [m['id'] for m in e.get('/api/swarms/4/messages?since=1')[1]['messages']] == [2]


def test_lease_reports_no_coordinator_and_the_gate_lock_note(tmp_path):
    e = Env(tmp_path)
    st, obj = e.get('/api/swarms/4/lease')
    assert st == 200 and obj['coordinator']['holder'] is None and obj['coordinator']['status'] == 'none'
    assert obj['serial_lock'] == {'holder': None, 'queued': []}
    (tmp_path / 'swarm-serial.lock.holder').write_text('hr-gate --apply 12 pid 4\n')
    assert e.get('/api/swarms/4/lease')[1]['serial_lock']['holder'] == 'hr-gate --apply 12 pid 4'
    assert not e.lock.exists()        # never probed (a probe would take the lock)


# -- human actions ---------------------------------------------------------------------------

def test_triage_moves_through_move_card_and_logs_the_human(tmp_path, no_real_events):
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue'), node(8, board.IN_PROGRESS)])
    st, obj = e.post('/api/swarms/4/cards/7/triage', {'action': 'approve'})
    assert st == 200 and obj == {'ok': True, 'changed': True, 'number': 7, 'from': board.TRIAGE,
                                 'col': board.BACKLOG}
    assert e.moved == [('item-7', board.BACKLOG)]
    rows = no_real_events.rows()
    assert [(r['event'], r['source'], r['actor'], r['action']) for r in rows] == [
        ('human-action', 'api', 'tangentstorm', 'triage approve'), ('move', 'board', 'api', board.BACKLOG)]
    # every write reads the board fresh (max_age 0), never from the snapshot of a read
    assert len(e.gql_calls) == 1
    st, obj = e.post('/api/swarms/4/cards/8/triage', {'action': 'backlog'})
    assert st == 409 and 'In Progress' in obj['error']
    assert e.post('/api/swarms/4/cards/7/triage', {'action': 'merge'})[0] == 400
    assert e.post('/api/swarms/4/cards/99/triage', {'action': 'active'})[0] == 404
    assert e.moved == [('item-7', board.BACKLOG)]


def test_triage_to_its_own_column_changes_nothing(tmp_path):
    e = Env(tmp_path, nodes4=[node(7, board.BACKLOG, 'Issue')])
    st, obj = e.post('/api/swarms/4/cards/7/triage', {'action': 'backlog'})
    assert st == 200 and obj['changed'] is False and not e.moved and not e.ran


def test_triage_on_another_board_uses_its_own_ids_and_needs_the_repo_when_ambiguous(tmp_path):
    o6 = board.BOARDS[1].option
    e = Env(tmp_path, nodes6=[node(3, board.TRIAGE, 'Issue', repo='tangentcode/tangentswarm', item='a', board_opts=o6),
                              node(3, board.INBOX, 'Issue', repo='tangentcode/methodology', item='b', board_opts=o6)])
    st, obj = e.post('/api/swarms/6/cards/3/triage', {'action': 'discuss'})
    assert st == 409 and 'give its repo' in obj['error']
    st, obj = e.post('/api/swarms/6/cards/3/triage', {'action': 'discuss', 'repo': 'tangentcode/methodology'})
    assert st == 200 and not e.moved
    assert e.ran == [['gh', 'project', 'item-edit', '--id', 'b', '--project-id', board.BOARDS[1].id,
                      '--field-id', board.BOARDS[1].status_field, '--single-select-option-id', o6[board.DISCUSS]]]


def test_answer_comments_on_the_ticket_then_moves_the_card(tmp_path, no_real_events):
    e = Env(tmp_path, nodes4=[node(12, board.NEEDS_INPUT), node(13, board.IN_PROGRESS)])
    st, obj = e.post('/api/swarms/4/cards/12/answer', {'body': 'Use the second option.'})
    assert st == 200 and obj['col'] == board.IN_PROGRESS
    assert e.moved == [('item-12', board.IN_PROGRESS)]   # through hr-gate's move (its column)
    assert e.ran == [['gh', 'issue', 'comment', '12', '-R', 'tangentcode/platform', '--body', 'Use the second option.']]
    assert [r['action'] for r in no_real_events.rows()] == ['answer', board.IN_PROGRESS]
    assert e.post('/api/swarms/4/cards/13/answer', {'body': 'x'})[0] == 409
    assert e.post('/api/swarms/4/cards/12/answer', {'body': '  '})[0] == 400


def test_answer_goes_on_the_linked_issue_that_holds_the_question(tmp_path):
    """Codex on PR #74: the Stop hook posts a PR's question on the issue it closes, and
    advance.open_question blocks the MinBot pass until that issue's latest comment is not the
    question. The answer goes there, not on the PR."""
    from tangentswarm.stophook import QUESTION_MARK
    e = Env(tmp_path, nodes4=[node(12, board.NEEDS_INPUT), node(30, board.NEEDS_INPUT, 'Issue')])
    e.closes[12] = [(20, 'tangentcode/platform', f'{QUESTION_MARK}\nQuestion from the worker'),
                    (21, 'tangentcode/platform', 'an old comment, no question'),
                    (22, 'tangentcode/other', f'{QUESTION_MARK}\nQuestion')]
    st, obj = e.post('/api/swarms/4/cards/12/answer', {'body': 'yes'})
    assert st == 200 and obj['answered_on'] == ['tangentcode/platform#20', 'tangentcode/other#22']
    assert e.ran == [['gh', 'issue', 'comment', '20', '-R', 'tangentcode/platform', '--body', 'yes'],
                     ['gh', 'issue', 'comment', '22', '-R', 'tangentcode/other', '--body', 'yes']]
    assert e.moved == [('item-12', board.IN_PROGRESS)]
    # an issue card in Needs Input: the answer goes on the issue itself, no PR read
    e.ran.clear()
    n = len(e.gql_calls)
    st, obj = e.post('/api/swarms/4/cards/30/answer', {'body': 'ok'})
    assert st == 200 and e.ran == [['gh', 'issue', 'comment', '30', '-R', 'tangentcode/platform', '--body', 'ok']]
    assert not any('closingIssuesReferences(first:20)' in q for q in e.gql_calls[n:])


def test_answer_a_hub_question(tmp_path):
    e = Env(tmp_path, msgs=[dict(id=4, sender='w1', recipient='michal', type='question', body='which?', read=0),
                            dict(id=5, sender='w2', recipient='michal', type='status', body='hi', read=0)])
    st, obj = e.post('/api/swarms/4/messages/4/answer', {'body': 'the first'})
    assert st == 200 and obj == {'ok': True, 'message': 4, 'reply': 101, 'to': 'w1'}
    assert e.hub.sent == [dict(sender='human:tangentstorm', recipient='w1', type='status', body='the first',
                               context=json.dumps({'in_reply_to': 4}))]
    assert e.hub.acked == [4]
    assert e.post('/api/swarms/4/messages/5/answer', {'body': 'x'})[0] == 409
    assert e.post('/api/swarms/4/messages/9/answer', {'body': 'x'})[0] == 404


def test_a_failed_move_is_an_error_reply_not_a_crash(tmp_path):
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue')])

    def boom(argv):
        raise RuntimeError('gh project item-edit...: HTTP 502')
    e.api.src.sh = boom
    e.api.src._gate = lambda: {'project_id': lambda: board.PROJECT_ID, 'STATUS_FIELD': board.STATUS_FIELD}
    st, obj = e.post('/api/swarms/4/cards/7/triage', {'action': 'active'})
    assert st == 502 and 'HTTP 502' in obj['error']


# -- the HTTP server -------------------------------------------------------------------------

def test_serves_on_loopback_only_and_over_http(tmp_path):
    with pytest.raises(SystemExit, match='loopback'):
        S.serve(Env(tmp_path).api, '0.0.0.0', 0)
    for host in ('::1', 'localhost'):   # Codex on PR #74: ::1 needs an AF_INET6 server
        try:
            s6 = S.serve(Env(tmp_path).api, host, 0)
        except OSError as err:
            if host == '::1' and 'Address family' not in str(err):
                pytest.skip(f'no IPv6 loopback here: {err}')
            raise
        s6.server_close()
    e = Env(tmp_path, nodes4=[node(7, board.TRIAGE, 'Issue')])
    srv = S.serve(e.api, '127.0.0.1', 0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f'http://127.0.0.1:{srv.server_address[1]}'
    try:
        def call(method, path, headers, body=None):
            req = urllib.request.Request(base + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers), json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, dict(err.headers), json.loads(err.read())
        st, h, obj = call('GET', '/api/whoami', {})
        assert st == 401 and h['WWW-Authenticate'].startswith('Bearer') and h['Cache-Control'] == 'no-store'
        st, h, obj = call('GET', '/api/swarms/4/places', HUMAN)
        assert st == 200 and h['Content-Type'] == 'application/json'
        st, h, obj = call('POST', '/api/swarms/4/cards/7/triage', dict(HUMAN, **JSON), {'action': 'discuss'})
        assert st == 200 and e.ran[-1][-1] == board.DEFAULT.option[board.DISCUSS]
    finally:
        srv.shutdown()
        srv.server_close()
