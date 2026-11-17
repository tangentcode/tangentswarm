"""swarm-watch (tangentswarm #40): Codex outage, promote stall, hung processes and sweeps."""
import os
import signal
from pathlib import Path

from tangentswarm import alerts, board, hung, watchdog as WD

T0 = 1_800_000_000.0


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


def pr_full(req=None, reply=None, body='Reviewed commit hhhhhhhhhh. Looks fine.'):
    comments = []
    if req is not None:
        comments.append({'author': {'login': 'tangentstorm'}, 'body': '@codex review', 'createdAt': iso(req)})
    if reply is not None:
        comments.append({'author': {'login': 'chatgpt-codex-connector'}, 'body': body, 'createdAt': iso(reply)})
    return {'headRefOid': 'h' * 40, 'comments': {'nodes': comments}, 'reviews': {'nodes': []}}


# -- item 3: Codex outage ----------------------------------------------------------------

def test_codex_wait_silent_after_30_minutes_reply_clears_limit_alerts():
    f = WD.codex_facts(pr_full(req=T0))
    assert WD.codex_wait(f, T0 + 600) is None
    assert WD.codex_wait(f, T0 + 1900)[0] == 'silent'
    assert WD.codex_wait(WD.codex_facts(pr_full(req=T0, reply=T0 + 60)), T0 + 9999) is None
    lim = WD.codex_facts(pr_full(req=T0, reply=T0 + 60, body='You have reached your Codex usage limits for code reviews.'))
    assert WD.codex_wait(lim, T0 + 61) == ('limit', 'Codex answered with a usage-limit notice')
    assert WD.codex_wait(WD.codex_facts(pr_full()), T0 + 9999) is None  # never asked


def test_one_alert_per_outage_not_one_per_pr():
    st = alerts.state()
    first = WD.outage_update(st, {5: ('silent', 'no answer for 31 min'), 6: ('limit', 'usage limit')}, T0)
    assert first and '#5' in first and '#6' in first and 'usage limit' in first
    assert WD.outage_update(st, {5: ('silent', 'x'), 6: ('silent', 'y'), 7: ('silent', 'z')}, T0 + 300) is None
    assert WD.outage_update(st, {}, T0 + 600) is None and 'codex_outage' not in st.data  # over
    assert WD.outage_update(st, {8: ('silent', 'x')}, T0 + 900)  # a new outage: a new alert


def snap_node(n, col, kind='PullRequest', branch='b', head='h' * 40, closes=()):
    c = {'__typename': kind, 'number': n, 'state': 'OPEN', 'title': f't{n}', 'repository': {'nameWithOwner': WD.REPO}}
    if kind == 'PullRequest':
        c.update(headRefName=branch, headRefOid=head, isDraft=False, updatedAt=f'u{n}',
                 closingIssuesReferences={'nodes': [{'number': i, 'repository': {'nameWithOwner': WD.REPO}}
                                                    for i in closes]})
    return {'id': f'ITEM{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]}, 'content': c}


class Rig:
    def __init__(self, nodes, details=None, closed=()):
        from boardfake import answer
        self.alerts, self.incidents, self.wakes, self.moves, self.killed = [], [], [], [], []
        self.details = details or {}

        def gql(q, **v):
            if 'search(' in q:
                return {'search': {'nodes': list(closed)}}
            if 'pullRequest(number:' in q:
                n = int(q.split('pullRequest(number:')[1].split(')')[0])
                return {'repository': {'p': self.details[n]}}
            return answer(nodes)
        self.panes = type('P', (), {'latest': {}, 'once': lambda s, prs, now: []})()
        self.w = WD.Watchdog(g={'gql': gql}, panes=self.panes,
                             alert=lambda text, key, pr=None, st=None: self.alerts.append((text, key)) or True,
                             wake=lambda why, key, pr=None, detail=None: self.wakes.append((why, detail)),
                             incident=lambda key, title, what, **kw: self.incidents.append(title),
                             move=lambda item, col, n=None: self.moves.append((item, col)),
                             kill=lambda pid: self.killed.append(pid) or [pid], hung_sample=lambda: {},
                             on_main=lambda head, n: False, out=lambda line: None)


def test_the_watchdog_alerts_once_for_a_codex_outage():
    r = Rig([snap_node(5, board.AUTO_REVIEW), snap_node(6, board.AUTO_REVIEW, branch='c')],
            details={5: pr_full(req=T0), 6: pr_full(req=T0)})
    r.w.codex_pass(_snap(r), T0 + 1900)
    r.w.codex_pass(_snap(r), T0 + 2200)
    assert len(r.alerts) == 1 and '2 PR(s)' in r.alerts[0][0] and r.incidents == ['Codex outage']


def _snap(r):
    from tangentswarm import boardcache
    return boardcache.snapshot(r.w.g['gql'], max_age=0)


# -- item 4a: Human Review empty while Staging is not --------------------------------------

def test_an_empty_human_review_alone_is_not_an_alert():
    assert WD.promote_stall(0, [], {}, None, T0) == (None, None)
    assert WD.promote_stall(1, [5], {}, T0 - 9999, T0) == (None, None)


def test_a_stalled_promote_alerts_after_the_grace_with_a_diagnosis_and_a_fix():
    text, since = WD.promote_stall(0, [5, 6], {5: 'head abc has no gate pass', 6: 'is a draft'}, None, T0, grace=600)
    assert text is None and since == T0
    text, _ = WD.promote_stall(0, [5, 6], {5: 'head abc has no gate pass', 6: 'is a draft'}, since, T0 + 601, grace=600)
    assert 'promote stalled' in text and '#5: head abc has no gate pass -> fix: run hr-gate --apply 5' in text
    assert 'gh pr ready 6' in text


def test_the_watchdog_sends_the_promote_alert_once_per_staging_set(monkeypatch):
    monkeypatch.setattr(WD, 'PROMOTE_GRACE', 0)
    r = Rig([snap_node(5, board.STAGING)])
    r.w.promoter = type('P', (), {'left': [(5, 'h', 'human comment after the gate (bob)')], 'g': r.w.g})()
    r.w.promote_pass(_snap(r), T0)
    assert len(r.alerts) == 1 and 'answer the comment on #5' in r.alerts[0][0]
    assert r.alerts[0][1] == 'promote-stall:5' and r.incidents == ['Human Review empty while Staging holds PRs']


# -- item 4d: hung processes ------------------------------------------------------------------

def test_hung_needs_age_and_a_flat_process_tree():
    cur = {1: dict(cmd='pytest', cwd='/w', cpu=10, started=T0, ppid=0),
           2: dict(cmd='vitest', cwd='/w', cpu=10, started=T0, ppid=0),
           3: dict(cmd='pytest worker', cwd='/w', cpu=10, started=T0, ppid=1)}
    tr = hung.track({}, cur, T0 + 1000)
    assert hung.hung(tr, T0 + 1000, hung_min=1800, flat_min=600) == []           # too young
    tr = hung.track(tr, {**cur, 2: dict(cur[2], cpu=99)}, T0 + 2500)
    got = hung.hung(tr, T0 + 2500, hung_min=1800, flat_min=600)
    assert [p for p, _ in got] == [1]  # 2 still uses CPU; 3 is a child of 1 (its kill takes it)
    assert hung.hung(tr, T0 + 99999, hung_min=1800, flat_min=99999) == []  # no CPU-blind age limit


def fake_proc(tmp_path, procs):
    """A /proc with stat, cmdline, comm and a cwd symlink per pid."""
    root = tmp_path / 'proc'
    root.mkdir(parents=True)
    (root / 'stat').write_text('cpu 1 2 3\nbtime 1000\n')
    for pid, (cmd, cwd, ppid, cpu, start) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / 'cmdline').write_bytes(cmd.replace(' ', '\0').encode())
        (d / 'comm').write_text(cmd.split()[0].rsplit('/', 1)[-1])
        f = ['S', str(ppid)] + ['0'] * 9 + [str(cpu), '0'] + ['0'] * 6 + [str(start)]
        (d / 'stat').write_text(f"{pid} ({cmd.split()[0]}) " + ' '.join(f))
        os.symlink(cwd, d / 'cwd')
    return root


def test_sample_finds_test_runs_in_worker_worktrees_only(tmp_path):
    w = str(tmp_path / 'platform.feat')
    root = fake_proc(tmp_path, {10: ('python -m pytest tests', w, 1, 50, 300),
                                11: ('python serve.py', w, 1, 5, 300),
                                12: ('python -m pytest x', '/elsewhere', 1, 5, 300),
                                13: ('bash -c pytest', w, 1, 5, 300),
                                14: ('/usr/bin/claude', w, 1, 5, 300)})
    got = hung.sample(lambda cwd: w if cwd.startswith(w) else None, proc=root, uid=os.getuid(), hz=100, btime=1000)
    assert list(got) == [10] and got[10]['cpu'] == 50 and got[10]['started'] == 1003 and got[10]['cwd'] == w


def test_a_launcher_that_waits_on_busy_workers_is_not_hung(tmp_path):
    """Codex on PR #51: the CPU of the whole process tree counts, not the launcher's own."""
    w = str(tmp_path / 'platform.feat')
    worker = lambda cpu: {20: ('npm test', w, 1, 0, 300), 21: ('node worker.js', w, 20, cpu, 300),  # noqa: E731
                          22: ('node leaf.js', w, 21, cpu, 300)}
    in_w = lambda cwd: w if cwd.startswith(w) else None  # noqa: E731
    s1 = hung.sample(in_w, proc=fake_proc(tmp_path / 'a', worker(100)), uid=os.getuid(), hz=100, btime=0)
    assert s1[20]['cpu'] == 200  # its own 0 plus both descendants
    tr = hung.track({}, s1, T0)
    s2 = hung.sample(in_w, proc=fake_proc(tmp_path / 'b', worker(900)), uid=os.getuid(), hz=100, btime=0)
    tr = hung.track(tr, s2, T0 + 5000)
    assert tr[20]['flat_since'] == T0 + 5000 and hung.hung(tr, T0 + 5000, hung_min=1800, flat_min=600) == []
    tr = hung.track(tr, s2, T0 + 6000)
    assert [p for p, _ in hung.hung(tr, T0 + 6000, hung_min=0, flat_min=600)] == [20]


def test_kill_terms_the_process_and_its_children_then_kills_survivors(tmp_path):
    root = fake_proc(tmp_path, {10: ('pytest', '/w', 1, 0, 0), 11: ('chrome --headless', '/w', 10, 0, 0),
                                12: ('other', '/w', 1, 0, 0)})
    sent = []
    pids = hung.kill(10, proc=root, signal_fn=lambda p, s: sent.append((p, s)), sleep=lambda s: None)
    assert pids == [10, 11]
    assert sent == [(10, signal.SIGTERM), (11, signal.SIGTERM), (10, signal.SIGKILL), (11, signal.SIGKILL)]


def test_the_watchdog_kills_a_hung_run_and_wakes_swarmbot_with_the_capture():
    r = Rig([])
    r.w.panes.latest = {'%3': {'id': '%3', 'pr': 7, 'wt': {'top': '/w'}, 'screen': 'line\nRunning pytest...'}}
    sample = {10: dict(cmd='pytest -x', cwd='/w', cpu=5, started=T0, ppid=1)}
    r.w.hung_sample = lambda: sample
    r.w.hung_pass(T0 + 100)
    assert r.killed == []
    out = r.w.hung_pass(T0 + 4000)
    assert r.killed == [10] and 'killed hung pid 10' in out[0]
    why, detail = r.wakes[0]
    assert 'pytest -x' in why and detail['capture'].endswith('Running pytest...') and detail['pane'] == '%3'
    assert detail['killed'] == [10] and r.incidents == ['hung process in w']


# -- item 6: sweeps -----------------------------------------------------------------------

def test_issue_cards_with_no_pr_go_to_backlog_but_not_a_launch_in_flight():
    from boardfake import answer
    from tangentswarm import boardcache
    nodes = [snap_node(1, board.IN_PROGRESS, kind='Issue'), snap_node(2, board.ACTIVE, kind='Issue'),
             snap_node(3, board.ACTIVE, kind='Issue'), snap_node(4, board.BACKLOG, kind='Issue'),
             snap_node(5, board.NEEDS_INPUT, kind='Issue'), snap_node(9, board.IN_PROGRESS, closes=[2])]
    snap = boardcache.snapshot(lambda q, **v: answer(nodes), max_age=0)
    assert WD.issue_sweep(snap, hold={3}) == [(1, 'ITEM1', board.IN_PROGRESS)]
    assert WD.launching_issues({'launching': {'7': 1}, 'unstarted': {'50': {'issue': 8}},
                                'named': {'9': T0, '10': T0 - 9999}}, T0) == {7, 8, 9}


def test_done_target():
    assert WD.done_target('OPEN', False, 'main', None) == board.IN_PROGRESS
    assert WD.done_target('CLOSED', False, 'main', None) == board.CANCELED
    assert WD.done_target('MERGED', True, 'work', None) == board.IN_PROGRESS
    assert WD.done_target('MERGED', True, 'main', False) == board.IN_PROGRESS
    assert WD.done_target('MERGED', True, 'main', True) is None
    assert WD.done_target('MERGED', True, 'main', None) is None  # unknown: stays


def test_the_sweep_moves_cards_out_of_done_and_issues_to_backlog():
    def closed(n, merged, base, col=board.DONE):
        return {'number': n, 'state': 'MERGED' if merged else 'CLOSED', 'merged': merged, 'baseRefName': base,
                'headRefOid': f'{n}' * 8, 'projectItems': {'nodes': [
                    {'id': f'C{n}', 'project': {'id': board.PROJECT_ID},
                     'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]}}]}}
    r = Rig([snap_node(1, board.IN_PROGRESS, kind='Issue'), snap_node(9, board.DONE)],
            closed=[closed(20, True, 'main'), closed(21, False, 'main'), closed(22, True, 'main', board.STAGING)])
    out = r.w.sweep_pass(_snap(r), T0)
    assert ('ITEM1', board.BACKLOG) in r.moves and ('ITEM9', board.IN_PROGRESS) in r.moves
    assert ('C20', board.IN_PROGRESS) in r.moves and ('C21', board.CANCELED) in r.moves
    assert not any(m[0] == 'C22' for m in r.moves)  # not in Done
    assert len(out) == 4 and len(r.incidents) == 4


class _R:
    def __init__(self, rc):
        self.returncode = rc


def head_check(ancestor_rcs, fetch_rc=0):
    calls, rcs = [], iter(ancestor_rcs)
    got = WD.head_on_main('abc', 7, git=lambda *a: calls.append(a[0]) or _R(next(rcs) if a[0] == 'merge-base'
                                                                          else fetch_rc))
    return got, calls


def test_head_on_main_trusts_only_a_yes_before_a_fetch():
    assert head_check([0]) == (True, ['merge-base'])
    assert head_check([1, 0]) == (True, ['merge-base', 'fetch', 'merge-base'])  # origin/main was stale
    assert head_check([1, 1]) == (False, ['merge-base', 'fetch', 'merge-base'])
    assert head_check([128, 0]) == (True, ['merge-base', 'fetch', 'merge-base'])  # the object was missing
    assert head_check([1], fetch_rc=1) == (None, ['merge-base', 'fetch'])  # cannot tell: the card stays


def test_worker_root(tmp_path, monkeypatch):
    from tangentswarm import slots
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', tmp_path)
    assert WD.worker_root(str(tmp_path / 'platform.feat' / 'web' / 'x')) == str(tmp_path / 'platform.feat')
    assert WD.worker_root(str(tmp_path / 'other')) is None
    assert Path(WD.worker_root(str(tmp_path / 'platform.feat') + ' (deleted)')).name == 'platform.feat'


RUNNING = ('<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + 'h' * 40 + '","has_any_findings":null,'
           '"status":"running"}} -->\n| 📝 **Code Review** | 🔄 **Running** since now | `abc` |')


def test_a_running_status_comment_is_not_a_codex_answer():
    """Codex on PR #51: a stalled review that only said "running" is still an outage."""
    from tangentswarm import webhook
    f = WD.codex_facts(pr_full(req=T0, reply=T0 + 30, body=RUNNING))
    assert f['reply'] is False and WD.codex_wait(f, T0 + 1900)[0] == 'silent'
    done = RUNNING.replace('"running"', '"completed"').replace('🔄 **Running**', '✅ **Completed**')
    assert WD.codex_facts(pr_full(req=T0, reply=T0 + 30, body=done))['reply'] is True
    assert webhook.codex_running(RUNNING) and not webhook.codex_running(done)
    assert webhook.codex_running('codex-review-state {"code_review": {"status": "running"}}')  # spaced too
    p = {'action': 'created', 'repository': {'full_name': webhook.REPO},
         'comment': {'user': {'login': 'chatgpt-codex-connector[bot]'}, 'body': RUNNING},
         'issue': {'number': 7, 'pull_request': {}}}
    assert webhook.classify('issue_comment', p)[0].why == 'Codex review is running'


def test_a_dry_run_records_no_outage_and_the_live_pass_still_alerts():
    """Codex on PR #51: a dry run saved the outage, so the daemon never announced it."""
    r = Rig([snap_node(5, board.AUTO_REVIEW)], details={5: pr_full(req=T0)})
    r.w.dry = True
    assert r.w.codex_pass(_snap(r), T0 + 1900)[0].startswith('would alert swarmbot')
    assert 'codex_outage' not in alerts.state().data and 'codex_outage' not in r.w.st.data
    r.w.dry = False
    r.w.codex_pass(_snap(r), T0 + 2000)
    assert len(r.alerts) == 1


def test_a_late_review_of_an_older_head_is_not_the_answer():
    """Codex on PR #51: only a result for the current head answers the request."""
    head, old = 'h' * 40, 'o' * 40
    full = pr_full(req=T0)
    full['reviews']['nodes'] = [{'author': {'login': 'chatgpt-codex-connector'}, 'submittedAt': iso(T0 + 60),
                                 'commit': {'oid': old}}]
    full['comments']['nodes'] += [
        {'author': {'login': 'chatgpt-codex-connector'}, 'createdAt': iso(T0 + 61),
         'body': f'<!-- codex-review-state:v1 {{"code_review":{{"commit_sha":"{old}","status":"completed"}}}} -->'},
        {'author': {'login': 'chatgpt-codex-connector'}, 'createdAt': iso(T0 + 62),
         'body': f"Reviewed commit `{old[:10]}`. Didn't find any major issues."}]
    f = WD.codex_facts(full)
    assert f['reply'] is False and WD.codex_wait(f, T0 + 1900)[0] == 'silent'
    full['reviews']['nodes'][0]['commit']['oid'] = head
    assert WD.codex_facts(full)['reply'] is True
    summary = pr_full(req=T0, reply=T0 + 60,
                      body=f'<!-- codex-review-state:v1 {{"code_review":{{"commit_sha":"{head}","status":"completed"}}}} -->')
    assert WD.codex_facts(summary)['reply'] is True
    limit = pr_full(req=T0, reply=T0 + 60, body='You have reached your Codex usage limits for code reviews.')
    assert WD.codex_facts(limit)['limit'] is True  # a limit notice names no head and still counts


def test_swarm_watch_once_drains_its_wake_posts_before_it_exits(monkeypatch):
    """Codex on PR #71: swarm-watch --once is a short-lived process; its wake POSTs run in
    daemon threads, so main() waits for them before it returns."""
    from tangentswarm import watchdog, webhook
    order = []

    class Fake:
        def __init__(self, dry=False):
            pass

        def once(self):
            order.append('once')
    monkeypatch.setattr(watchdog, 'Watchdog', Fake)
    monkeypatch.setattr(webhook, 'drain_posts', lambda *a, **k: order.append('drain') or 0)
    assert watchdog.main(['--once']) == 0
    assert order == ['once', 'drain']
