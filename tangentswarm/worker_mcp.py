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
  request_review   post "@codex review" for the current head (once per head; the key is
                   shared with swarm-tick and the Stop hook).

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
(the same as the Stop hook). request_review only posts "@codex review" for your head."""

REPO = 'tangentcode/platform'
_G = {}


def worktree():
    """The worker worktree of this server, or raise ToolError."""
    from . import slots
    start = os.environ.get('SWARM_WORKER_CWD') or os.getcwd()
    r = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    top = Path((r.stdout or '').strip() or start)
    if r.returncode or not slots.is_platform_worktree(top):
        raise ToolError(f"not a worker worktree: {top} (only ~/ver/platform.<slug> worktrees have a PR here)")
    return top


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


def do_handoff(wt=None):
    from . import stophook
    wt = wt or worktree()
    branch, head = stophook.pushed_head(wt)
    if not branch or len(head) != 40:
        return {'done': False, 'why': f"not finished: {head}"}
    if stophook.seen(f"{branch}@{head}"):
        return {'done': True, 'why': f"head {head[:8]} was already handed off"}
    line = stophook.run(wt, g=gate())
    return {'done': line is not None, 'log': line}


def do_request_review(wt=None, sh=None):
    from . import stophook, tick
    wt = wt or worktree()
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
    state = tick.State(tick.STATE_DIR / 'state.json')
    key = f"codex-request:{n}@{head}"
    if state.done(key) or tick.codex_requested(full):
        return {'posted': False, 'why': f"#{n}: a Codex review was already asked for {head[:8]}"}
    (sh or (lambda argv: tick.sh(argv, check=True)))(['gh', 'pr', 'comment', str(n), '-R', REPO,
                                                      '--body', '@codex review'])
    state.mark(key)
    return {'posted': True, 'pr': n, 'head': head}


def via_daemon(op):
    """Run op for this worktree through the spool: the GitHub calls need sudo (the minbot gh
    shim), which fails inside swarm-workers.slice, so the swarm-promoted daemon runs them.
    Never raises: an error comes back as {'ok': False, 'error': '<the real text>'}."""
    from . import spool
    try:
        wt = worktree()
    except ToolError as e:
        return {'ok': False, 'error': str(e)}
    try:
        return spool.submit(op, wt, wait=spool.WAIT)
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


async def worker_status() -> dict:
    """Your worktree, branch and head (pushed or not), your open PR, its card column, draft
    state, the Codex state on the head and the CI checks (failed, pending, passed). On a
    problem: {'ok': false, 'error': '...'} with the real error text."""
    return await anyio.to_thread.run_sync(via_daemon, 'status')


async def handoff() -> dict:
    """Call this when your work is committed and pushed: mark the PR ready, ask Codex for a
    review (once per head) and move the card to Auto Review. A head that is already
    Codex-clean gets the MinBot check instead. Nothing happens for a dirty or unpushed
    worktree. On a problem: {'ok': false, 'error': '...'}."""
    return await anyio.to_thread.run_sync(via_daemon, 'handoff')


async def request_review() -> dict:
    """Post "@codex review" on your PR for the pushed head (once per head). On a problem:
    {'ok': false, 'error': '...'}."""
    return await anyio.to_thread.run_sync(via_daemon, 'request_review')


TOOLS = [worker_status, handoff, request_review]


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
