"""The sanity check (tangentswarm #43): board column rules, one Claude per worktree, and one
deduped report per pass (#82). Fake GitHub and /proc data only, no network."""
import json
import os

from tangentswarm import alerts, board, boardcache, boards, sanity, watchdog as WD
from tangentswarm import webhook as W

T0 = 1_800_000_000.0
P = 'tangentcode/platform'
TS = 'tangentcode/tangentswarm'


def pr(n, col, draft=None, branch=None, closes=((1, ()),), repo=P, b=4):
    draft = col in sanity.DRAFT_COLS if draft is None else draft
    return dict(board=b, kind='pr', repo=repo, n=n, col=col, draft=draft, branch=branch or f'b{n}', labels=set(),
                closes=None if closes is None else [(i, set(lab)) for i, lab in closes])


def issue(n, col, labels=(), repo=P, b=4):
    return dict(board=b, kind='issue', repo=repo, n=n, col=col, draft=False, branch=None, labels=set(labels),
                closes=[])


def claude(pid, root, branch=None, repo=P):
    return dict(pid=pid, cwd=root, root=root, repo=repo, branch=branch)


def rules(found):
    return sorted((f['rule'], f['n']) for f in found)


# -- the rules --------------------------------------------------------------------------------

def test_a_clean_board_has_no_findings():
    cards = [issue(1, board.ACTIVE), pr(10, board.IN_PROGRESS, closes=[(1, ())]),
             issue(2, board.ACTIVE), pr(11, board.HUMAN_REVIEW, closes=[(2, ())]),
             issue(3, board.BACKLOG), issue(4, board.TRIAGE), issue(5, board.INBOX, labels=['work2']),
             pr(12, board.READY, closes=[(5, ['work2'])]),  # a work2 PR: no Active card
             pr(13, board.PARKED, closes=[(3, ())])]
    assert sanity.check(cards, [claude(1, '/w/platform.b10', 'b10')]) == []


def test_each_column_rule_has_its_finding():
    cards = [pr(1, None), issue(2, None),
             pr(3, board.BACKLOG), issue(4, board.READY), issue(5, board.DONE),
             pr(6, board.DONE), pr(7, board.CANCELED),
             pr(8, board.NEEDS_INPUT, draft=False), pr(9, board.PARKED, draft=False),
             pr(10, board.AUTO_REVIEW, draft=True), pr(11, board.STAGING, draft=True)]
    got = [r for r in rules(sanity.check(cards, [])) if r[0] != 'flow-pr-active-count']
    assert got == [('draft', 10), ('draft', 11), ('issue-in-pr-column', 4), ('issue-in-pr-column', 5),
                   ('no-status', 1), ('no-status', 2), ('not-draft', 8), ('not-draft', 9),
                   ('open-pr-in-canceled', 7), ('open-pr-in-done', 6), ('pr-in-issue-column', 3)]


def test_a_column_that_workflow_md_does_not_name_is_one_finding():
    cards = [issue(1, 'Later'), pr(2, 'Later'), issue(3, 'Later', b=6)]
    got = sanity.check(cards, [])
    assert [(f['rule'], f['key']) for f in got] == [('unknown-column', 'unknown-column:project 4:Later'),
                                                    ('unknown-column', 'unknown-column:project 6:Later')]
    assert '2 open card(s)' in got[0]['text']


def test_deferred_is_a_valid_column_of_project_4_and_its_cards_are_never_flagged():
    """#82 rule 3: Deferred holds tickets from older workflow versions. Not on Project 6."""
    cards = [issue(1, sanity.DEFERRED), issue(2, sanity.DEFERRED, labels=['work2']),
             pr(3, sanity.DEFERRED, draft=False, closes=[]), pr(4, sanity.DEFERRED, draft=True, closes=[(9, ())]),
             pr(5, sanity.DEFERRED, branch='nobody')]
    assert sanity.check(cards, []) == []
    got = sanity.check([issue(6, sanity.DEFERRED, b=6, repo=TS)], [])
    assert [f['key'] for f in got] == ['unknown-column:project 6:Deferred']


def test_a_deferred_pr_counts_as_the_pr_of_its_active_issue():
    """#82 addendum: platform #471 in Active with PR #486 in Deferred is not an active-pr-count finding."""
    cards = [issue(471, board.ACTIVE), pr(486, sanity.DEFERRED, closes=[(471, ())])]
    assert sanity.check(cards, []) == []
    cards.append(pr(487, board.READY, closes=[(471, ())]))  # a second PR is still one too many
    assert rules(sanity.check(cards, [])) == [('active-pr-count', 471)]
    assert 'flow column or Deferred (#486, #487' in sanity.check(cards, [])[0]['text']


def test_a_flow_pr_of_a_deferred_issue_is_not_flagged():
    cards = [issue(5, sanity.DEFERRED), pr(50, board.READY, closes=[(5, ())])]
    assert sanity.check(cards, []) == []


def test_an_issue_in_needs_input_goes_to_triage_or_discuss_or_its_pr_goes_there():
    """#82 rule 4: with no PR, the issue goes in Triage or Discuss; with a PR, the PR goes in
    Needs Input (and the issue in Active)."""
    got = sanity.check([issue(1, board.NEEDS_INPUT)], [])
    assert rules(got) == [('issue-in-needs-input', 1)] and 'Triage' in got[0]['text'] and 'Discuss' in got[0]['text']
    cards = [issue(2, board.NEEDS_INPUT), pr(20, board.NEEDS_INPUT, closes=[(2, ())])]
    got = sanity.check(cards, [])
    assert rules(got) == [('issue-in-needs-input', 2)]  # the PR finding is not sent twice
    assert 'its PR (#20) goes in Needs Input and the issue in Active' in got[0]['text']
    cards = [issue(3, board.NEEDS_INPUT), pr(30, board.PARKED, closes=[(3, ())])]
    assert 'its PR (#30)' in sanity.check(cards, [])[0]['text']


def test_an_issue_moves_to_active_as_soon_as_its_pr_is_made():
    """#82 rule 4: an issue with an open flow-column PR is in Active."""
    for col in (board.INBOX, board.TRIAGE, board.DISCUSS, board.BACKLOG):
        cards = [issue(1, col), pr(10, board.IN_PROGRESS, closes=[(1, ())])]
        got = sanity.check(cards, [claude(1, '/w/x', 'b10')])
        assert rules(got) == [('issue-not-active', 1)], col  # one finding, not a second one on the PR
        assert f'is in {col} but has an open PR (#10); it goes in Active' in got[0]['text']
    assert sanity.check([issue(1, board.BACKLOG), pr(10, board.PARKED, closes=[(1, ())])], []) == []
    assert sanity.check([issue(1, board.INBOX, labels=['work2']),
                         pr(10, board.READY, closes=[(1, ['work2'])])], []) == []


def test_in_progress_needs_a_draft_and_a_live_claude_on_its_branch():
    cards = [issue(1, board.ACTIVE), pr(10, board.IN_PROGRESS, draft=False, closes=[(1, ())])]
    assert rules(sanity.check(cards, [claude(1, '/w/x', 'b10')])) == [('not-draft', 10)]
    cards[1]['draft'] = True
    assert rules(sanity.check(cards, [claude(1, '/w/x', 'other')])) == [('no-live-claude', 10)]
    assert rules(sanity.check(cards, [claude(1, '/w/x', 'b10', repo=TS)])) == [('no-live-claude', 10)]  # other repo
    assert sanity.check(cards, [claude(1, '/w/x', 'b10', repo=None)]) == []  # no origin: branch alone
    assert sanity.check(cards, [], hold_prs={(P, 10)}) == []  # its Claude is starting now
    assert rules(sanity.check(cards, [], hold_prs={(TS, 10)})) == [('no-live-claude', 10)]  # another repo's #10


def test_human_review_never_holds_more_than_two_per_board():
    cards = [pr(n, board.HUMAN_REVIEW, closes=None) for n in (1, 2)] + \
            [pr(n, board.HUMAN_REVIEW, closes=None, repo=TS, b=6) for n in (3, 4, 5)]
    got = sanity.check(cards, [])
    assert [(f['rule'], f['board']) for f in got] == [('human-review-over-two', 6)]
    assert 'holds 3 PRs' in got[0]['text'] and f'{TS}#5' in got[0]['text']


def test_active_holds_open_issues_with_exactly_one_flow_pr_and_no_work2():
    cards = [issue(1, board.ACTIVE),                                     # no PR
             issue(2, board.ACTIVE), pr(20, board.READY, closes=[(2, ())]), pr(21, board.STAGING, closes=[(2, ())]),
             issue(3, board.ACTIVE, labels=['work2']), pr(30, board.READY, closes=[(3, ['work2'])]),
             issue(4, board.ACTIVE), pr(40, board.PARKED, closes=[(4, ())])]   # Parked is not a flow column
    got = rules(sanity.check(cards, []))
    assert got == [('active-pr-count', 1), ('active-pr-count', 2), ('active-pr-count', 4), ('active-work2', 3)]
    assert rules(sanity.check(cards[:1], [], hold_issues={(P, 1)})) == []  # launch in flight
    assert rules(sanity.check(cards[:1], [], hold_issues={(TS, 1)})) == [('active-pr-count', 1)]


def test_a_flow_pr_belongs_to_exactly_one_active_issue():
    cards = [pr(10, board.READY, closes=[]),                              # no closing link
             issue(1, board.BACKLOG), pr(11, board.READY, closes=[(1, ())]),   # its issue not in Active
             issue(2, board.ACTIVE), issue(3, board.ACTIVE),
             pr(12, board.READY, closes=[(2, ()), (3, ())])]               # two Active issues
    got = rules(sanity.check(cards, []))
    assert ('flow-pr-active-count', 10) in got and ('flow-pr-active-count', 12) in got
    assert ('issue-not-active', 1) in got and ('flow-pr-active-count', 11) not in got  # one finding: the issue card
    assert 'no closing link' in next(f['text'] for f in sanity.check(cards, []) if f['n'] == 10)


def test_project_6_cards_without_issue_data_skip_the_active_rules():
    cards = [pr(5, board.READY, closes=None, repo=TS, b=6)]
    assert sanity.check(cards, []) == []


def test_two_claudes_in_one_worktree_is_a_finding_keyed_by_the_worktree():
    got = sanity.check([], [claude(1, '/w/a'), claude(2, '/w/a'), claude(3, '/w/b'), claude(4, None)])
    assert [f['rule'] for f in got] == ['duplicate-claudes']
    assert got[0]['key'] == 'duplicate-claudes:/w/a' and 'pids 1, 2' in got[0]['text']


# -- reading the board ------------------------------------------------------------------------

def node(n, col, kind='PullRequest', draft=False, labels=(), closes=()):
    c = {'__typename': kind, 'number': n, 'state': 'OPEN', 'title': f't{n}', 'repository': {'nameWithOwner': P},
         'labels': {'nodes': [{'name': x} for x in labels]}}
    if kind == 'PullRequest':
        c.update(headRefName=f'b{n}', headRefOid='h' * 40, isDraft=draft, updatedAt='u',
                 closingIssuesReferences={'nodes': [{'number': i, 'repository': {'nameWithOwner': P},
                                                     'labels': {'nodes': [{'name': x} for x in lab]}}
                                                    for i, lab in closes]})
    status = {'name': col, 'optionId': board.OPTION[col]} if col else None
    return {'id': f'ITEM{n}', 'fieldValueByName': status, 'content': c}


def test_cards_from_the_project_4_snapshot():
    from boardfake import gql
    snap = boardcache.snapshot(gql([node(7, board.IN_PROGRESS, draft=True, closes=[(1, ['work2'])]),
                                    node(1, board.INBOX, kind='Issue', labels=['work2']), node(8, None)]), max_age=0)
    got = {c['n']: c for c in sanity.cards_from_snap(snap)}
    assert got[7]['kind'] == 'pr' and got[7]['draft'] and got[7]['branch'] == 'b7' and got[7]['col'] == board.IN_PROGRESS
    assert got[7]['closes'] == [(1, {'work2'})] and got[1]['labels'] == {'work2'} and got[8]['col'] is None


def test_cards_from_another_board_carry_the_branch():
    b6 = board.BOARDS[1]
    page = {'organization': {'projectV2': {'items': {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [
        {'id': 'I1', 'fieldValueByName': {'name': 'In Progress', 'optionId': b6.option[board.IN_PROGRESS]},
         'content': {'__typename': 'PullRequest', 'number': 79, 'state': 'OPEN', 'isDraft': True,
                     'headRefName': 'feat/x', 'headRefOid': 'h', 'repository': {'nameWithOwner': TS},
                     'labels': {'nodes': []}, 'closingIssuesReferences': {'nodes': []}}}]}}}}
    assert boards.open_prs(lambda q, **v: page, b6)[board.IN_PROGRESS][0]['branch'] == 'feat/x'
    (c,) = sanity.cards_from_board(lambda q, **v: page, b6)
    assert (c['repo'], c['n'], c['branch'], c['draft'], c['closes'], c['board']) == (TS, 79, 'feat/x', True, None, 6)


# -- live Claudes from /proc ------------------------------------------------------------------

def git_repo(path, branch, url='git@github.com:tangentcode/platform.git'):
    (path / '.git').mkdir(parents=True)
    (path / '.git' / 'HEAD').write_text(f'ref: refs/heads/{branch}\n')
    (path / '.git' / 'config').write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n'
                                          '[remote "fork"]\n\turl = https://github.com/me/fork.git\n')
    return path


def git_worktree(main, path, branch):
    gd = main / '.git' / 'worktrees' / path.name
    gd.mkdir(parents=True)
    (gd / 'HEAD').write_text(f'ref: refs/heads/{branch}\n')
    (gd / 'commondir').write_text('../..\n')
    path.mkdir(parents=True)
    (path / '.git').write_text(f'gitdir: {gd}\n')
    return path


def fake_proc(tmp_path, procs):
    root = tmp_path / 'proc'
    root.mkdir()
    for pid, (comm, cmd, cwd, ppid) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        (d / 'cmdline').write_bytes(cmd.replace(' ', '\0').encode())
        (d / 'stat').write_text(f"{pid} ({comm}) S {ppid} " + ' '.join(['0'] * 30))
        os.symlink(str(cwd), d / 'cwd')
    return root


def test_git_info_reads_the_origin_and_branch_of_a_checkout_and_a_worktree(tmp_path):
    main = git_repo(tmp_path / 'platform', 'main')
    wt = git_worktree(main, tmp_path / 'platform.feat', 'feat/x')
    assert sanity.git_info(str(main)) == (P, 'main')
    assert sanity.git_info(str(wt)) == (P, 'feat/x')
    (wt / 'sub').mkdir()
    assert sanity.worktree_root(str(wt / 'sub')) == str(wt) and sanity.worktree_root('/') is None


def test_live_claudes_finds_each_top_claude_with_its_worktree(tmp_path):
    main = git_repo(tmp_path / 'platform', 'main')
    a = git_worktree(main, tmp_path / 'platform.a', 'a')
    b = git_worktree(main, tmp_path / 'platform.b', 'b')
    (a / 'web').mkdir()
    root = fake_proc(tmp_path, {10: ('claude', 'claude --resume x', a, 1),
                                11: ('claude', 'claude', a / 'web', 1),
                                12: ('bash', 'bash -c claude -p hi', a, 11),
                                13: ('claude', 'claude -p hi', a, 12),  # a Claude started by a Claude
                                14: ('claude', 'claude', b, 1),
                                15: ('python3', 'python3 -m pytest', b, 14)})
    got = sanity.live_claudes(proc=root, uid=os.getuid())
    assert [(c['pid'], c['branch']) for c in got] == [(10, 'a'), (11, 'a'), (14, 'b')]
    assert {c['root'] for c in got} == {str(a), str(b)} and got[0]['repo'] == P
    found = sanity.check([], got)
    assert [f['rule'] for f in found] == ['duplicate-claudes'] and str(a) in found[0]['text']


# -- report: grace, one alert and one incident per pass, dedupe --------------------------------

class Sink:
    def __init__(self, results=None):
        self.alerts, self.incidents, self.results = [], [], list(results or [])

    def alert(self, text, key, pr=None):
        self.alerts.append((text, key, pr))

    def incident(self, key, title, what, **kw):
        self.incidents.append((key, title, what))
        return self.results.pop(0) if self.results else f'https://github.com/tangentcode/methodology/issues/{len(self.incidents)}'

    def run(self, found, now, apply=True):
        return sanity.report(found, now, apply=apply, alert=self.alert, incident=self.incident, grace=600)


F1 = sanity.finding('not-draft', 'PR tangentcode/platform#8 is in Parked but is not a draft', P, 8, board_no=4)
F2 = sanity.finding('duplicate-claudes', '2 live Claudes in the worktree /w/a: pids 1, 2', scope='/w/a')


def test_a_finding_waits_its_grace_then_is_told_and_filed_once():
    s = Sink()
    lines, due = s.run([F1, F2], T0)
    assert due == T0 + 600 and s.alerts == [] and s.incidents == [] and all(x.startswith('pending') for x in lines)
    lines, due = s.run([F1, F2], T0 + 600)
    assert due is None and len(s.alerts) == 1 and len(s.incidents) == 1  # #82: one ticket for the pass
    assert '2 new finding(s)' in s.alerts[0][0] and s.alerts[0][2] == 8
    assert 'issues/1' in s.alerts[0][0]  # the message names the ticket
    (key, title, what), = s.incidents
    assert title.startswith('sanity: 2 finding(s)') and '(+1 more)' in title
    assert F1['text'] in what and F2['text'] in what and 'Rule `duplicate-claudes`' in what and 'Rule `not-draft`' in what
    s.run([F1, F2], T0 + 4200)  # the hourly backstop: same findings, nothing new
    assert len(s.alerts) == 1 and len(s.incidents) == 1


def test_only_new_findings_make_a_report_and_each_finding_is_reported_once():
    """#82 rules 1 and 2: a later pass reports only its new findings, in one ticket and one message."""
    F3 = sanity.finding('draft', 'PR tangentcode/platform#9 is in Ready but is a draft', P, 9, board_no=4)
    s = Sink()
    s.run([F1, F2], T0)
    s.run([F1, F2], T0 + 600)
    s.run([F1, F2, F3], T0 + 700)
    s.run([F1, F2, F3], T0 + 1300)
    assert len(s.incidents) == 2 and len(s.alerts) == 2
    _, title, what = s.incidents[1]
    assert F3['text'] in what and F1['text'] not in what and F2['text'] not in what
    assert '1 new finding(s)' in s.alerts[1][0] and F1['text'] not in s.alerts[1][0]
    s.run([F1, F2, F3], T0 + 9000)
    assert len(s.incidents) == 2 and len(s.alerts) == 2


def test_a_finding_that_clears_inside_its_grace_is_never_reported():
    s = Sink()
    s.run([F1], T0)
    lines, _ = s.run([], T0 + 300)
    s.run([], T0 + 900)
    assert s.alerts == [] and s.incidents == [] and lines == [f"resolved: {F1['text']}"]


def test_a_capped_ticket_is_tried_again_and_a_dup_counts_as_filed():
    s = Sink(results=['capped', 'dup'])
    s.run([F1], T0)
    lines, due = s.run([F1], T0 + 600)
    assert due == T0 + 600 + sanity.RETRY and any('not filed (capped)' in x for x in lines) and len(s.alerts) == 1
    lines, due = s.run([F1], T0 + 1200)
    assert due is None and len(s.incidents) == 2 and len(s.alerts) == 1  # told once only
    s.run([F1], T0 + 1800)
    assert len(s.incidents) == 2


def test_a_finding_that_comes_back_is_a_new_incident():
    s = Sink()
    s.run([F1], T0)
    s.run([F1], T0 + 600)
    s.run([], T0 + 700)
    s.run([F1], T0 + 5000)
    s.run([F1], T0 + 5600)
    assert len(s.incidents) == 2 and len(s.alerts) == 2
    assert s.incidents[0][0] != s.incidents[1][0]  # a new key: alerts.incident does not drop it as a dup


def test_a_dry_run_says_what_it_would_do_and_writes_nothing():
    s = Sink()
    lines, _ = s.run([F1], T0, apply=False)
    assert lines[0].startswith('pending') and not sanity.state_path().exists()
    s.run([F1], T0)  # the real first sighting
    lines, _ = s.run([F1], T0 + 600, apply=False)
    assert s.alerts == [] and s.incidents == []
    assert any(x.startswith('would file one incident for 1 finding(s)') for x in lines) and any('would tell swarmbot' in x for x in lines)
    assert json.loads(sanity.state_path().read_text())['open'][F1['key']]['told'] is False


def test_the_default_incident_goes_to_project_6_once_per_key(monkeypatch):
    calls = []

    def sh(argv):
        calls.append(argv)
        return 'https://github.com/tangentcode/methodology/issues/900\n' if argv[1] == 'issue' else ''
    monkeypatch.setattr(alerts, 'record', lambda *a, **k: None)
    inc = lambda key, title, what, **kw: alerts.incident(key, title, what, sh=sh, now=lambda: T0, **kw)  # noqa: E731
    sanity.report([F1], T0, apply=True, alert=lambda *a, **k: None, incident=inc, grace=0)
    sanity.report([F1], T0 + 60, apply=True, alert=lambda *a, **k: None, incident=inc, grace=0)
    assert [c[:3] for c in calls] == [['gh', 'issue', 'create'], ['gh', 'project', 'item-add']]
    assert calls[0][calls[0].index('--repo') + 1] == 'tangentcode/methodology' and '6' in calls[1]


# -- triggers: startup, board change, hourly, recheck -------------------------------------------

def test_the_watchdog_runs_the_check_at_startup_on_a_board_change_hourly_and_on_recheck(monkeypatch):
    from boardfake import gql
    runs = []
    monkeypatch.setattr(sanity, 'gather', lambda g, snap, now, failed=None: runs.append(now) or [F1])
    monkeypatch.setattr(sanity, 'GRACE', 600)
    w = WD.Watchdog(g={'gql': gql([])}, panes=type('P', (), {'latest': {}})(), out=lambda line: None,
                    alert=lambda text, key, pr=None, st=None: True,
                    incident=lambda key, title, what, **kw: 'https://x/1')
    snap = {'prs': [node(7, board.IN_PROGRESS)], 'issues': []}
    assert w.sanity_tick(snap, T0)[0].startswith('sanity (startup): pending')
    assert w.sanity_tick(snap, T0 + 60) == []                               # nothing changed
    moved = {'prs': [node(7, board.AUTO_REVIEW)], 'issues': []}
    assert w.sanity_tick(moved, T0 + 120)[0].startswith('sanity (board change)')
    assert w.sanity_tick(moved, T0 + 180) == []
    sanity.poke()                                                         # a webhook board event
    os.utime(sanity.poke_path(), (T0 + 200, T0 + 200))
    assert w.sanity_tick(moved, T0 + 240)[0].startswith('sanity (board change)')
    assert w.sanity_tick(moved, T0 + 590) == []
    assert w.sanity_tick(moved, T0 + 610)[0].startswith('sanity (recheck)')  # F1's grace ended
    assert w.sanity_tick(moved, T0 + 700) == []
    assert w.sanity_tick(moved, T0 + 610 + sanity.EVERY)[0].startswith('sanity (hourly)')
    assert len(runs) == 5


def test_a_dry_watchdog_never_writes_the_sanity_state(monkeypatch):
    from boardfake import gql
    monkeypatch.setattr(sanity, 'gather', lambda g, snap, now, failed=None: [F1])
    w = WD.Watchdog(g={'gql': gql([])}, panes=type('P', (), {'latest': {}})(), out=lambda line: None, dry=True)
    w.sanity_tick({'prs': [], 'issues': []}, T0)
    assert not sanity.state_path().exists()


def test_a_project_board_event_pokes_the_sanity_check():
    assert not sanity.poke_path().exists()
    W.classify('projects_v2_item', {'action': 'edited', 'projects_v2_item': {'project_node_id': 'other'},
                                    'changes': {'field_value': {'field_node_id': 'f'}}})
    assert sanity.poked_at() > 0


def test_a_failed_sanity_pass_is_tried_again_after_the_retry(monkeypatch):
    from boardfake import gql
    calls = []

    def gather(g, snap, now, failed=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError('gh api graphql: rate limited')
        return []
    monkeypatch.setattr(sanity, 'gather', gather)
    lines = []
    w = WD.Watchdog(g={'gql': gql([])}, panes=type('P', (), {'latest': {}})(), out=lines.append)
    snap = {'prs': [], 'issues': []}
    w.step('sanity', w.sanity_tick, snap, T0)
    assert 'rate limited' in lines[-1]
    assert w.sanity_tick(snap, T0 + sanity.RETRY - 1) == []
    assert w.sanity_tick(snap, T0 + sanity.RETRY)[0].startswith('sanity (recheck)')


def test_a_failed_board_read_still_runs_the_other_checks_and_keeps_its_findings(monkeypatch):
    """Codex on PR #79: a Project 6 read error must not stop the Project 4 and Claude
    checks, and must not resolve (then file again) the Project 6 findings."""
    from boardfake import gql
    F6 = sanity.finding('draft', 'PR tangentcode/tangentswarm#5 is in Ready but is a draft', TS, 5, board_no=6)
    s = Sink()
    s.run([F1, F6], T0)
    s.run([F1, F6], T0 + 600)
    assert len(s.incidents) == 1

    def broken(gql_, b):
        raise RuntimeError('gh api graphql: rate limited')
    monkeypatch.setattr(sanity, 'cards_from_board', broken)
    snap = boardcache.snapshot(gql([node(8, board.PARKED, draft=False, closes=[(1, ())])]), max_age=0)
    failed = {}
    found = sanity.gather({'gql': None}, snap, T0 + 700, claudes=[], failed=failed, units=[])
    assert [f['key'] for f in found] == [F1['key']] and 'rate limited' in failed[6]
    lines, due = sanity.report(found, T0 + 700, apply=True, alert=s.alert, incident=s.incident, grace=600,
                               unread={6})
    assert not any(x.startswith('resolved') for x in lines) and due == T0 + 700 + sanity.RETRY
    s.run([F1, F6], T0 + 1300)  # the read works again: the same finding, not a new incident
    assert len(s.incidents) == 1 and len(s.alerts) == 1
    s.run([F1], T0 + 1400)
    assert F6['key'] not in json.loads(sanity.state_path().read_text())['open']


def test_gather_holds_only_the_platform_numbers_of_the_launch_records(monkeypatch):
    """Codex on PR #79: an unstarted platform #79 must not hide tangentswarm #79."""
    from tangentswarm import slots
    monkeypatch.setattr(slots, 'load', lambda: {'unstarted': {'79': {'issue': 43}}})
    monkeypatch.setattr(sanity, 'cards_from_board', lambda gql_, b: [pr(79, board.IN_PROGRESS, repo=TS, b=6, closes=None)])
    snap = {'prs': [node(79, board.IN_PROGRESS, draft=True, closes=[(43, ())])],
            'issues': [node(43, board.ACTIVE, kind='Issue')], 'at': T0}
    got = rules(sanity.gather({'gql': None}, snap, T0, claudes=[], units=[]))
    assert got == [('no-live-claude', 79)]
    assert next(f for f in sanity.gather({'gql': None}, snap, T0, claudes=[], units=[]))['repo'] == TS
