"""needs_input (tangentswarm #40 item 4) on top of agent_report (#53): the worker tool queues
a needs-input report, and a new Needs Input card pings Michal once."""
import pytest  # noqa: F401

from tangentswarm import alerts, board, launcher, needsinput as NI, spool, stophook, webhook, worker_mcp as W

from test_stophook import G, run, wt  # noqa: F401  (the worktree fixture)


def test_a_new_needs_input_card_pings_michal_but_not_twice():
    st, pings = alerts.state(), []
    ping = lambda text, key, pr=None, st=None: pings.append(text)  # noqa: E731
    assert NI.new_cards(st, [3], ping) == []          # the first look only records
    assert NI.new_cards(st, [3, 9], ping, titles={9: 'Logo'}) == [9]
    assert NI.new_cards(st, [3, 9], ping) == [] and len(pings) == 1 and 'Logo' in pings[0]
    assert NI.new_cards(st, [3], ping) == [] and NI.new_cards(st, [3, 9], ping) == [9]  # it left and came back


def test_the_worker_tool_queues_a_needs_input_report_for_its_own_pr_only(wt, monkeypatch):  # noqa: F811
    head = run(wt, 'rev-parse', 'HEAD')
    sent = []
    monkeypatch.setattr(W, 'gate', lambda: G(head))
    accept = lambda *a, **k: sent.append((a, k)) or {'ok': True, 'queued': 'r1', 'next': 'end your turn now'}  # noqa: E731
    out = W.do_needs_input(wt, 8, 'Q?', accept=accept, window_of=lambda n: 'mn-7-feat')
    assert out == {'posted': False, 'why': '#8 is not your PR; yours is #7'} and sent == []
    out = W.do_needs_input(wt, 7, 'Blue or red?', accept=accept, window_of=lambda n: 'mn-7-feat')
    assert out['posted'] and out['queued'] == 'r1'
    assert sent == [(('needs-input', 7, 'mn-7-feat'), {'question': 'Blue or red?', 'agent': 'needs_input tool'})]


def test_the_queued_report_is_the_spool_op_agent_report_runs(monkeypatch):
    from tangentswarm import agent_report
    out = agent_report.accept('needs-input', 7, 'mn-7-feat', question='Blue or red?', agent='needs_input tool')
    assert out['ok'] and out['queued']
    req = __import__('json').loads(next(spool.SPOOL.glob('*.req.json')).read_text())
    assert req['op'] == 'report' and req['state'] == 'needs-input' and req['question'] == 'Blue or red?'


def test_the_spool_runs_needs_input_for_the_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr(W, 'do_needs_input', lambda cwd, pr, q: seen.append((str(cwd), pr, q)) or {'posted': True})
    assert spool.execute({'op': 'needs_input', 'cwd': '/w', 'pr': 7, 'question': 'Q?'}) == {'posted': True, 'ok': True}
    assert seen == [('/w', 7, 'Q?')]
    assert spool.submit('needs_input', '/w', pr=7, question='Q?')['queued']


def test_the_brief_says_to_report_the_question_not_ask_in_the_pane():
    text = launcher.brief_text(40, 'T', 'b', 51)
    assert "report_status(state='needs-input', pr=51" in text and 'needs_input(pr, question)' in text
    assert 'Never ask Michal in your pane' in text


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


def balanced(q):
    depth = {'{': 0, '(': 0}
    for ch in q:
        if ch in '{(':
            depth[ch] += 1
        elif ch in '})':
            depth['{' if ch == '}' else '('] -= 1
            if min(depth.values()) < 0:
                return False
    return depth == {'{': 0, '(': 0}


def test_every_query_the_new_code_sends_is_balanced():
    """Codex on PR #51: the answer query had one closing brace too many."""
    from tangentswarm import watchdog
    for q in (webhook.ANSWERS_Q, watchdog.CLOSED_Q, watchdog.CODEX_Q.format(n=7)):
        assert balanced(q), q
    from test_webhook import Fake
    ex, sent = Fake(col=board.IN_PROGRESS), []
    ex._g['gql'] = lambda q, **v: sent.append(q) or {}
    ex.issue_answers(7)
    assert sent and all(balanced(q) for q in sent)


def test_a_failed_answer_read_still_resumes_the_worker():
    from test_webhook import Fake
    ex = Fake(col=board.IN_PROGRESS)
    ex.pr_of = lambda node: 7
    ex.full = {'comments': {'nodes': []}, 'commits': {'nodes': [{'commit': {'committedDate': '0'}}]}}
    real = ex._g['gql']

    def gql(q, **v):
        if 'closingIssuesReferences' in q:
            raise RuntimeError('GraphQL: parse error')
        return real(q, **v)
    ex._g['gql'] = gql
    ex.run(webhook.Action('answered', 0, 'x', dict(node='N')))
    assert ex.relays and ex.errors == [] and any('not read' in d for d in ex.done)
