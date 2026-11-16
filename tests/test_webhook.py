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
    ('pull_request', {'action': 'opened', 'repository': {'full_name': 'tangentcode/other'},
                      'pull_request': pr()}, 'not a swarm repo'),
    ('check_run', {'action': 'completed', 'repository': REPO}, 'check_run'),
    ('workflow_job', {'action': 'queued', 'repository': REPO}, 'workflow_job'),
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
    def move(frm, to, who=MICHAL, project=board.PROJECT_ID, field=board.STATUS_FIELD):
        return {'action': 'edited', 'sender': who,
                'projects_v2_item': {'content_node_id': 'PR_x', 'project_node_id': project},
                'changes': {'field_value': {'field_node_id': field, 'from': {'id': board.OPTION[frm]},
                                            'to': {'id': board.OPTION[to]}}}}
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS))] == \
        ['reject', 'cascade']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.NEEDS_INPUT, board.IN_PROGRESS))] == ['answered']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.READY, board.BACKLOG))] == ['skip']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS,
                                                                 {'login': 'tangentcode-minbot[bot]'}))] == ['skip']
    # Codex on PR #26: another project, or another field, never acts
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS,
                                                                 project='PVT_other'))] == ['skip']
    assert [a.kind for a in W.classify('projects_v2_item', move(board.HUMAN_REVIEW, board.IN_PROGRESS,
                                                                 field='PVTSSF_other'))] == ['skip']


def test_a_card_of_another_repo_is_not_a_platform_pr():
    ex = Fake()
    ex._g['gql'] = lambda q, **v: {'node': {'number': 7, 'repository': {'nameWithOwner': 'tangentcode/other'}}}
    assert ex.pr_of('X') is None
    ex._g['gql'] = lambda q, **v: {'node': {'number': 7, 'repository': {'nameWithOwner': W.REPO}}}
    assert ex.pr_of('X') == 7


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
        self._g['gql'] = lambda q, **v: {'repository': {'p': self.full, 'pullRequest': self.full}}

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
    monkeypatch.setattr(advance, 'check_runs', lambda sha, repo=None: [{'name': 'Linux mna tests', 'status': 'completed',
                                                              'conclusion': 'failure'}])
    ex = Fake(col=board.READY, full=codex_pr(head='c' * 40))
    ex.run(W.Action('ci', 9, args={'sha': 'c' * 40, 'conclusion': 'failure'}))
    assert '"Linux mna tests" failure' in ex.relays[0][1] and ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS]
    ex2 = Fake(col=board.READY, full=codex_pr(head='d' * 40))
    ex2.run(W.Action('ci', 9, args={'sha': 'c' * 40}))
    assert ex2.relays == [] and 'older head' in ex2.done[0]


def test_green_ci_in_auto_review_runs_minbot_and_in_staging_the_cascade(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda sha, repo=None: [{'name': 'x', 'status': 'completed', 'conclusion': 'success'}])
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
    other = {'number': 17, 'repository': {'nameWithOwner': 'tangentcode/other'}}
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': [
        {'number': 418, 'repository': {'nameWithOwner': W.REPO}}, other]}})
    ex.run(W.Action('merged', 540))
    assert ex.cascades == [1] and ['gh', 'issue', 'close', '418', '-R', W.REPO, '--reason', 'completed'] in ex.calls
    assert [c[-1] for c in ex.calls if c[:3] == ['gh', 'project', 'item-edit']].count(board.OPTION[board.DONE]) == 2
    # Codex on PR #26: a cross-repo "Fixes tangentcode/other#17" never closes platform #17
    assert not any(c[:4] == ['gh', 'issue', 'close', '17'] for c in ex.calls)
    assert any('tangentcode/other#17' in d for d in ex.done)


def test_a_failing_action_wakes_swarmbot_with_the_error():
    ex = Fake()
    ex._g['gql'] = lambda q, **v: (_ for _ in ()).throw(RuntimeError('rate limit'))
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


# -- replay: the handlers on a PR's current state ----------------------------------------------

def test_replay_plan_gives_the_live_actions_for_the_head():
    head = 'e' * 40
    plan = lambda full, ci=None: [(a.kind, a.args.get('commit') or a.args.get('sha')) for a in W.replay_plan(5, full, ci)]
    assert plan(codex_pr(head, clean=True)) == [('codex', head)]
    assert plan(codex_pr(head, findings=True)) == [('codex', head)]
    assert plan(codex_pr(head), 'CI is not green: "x" failure on this head') == [('ci', head)]
    assert plan(codex_pr(head, findings=True), 'CI is not green: "x" failure') == [('ci', head), ('codex', head)]
    assert plan(codex_pr(head, clean=True), 'CI is still running: "x"') == [('codex', head)]
    assert plan(codex_pr(head, clean=True), 'CI is not green: "x" failure') == [('ci', head)]
    assert plan(codex_pr(head)) == [('skip', None)]
    assert plan(dict(codex_pr(head, clean=True), state='MERGED')) == [('skip', None)]


def test_replay_runs_the_handlers(monkeypatch, tmp_path):
    from tangentswarm import advance
    monkeypatch.setattr(W, 'LOG', tmp_path / 'log')
    monkeypatch.setattr(advance, 'check_runs', lambda sha, repo=None: [{'name': 'x', 'status': 'completed', 'conclusion': 'success'}])
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(clean=True))
    acts, done = W.replay(541, ex)
    assert ex.queued == [['--minbot', '541']] and ex.cascades == [1]
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(findings=True))
    W.replay(541, ex)
    assert 'ROOT-CAUSE RULE' in ex.relays[0][1] and ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS]
    assert '"event": "replay"' in (tmp_path / 'log').read_text()


def test_the_wake_sends_the_bearer_key_when_the_key_file_exists(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append((self.headers.get('Authorization'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200)
            self.end_headers()
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(W, 'LOG', tmp_path / 'log')
    url, key = tmp_path / 'url', tmp_path / 'key'
    url.write_text(f'http://127.0.0.1:{srv.server_address[1]}/wake\n')
    try:
        W.post_wake({'why': 'a'}, url_file=url, key_file=tmp_path / 'missing', background=False)
        key.write_text('k123\n')
        W.post_wake({'why': 'b'}, url_file=url, key_file=key, background=False)
    finally:
        srv.shutdown()
    assert got == [(None, {'why': 'a'}), ('Bearer k123', {'why': 'b'})]
    assert 'k123' not in (tmp_path / 'log').read_text()


def test_move_card_uses_the_option_id_for_columns_hr_gate_does_not_know():
    calls, moved = [], []
    g = {'OPT': {'In Progress': 'x'}, 'move': lambda item, col: moved.append(col),
         'project_id': lambda: 'P', 'STATUS_FIELD': 'F'}
    board.move_card(g, 'I', board.IN_PROGRESS, calls.append)
    board.move_card(g, 'I', board.READY, calls.append)
    assert moved == ['In Progress'] and calls[0][-1] == board.OPTION[board.READY]


def test_a_merged_or_closed_pr_card_is_found_outside_the_open_snapshot(monkeypatch):
    """Codex on PR #26: do_merged/do_closed looked only in the open-only board snapshot, so
    the Done/Canceled move never found the card."""
    from tangentswarm import boardcache
    monkeypatch.setattr(boardcache, 'snapshot', lambda gql, max_age=0: {})
    monkeypatch.setattr(boardcache, 'open_prs', lambda snap: [])
    monkeypatch.setattr(boardcache, 'open_issues', lambda snap: [])
    calls = []
    ex = W.Executor(g={'project_id': lambda: 'P', 'STATUS_FIELD': 'F',
                       'gql': lambda q, **v: {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': [
                           {'id': 'OTHER', 'project': {'id': 'PVT_other'}, 'fieldValueByName': None},
                           {'id': 'IT9', 'project': {'id': board.PROJECT_ID},
                            'fieldValueByName': {'name': 'Ready', 'optionId': board.OPTION[board.READY]}}]}}}},
                       'fetch': lambda n, refresh=False: {}},
                    sh=lambda argv: calls.append(argv) or '')
    assert ex.card(9) == ('IT9', board.READY)
    ex.run(W.Action('closed', 9))
    assert calls and calls[0][-1] == board.OPTION[board.CANCELED] and calls[0][calls[0].index('--id') + 1] == 'IT9'


def test_a_clean_review_of_the_fix_advances_despite_an_open_thread_from_an_older_head():
    """Codex on PR #26: an open thread from an older head made the webhook relay fixed
    findings and demote the PR; the MinBot pass (which resolves those threads) must run."""
    head = 'f' * 40
    old = {'isResolved': False, 'isOutdated': False, 'id': 'T1',
           'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'}, 'originalCommit': {'oid': 'e' * 40}}]}}
    full = dict(codex_pr(head, clean=True), reviewThreads={'nodes': [old]})
    ex = Fake(col=board.AUTO_REVIEW, full=full)
    ex.run(W.Action('codex', 541, args={'commit': head}))
    assert ex.queued == [['--minbot', '541']] and ex.relays == []
    on_head = dict(old, comments={'nodes': [{'author': {'login': 'chatgpt-codex-connector'}, 'originalCommit': {'oid': head}}]})
    ex = Fake(col=board.AUTO_REVIEW, full=dict(codex_pr(head), reviewThreads={'nodes': [on_head]}))
    ex.run(W.Action('codex', 541, args={'commit': head}))
    assert ex.relays and ex.queued == []


def test_wakes_are_one_post_per_interval_and_a_burst_waits_in_also():
    """Codex on PR #26: distinct exceptions each woke swarmbot at once."""
    ex = Fake()
    t = [1_000_000.0]
    ex.wake(1, 'a failed', key='a', now=lambda: t[0])
    ex.wake(2, 'b failed', key='b', now=lambda: t[0] + 1)
    ex.wake(3, 'c failed', key='c', now=lambda: t[0] + 2)
    assert [w['why'] for w in ex.wakes] == ['a failed']
    assert not W.flush_wakes(ex.wake_post, now=lambda: t[0] + 30)
    assert W.flush_wakes(ex.wake_post, now=lambda: t[0] + W.WAKE_EVERY + 1)
    assert ex.wakes[1]['why'] == 'b failed' and [x['why'] for x in ex.wakes[1]['also']] == ['c failed']
    assert not W.flush_wakes(ex.wake_post, now=lambda: t[0] + 999)  # nothing pending


def test_no_wake_url_is_logged_not_a_type_error(tmp_path):
    """Codex on PR #26: post_wake passed `why` twice when no URL was configured."""
    W.post_wake({'source': 'swarm-webhook', 'why': 'x', 'pr': 1}, url_file=tmp_path / 'none', background=False)
    rec = json.loads(W.LOG.read_text().splitlines()[-1])
    assert rec['decision'] == 'wake-not-sent' and rec['why'] == 'x' and rec['reason'] == 'no wake URL configured'


def test_a_late_delivery_never_restarts_a_merged_or_done_pr():
    """Codex on PR #26: Codex findings after the merge resumed the worker and moved Done back."""
    ex = Fake(col=board.DONE, full=dict(codex_pr(findings=True), state='MERGED'))
    ex.run(W.Action('codex', 541))
    assert ex.relays == [] and ex.calls == [] and 'no rework' in ex.done[-1]
    ex = Fake(col=board.CANCELED, full=codex_pr(findings=True))  # open PR, but its card is Canceled
    ex.run(W.Action('codex', 541))
    assert ex.relays == [] and ex.calls == []


def test_a_closed_pr_drops_its_unstarted_launch():
    from tangentswarm import slots
    slots.unstarted_add(77, branch='b', brief='', issue=5)
    Fake(col=board.IN_PROGRESS).run(W.Action('closed', 77))
    assert '77' not in slots.load()['unstarted']


def test_a_closed_pr_drops_its_held_launch():
    from tangentswarm import launches
    launches.hold('b', pr=78)
    Fake(col=board.IN_PROGRESS).run(W.Action('closed', 78))
    assert launches.read_queue() == []


def test_a_reopened_pr_leaves_canceled_for_its_working_column():
    """Codex on PR #26: a reopened PR stayed in Canceled."""
    opened = {'action': 'reopened', 'repository': REPO, 'pull_request': pr(9, draft=True)}
    (a,) = W.classify('pull_request', opened)
    assert a.kind == 'card' and a.args['reopened']
    ex = Fake(col=board.CANCELED)
    ex.run(a)
    assert ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS]
    ex = Fake(col=board.AUTO_REVIEW)  # a plain "opened" never moves an existing card
    ex.run(W.Action('card', 9, args=dict(draft=True, what='pr', reopened=False)))
    assert ex.calls == [] and 'already has a card' in ex.done[0]


def test_a_merge_deletes_the_vn_siblings_but_not_one_that_heads_an_open_pr():
    """tangentswarm #39 (Michal 2026-11-15 6:27 PM ET)."""
    p = {'action': 'closed', 'repository': REPO,
         'pull_request': dict(pr(540, merged=True), head={'sha': 'a' * 40, 'ref': 'ptw-quiz'})}
    (a,) = W.classify('pull_request', p)
    assert a.kind == 'merged' and a.args['branch'] == 'ptw-quiz'
    fork = dict(p, pull_request=dict(p['pull_request'], head={'sha': 'a' * 40, 'ref': 'ptw-quiz',
                                                              'repo': {'full_name': 'someone/platform'}}))
    assert W.classify('pull_request', fork)[0].args['branch'] is None  # Codex on PR #42: a fork's branch
    refs = [{'ref': f'refs/heads/{b}'} for b in ('ptw-quiz-v1', 'ptw-quiz-v2', 'ptw-quiz-v10', 'ptw-quiz-vx',
                                                 'ptw-quiz-v1-old')]
    calls = []

    def sh(argv):
        calls.append(argv)
        if 'matching-refs' in argv[2]:
            return json.dumps(refs)
        if 'pulls?state=open' in ' '.join(argv):
            return 'ptw-quiz-v2\nother-branch'
        return '{}'
    ex = Fake(col=board.HUMAN_REVIEW)
    ex.sh = sh
    assert ex.delete_vn(540, 'ptw-quiz') == ['ptw-quiz-v1', 'ptw-quiz-v10']
    deletes = [c[-1] for c in calls if c[:4] == ['gh', 'api', '-X', 'DELETE']]
    assert deletes == [f'repos/{W.REPO}/git/refs/heads/ptw-quiz-v1', f'repos/{W.REPO}/git/refs/heads/ptw-quiz-v10']
    assert any('kept ptw-quiz-v2' in d for d in ex.done)
    assert Fake().delete_vn(1, 'main') == [] and Fake().delete_vn(1, '') == []
