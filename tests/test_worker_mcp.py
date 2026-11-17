"""swarm-worker-mcp (tangentswarm #29): a worker Claude's own MCP server. Its tools only see
the caller's worktree and PR; there is no tmux, pane or shell tool."""
import anyio
import pytest

from tangentswarm import board, worker_mcp as W

from test_stophook import G, run, wt  # noqa: F401  (the worktree fixture)


def test_only_the_four_worker_tools_are_exposed():
    names = [t.name for t in anyio.run(W.build_server().list_tools)]
    assert names == ['worker_status', 'handoff', 'request_review', 'needs_input']


def test_outside_a_worker_worktree_every_tool_refuses_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv('SWARM_WORKER_CWD', str(tmp_path))
    for op in ('status', 'handoff', 'request_review', 'needs_input'):
        r = W.via_daemon(op)
        assert r['ok'] is False and 'not a worker worktree' in r['error']


def test_status_reports_the_pr_card_codex_and_ci(wt):  # noqa: F811
    head = run(wt, 'rev-parse', 'HEAD')
    s = W.status(wt, g=G(head), checks_of=lambda n: [('Linux mna tests', 'fail'), ('lint', 'pass'),
                                                     ('build', 'pending')])
    assert s['branch'] == 'feat' and s['pushed'] and s['pr'] == 7 and s['card'] == board.IN_PROGRESS
    assert s['draft'] is True and s['codex'] == 'pending'
    assert s['ci'] == {'failed': ['Linux mna tests'], 'pending': ['build'], 'passed': 1}


def test_request_review_posts_once_per_head(wt, monkeypatch):  # noqa: F811
    head = run(wt, 'rev-parse', 'HEAD')
    monkeypatch.setitem(W._G, 'g', G(head))
    posted = []
    assert W.do_request_review(wt, sh=posted.append)['posted'] is True
    assert posted == [['gh', 'pr', 'comment', '7', '-R', W.REPO, '--body', '@codex review']]
    again = W.do_request_review(wt, sh=posted.append)
    assert again['posted'] is False and 'still pending' in again['why'] and len(posted) == 1


def test_handoff_refuses_an_unpushed_head(wt, monkeypatch):  # noqa: F811
    (wt / 'x.txt').write_text('x\n')
    run(wt, 'add', '.')
    run(wt, 'commit', '-qm', 'x')
    monkeypatch.setitem(W._G, 'g', G(run(wt, 'rev-parse', 'HEAD')))
    r = W.do_handoff(wt)
    assert r['done'] is False and 'is not pushed' in r['why']


def test_the_spool_handoff_passes_the_transcript_to_the_stop_hook(wt, monkeypatch):  # noqa: F811
    """Codex on PR #52: the pane watcher's recovered handoff runs the hook's question check."""
    from tangentswarm import spool, stophook
    seen = []
    monkeypatch.setitem(W._G, 'g', G(run(wt, 'rev-parse', 'HEAD')))
    monkeypatch.setattr(stophook, 'run', lambda cwd, **k: seen.append(k.get('transcript')) or 'ran')
    assert spool.execute({'op': 'handoff', 'cwd': str(wt), 'transcript': '/t/s.jsonl'})['done'] is True
    assert spool.execute({'op': 'handoff', 'cwd': str(wt)})['done'] is True  # the worker's own tool
    assert seen == ['/t/s.jsonl', None]


# -- Project 6 worktrees (tangentswarm #19): done and needs_input wake MethBot ---------------

@pytest.fixture
def p6(wt):  # noqa: F811
    """A Project 6 worker worktree ~/ver/tangentswarm.feat (the platform.feat clone renamed)."""
    w = wt.parent / 'tangentswarm.feat'
    wt.rename(w)
    return w


def gh_prs(head, n=71, branch='feat'):
    calls = []

    def sh(argv):
        calls.append(argv)
        import json
        return json.dumps([{'number': n, 'headRefOid': head, 'headRefName': branch}])
    sh.calls = calls
    return sh


def test_project6_repo_is_known_by_the_worktree_name(wt):  # noqa: F811
    parent = wt.parent
    assert W.project6_repo(parent / 'tangentswarm.fix-x') == 'tangentcode/tangentswarm'
    assert W.project6_repo(parent / 'methodology.doc') == 'tangentcode/methodology'
    assert W.project6_repo(wt) is None
    assert W.project6_repo(parent / 'tangentswarm') is None  # the main checkout
    assert W.project6_repo(parent / 'tangentswarm.') is None
    assert W.project6_repo(parent / 'elsewhere' / 'tangentswarm.x') is None


def test_project6_done_runs_the_server_merge_for_the_pushed_head(p6):
    """tangentswarm #89: done no longer wakes MethBot; the swarm verifies and merges."""
    head = run(p6, 'rev-parse', 'HEAD')
    sh, ran = gh_prs(head), []
    merge = lambda *a, **k: ran.append((a, k)) or {'merged': True, 'notes': ['card -> Done']}  # noqa: E731
    r = W.p6_done(p6, 'tangentcode/tangentswarm', sh=sh, merge=merge)
    assert r['done'] is True and r['pr'] == 71 and r['head'] == head
    assert r['log'] == f'tangentcode/tangentswarm#71 merged at {head[:8]}; card -> Done'
    assert ran == [(('tangentcode/tangentswarm', 71, head, 'done'), {'wt': p6})]
    assert sh.calls[0][:5] == ['gh', 'pr', 'list', '-R', 'tangentcode/tangentswarm']


def test_project6_done_that_fails_a_check_says_why(p6):
    head = run(p6, 'rev-parse', 'HEAD')
    merge = lambda *a, **k: {'merged': False, 'why': 'the PR is a draft', 'methbot': True}  # noqa: E731
    r = W.p6_done(p6, 'tangentcode/tangentswarm', sh=gh_prs(head), merge=merge)
    assert r['done'] is False and r['pr'] == 71
    assert r['why'] == f'tangentcode/tangentswarm#71 not merged at {head[:8]}: the PR is a draft; MethBot has the exception'


def test_project6_done_through_the_spool_never_posts_done_to_methbot(p6, monkeypatch):
    from tangentswarm import alerts, p6merge, spool, stophook
    head = run(p6, 'rev-parse', 'HEAD')
    monkeypatch.setattr(W, 'p6_pr', lambda repo, branch, sh=None: (71, head))
    posts, ran = [], []
    monkeypatch.setattr(alerts, 'methbot_post', posts.append)
    monkeypatch.setattr(p6merge, 'run', lambda *a, **k: ran.append(a) or {'merged': True, 'notes': []})
    monkeypatch.setattr(stophook, 'run', lambda *a, **k: pytest.fail('no platform Stop-hook handoff'))
    assert spool.execute({'op': 'handoff', 'cwd': str(p6)})['done'] is True
    assert ran == [('tangentcode/tangentswarm', 71, head, 'done')] and posts == []


def test_project6_done_refuses_an_unpushed_or_unseen_head(p6):
    head = run(p6, 'rev-parse', 'HEAD')
    ran = []
    merge = lambda *a, **k: ran.append(a) or {'merged': True}  # noqa: E731
    r = W.p6_done(p6, 'tangentcode/tangentswarm', sh=gh_prs('c' * 40), merge=merge)
    assert r['done'] is False and 'does not show head' in r['why']
    r = W.p6_done(p6, 'tangentcode/tangentswarm', sh=lambda argv: '[]', merge=merge)
    assert r['done'] is False and 'no open tangentcode/tangentswarm PR' in r['why']
    (p6 / 'x.txt').write_text('x\n')
    run(p6, 'add', '.')
    run(p6, 'commit', '-qm', 'x')
    r = W.p6_done(p6, 'tangentcode/tangentswarm', sh=gh_prs(head), merge=merge)
    assert r['done'] is False and 'is not pushed' in r['why']
    assert ran == []


def test_project6_needs_input_wakes_methbot_with_the_question(p6, monkeypatch):
    from tangentswarm import agent_report, alerts, spool
    head = run(p6, 'rev-parse', 'HEAD')
    monkeypatch.setattr(W, 'p6_pr', lambda repo, branch, sh=None: (71, head))
    monkeypatch.setattr(agent_report, 'accept', lambda *a, **k: pytest.fail('no platform report'))
    posts = []
    monkeypatch.setattr(alerts, 'methbot_post', posts.append)
    r = spool.execute({'op': 'needs_input', 'cwd': str(p6), 'pr': 71, 'question': 'Which flag?'})
    assert r['posted'] is True and r['pr'] == 71
    assert len(posts) == 1 and posts[0]['kind'] == 'needs-input' and posts[0]['question'] == 'Which flag?'
    assert posts[0]['repo'] == 'tangentcode/tangentswarm' and 'head' not in posts[0]
    wrong = spool.execute({'op': 'needs_input', 'cwd': str(p6), 'pr': 70, 'question': 'Which flag?'})
    assert wrong['posted'] is False and 'not your PR' in wrong['why'] and len(posts) == 1


def test_a_platform_needs_input_or_done_never_posts_to_methbot(wt, monkeypatch):  # noqa: F811
    from tangentswarm import alerts, stophook
    posts = []
    monkeypatch.setattr(alerts, 'methbot_post', posts.append)
    monkeypatch.setitem(W._G, 'g', G(run(wt, 'rev-parse', 'HEAD')))
    monkeypatch.setattr(stophook, 'run', lambda *a, **k: 'ran')
    assert W.do_handoff(wt)['done'] is True
    W.do_needs_input(wt, 7, 'q?', accept=lambda *a, **k: {'ok': True}, window_of=lambda n: 'w')
    assert posts == []


def test_platform_only_tools_refuse_a_project6_worktree(p6):
    from mcp.server.mcpserver.exceptions import ToolError
    for fn in (W.status, W.do_request_review):
        with pytest.raises(ToolError, match='platform worktrees only'):
            fn(p6)


def test_the_worker_mcp_accepts_a_project6_worktree(p6, monkeypatch):
    monkeypatch.setenv('SWARM_WORKER_CWD', str(p6))
    assert W.worktree() == p6


def test_handoff_from_a_methodology_worktree_passes_through_via_daemon(wt, monkeypatch):  # noqa: F811
    """tangentswarm #77: a ~/ver/methodology.<slug> worker gets a done handoff from the MCP
    tool (via_daemon), not 'not a worker worktree'."""
    from tangentswarm import alerts, p6merge, spool
    m = wt.parent / 'methodology.docs-x'
    wt.rename(m)
    head = run(m, 'rev-parse', 'HEAD')
    monkeypatch.setenv('SWARM_WORKER_CWD', str(m))
    monkeypatch.setattr(spool, 'submit', lambda op, cwd, wait=0, **extra:
                        spool.execute(dict(extra, op=op, cwd=str(cwd))))
    monkeypatch.setattr(W, 'p6_pr', lambda repo, branch, sh=None: (96, head))
    posts, ran = [], []
    monkeypatch.setattr(alerts, 'methbot_post', posts.append)
    monkeypatch.setattr(p6merge, 'run', lambda *a, **k: ran.append(a) or {'merged': True, 'notes': []})
    r = W.via_daemon('handoff')
    assert r['ok'] is True and r['done'] is True, r
    assert r['repo'] == 'tangentcode/methodology' and r['pr'] == 96 and r['head'] == head
    assert ran == [('tangentcode/methodology', 96, head, 'done')]  # tangentswarm #89: the server merge
    assert posts == []
