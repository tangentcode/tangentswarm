"""needs_input (tangentswarm #40 item 4): the question goes on the ticket, the card to Needs
Input, Michal is pinged once, and the worker's /exit is pending for the pane watcher."""
import pytest

from tangentswarm import alerts, board, launcher, needsinput as NI, spool, stophook, webhook, worker_mcp as W

from test_stophook import G, run, wt  # noqa: F401  (the worktree fixture)


def node(n, col, closes=()):
    return {'id': f'ITEM{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]},
            'content': {'__typename': 'PullRequest', 'number': n, 'state': 'OPEN', 'isDraft': True, 'title': 'Blue',
                        'headRefName': 'feat', 'headRefOid': 'h' * 40, 'repository': {'nameWithOwner': NI.REPO},
                        'closingIssuesReferences': {'nodes': [{'number': i, 'repository': {'nameWithOwner': NI.REPO}}
                                                              for i in closes]}}}


@pytest.fixture
def g():
    from boardfake import answer
    return dict(gql=lambda q, **v: answer([node(7, board.IN_PROGRESS, closes=[5])]), project_id=lambda: 'P',
                STATUS_FIELD='F')


def test_route_posts_on_the_ticket_moves_the_card_pings_once_and_marks_the_exit(g):
    calls, pings = [], []
    ping = lambda text, key, pr=None, st=None: pings.append((text, key))  # noqa: E731
    note = NI.route(7, 'Blue or red?', wt='/w/platform.feat', g=g, sh=calls.append, ping=ping)
    assert calls[0][:6] == ['gh', 'issue', 'comment', '5', '-R', NI.REPO]
    assert stophook.QUESTION_MARK in calls[0][-1] and '> Blue or red?' in calls[0][-1]
    assert calls[1][:5] == ['gh', 'project', 'item-edit', '--id', 'ITEM7']
    assert calls[1][-1] == board.OPTION[board.NEEDS_INPUT]
    assert len(pings) == 1 and 'Blue or red?' in pings[0][0]
    assert 'Needs Input' in note and 'issue #5' in note
    assert NI.pending_exits()['/w/platform.feat']['pr'] == 7
    again = NI.route(7, 'Blue or red?', wt='/w/platform.feat', g=g, sh=calls.append, ping=ping)
    assert 'already posted' in again and len(calls) == 2 and len(pings) == 1


def test_a_new_needs_input_card_pings_michal_but_not_twice():
    st, pings = alerts.state(), []
    ping = lambda text, key, pr=None, st=None: pings.append(text)  # noqa: E731
    assert NI.new_cards(st, [3], ping) == []          # the first look only records
    assert NI.new_cards(st, [3, 9], ping, titles={9: 'Logo'}) == [9]
    assert NI.new_cards(st, [3, 9], ping) == [] and len(pings) == 1 and 'Logo' in pings[0]
    NI.mark_pinged(11, __import__('time').time())  # route() pinged it already
    assert NI.new_cards(st, [3, 9, 11], ping) == []


def test_the_worker_tool_routes_only_its_own_pr(wt, monkeypatch):  # noqa: F811
    head = run(wt, 'rev-parse', 'HEAD')
    routed = []
    monkeypatch.setattr(W, 'gate', lambda: G(head))
    out = W.do_needs_input(wt, 8, 'Q?', route=lambda *a, **k: routed.append(a))
    assert out == {'posted': False, 'why': '#8 is not your PR; yours is #7'} and routed == []
    out = W.do_needs_input(wt, 7, 'Blue or red?', route=lambda n, q, **k: routed.append((n, q, k['wt'])) or 'ok')
    assert out['posted'] and routed == [(7, 'Blue or red?', str(wt))] and 'End your turn' in out['next']


def test_the_spool_runs_needs_input_for_the_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr(W, 'do_needs_input', lambda cwd, pr, q: seen.append((str(cwd), pr, q)) or {'posted': True})
    assert spool.execute({'op': 'needs_input', 'cwd': '/w', 'pr': 7, 'question': 'Q?'}) == {'posted': True, 'ok': True}
    assert seen == [('/w', 7, 'Q?')]
    assert spool.submit('needs_input', '/w', pr=7, question='Q?')['queued']


def test_the_brief_says_to_use_needs_input_not_the_pane():
    text = launcher.brief_text(40, 'T', 'b', 51)
    assert 'needs_input' in text and 'pr=51' in text and 'Never ask Michal in your pane' in text


def test_the_answer_on_the_ticket_reaches_the_resumed_claude():
    from test_webhook import Fake
    ex = Fake(col=board.IN_PROGRESS)
    ex.pr_of = lambda node: 7
    issue = {'comments': {'nodes': [
        {'author': {'login': 'tangentstorm'}, 'body': 'old note', 'createdAt': '1'},
        {'author': {'login': 'tangentstorm'}, 'body': stophook.QUESTION_MARK + '\nBlue or red?', 'createdAt': '2'},
        {'author': {'login': 'tangentstorm'}, 'body': 'Blue.', 'createdAt': '3'}]}}
    ex.full = {'comments': {'nodes': []}, 'commits': {'nodes': [{'commit': {'committedDate': '0'}}]},
               'closingIssuesReferences': {'nodes': [issue]}}
    ex.run(webhook.Action('answered', 0, 'x', dict(node='N')))
    assert ex.relays and 'Michal answered: Blue.' in ex.relays[0][1] and 'old note' not in ex.relays[0][1]


def test_a_pending_exit_from_the_tool_survives_the_daemons_state_save():
    daemon = alerts.state()  # loaded before the tool's request
    NI.mark_exit('/w/platform.feat', 7, 'asked Michal (needs_input)')
    daemon.data['codex_outage'] = {'since': 1}
    daemon.save()
    assert '/w/platform.feat' in NI.pending_exits()
