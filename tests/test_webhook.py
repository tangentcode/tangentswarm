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
        ['cascade', 'reject']  # tangentswarm #135: the refill first
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


CODEX_REVIEW = ['gh', 'pr', 'comment', '7', '-R', W.REPO, '--body', '@codex review']


def test_a_push_to_a_non_draft_pr_in_in_progress_goes_to_auto_review_with_codex_for_the_head():
    """tangentswarm #84: platform #539 got a push in In Progress (not a draft) and sat there."""
    payload = {'action': 'synchronize', 'repository': REPO, 'pull_request': pr(head='e' * 40)}
    acts = W.classify('pull_request', payload)
    assert [(a.kind, a.args.get('push'), a.args.get('head')) for a in acts] == [('handoff', True, 'e' * 40)]
    ex = Fake(col=board.IN_PROGRESS, full=dict(codex_pr(head='e' * 40), isDraft=False))
    ex.run(acts[0])
    assert ex.calls[0][-1] == board.OPTION[board.AUTO_REVIEW]
    assert ex.calls.count(CODEX_REVIEW) == 1
    assert not any(c[:3] == ['gh', 'pr', 'ready'] for c in ex.calls)  # it is ready already
    assert ex.cascades == [] and any('posted "@codex review" for eeeeeeee' in d for d in ex.done)


def test_a_second_delivery_of_the_same_push_posts_codex_once():
    ex = Fake(col=board.IN_PROGRESS, full=dict(codex_pr(), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    ex.col = board.AUTO_REVIEW
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls.count(CODEX_REVIEW) == 1
    assert [c[-1] for c in ex.calls if c[:3] == ['gh', 'project', 'item-edit']] == [board.OPTION[board.AUTO_REVIEW]]


def test_a_push_in_in_progress_skips_codex_when_the_head_already_has_a_request_or_a_review():
    ex = Fake(col=board.IN_PROGRESS, full=dict(codex_pr(clean=True), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls[0][-1] == board.OPTION[board.AUTO_REVIEW] and CODEX_REVIEW not in ex.calls


def test_a_push_to_a_draft_in_in_progress_is_left_to_the_stop_hook():
    payload = {'action': 'synchronize', 'repository': REPO, 'pull_request': pr(draft=True)}
    assert [a.kind for a in W.classify('pull_request', payload)] == ['skip']
    # the payload said ready, but the PR is a draft by the time the handler reads it
    ex = Fake(col=board.IN_PROGRESS, full=dict(codex_pr(), isDraft=True))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls == [] and 'left to the Stop hook' in ex.done[0]


@pytest.mark.parametrize('col', [None, board.NEEDS_INPUT, board.DONE, board.CANCELED, board.PARKED])
def test_a_push_with_no_card_or_in_a_held_column_is_not_moved(col):
    ex = Fake(col=col, full=dict(codex_pr(), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls == [] and 'not moved' in ex.done[0]


def test_ready_for_review_in_in_progress_marks_ready_and_requests_codex():
    ex = Fake(col=board.IN_PROGRESS, full=dict(codex_pr(), isDraft=True))
    ex.run(W.Action('handoff', 7, args={'head': 'a' * 40}))
    assert ['gh', 'pr', 'ready', '7', '-R', W.REPO] in ex.calls and CODEX_REVIEW in ex.calls
    assert ex.calls[1][-1] == board.OPTION[board.AUTO_REVIEW]


def test_a_new_head_past_in_progress_returns_to_auto_review():
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


def test_a_project_6_merge_closes_and_moves_a_cross_repo_issue_of_its_board():
    """tangentswarm #114: a methodology PR that fixes tangentswarm #107 takes #107 out of Active,
    in its own repo; an issue of a repo off the board is still left to GitHub."""
    meth, ts = 'tangentcode/methodology', 'tangentcode/tangentswarm'
    b6 = board.for_repo(ts)
    reads = []
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': [
        {'number': 107, 'repository': {'nameWithOwner': ts}},
        {'number': 17, 'repository': {'nameWithOwner': W.REPO}}]}})

    def gql(q, **v):
        if 'issueOrPullRequest' in q:
            reads.append((f"{v['owner']}/{v['name']}", v['n']))
            return {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': [
                {'id': 'ISSUE107', 'project': {'id': b6.id}, 'fieldValueByName': {'name': board.ACTIVE}}]}}}}
        return {'repository': {'p': ex.full, 'pullRequest': ex.full}}
    ex._g['gql'] = gql
    ex.run(W.Action('merged', 111, args={'repo': meth}))
    assert ['gh', 'issue', 'close', '107', '-R', ts, '--reason', 'completed'] in ex.calls
    assert reads == [(ts, 107)]
    edits = [c for c in ex.calls if c[:3] == ['gh', 'project', 'item-edit']]
    assert [(c[c.index('--id') + 1], c[-1]) for c in edits][-1] == ('ISSUE107', b6.option[board.DONE])
    assert not any(c[:4] == ['gh', 'issue', 'close', '17'] for c in ex.calls)
    assert any(f'{W.REPO}#17: not an issue of this board' in d for d in ex.done)
    assert f'{ts}#107 -> {board.DONE}' in ex.done


def merged_payload(base='main', merged=True, n=540):
    return {'action': 'closed', 'repository': REPO, 'pull_request': pr(n, base=base, merged=merged)}


def test_a_merge_into_main_wakes_swarm_promoted_before_the_done_move_and_the_sweeps():
    """tangentswarm #67 (a): SIGUSR1 to swarm-promoted is the FIRST thing a merge into main
    does: before the cascade, the Done-card move and active-gate --sweep / --promote-missing."""
    order = []
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': []}})
    ex.signal_promoted = lambda: order.append('signal') or 'woken (test)'
    ex.cascade_fn = lambda: order.append('cascade') or 'queued'
    ex.sh = lambda argv: order.append(argv) or '[]'
    W.handle('pull_request', merged_payload(), ex=ex)
    assert order[0] == 'signal' and order.count('signal') == 1
    done = next(i for i, c in enumerate(order) if isinstance(c, list) and c[-1] == board.OPTION[board.DONE])
    sweep = next(i for i, c in enumerate(order) if isinstance(c, list) and c[1:] == ['--sweep', '--demote'])
    missing = next(i for i, c in enumerate(order) if isinstance(c, list) and c[1:] == ['--promote-missing'])
    assert 0 < order.index('cascade') < done < sweep < missing
    assert any('swarm-promoted woken' in d for d in ex.done)
    assert W.PROMOTED_WOKEN == []  # the injected signal ran, never the module default


@pytest.mark.parametrize('payload', [merged_payload(base='deploy')])
def test_a_merge_into_another_base_sends_no_signal(payload):
    """tangentswarm #67 (a): a merge into another base takes no wake path. (tangentswarm #136:
    an unmerged close does: it may free a Human Review slot; see test_refill.)"""
    woken = []
    ex = Fake(col=board.HUMAN_REVIEW)
    ex.signal_promoted = lambda: woken.append(1) or 'woken (test)'
    W.handle('pull_request', payload, ex=ex)
    assert woken == [] and W.PROMOTED_WOKEN == []


def test_a_dry_run_or_another_repo_merge_sends_no_signal():
    woken = []
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': []}})
    ex.dry = True
    ex.signal_promoted = lambda: woken.append(1) or 'woken (test)'
    ex.run(W.Action('merged', 540))
    assert woken == [] and 'would send SIGUSR1 to swarm-promoted' in ex.done
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': []}})
    ex.signal_promoted = lambda: woken.append(1) or 'woken (test)'
    ex.run(W.Action('merged', 3, args={'repo': 'tangentcode/tangentswarm'}))
    assert woken == []  # swarm-promoted refills Project 4 only


def test_a_failed_signal_never_stops_the_merge_handling():
    ex = Fake(col=board.HUMAN_REVIEW, full={'closingIssuesReferences': {'nodes': []}})
    ex.signal_promoted = lambda: (_ for _ in ()).throw(OSError('no such process'))
    ex.run(W.Action('merged', 540))
    assert ex.errors == [] and ex.cascades == [1]
    assert any('not woken: OSError: no such process' in d for d in ex.done)


def test_signal_promoted_sends_sigusr1_only_to_a_pid_that_catches_it(tmp_path):
    """The default action of SIGUSR1 ends a process: no MainPID, or a pid with no handler
    (the daemon before its run() loop, a reused pid), is never sent it. Never a real signal here."""
    import signal
    real = W.REAL_SIGNAL_PROMOTED  # conftest stubs W.signal_promoted
    sent = []
    proc = tmp_path / 'proc'
    (proc / '41').mkdir(parents=True)
    (proc / '41' / 'status').write_text('Name:\tpython\nSigCgt:\t0000000000000200\n')  # bit 10: SIGUSR1
    (proc / '42').mkdir()
    (proc / '42' / 'status').write_text('Name:\tother\nSigCgt:\t0000000000004002\n')
    assert W.catches_sigusr1(41, proc) and not W.catches_sigusr1(42, proc) and not W.catches_sigusr1(43, proc)
    caught = lambda pid: W.catches_sigusr1(pid, proc)
    kill = lambda pid, sig: sent.append((pid, sig))
    assert real(pid_fn=lambda: 41, catches=caught, kill=kill) == 'woken (SIGUSR1 to pid 41)'
    assert sent == [(41, signal.SIGUSR1)]
    assert 'no SIGUSR1 handler' in real(pid_fn=lambda: 42, catches=caught, kill=kill)
    assert 'no MainPID' in real(pid_fn=lambda: None, catches=caught, kill=kill)
    assert sent == [(41, signal.SIGUSR1)]
    assert W.promoted_pid(sh=lambda argv: '784477\n') == 784477
    assert W.promoted_pid(sh=lambda argv: '0\n') is None


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


# -- tangentswarm #76: tangentstorm comments on Project 6 repos are logged only -------------

def comment(repo, body, user=MICHAL, n=73, is_pr=True):
    issue = {'number': n, 'pull_request': {}} if is_pr else {'number': n}
    return {'action': 'created', 'repository': {'full_name': repo}, 'issue': issue,
            'comment': {'user': user, 'body': body, 'html_url': 'u'}}


def run_all(event, payload, col=board.IN_PROGRESS):
    acts = W.classify(event, payload)
    ex = Fake(col=col, full=codex_pr())
    for a in acts:
        ex.run(a)
    return acts, ex


P6 = ('tangentcode/tangentswarm', 'tangentcode/methodology')


@pytest.mark.parametrize('repo', P6)
@pytest.mark.parametrize('body, marker', [
    ('MERGE-READY c4648b9d0000\nAll checks pass.', 'MERGE-READY'),
    ('NEEDS-INPUT on 044a9d4: which way?', 'NEEDS-INPUT'),
    ('\n  **BLOCKED** on the lock', 'BLOCKED'),
    ('STEP-DONE 3', 'STEP-DONE'),
])
def test_worker_status_comment_on_project_6_is_log_only_with_no_wake(repo, body, marker):
    acts, ex = run_all('issue_comment', comment(repo, body))
    assert [(a.kind, a.pr) for a in acts] == [('log-only', 73)]
    assert acts[0].why == f'worker status comment ({marker})' and acts[0].args['repo'] == repo
    assert ex.wakes == [] and ex.relays == [] and ex.calls == []


@pytest.mark.parametrize('repo', P6)
@pytest.mark.parametrize('is_pr', [True, False])
def test_michals_plain_comment_on_project_6_is_log_only_with_no_wake(repo, is_pr):
    acts, ex = run_all('issue_comment', comment(repo, 'Please also cover rejects.', is_pr=is_pr), col=board.TRIAGE)
    assert [a.kind for a in acts] == ['log-only'] and 'MethBot reads it' in acts[0].why
    assert ex.wakes == [] and ex.relays == []


@pytest.mark.parametrize('repo', P6)
def test_michals_review_on_project_6_is_log_only_with_no_wake(repo):
    review = {'action': 'submitted', 'repository': {'full_name': repo}, 'pull_request': pr(73),
              'review': {'user': MICHAL, 'body': 'MERGE-READY abc', 'html_url': 'u'}}
    acts, ex = run_all('pull_request_review', review)
    assert [a.kind for a in acts] == ['log-only'] and ex.wakes == [] and ex.relays == []


def test_a_marker_later_in_the_body_is_not_a_worker_status():
    assert W.worker_status('hi\nMERGE-READY abc') is None
    assert W.worker_status('Merge-ready abc') == 'MERGE-READY'
    assert W.worker_status('') is None


def test_another_author_on_project_6_is_unchanged():
    acts = W.classify('issue_comment', comment('tangentcode/methodology', 'MERGE-READY x', user={'login': 'someone'}))
    assert [a.kind for a in acts] == ['relay']


def test_the_swarms_own_comment_on_project_6_is_still_a_skip():
    acts = W.classify('issue_comment', comment('tangentcode/methodology', '@codex review'))
    assert [a.kind for a in acts] == ['skip']


def test_a_tangentstorm_comment_on_platform_still_relays():
    acts, ex = run_all('issue_comment', comment('tangentcode/platform', 'MERGE-READY abc', n=541))
    assert [(a.kind, a.pr) for a in acts] == [('relay', 541)]
    assert ex.relays and ex.relays[0][0] == 541 and ex.wakes == []
    acts = W.classify('issue_comment', comment('tangentcode/platform', 'please fix x', n=541))
    assert [a.kind for a in acts] == ['relay']


# -- tangentswarm #95: In Progress PRs are drafts -------------------------------------------

def test_codex_findings_send_the_pr_back_to_in_progress_as_a_draft():
    ex = Fake(col=board.READY, full=codex_pr(findings=True))
    ex.run(W.Action('codex', 541))
    assert ex.calls[0][-1] == board.OPTION[board.IN_PROGRESS] and board.DRAFTED == [(541, W.REPO)]


def test_no_draft_when_the_card_is_in_progress_already_or_no_worker_takes_it():
    Fake(col=board.IN_PROGRESS, full=codex_pr(findings=True)).run(W.Action('codex', 541))
    Fake(col=board.READY, full=codex_pr(findings=True), worker=False).run(W.Action('codex', 541))
    assert board.DRAFTED == []


def _node_fake(typename, number=541, repo=None):
    ex = Fake(full=codex_pr())
    full = ex._g['gql']
    ex._g['gql'] = lambda q, **v: ({'node': {'__typename': typename, 'number': number,
                                             'repository': {'nameWithOwner': repo or W.REPO}}}
                                   if 'node(id:' in q else full(q, **v))
    return ex


def test_michal_rejecting_from_human_review_makes_the_pr_a_draft():
    ex = _node_fake('PullRequest')
    ex.run(W.Action('reject', 0, 'x', dict(node='PR_x', board=board.DEFAULT.number)))
    assert board.DRAFTED == [(541, W.REPO)] and ex.relays


def _real_handoff(monkeypatch, tmp_path, rc, said, ex=None, action=None):
    """Run an action with the real Flow.hand_off (no injected give): only slots is faked.
    Default: a reject of #541."""
    from tangentswarm import flow, slots
    monkeypatch.setattr(flow, 'RELAY_DIR', tmp_path)
    got = {'revive': [], 'start': [], 'queue': []}
    def revive(n, text, out=None, **kw):
        got['revive'].append((n, text, kw))
        out(said)
        return rc
    monkeypatch.setattr(slots, 'revive', revive)
    monkeypatch.setattr(slots, 'start_later', lambda n, branch, relay: got['start'].append((n, branch, relay)) or True)
    monkeypatch.setattr(slots, 'queue_revive', lambda n, text, **kw: got['queue'].append((n, kw)))
    ex = ex or _node_fake('PullRequest')
    ex.give = None
    full = ex._g['gql']
    ex._g['gql'] = lambda q, **v: ({'repository': {'pullRequest': {'headRefName': 'fix-541'}}}
                                   if q.endswith('{headRefName}}}') else full(q, **v))
    ex.run(action or W.Action('reject', 0, 'x', dict(node='PR_x', board=board.DEFAULT.number)))
    return ex, got


def _ci_red(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda sha, repo=None: [{'name': 'Linux mna tests', 'status': 'completed',
                                                                        'conclusion': 'failure'}])
    return Fake(col=board.READY, full=codex_pr(head='c' * 40)), W.Action('ci', 541, args={'sha': 'c' * 40, 'conclusion': 'failure'})


# tangentswarm #135: every caller of Executor.to_worker, each with the real Flow.hand_off
TO_WORKER_PATHS = {
    'reject': (lambda mp: (None, None), 'Michal sent it back from Human Review'),
    'answered': (lambda mp: (None, W.Action('answered', 0, 'x', dict(node='PR_x', board=board.DEFAULT.number))),
                 'Michal answered'),
    'codex': (lambda mp: (Fake(col=board.AUTO_REVIEW, full=codex_pr(findings=True)), W.Action('codex', 541)),
              'gh pr view 541 --comments'),
    'ci': (_ci_red, '"Linux mna tests" failure'),
    'comment': (lambda mp: (Fake(col=board.IN_PROGRESS, full=codex_pr()),
                            W.Action('relay', 541, args={'author': 'someone', 'url': 'u', 'body': 'fix the typo'})),
                'fix the typo'),
}


@pytest.mark.parametrize('path', sorted(TO_WORKER_PATHS))
def test_every_to_worker_path_reaches_the_worker_through_the_real_hand_off(monkeypatch, tmp_path, path):
    """tangentswarm #135: since PR #109, Flow.hand_off takes (p, text, relay_path); the webhook
    passed (n, text), so every relay (reject, Needs Input answer, Codex findings, red CI, a
    comment) raised a TypeError and woke swarmbot. The injected give of the other tests hid it."""
    build, says = TO_WORKER_PATHS[path]
    ex, action = build(monkeypatch)
    ex, got = _real_handoff(monkeypatch, tmp_path, 0, 'resumed', ex=ex, action=action)
    assert ex.errors == [] and ex.wakes == []
    (n, text, kw), = got['revive']
    assert n == 541 and says in text and kw == {'queue': True}
    assert (tmp_path / 'relay-541-webhook.md').read_text() == text + '\n'
    assert any('(resumed)' in d for d in ex.done)


@pytest.mark.parametrize('path', sorted(TO_WORKER_PATHS))
def test_every_to_worker_path_with_no_worktree_queues_a_worker_start(monkeypatch, tmp_path, path):
    """tangentswarm #135: no worktree to resume: the relay waits for a Claude (#101), with the
    PR's head branch and the relay file, and no swarmbot wake."""
    build, _ = TO_WORKER_PATHS[path]
    ex, action = build(monkeypatch)
    ex, got = _real_handoff(monkeypatch, tmp_path, 1, 'no surviving worktree for #541', ex=ex, action=action)
    assert ex.errors == [] and ex.wakes == []
    assert got['start'] == [(541, 'fix-541', tmp_path / 'relay-541-webhook.md')]
    assert any('waiting for a Claude' in d for d in ex.done)


def test_a_reject_wakes_swarm_promoted_before_the_relay():
    """tangentswarm #135: a reject frees a Human Review slot, so it refills as fast as a merge
    (#67, #116): SIGUSR1 to swarm-promoted and the cascade come before the slow relay."""
    order = []
    ex = _node_fake('PullRequest')
    ex.signal_promoted = lambda: order.append('signal') or 'woken (test)'
    ex.cascade_fn = lambda: order.append('cascade') or 'queued'
    ex.give = lambda n, text: order.append('relay') or (True, 'sent')
    payload = {'action': 'edited', 'sender': {'login': W.MICHAL},
               'projects_v2_item': {'content_node_id': 'PR_x', 'project_node_id': board.PROJECT_ID},
               'changes': {'field_value': {'field_node_id': board.STATUS_FIELD,
                                           'from': {'id': board.OPTION[board.HUMAN_REVIEW]},
                                           'to': {'id': board.OPTION[board.IN_PROGRESS]}}}}
    W.handle('projects_v2_item', payload, ex=ex)
    assert order == ['signal', 'cascade', 'relay'] and ex.errors == []
    assert any('a Human Review slot is free: swarm-promoted woken' in d for d in ex.done)
    assert W.PROMOTED_WOKEN == []


@pytest.mark.parametrize('frm,to,woken', [(board.HUMAN_REVIEW, board.READY, 1), (board.STAGING, board.DONE, 0)])
def test_only_a_move_out_of_human_review_wakes_swarm_promoted(frm, to, woken):
    woke = []
    ex = Fake()
    ex.signal_promoted = lambda: woke.append(1) or 'woken (test)'
    (a,) = W.classify('projects_v2_item', {
        'action': 'edited', 'sender': {'login': W.MICHAL},
        'projects_v2_item': {'content_node_id': 'PR_x', 'project_node_id': board.PROJECT_ID},
        'changes': {'field_value': {'field_node_id': board.STATUS_FIELD, 'from': {'id': board.OPTION[frm]},
                                    'to': {'id': board.OPTION[to]}}}})
    ex.run(a)
    assert len(woke) == woken and ex.cascades == [1]


def test_michal_answering_needs_input_makes_the_pr_a_draft_but_not_an_issue():
    ex = _node_fake('PullRequest')
    ex.run(W.Action('answered', 0, 'x', dict(node='PR_x', board=board.DEFAULT.number)))
    assert board.DRAFTED == [(541, W.REPO)]
    ex = _node_fake('Issue', number=600)
    ex.run(W.Action('answered', 0, 'x', dict(node='I_x', board=board.DEFAULT.number)))
    assert board.DRAFTED == [(541, W.REPO)]


def test_a_dry_run_never_drafts():
    ex = _node_fake('PullRequest')
    ex.dry = True
    ex.draft(541)
    assert board.DRAFTED == []


# -- tangentswarm #99: Codex findings on Project 6 relay to the PR's live worker ------------

class FakeHost:
    def __init__(self, fail=None):
        self.told, self.fail = [], fail

    def tell(self, pane, text):
        if self.fail:
            raise self.fail
        self.told.append((pane, text))


def p6_fake(monkeypatch, tmp_path, repo, col=board.AUTO_REVIEW, live=True, branch='fix-x', host=None):
    """A real to_worker (no give) on a Project 6 PR; the Claude sessions and tmux are fakes."""
    from tangentswarm import slots
    ver = tmp_path / 'ver'
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', ver)
    monkeypatch.setattr(slots, '_branch_of', lambda cwd: 'fix-x')
    monkeypatch.setattr(slots, '_alive', lambda pid: True)
    host = host or FakeHost()
    monkeypatch.setattr(slots, 'parking', lambda: object())
    monkeypatch.setattr(slots, 'make_host', lambda sp: host)
    if live:
        slots.SESSIONS.mkdir(parents=True, exist_ok=True)
        wt = ver / (repo.split('/')[1] + '.fix-x')
        (slots.SESSIONS / '41.json').write_text(json.dumps(dict(pid=41, sessionId='s', cwd=str(wt),
                                                                tmux='agents:@5.%55', status='idle')))
    ex = Fake(col=col, full=dict(codex_pr(findings=True), headRefName=branch, state='OPEN'))
    ex.give = None
    return ex, host


@pytest.mark.parametrize('repo', P6)
def test_codex_findings_on_project_6_reach_the_live_worker(monkeypatch, tmp_path, repo):
    ex, host = p6_fake(monkeypatch, tmp_path, repo)
    ex.run(W.Action('codex', 73, args={'repo': repo}))
    assert ex.errors == [] and ex.wakes == []
    assert [p for p, _ in host.told] == ['%55']
    text = host.told[0][1]
    assert 'Codex found issues' in text and 'ROOT-CAUSE RULE' in text
    assert 'follow your brief for the Codex threads' in text and 'do not touch the Codex threads' not in text
    assert any('-> In Progress' in d for d in ex.done) and board.DRAFTED == [(73, repo)]
    assert any(f'sent to the live Claude of {repo}#73' in d for d in ex.done)


@pytest.mark.parametrize('repo', P6)
@pytest.mark.parametrize('live, branch, host', [
    (False, 'fix-x', None),                                  # no live Claude
    (True, 'other-branch', None),                            # a live Claude on another branch
    (True, 'fix-x', FakeHost(fail=RuntimeError('never reached an empty prompt'))),  # the send failed
])
def test_codex_findings_on_project_6_with_no_live_worker_never_wake(monkeypatch, tmp_path, repo, live, branch, host):
    ex, host = p6_fake(monkeypatch, tmp_path, repo, col=board.READY, live=live, branch=branch, host=host)
    ex.run(W.Action('codex', 73, args={'repo': repo}))
    ex.run(W.Action('codex', 73, args={'repo': repo}))
    assert ex.wakes == [] and ex.errors == [] and host.told == []
    assert ex.calls == [] and board.DRAFTED == []  # never demoted with no worker
    logged = [d for d in ex.done if 'logged only, no swarmbot wake' in d]
    assert len(logged) == 1 and logged[0].startswith(f'{repo}#73: Codex findings')  # one line, deduped
    assert any(d.startswith('already logged') for d in ex.done)
    lines = [json.loads(l) for l in W.LOG.read_text().splitlines()]
    assert [r['decision'] for r in lines].count('logged-no-wake') == 1
    assert not any(r['decision'] == 'wake' for r in lines)


def test_the_no_wake_log_is_per_repo_and_pr(monkeypatch, tmp_path):
    ex, _ = p6_fake(monkeypatch, tmp_path, P6[0], live=False)
    for repo, n in ((P6[0], 73), (P6[1], 73), (P6[0], 74)):
        ex.run(W.Action('codex', n, args={'repo': repo}))
    assert len([d for d in ex.done if 'logged only' in d]) == 3 and ex.wakes == []


def test_a_project_6_ci_failure_with_no_live_worker_still_wakes(monkeypatch, tmp_path):
    """Only Codex findings are logged only (#99); the other relays keep their wake."""
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda sha, repo=None: [{'name': 't', 'status': 'completed',
                                                                         'conclusion': 'failure'}])
    ex, _ = p6_fake(monkeypatch, tmp_path, P6[0], live=False)
    ex.run(W.Action('ci', 73, args={'repo': P6[0], 'sha': 'a' * 40}))
    assert len(ex.wakes) == 1 and 'no live Claude' in ex.wakes[0]['why']


def test_codex_findings_on_platform_with_no_worker_still_wake():
    ex = Fake(col=board.READY, full=codex_pr(findings=True), worker=False)
    ex.run(W.Action('codex', 541, args={'repo': W.REPO}))
    assert len(ex.wakes) == 1 and not any('logged only' in d for d in ex.done)
    assert 'do not touch the Codex threads' in ex.relays[0][1]


@pytest.mark.parametrize('repo', P6)
def test_a_codex_comment_on_project_6_classifies_as_codex_not_log_only(repo):
    acts = W.classify('issue_comment', comment(repo, "P1: bad thing", user=CODEX))
    assert [(a.kind, a.pr, a.args['repo']) for a in acts] == [('codex', 73, repo)]
    review = {'action': 'submitted', 'repository': {'full_name': repo}, 'pull_request': pr(73),
              'review': {'user': CODEX, 'body': 'P1', 'commit_id': 'a' * 40}}
    assert [(a.kind, a.args['repo']) for a in W.classify('pull_request_review', review)] == [('codex', repo)]


# -- tangentswarm #105: Human Review rejections and Needs Input answers on Project 6 --------

def p6_move_fake(monkeypatch, tmp_path, repo, live=True, issue=None):
    """p6_fake with a card move: the node is repo's PR 73, Michal commented after the head,
    and the answer query is recorded."""
    ex, host = p6_fake(monkeypatch, tmp_path, repo, col=board.IN_PROGRESS, live=live)
    ex.full['comments'] = {'nodes': [{'author': {'login': W.MICHAL}, 'body': 'Use the ET clock.',
                                      'createdAt': '2026-11-16T00:00:00Z'}]}
    ex.answer_reads = []
    full = ex._g['gql']

    def gql(q, **v):
        if 'node(id:' in q:
            return {'node': {'__typename': 'PullRequest', 'number': 73, 'repository': {'nameWithOwner': repo}}}
        if q == W.ANSWERS_Q:
            ex.answer_reads.append((v['owner'], v['name']))
            return {'repository': {'pullRequest': {'closingIssuesReferences': {'nodes': [issue] if issue else []}}}}
        return full(q, **v)
    ex._g['gql'] = gql
    return ex, host


def _move(kind, repo):
    return W.Action(kind, 0, 'x', dict(node='PR_x', board=board.for_repo(repo).number))


@pytest.mark.parametrize('repo', P6)
def test_a_project_6_rejection_reaches_the_live_worker_with_no_wake(monkeypatch, tmp_path, repo):
    ex, host = p6_move_fake(monkeypatch, tmp_path, repo)
    ex.run(_move('reject', repo))
    assert ex.errors == [] and ex.wakes == [] and [p for p, _ in host.told] == ['%55']
    text = host.told[0][1]
    assert 'Michal sent it back from Human Review: Use the ET clock.' in text
    assert text.endswith(W.ROOT_CAUSE_P6) and 'no worker relay' not in ' '.join(ex.done)
    assert board.DRAFTED == [(73, repo)] and any("rejection relayed" in d for d in ex.done)


@pytest.mark.parametrize('repo', P6)
def test_a_project_6_answer_reaches_the_live_worker_with_no_wake(monkeypatch, tmp_path, repo):
    from tangentswarm import stophook
    issue = {'comments': {'nodes': [
        {'author': {'login': W.MICHAL}, 'body': stophook.QUESTION_MARK + '\nET or UTC?', 'createdAt': '1'},
        {'author': {'login': W.MICHAL}, 'body': 'ET.', 'createdAt': '2'}]}}
    ex, host = p6_move_fake(monkeypatch, tmp_path, repo, issue=issue)
    ex.run(_move('answered', repo))
    assert ex.errors == [] and ex.wakes == [] and [p for p, _ in host.told] == ['%55']
    assert 'Michal answered: Use the ET clock. | ET. Build it, then push.' in host.told[0][1]
    assert ex.answer_reads == [tuple(repo.split('/'))]  # the PR's own repo, not platform
    assert board.DRAFTED == [(73, repo)] and any("answer relayed" in d for d in ex.done)


@pytest.mark.parametrize('repo', P6)
@pytest.mark.parametrize('kind', ['reject', 'answered'])
def test_a_project_6_move_with_no_live_worker_wakes_once(monkeypatch, tmp_path, repo, kind):
    ex, host = p6_move_fake(monkeypatch, tmp_path, repo, live=False)
    ex.run(_move(kind, repo))
    ex.run(_move(kind, repo))
    assert host.told == [] and ex.errors == [] and len(ex.wakes) == 1  # deduped
    why = ex.wakes[0]['why']
    assert why.startswith(f'{repo}: #73 ') and 'no worker could take it' in why and 'no live Claude' in why


def test_the_wake_at_is_eastern_time_whatever_the_host_clock():
    sent = []
    W.wake_exception(103, 'x', now=lambda: 1794903060.0, post=sent.append)  # 08:11 UTC
    assert sent[0]['at'] == '2026-11-17T03:11:00-05:00'


# -- tangentswarm #122: a closed issue leaves Triage and Discuss -------------------------------

TS = {'full_name': 'tangentcode/tangentswarm'}


def issue_closed(n=122, reason='completed', repo=TS):
    return {'action': 'closed', 'repository': repo, 'issue': {'number': n, 'state_reason': reason}}


def closed_ex(col, b, monkeypatch):
    """An Executor whose card read finds issue n in col on board b; gh calls go to calls."""
    from tangentswarm import boardcache
    monkeypatch.setattr(boardcache, 'snapshot', lambda gql, max_age=0: {})
    monkeypatch.setattr(boardcache, 'open_prs', lambda snap: [])
    monkeypatch.setattr(boardcache, 'open_issues', lambda snap: [])
    monkeypatch.setattr(board, 'BOARDS', [board.DEFAULT, b])
    node = {'projectItems': {'nodes': [{'id': 'IT1', 'project': {'id': b.id},
                                        'fieldValueByName': {'name': col, 'optionId': b.option.get(col)}}]}}
    calls = []
    ex = W.Executor(g={'project_id': lambda: b.id, 'STATUS_FIELD': b.status_field,
                       'gql': lambda q, **v: {'repository': {'issueOrPullRequest': node}}},
                    sh=lambda argv: calls.append(argv) or '')
    return ex, calls


def p6(missing=()):
    opts = {c: f'opt-{c}' for c in board.COLUMNS if c not in missing}
    return board.Board(6, 'PVT_six', 'F6', opts, ['tangentcode/tangentswarm'], False)


def test_an_issue_close_is_an_issue_closed_action_with_its_reason():
    acts = W.classify('issues', issue_closed(reason='not_planned', repo=REPO))
    assert [(a.kind, a.pr, a.args['reason']) for a in acts] == [('issue_closed', 122, 'not_planned')]
    no_reason = issue_closed(repo=REPO)
    no_reason['issue'].pop('state_reason')
    assert W.classify('issues', no_reason)[0].args['reason'] == 'completed'


@pytest.mark.parametrize('col', [board.TRIAGE, board.DISCUSS])
@pytest.mark.parametrize('reason, to', [('completed', board.DONE), ('not_planned', board.CANCELED),
                                        ('duplicate', board.CANCELED)])
def test_a_closed_issue_in_triage_or_discuss_goes_to_done_or_canceled(col, reason, to, monkeypatch):
    b = p6()
    ex, calls = closed_ex(col, b, monkeypatch)
    for a in W.classify('issues', issue_closed(reason=reason)):
        ex.run(a)
    assert ex.errors == []
    assert len(calls) == 1 and calls[0][:2] == ['gh', 'project'] and calls[0][2] == 'item-edit'
    assert calls[0][calls[0].index('--id') + 1] == 'IT1' and calls[0][-1] == b.option[to]


@pytest.mark.parametrize('col', [board.BACKLOG, board.ACTIVE, board.DONE, board.CANCELED, board.PARKED])
def test_a_closed_issue_in_another_column_is_left_alone(col, monkeypatch):
    ex, calls = closed_ex(col, p6(), monkeypatch)
    for a in W.classify('issues', issue_closed()):
        ex.run(a)
    assert calls == [] and ex.errors == []


@pytest.mark.parametrize('reason, to', [('completed', board.DONE), ('not_planned', board.CANCELED)])
def test_a_board_without_the_column_takes_the_card_off(reason, to, monkeypatch):
    ex, calls = closed_ex(board.TRIAGE, p6(missing=(to,)), monkeypatch)
    for a in W.classify('issues', issue_closed(reason=reason)):
        ex.run(a)
    assert ex.errors == []
    assert calls == [['gh', 'project', 'item-delete', '6', '--owner', 'tangentcode', '--id', 'IT1']]


def test_a_closed_issue_in_triage_on_project_4_goes_to_done(monkeypatch):
    """Same on every board: the platform board moves by the default option ids."""
    ex, calls = closed_ex(board.TRIAGE, board.DEFAULT, monkeypatch)
    monkeypatch.setattr(board, 'BOARDS', [board.DEFAULT])
    for a in W.classify('issues', issue_closed(repo=REPO)):
        ex.run(a)
    assert ex.errors == [] and calls and calls[0][-1] == board.OPTION[board.DONE]


def test_the_dry_run_moves_nothing(monkeypatch):
    ex, calls = closed_ex(board.TRIAGE, p6(missing=(board.CANCELED,)), monkeypatch)
    ex.dry = True
    ex.run(W.classify('issues', issue_closed(reason='not_planned'))[0])
    assert calls == [] and any('would take issue #122 off' in d for d in ex.done)
