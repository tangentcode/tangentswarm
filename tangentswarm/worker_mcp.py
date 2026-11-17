"""swarm-worker-mcp: the MCP server of a swarm worker Claude (tangentswarm #29).

The swarm Claudes on the host reached git and gh only through Bash (two days: 45 sessions,
9,524 Bash calls, 0 tangentswarm MCP calls). This server gives a worker three tools about
ITS OWN worktree and pull request, and nothing else: no tmux, no other pane, no shell, no
arbitrary command. (swarm-mcp, the operator's server on the host, drives tmux and types
into agents; a worker must not get that power.)

Transport: stdio, one process per Claude, started by Claude Code from its MCP config, so
there is no port and no key. The tools run their GitHub calls through the spool
(tangentswarm.spool): inside swarm-workers.slice sudo, and so the minbot gh shim, fails, so
the swarm-promoted daemon runs them outside the slice and writes the answer back. The worktree is the git top level of the server's working
directory (Claude Code starts it in the project directory), or $SWARM_WORKER_CWD.

Tools:
  worker_status    branch, local and pushed head, the open PR, its card column, draft or
                   not, the Codex state on the head, and the CI checks.
  handoff          "I am done and pushed": the Stop-hook handoff now (tangentswarm #28):
                   mark ready, "@codex review" once per head, card to Auto Review; a
                   Codex-clean head gets the MinBot check.
  request_review   post "@codex review" for the current head (once per head, through
                   codexrounds.ask(), the one guarded poster: tangentswarm #63).
  needs_input      a question only Michal can answer (tangentswarm #40): it goes on the
                   ticket, the card moves to Needs Input, and this Claude is closed with
                   /exit; it is resumed with his answer. Never ask Michal in the pane.

Project 6 worktrees (tangentswarm #19): a worker in ~/ver/tangentswarm.<slug> or
~/ver/methodology.<slug> works on a tangentcode/tangentswarm or tangentcode/methodology PR.
For it, handoff ("done") and needs_input wake MethBot (alerts.tell_methbot: done once per
PR and head sha, needs-input once per question) and log an events DB row; nothing else
(no card move, no /exit). worker_status and request_review serve platform worktrees only.

Install (user scope, every Claude on the host):
    claude mcp add --scope user tangentswarm -- ~/bin/swarm-worker-mcp
Outside a worker worktree ~/ver/platform.<slug> every tool answers "not a worker
worktree" and changes nothing.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio

from . import __version__

from mcp.server.mcpserver import MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError

INSTRUCTIONS = """Tools about your own swarm worktree and its pull request. Call worker_status
to see your PR, card, Codex and CI state. When your work is committed and pushed, call
handoff: it marks the PR ready, asks Codex for a review and moves the card to Auto Review
(the same as the Stop hook). request_review only posts "@codex review" for your head.
In a tangentswarm or methodology worktree (Project 6), handoff reports done and needs_input
sends the question to MethBot; worker_status and request_review are platform-only there.
Never ask Michal a question in your pane: call needs_input(pr, question), then end your turn."""

REPO = 'tangentcode/platform'
_G = {}


def worktree():
    """The worker worktree of this server, or raise ToolError."""
    from . import slots
    start = os.environ.get('SWARM_WORKER_CWD') or os.getcwd()
    r = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    top = Path((r.stdout or '').strip() or start)
    if r.returncode or not (slots.is_platform_worktree(top) or project6_repo(top)):
        raise ToolError(f"not a worker worktree: {top} (only ~/ver/platform.<slug>, ~/ver/tangentswarm.<slug> "
                        "and ~/ver/methodology.<slug> worktrees have a PR here)")
    return top


P6_PREFIX = {'tangentswarm.': 'tangentcode/tangentswarm', 'methodology.': 'tangentcode/methodology'}


def project6_repo(wt):
    """The Project 6 repo of a worker worktree ~/ver/tangentswarm.<slug> or
    ~/ver/methodology.<slug> (tangentswarm #19), else None."""
    from . import slots
    p = Path(wt or '')
    if p.parent != slots.WORKTREE_PARENT:
        return None
    return next((repo for pre, repo in P6_PREFIX.items() if p.name.startswith(pre) and len(p.name) > len(pre)), None)


def p6_pr(repo, branch, sh=None):
    """(number, head sha) of the open PR of repo whose head branch is branch, or None."""
    if sh is None:
        from . import tick
        sh = lambda argv: tick.sh(argv, check=True).stdout  # noqa: E731
    out = sh(['gh', 'pr', 'list', '-R', repo, '--head', branch, '--state', 'open',
              '--json', 'number,headRefOid,headRefName'])
    for p in json.loads(out or '[]'):
        if p.get('headRefName') == branch:
            return int(p['number']), p.get('headRefOid') or ''
    return None


def p6_only(wt, tool):
    """Refuse a platform-only tool in a Project 6 worktree."""
    repo = project6_repo(wt)
    if repo:
        raise ToolError(f"{tool} serves platform worktrees only; {repo} PRs use handoff (done) and needs_input")


def p6_done(wt, repo, sh=None, tell=None):
    """handoff in a Project 6 worktree: the worker is done. The worktree must be clean and
    pushed and GitHub must show that head on the open PR; then MethBot is woken once per PR
    and head (tangentswarm #19)."""
    from . import alerts, stophook
    branch, head = stophook.pushed_head(wt)
    if not branch or len(head or '') != 40:
        return {'done': False, 'why': f"not finished: {head}"}
    found = p6_pr(repo, branch, sh)
    if not found:
        return {'done': False, 'why': f"no open {repo} PR has branch {branch}"}
    n, pr_head = found
    if pr_head != head:
        return {'done': False, 'pr': n, 'why': f"GitHub does not show head {head[:8]} on #{n} yet; try again in a minute"}
    sent = (tell or alerts.tell_methbot)('done', repo, n, head=head)
    if sent is True:
        return {'done': True, 'pr': n, 'repo': repo, 'head': head, 'log': f"{repo}#{n} done at {head[:8]}: MethBot woken"}
    return {'done': sent == 'already sent', 'pr': n, 'repo': repo, 'head': head,
            'why': f"{repo}#{n} done at {head[:8]}: MethBot not woken: {sent}"}


def p6_needs_input(wt, repo, pr, question, sh=None, tell=None):
    """needs_input in a Project 6 worktree: the PR must be the worktree's own open PR; then
    MethBot is woken with the question (tangentswarm #19)."""
    from . import alerts, stophook
    question = (question or '').strip()
    if not question:
        return {'posted': False, 'why': 'needs_input needs the question'}
    branch, _ = stophook.pushed_head(wt)
    found = p6_pr(repo, branch, sh) if branch else None
    if not found:
        return {'posted': False, 'why': f"no open {repo} PR has this branch"}
    if int(pr) != found[0]:
        return {'posted': False, 'why': f"#{pr} is not your PR; yours is {repo}#{found[0]}"}
    sent = (tell or alerts.tell_methbot)('needs-input', repo, found[0], question=question)
    if sent is True or sent == 'already sent':
        return {'posted': True, 'pr': found[0], 'repo': repo,
                'next': 'MethBot has the question; end your turn and wait for the answer'}
    return {'posted': False, 'pr': found[0], 'repo': repo, 'why': f"MethBot not woken: {sent}"}


def gate():
    if 'g' not in _G:
        from .advance import hr_gate
        _G['g'] = hr_gate()
    return _G['g']


def checks(n, run=subprocess.run):
    """[(name, bucket)] of the PR's CI checks (gh pr checks; bucket: pass, fail, pending,
    skipping, cancel)."""
    r = run(['gh', 'pr', 'checks', str(n), '-R', REPO, '--json', 'name,bucket'],
            capture_output=True, text=True, timeout=60)
    try:
        return [(c.get('name'), c.get('bucket')) for c in json.loads(r.stdout or '[]')]
    except ValueError:
        return []


def status(wt=None, g=None, checks_of=checks):
    from . import stophook, tick
    wt = wt or worktree()
    p6_only(wt, 'worker_status')
    branch, head = stophook.pushed_head(wt)
    _, local = stophook.git(wt, 'rev-parse', 'HEAD')
    out = {'worktree': str(wt), 'branch': branch, 'head': local,
           'pushed': bool(branch) and head == local, 'not_pushed_why': None if head == local else head}
    if not branch:
        return out
    g = g or gate()
    found = stophook.find_pr(g, branch)
    if not found:
        return dict(out, pr=None, note='no open PR on the board has this branch')
    n, _, col, draft = found
    full = g['fetch'](n, refresh=True)
    codex, detail = tick.codex_status(full)
    cis = checks_of(n)
    return dict(out, pr=n, card=col, draft=bool(full.get('isDraft', draft)), pr_head=full.get('headRefOid'),
                codex=codex, codex_detail=detail,
                ci={'failed': [c for c, b in cis if b in ('fail', 'cancel')],
                    'pending': [c for c, b in cis if b == 'pending'],
                    'passed': len([1 for _, b in cis if b == 'pass'])})


def do_handoff(wt=None, transcript=None):
    """With transcript (the pane watcher's recovered Stop hook), the hook's question check runs
    first: a question, partial scope or a pending decision goes to Needs Input, never ready."""
    from . import stophook
    wt = wt or worktree()
    repo = project6_repo(wt)
    if repo:
        return p6_done(wt, repo)
    branch, head = stophook.pushed_head(wt)
    if not branch or len(head) != 40:
        return {'done': False, 'why': f"not finished: {head}"}
    if stophook.seen(f"{branch}@{head}"):
        return {'done': True, 'why': f"head {head[:8]} was already handed off"}
    line = stophook.run(wt, g=gate(), transcript=transcript)
    return {'done': line is not None, 'log': line}


def do_request_review(wt=None, sh=None):
    from . import stophook
    wt = wt or worktree()
    p6_only(wt, 'request_review')
    branch, head = stophook.pushed_head(wt)
    if not branch or len(head) != 40:
        return {'posted': False, 'why': f"push first: {head}"}
    found = stophook.find_pr(gate(), branch)
    if not found:
        return {'posted': False, 'why': 'no open PR on the board has this branch'}
    n = found[0]
    full = gate()['fetch'](n, refresh=True)
    if full.get('headRefOid') != head:
        return {'posted': False, 'why': f"GitHub does not show head {head[:8]} yet; try again in a minute"}
    from . import codexrounds  # tangentswarm #63: the one guarded poster
    res = codexrounds.ask(n, head, 'worker request_review', full, sh=sh)
    if not res['posted']:
        return {'posted': False, 'why': f"#{n} head {head[:8]}: {res['why']}"}
    return {'posted': True, 'pr': n, 'head': head, 'kind': res['kind']}


def do_needs_input(wt, pr, question, accept=None, window_of=None, g=None):
    """The needs_input tool, daemon side: the PR must be this worktree's own open PR. It is
    the report_status needs-input of swarm-agent (tangentswarm #53), for a worker that has
    only this stdio server: queued, so the worker's turn can end and the close can run."""
    from . import agent_report, stophook
    repo = project6_repo(wt)
    if repo:
        return p6_needs_input(wt, repo, pr, question)
    branch, _ = stophook.pushed_head(wt)
    found = stophook.find_pr(g or gate(), branch) if branch else None
    if not found:
        return {'posted': False, 'why': 'no open PR on the board has this branch'}
    if int(pr) != found[0]:
        return {'posted': False, 'why': f"#{pr} is not your PR; yours is #{found[0]}"}
    window = (window_of or (lambda n: agent_report.Reporter().resolve(n)[0]))(found[0])
    out = (accept or agent_report.accept)('needs-input', found[0], window, question=question, agent='needs_input tool')
    return dict(out, posted=bool(out.get('ok')))


def via_daemon(op, **extra):
    """Run op for this worktree through the spool: the GitHub calls need sudo (the minbot gh
    shim), which fails inside swarm-workers.slice, so the swarm-promoted daemon runs them.
    Never raises: an error comes back as {'ok': False, 'error': '<the real text>'}."""
    from . import eventdb, spool
    try:
        wt = worktree()
    except ToolError as e:
        eventdb.log_event('mcp', source='mcp', action=op, error=str(e)[:300])
        return {'ok': False, 'error': str(e)}
    try:
        res = spool.submit(op, wt, wait=spool.WAIT, **extra)
    except Exception as e:
        res = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    # tangentswarm #32: every worker MCP call is one events DB row (a Claude reporting done, ...)
    pr = res.get('pr') if isinstance(res, dict) else None
    eventdb.log_event('mcp', source='mcp', action=op, actor=Path(wt).name, pr=pr if isinstance(pr, int) else None,
                      error=(res or {}).get('error') if isinstance(res, dict) and not res.get('ok', True) else None,
                      detail=res if isinstance(res, dict) else {'result': str(res)[:500]})
    return res


async def worker_status() -> dict:
    """Your worktree, branch and head (pushed or not), your open PR, its card column, draft
    state, the Codex state on the head and the CI checks (failed, pending, passed). On a
    problem: {'ok': false, 'error': '...'} with the real error text."""
    return await anyio.to_thread.run_sync(via_daemon, 'status')


async def handoff() -> dict:
    """Call this when your work is committed and pushed: mark the PR ready, ask Codex for a
    review (once per head) and move the card to Auto Review. A head that is already
    Codex-clean gets the MinBot check instead. Nothing happens for a dirty or unpushed
    worktree. In a tangentswarm or methodology worktree (Project 6) it reports done: MethBot
    is woken once for the pushed head. On a problem: {'ok': false, 'error': '...'}."""
    return await anyio.to_thread.run_sync(via_daemon, 'handoff')


async def request_review() -> dict:
    """Post "@codex review" on your PR for the pushed head (once per head). On a problem:
    {'ok': false, 'error': '...'}."""
    return await anyio.to_thread.run_sync(via_daemon, 'request_review')


async def needs_input(pr: int, question: str) -> dict:
    """Ask Michal a question only he can answer, instead of asking in your pane. The
    question goes on the ticket, the card moves to Needs Input and Michal is pinged. Then end
    your turn: the server closes this session with /exit (the window stays) and resumes it
    with his answer. On a problem: {'ok': false, 'error': '...'}."""
    return await anyio.to_thread.run_sync(lambda: via_daemon('needs_input', pr=int(pr), question=question))


TOOLS = [worker_status, handoff, request_review, needs_input]


def build_server(**kwargs):
    kwargs.setdefault('version', __version__)
    server = _Server(name='tangentswarm', instructions=INSTRUCTIONS, **kwargs)
    for fn in TOOLS:
        server.add_tool(fn, name=fn.__name__)
    return server


def main(argv=None):
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if (argv if argv is not None else sys.argv[1:]) in (['--version'],):
        print(f'tangentswarm {__version__}')
        return
    build_server().run('stdio')


if __name__ == '__main__':
    main()
