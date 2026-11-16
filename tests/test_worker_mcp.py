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
    assert again['posted'] is False and 'already asked' in again['why'] and len(posted) == 1


def test_handoff_refuses_an_unpushed_head(wt, monkeypatch):  # noqa: F811
    (wt / 'x.txt').write_text('x\n')
    run(wt, 'add', '.')
    run(wt, 'commit', '-qm', 'x')
    monkeypatch.setitem(W._G, 'g', G(run(wt, 'rev-parse', 'HEAD')))
    r = W.do_handoff(wt)
    assert r['done'] is False and 'is not pushed' in r['why']
