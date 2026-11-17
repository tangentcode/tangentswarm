"""Launch a Backlog issue: branch, draft PR, brief, worker Claude (methodology #70).

MethBot 2026-11-15 3:35 AM ET: nothing consumed START_CLAUDE. From 2:56 AM the cascade
printed it seven times with new=0/3, no Claude started, and Platform In Progress stayed
empty; mark_named also hid each named issue for 45 minutes, so the queue drained (19 to 8)
while nothing launched. Now the cascade launches the issues it names, through the spool
(the swarm-promoted daemon runs it outside the serial lock and outside the worker slice):

  1. branch issue-<N>-<slug> from origin/main, with one empty "Start #N" scaffold commit
     (pushed; an existing branch is reused)
  2. a draft PR (base main, body "Fixes #N"), put on Project 4 in In Progress; an open PR
     for the branch is reused
  3. the issue's milestone is copied to the PR; the issue card moves Backlog -> Active
  4. the brief file (tangentswarm.brief) and its section in the PR body
  5. launches.launch: swarm-start-scoped (worktree, window <code>-<PR>-<branch>, the Stop
     hook, memory admission; a memory hold queues it for swarm-tick) and the brief line

Each step is idempotent, so a failed launch can run again. The issue counts as named
(slots.mark_named) only once its draft PR exists; until then it is "launching" (the picker
skips it for LAUNCHING_TTL, so the next pass does not name it twice).
"""
import json
import time
import re
import subprocess
from pathlib import Path

from . import board

REPO = 'tangentcode/platform'
GIT = Path.home() / 'ver' / 'platform'


STOP = {'a', 'an', 'the', 'on', 'of', 'to', 'in', 'for', 'and', 'or', 'with', 'is', 'are', 'be', 'should',
        'must', 'can', 'it', 'its', 'as', 'at', 'by', 'from', 'into', 'when', 'that', 'this', 'not', 'non', 'show'}


def closes(body, n):
    """True when body has a closing reference to exactly issue n ("Fixes #50", not "#507").
    Codex on PR #26: a substring test let issue #50 reuse the branch of a PR for #507."""
    return re.search(rf'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:{re.escape(REPO)})?#{int(n)}(?!\d)',
                     body or '') is not None


def slug(title, n=None):
    """A short branch slug like the hand launches (avo-save-icon): three or four words of the
    title, at most 28 characters. The window becomes mn-<PR>-<slug>."""
    words = [w for w in re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).split() if w not in STOP]
    out = ''
    for w in words[:4]:
        nxt = f"{out}-{w}" if out else w
        if len(nxt) > 28:
            break
        out = nxt
    return out or f"issue-{n}"


# tangentswarm #53: the worker tells the swarm its state over MCP HTTP (swarm-agent), not by
# leaving a screen for the watchdog to read.
REPORT_RULE = (
    "Report your state to the swarm with the report_status tool of the swarm-agent MCP server "
    "(HTTP): pr={pr}; window is your tmux window name (tmux display-message -p '#W'). "
    "state='done' when your work is committed and pushed (the swarm hands off the PR and closes you "
    "with /exit; the window stays), 'needs-input' with question='...' when only Michal can decide (the "
    "question goes on the ticket and you are closed until he answers), 'blocked' with message='...' "
    "when you cannot go on, 'working' with message='...' for progress. End your turn right after "
    "done or needs-input. If the tool is missing, end with the question as your last line or push "
    "and stop: the Stop hook is the fallback.")


def brief_text(n, title, branch, pr):
    from .gatechecks import SCRATCH_RULE, SHOTS_RULE, STATUS_RULE
    return (
        f"Task #{n}: {title}\n\n"
        f"Read the issue first, with its comments: gh issue view {n} --comments --repo {REPO}. "
        "Follow the issue as written. If it leaves a real choice open that only Michal can make, "
        f"call report_status(state='needs-input', pr={pr}, window=<your window>, question='<one clear "
        "question>') (or needs_input(pr, question) of the tangentswarm MCP server) and end your turn; do "
        "not guess. Never ask Michal in your pane.\n\n"
        f"{REPORT_RULE.format(pr=pr)}\n\n"
        f"Rules: you are in your own worktree on branch {branch}, cut from origin/main; draft PR #{pr} "
        f"(base main, 'Fixes #{n}') already exists and is in In Progress. Never post on GitHub: no gh "
        f"comments, reviews, or PR edits; just commit and push to {branch}. Do not rebase and do not "
        "force-push. Keep comments sparse; every new code file starts with a 1-2 line file-purpose "
        "header. Run only the exact test files you add or change, through ~/bin/test-slot, never the "
        "whole suite. Fix the root cause and cover the neighbor cases. A UI change needs screenshots "
        f"from staging or a local run of the real app, never an isolated component page. {SCRATCH_RULE} {SHOTS_RULE} "
        "Before your final push, stop every dev server, watcher and background test run you started "
        "(kill the pids you started): the handoff does not run while one is left in your worktree. "
        "When you are done, commit and push, reply with: what changed, the tests you ran and their "
        f"result, and a short PR body draft (Before, After, how to see it). {STATUS_RULE} Then call "
        f"report_status(state='done', pr={pr}, window=<your window>) and end your turn.\n")


class Launcher:
    def __init__(self, g=None, dry=False, out=print, run=subprocess.run, git_dir=None, launch=None):
        self.dry, self.out, self.run = dry, out, run
        self._g = g
        self.git_dir = Path(git_dir or GIT)
        self._launch = launch

    @property
    def g(self):
        if self._g is None:
            from .advance import hr_gate
            self._g = hr_gate()
        return self._g

    def sh(self, *argv, check=True, input=None):
        r = self.run([str(a) for a in argv], capture_output=True, text=True, timeout=300, input=input)
        if check and r.returncode:
            raise RuntimeError(f"{' '.join(str(a) for a in argv[:4])}: {(r.stderr or r.stdout).strip()[:300]}")
        return (r.stdout or '').strip()

    def git(self, *args, check=True):
        return self.sh('git', '-C', self.git_dir, *args, check=check)

    # -- the steps --------------------------------------------------------------------
    def pick_branch(self, n, title):
        """The short slug, or slug-<N> when another PR's branch already has that name."""
        b = slug(title, n)
        if not self.git('ls-remote', 'origin', f'refs/heads/{b}', check=False).strip():
            return b
        body = self.sh('gh', 'pr', 'list', '-R', REPO, '--head', b, '--state', 'open', '--json', 'body',
                       '--jq', '.[0].body', check=False)
        return b if closes(body, n) else f"{b}-{n}"

    def branch(self, n, branch):
        """Push the scaffold branch (one empty "Start #N" commit on origin/main), unless it exists."""
        have = self.git('ls-remote', 'origin', f'refs/heads/{branch}', check=False)
        if have.strip():
            return f"branch {branch} exists"
        self.git('fetch', '-q', 'origin', 'main')
        if self.dry:
            return f"would push branch {branch}: one empty 'Start #{n}' commit on origin/main"
        sha = self.git('commit-tree', 'origin/main^{tree}', '-p', 'origin/main', '-m', f'Start #{n}')
        self.git('push', '-q', 'origin', f'{sha}:refs/heads/{branch}')
        return f"pushed branch {branch} ({sha[:8]}: Start #{n})"

    def draft_pr(self, n, title, branch):
        """(PR number, note): the open PR of the branch, or a new draft PR "Fixes #N"."""
        found = self.sh('gh', 'pr', 'list', '-R', REPO, '--head', branch, '--state', 'open',
                        '--json', 'number', '--jq', '.[0].number', check=False)
        if found.strip().isdigit():
            return int(found), f"draft PR #{found.strip()} exists"
        if self.dry:
            return 0, f"would open a draft PR: {title!r}, Fixes #{n}"
        url = self.sh('gh', 'pr', 'create', '-R', REPO, '--draft', '--base', 'main', '--head', branch,
                      '--title', title, '--body', f'Fixes #{n}')
        m = re.search(r'/pull/(\d+)', url)
        if not m:
            raise RuntimeError(f"gh pr create gave no PR URL: {url[:200]}")
        return int(m.group(1)), f"opened draft PR #{m.group(1)}"

    def milestone(self, n, pr):
        """Copy the issue's milestone to the PR (REST; swarm-label-sync cannot write
        milestones, and `gh pr edit` fails on the Projects classic deprecation)."""
        ms = self.sh('gh', 'api', f'repos/{REPO}/issues/{n}', '--jq', '.milestone.number // empty', check=False)
        if not ms.strip().isdigit():
            return f"issue #{n} has no milestone"
        if self.dry:
            return f"would set milestone {ms.strip()} on PR #{pr}"
        self.sh('gh', 'api', '-X', 'PATCH', f'repos/{REPO}/issues/{pr}', '-F', f'milestone={ms.strip()}')
        return f"milestone {ms.strip()} set on PR #{pr}"

    def set_status(self, item, col):
        self.sh('gh', 'project', 'item-edit', '--id', item, '--project-id', self.g['project_id'](),
                '--field-id', self.g['STATUS_FIELD'], '--single-select-option-id', board.OPTION[col])

    def board(self, n, pr):
        """The PR card on Project 4 in In Progress; the issue card Backlog -> Active."""
        if self.dry:
            return f"would put PR #{pr} in In Progress and move issue #{n} Backlog -> Active"
        item = json.loads(self.sh('gh', 'project', 'item-add', '4', '--owner', 'tangentcode', '--url',
                                  f'https://github.com/{REPO}/pull/{pr}', '--format', 'json'))['id']
        self.set_status(item, board.IN_PROGRESS)
        from . import boardcache
        snap = boardcache.snapshot(self.g['gql'], max_age=0)
        issue = [it for it, c in boardcache.open_issues(snap) if c['number'] == n]
        note = f"PR #{pr} in In Progress"
        if issue and boardcache.col(issue[0]) == board.BACKLOG:
            self.set_status(issue[0]['id'], board.ACTIVE)
            note += f"; issue #{n} Backlog -> Active"
        boardcache.invalidate()
        return note

    def brief(self, n, title, branch, pr):
        from . import brief
        path = brief.brief_path(pr)
        if self.dry:
            return path, f"would write the brief {path}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(brief_text(n, title, branch, pr))
        try:
            brief.write_brief(pr)
        except Exception as e:  # the worker still has the file
            return path, f"brief {path} (PR body section not written: {str(e)[:120]})"
        return path, f"brief {path}"

    def start(self, branch, pr, path, issue=None):
        """Start the worker. Done only when a claude process runs in its pane (MethBot
        2026-11-15 3:55 AM ET: #537-#542 got a window with only a bash prompt) and has the
        brief line; then the unstarted record goes. A memory hold keeps the record (no held
        list, tangentswarm #40). A Claude that runs without the brief line keeps
        the record (slots.brief_pending) for the retry. Raises otherwise."""
        if self.dry:
            return f"would start the worker: swarm-start-scoped {branch}, then send the brief line"
        from . import launches, slots
        what, detail = (self._launch or launches.launch)(branch, pr=pr, brief=str(path) if path else None)
        if what == 'held':  # no held list (#40): the unstarted record stays, and the cascade retries it
            return (f"worker held (memory): the cascade starts it when memory frees "
                    f"({detail.get('window') if isinstance(detail, dict) else ''})")
        if what == 'refused':
            raise RuntimeError(f"swarm-start-scoped refused: {str(detail)[-300:]}")
        pane = detail.get('pane') if isinstance(detail, dict) else None
        cmd = self.pane_command(pane) if pane else None
        if cmd not in ('claude', 'node'):
            raise RuntimeError(f"no claude runs in {pane or 'the window'} after the start (it runs {cmd or 'nothing'})")
        if detail.get('brief_error'):
            slots.brief_pending(pr, pane, detail.get('unit'), path, branch=branch, issue=issue)
            return (f"worker launched in {detail.get('window')} ({pane}, claude runs); brief not sent: "
                    f"{detail['brief_error'][:120]}; the start retry sends it")
        slots.unstarted_done(pr)
        return f"worker launched in {detail.get('window')} ({pane}, claude runs)"

    def pane_command(self, pane):
        return self.sh('tmux', 'display-message', '-p', '-t', pane, '#{pane_current_command}', check=False)

    def launch(self, n, title):
        """Launch issue n. Returns the steps done; raises with the failed step's text, or
        Deferred when there is no live capacity (then nothing is made, and the issue stays in
        Backlog, which is the queue: tangentswarm #40)."""
        from . import slots
        if not self.dry:
            free, why = slots.capacity(mine=1)  # this launch's own spool request is counted
            if free <= 0:
                raise Deferred(f"no capacity for issue #{n}: {why}; it stays in Backlog")
        branch = self.pick_branch(n, title)
        steps = [self.branch(n, branch)]
        pr, note = self.draft_pr(n, title, branch)
        steps.append(note)
        if not self.dry:
            # Codex on PR #26: from here the PR exists and the picker skips the issue, so the
            # launch is recorded as unstarted at once (setup pending, a start in flight). A
            # failed setup step is redone by retry_starts -> start_existing; the record also
            # holds the launch's slot, so "launching" can end now.
            slots.mark_named([n])
            with slots.update() as d:
                d['unstarted'][str(pr)] = dict(branch=branch, issue=n, title=title, brief='', setup='pending',
                                               at=time.time(), queued=time.time())
            slots.launch_done(n)
        path, notes = self.setup(n, title, branch, pr)
        steps += notes
        steps.append(self.start(branch, pr, path, issue=n))
        return steps

    def setup(self, n, title, branch, pr):
        """Milestone, board cards and brief of a launched PR (each step is safe to redo).
        Returns (brief path, notes) and marks the unstarted record's setup done."""
        from . import slots
        notes = [self.milestone(n, pr), self.board(n, pr)]
        path, note = self.brief(n, title, branch, pr)
        notes.append(note)
        if not self.dry:
            with slots.update() as d:
                if str(pr) in d['unstarted']:
                    d['unstarted'][str(pr)].update(brief=str(path), setup='done')
        return path, notes

    def start_existing(self, pr, rec):
        """Retry the worker start of a launched PR with no Claude; an unfinished setup is
        redone first."""
        from . import launches, slots
        state = self.sh('gh', 'pr', 'view', str(pr), '-R', REPO, '--json', 'state', '--jq', '.state',
                        check=False).strip()
        if state and state != 'OPEN':  # Codex on PR #26: a closed launch must not start again
            slots.unstarted_done(pr)
            return f"PR #{pr} is {state.lower()}: its unstarted record is dropped, no worker start"
        if rec.get('brief_pane'):
            if self.pane_command(rec['brief_pane']) in ('claude', 'node'):
                launches.send_brief(rec['brief_pane'], rec.get('unit'), rec['brief'])  # raises: retried later
                slots.unstarted_done(pr)
                return f"brief line sent to the running Claude in {rec['brief_pane']}"
            with slots.update() as d:  # its Claude is gone: start a worker again
                d['unstarted'].get(str(pr), {}).pop('brief_pane', None)
        path = rec.get('brief') or None
        if rec.get('setup', 'done') != 'done' or not path:
            if rec.get('issue') is None:
                # Codex on PR #46: a row of the retired held list may have no brief and no
                # issue (swarm-launch never needed them); start it with no brief, as before
                if rec.get('setup', 'done') != 'done':
                    raise RuntimeError(f"PR #{pr}: setup is not done and the record names no issue")
            else:
                path, _ = self.setup(int(rec['issue']), rec.get('title') or f"Issue #{rec['issue']}",
                                     rec['branch'], pr)
        return self.start(rec['branch'], pr, path, issue=rec.get('issue'))


def execute_start(req):
    """The spool op 'start' (daemon side): {pr}: start the worker of an unstarted launch."""
    from . import slots
    pr = int(req['pr'])
    rec = slots.load()['unstarted'].get(str(pr))
    if not rec:
        return {'ok': True, 'pr': pr, 'note': 'not in the unstarted list'}
    from . import eventdb
    try:
        note = Launcher().start_existing(pr, rec)
    except Exception as e:
        slots.unstarted_retry(pr)
        eventdb.log_event('start', source='launcher', pr=pr, actor=rec.get('branch'),
                          error=f'{type(e).__name__}: {str(e)[:300]}')
        raise
    eventdb.log_event('start', source='launcher', pr=pr, actor=rec.get('branch'), notes=[note])  # tangentswarm #32
    from .stophook import say
    say(f"start #{pr} (issue #{rec.get('issue')}): {note}")
    return {'ok': True, 'pr': pr, 'note': note}


class Deferred(Exception):
    """No live capacity: the launch made nothing, and the issue stays in Backlog."""


def execute(req):
    """The spool op 'launch' (daemon side): {issue, title}."""
    from . import slots
    n, title = int(req['issue']), req.get('title') or f'Issue #{req["issue"]}'
    from . import eventdb
    try:
        steps = Launcher().launch(n, title)
    except Deferred as e:
        slots.launch_done(n)  # the picker may name it again when capacity is free
        from .stophook import say
        say(f"launch #{n}: {e}")
        return {'ok': True, 'issue': n, 'deferred': str(e)}
    except Exception as e:
        slots.launch_done(n)  # free it for the picker: a later pass may try again
        eventdb.log_event('launch', source='launcher', action=f'issue #{n}', actor=title[:80],
                          error=f'{type(e).__name__}: {str(e)[:300]}')
        raise
    m = next((re.search(r'PR #(\d+)', s) for s in steps if re.search(r'PR #(\d+)', s)), None)
    eventdb.log_event('launch', source='launcher', action=f'issue #{n}', pr=int(m.group(1)) if m else None,
                      actor=title[:80], notes=steps)  # tangentswarm #32
    from .stophook import say
    say(f"launch #{n}: " + '; '.join(steps))
    return {'ok': True, 'issue': n, 'steps': steps}
