"""swarm-webhook (tangentswarm #31, Michal 2026-11-15 4:44 AM ET): the swarmbot hook rules
without an LLM. Payloads have the shape GitHub sends."""
import hashlib
import hmac
import json
import threading
import urllib.request

import pytest

from tangentswarm import board, webhook as W

REPO = {'full_name': 'tangentcode/platform'}
MICHAL = {'login': 'tangentstorm'}
CODEX = {'login': 'chatgpt-codex-connector[bot]'}


def kinds(event, payload):
    return [(a.kind, a.pr) for a in W.classify(event, payload)]


def pr(n=7, draft=False, base='main', head='a' * 40, merged=False):
    return {'number': n, 'draft': draft, 'base': {'ref': base}, 'head': {'sha': head}, 'merged': merged}


# -- step 0: skips --------------------------------------------------------------------------

@pytest.mark.parametrize('event, payload, why', [
    ('ping', {'zen': 'x'}, 'ping'),
    ('pull_request', {'action': 'opened', 'repository': {'full_name': 'tangentcode/methodology'},
                      'pull_request': pr()}, 'not tangentcode/platform'),
    ('check_run', {'action': 'completed', 'repository': REPO}, 'check_run'),
    ('workflow_job', {'action': 'queued', 'repository': REPO}, 'workflow_job'),
    ('pull_request_review_comment', {'action': 'created', 'repository': REPO}, 'pull_request_review_comment'),
    ('pull_request', {'action': 'labeled', 'repository': REPO, 'pull_request': pr()}, 'labeled'),
    ('pull_request', {'action': 'synchronize', 'repository': REPO, 'pull_request': pr(draft=True)}, 'draft'),
    ('issue_comment', {'action': 'created', 'repository': REPO, 'issue': {'number': 7, 'pull_request': {}},
                       'comment': {'user': MICHAL, 'body': '@codex review'}}, "swarm's own"),
    ('issue_comment', {'action': 'created', 'repository': REPO, 'issue': {'number': 7, 'pull_request': {}},
                       'comment': {'user': {'login': 'tangentcode-minbot[bot]'}, 'body': '### MinBot pass'}}, "own"),
    ('check_suite', {'action': 'completed', 'repository': REPO, 'check_suite': {'pull_requests': []}}, 'no open PR'),
])
def test_step_0_skips(event, payload, why):
    acts = W.classify(event, payload)
    assert [a.kind for a in acts] == ['skip'] and why in acts[0].why


# -- the handlers ---------------------------------------------------------------------------

def test_codex_comments_and_reviews_are_never_skipped():
    summary = {'action': 'created', 'repository': REPO, 'issue': {'number': 541, 'pull_request': {}},
               'comment': {'user': CODEX, 'body': '<!-- codex-review-state:v1 {"code_review": {}} -->'}}
    assert kinds('issue_comment', summary) == [('codex', 541)]
    review = {'action': 'submitted', 'repository': REPO, 'pull_request': pr(541, draft=True),
              'review': {'user': CODEX, 'body': 'P1: ...', 'commit_id': 'b' * 40}}
    assert kinds('pull_request_review', review) == [('codex', 541)]  # also on a draft
    running = dict(summary, comment={'user': CODEX, 'body': 'codex-review-state {"status": "running"}'})
    assert W.classify('issue_comment', running)[0].why == 'Codex review is running'


def test_ci_completed_on_a_pr_and_merged_closed_opened():
    cs = {'action': 'completed', 'repository': REPO,
          'check_suite': {'conclusion': 'failure', 'head_sha': 'c' * 40, 'pull_requests': [{'number': 9}]}}
    acts = W.classify('check_suite', cs)
    assert [(a.kind, a.pr, a.args['conclusion']) for a in acts] == [('ci', 9, 'failure')]
    assert kinds('pull_request', {'action': 'closed', 'repository': REPO, 'pull_request': pr(merged=True)}) == [('merged', 7)]
    assert kinds('pull_request', {'action': 'closed', 'repository': REPO, 'pull_request': pr()}) == [('closed', 7)]
    assert kinds('pull_request', {'action': 'opened', 'repository': REPO, 'pull_request': pr(draft=True)}) == [('card', 7)]
    assert kinds('pull_request', {'action': 'opened', 'repository': REPO, 'pull_request': pr(base='work')}) == [('wake', 7)]
    assert kinds('issues', {'action': 'opened', 'repository': REPO, 'issue': {'number': 600}}) == [('card', 600)]


def test_michal_card_moves():
    def move(frm, to, who=MICHAL):
        return {'action': 'edited', 'sender': who, 'projects_v2_item': {'content_node_id': 'PR_x'},
                'changes': {'field_value': {'from': {'id': board.OPTION[frm]}, 'to': {'id': board.OPTION[to]}}}}
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS))] == \
        ['reject', 'cascade']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.NEEDS_INPUT, board.IN_PROGRESS))] == ['answered']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.READY, board.BACKLOG))] == ['skip']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS,
                                                                 {'login': 'tangentcode-minbot[bot]'}))] == ['skip']


class Fake(W.Executor):
    """An executor with a fake board and fake tools."""
    def __init__(self, col=board.IN_PROGRESS, full=None, worker=True):
        super().__init__(g={'project_id': lambda: 'P', 'STATUS_FIELD': 'F'},
                         sh=lambda argv: self.calls.append(argv) or '{"id": "NEW"}',
                         enqueue=lambda argv, by='': self.queued.append(argv[1:]),
                         cascade=lambda: self.cascades.append(1) or 'queued',
                         give=lambda n, text: (self.relays.append((n, text)) or (worker, 'sent')),
                         wake_post=lambda payload: self.wakes.append(payload))
        self.calls, self.queued, self.cascades, self.relays, self.wakes = [], [], [], [], []
        self.col, self.full = col, full or {}
        self._g['fetch'] = lambda n, refresh=False: self.full

    def card(self, n):
        return ('ITEM', self.col)


def codex_pr(head='a' * 40, findings=False, clean=False):
    threads = [{'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'}}]}}] if findings else []
    comments = [{'author': {'login': 'chatgpt-codex-connector'}, 'body': f"Reviewed commit {head[:8]}. Didn't find any major issues."}] if clean else []
    return {'headRefOid': head, 'reviewThreads': {'nodes': threads}, 'reviews': {'nodes': []},
            'comments': {'nodes': comments}, 'commits': {'nodes': [{'commit': {'committedDate': '2026-11-15T05:00:00Z'}}]}}


def test_codex_findings_relay_to_the_worker_and_go_to_in_progress():
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(findings=True))
    ex.run(W.Action('codex', 541))
    assert ex.relays and 'ROOT-CAUSE RULE' in ex.relays[0][1] and 'gh pr view 541 --comments' in ex.relays[0][1]
    assert ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS]


def test_codex_clean_queues_the_minbot_check_and_a_cascade():
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(clean=True))
    ex.run(W.Action('codex', 541))
    assert ex.queued == [['--minbot', '541']] and ex.cascades == [1] and ex.relays == []


def test_no_worker_means_no_demote_and_one_wake():
    ex = Fake(col=board.READY, full=codex_pr(findings=True), worker=False)
    ex.run(W.Action('codex', 541))
    ex.run(W.Action('codex', 541))
    assert ex.calls == [] and len(ex.wakes) == 1  # deduped
    assert 'no worker could take the relay' in ex.wakes[0]['why']


def test_red_ci_on_the_head_goes_back_to_the_worker(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda sha: [{'name': 'Linux mna tests', 'status': 'completed',
                                                              'conclusion': 'failure'}])
    ex = Fake(col=board.READY, full=codex_pr(head='c' * 40))
    ex.run(W.Action('ci', 9, args={'sha': 'c' * 40, 'conclusion': 'failure'}))
    assert '"Linux mna tests" failure' in ex.relays[0][1] and ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS]
    ex2 = Fake(col=board.READY, full=codex_pr(head='d' * 40))
    ex2.run(W.Action('ci', 9, args={'sha': 'c' * 40}))
    assert ex2.relays == [] and 'older head' in ex2.done[0]


def test_green_ci_in_auto_review_runs_minbot_and_in_staging_the_cascade(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda sha: [{'name': 'x', 'status': 'completed', 'conclusion': 'success'}])
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(head='c' * 40))
    ex.run(W.Action('ci', 9, args={'sha': 'c' * 40}))
    assert ex.queued == [['--minbot', '9']]
    ex = Fake(col=board.STAGING, full=codex_pr(head='c' * 40))
    ex.run(W.Action('ci', 9, args={'sha': 'c' * 40}))
    assert ex.cascades == [1]


def test_a_push_in_in_progress_is_left_to_the_stop_hook_but_a_new_head_past_it_returns_to_auto_review():
    ex = Fake(col=board.IN_PROGRESS, full=codex_pr())
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls == [] and 'left to the Stop hook' in ex.done[0]
    ex = Fake(col=board.HUMAN_REVIEW, full=dict(codex_pr(), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls[0][-1] == board.OPTION[board.AUTO_REVIEW]
    assert ['gh', 'pr', 'comment', '7', '-R', W.REPO, '--body', '@codex review'] in ex.calls and ex.cascades == [1]


def test_a_human_comment_in_human_review_waits_for_michals_move():
    ex = Fake(col=board.HUMAN_REVIEW)
    ex.run(W.Action('relay', 7, args={'author': 'tangentstorm', 'body': 'x'}))
    assert ex.relays == [] and 'waiting for Michal' in ex.done[0]


def test_a_comment_on_a_triage_issue_wakes_swarmbot():
    ex = Fake(col=board.TRIAGE)
    ex.run(W.Action('issue_comment', 600, args={'author': 'tangentstorm', 'url': 'u'}))
    assert ex.wakes and ex.wakes[0]['why'].startswith('TRIAGE handoff')


def test_a_merge_runs_the_cascade_and_closes_the_issues():
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': [{'number': 418}]}})
    ex.run(W.Action('merged', 540))
    assert ex.cascades == [1] and ['gh', 'issue', 'close', '418', '-R', W.REPO, '--reason', 'completed'] in ex.calls
    assert [c[-1] for c in ex.calls if c[:3] == ['gh', 'project', 'item-edit']].count(board.OPTION[board.DONE]) == 2


def test_a_failing_action_wakes_swarmbot_with_the_error():
    ex = Fake()
    ex._g['fetch'] = lambda n, refresh=False: (_ for _ in ()).throw(RuntimeError('rate limit'))
    ex.run(W.Action('codex', 7))
    assert ex.wakes[0]['why'] == 'codex on #7 failed: RuntimeError: rate limit'


# -- the server -----------------------------------------------------------------------------

def test_the_server_checks_the_signature_queues_once_and_logs(tmp_path, monkeypatch):
    secret = tmp_path / 'secret'
    secret.write_text('s3cret\n')
    monkeypatch.setattr(W, 'SECRET_FILES', [secret])
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', 0), W.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_port}/github'
    body = json.dumps({'action': 'opened', 'repository': REPO, 'pull_request': pr(draft=True)}).encode()

    def post(sig, delivery='d1'):
        req = urllib.request.Request(url, data=body, method='POST', headers={
            'X-GitHub-Event': 'pull_request', 'X-GitHub-Delivery': delivery, 'X-Hub-Signature-256': sig})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    good = 'sha256=' + hmac.new(b's3cret', body, hashlib.sha256).hexdigest()
    try:
        assert post('sha256=bad') == 401
        assert post(good) == 200 and post(good) == 200  # a redelivery is not queued twice
    finally:
        srv.shutdown()
    assert len(list(W.INBOX.glob('*.json'))) == 1
    seen = []
    W.drain(lambda: Fake())
    lines = [json.loads(l) for l in W.LOG.read_text().splitlines()]
    assert not any('error' in l for l in lines), lines
    assert lines[0]['decision'] == 'rejected: bad signature'
    assert lines[-1]['event'] == 'pull_request' and lines[-1]['decisions'][0]['kind'] == 'card'
    assert list(W.INBOX.glob('*.json')) == []
