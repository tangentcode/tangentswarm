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
  3. the issue card moves Backlog -> Active (option ids)
  4. the brief file (tangentswarm.brief) and its section in the PR body
  5. launches.launch: swarm-start-scoped (worktree, window <code>-<PR>-<branch>, the Stop
     hook, memory admission; a memory hold queues it for swarm-tick) and the brief line

Each step is idempotent, so a failed launch can run again. The issue counts as named
(slots.mark_named) only once its draft PR exists; until then it is "launching" (the picker
skips it for LAUNCHING_TTL, so the next pass does not name it twice).
"""
import json
import re
import subprocess
from pathlib import Path

from . import board

REPO = 'tangentcode/platform'
GIT = Path.home() / 'ver' / 'platform'


def slug(title, n):
    s = re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-')
    s = '-'.join(s.split('-')[:6])[:34].strip('-')
    return f"issue-{n}-{s}" if s else f"issue-{n}"


def brief_text(n, title, branch, pr):
    from .gatechecks import SHOTS_RULE
    return (
        f"Task #{n}: {title}\n\n"
        f"Read the issue first, with its comments: gh issue view {n} --comments --repo {REPO}. "
        "Follow the issue as written. If it leaves a real choice open that only Michal can make, "
        "stop and ask one clear question as your last line (the Stop hook moves the card to Needs "
        "Input); do not guess.\n\n"
        f"Rules: you are in your own worktree on branch {branch}, cut from origin/main; draft PR #{pr} "
        f"(base main, 'Fixes #{n}') already exists and is in In Progress. Never post on GitHub: no gh "
        f"comments, reviews, or PR edits; just commit and push to {branch}. Do not rebase and do not "
        "force-push. Keep comments sparse; every new code file starts with a 1-2 line file-purpose "
        "header. Run only the exact test files you add or change, through ~/bin/test-slot, never the "
        "whole suite. Fix the root cause and cover the neighbor cases. A UI change needs screenshots "
        f"from staging or a local run of the real app, never an isolated component page. {SHOTS_RULE} "
        "When you are done, commit and push: the Stop hook marks the PR ready and asks Codex for a "
        "review. Then reply with: what changed, the tests you ran and their result, and a short PR "
        "body draft (Before, After, how to see it).\n")


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

    def start(self, branch, pr, path):
        if self.dry:
            return f"would start the worker: swarm-start-scoped {branch}, then send the brief line"
        from . import launches
        what, detail = (self._launch or launches.launch)(branch, pr=pr, brief=str(path))
        if what == 'refused':
            raise RuntimeError(f"swarm-start-scoped refused: {detail}")
        return f"worker {what}" + (f" in {detail.get('window')} ({detail.get('pane')})"
                                   if isinstance(detail, dict) and detail.get('pane') else '')

    def launch(self, n, title):
        """Launch issue n. Returns the steps done; raises with the failed step's text."""
        from . import slots
        branch = slug(title, n)
        steps = [self.branch(n, branch)]
        pr, note = self.draft_pr(n, title, branch)
        steps.append(note)
        if not self.dry:
            slots.mark_named([n])  # the draft PR exists: now it counts as named
            slots.launch_done(n)
        steps.append(self.board(n, pr))
        path, note = self.brief(n, title, branch, pr)
        steps.append(note)
        steps.append(self.start(branch, pr, path))
        return steps


def execute(req):
    """The spool op 'launch' (daemon side): {issue, title}."""
    from . import slots
    n, title = int(req['issue']), req.get('title') or f'Issue #{req["issue"]}'
    try:
        steps = Launcher().launch(n, title)
    except Exception:
        slots.launch_done(n)  # free it for the picker: a later pass may try again
        raise
    from .stophook import say
    say(f"launch #{n}: " + '; '.join(steps))
    return {'ok': True, 'issue': n, 'steps': steps}
