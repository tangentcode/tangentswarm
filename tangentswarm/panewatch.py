"""The pane watcher (tangentswarm #40 items 1, 4 and 8): capture every swarm pane once a
minute, compare it with the last capture, classify the panes that did not change, and act.

No LLM. A working Claude shows a running timer, so its screen changes between two
captures; a screen that did not change for a minute has finished or stopped. The latest
capture of every pane is kept (REPORT) so it can be read at any moment (`swarm-watch
--panes`).

Classes: shell (no Claude), new (first capture), working (changed since the last
capture), frozen (the busy line is on screen but nothing moved for FROZEN_S), dialog (the
trust-folder dialog), menu (a choice or permission menu), asking (the last message asks
Michal something), input (unsent text in the prompt), idle (an empty prompt).

Agents report their own state (report_status, tangentswarm #53), so the pane capture is
the fallback: a PR whose agent reported in the last REPORT_QUIET s is left to that report.

Actions on a worker pane (~/ver/platform.<slug>, not a coordinator window):
  input     press Enter (a relay that was typed but not sent)
  asking    a bug (a Claude never asks in its pane): routed as a needs-input report
            (agent_report: question on the ticket, card to Needs Input, /exit), and an
            incident ticket
  idle      card in Needs Input (dirty work is fine: --resume brings it back), or the work
            is handed off (card past In Progress, PR closed, no PR): /exit, the window and
            its shell stay
            card In Progress or draft with a clean, pushed head: the Stop hook missed it,
            so the handoff runs now (through the spool), and an incident ticket
            dirty or unpushed: one nudge to commit and push or call needs_input
  dialog    press Enter (trust the worktree)
  menu, frozen   swarmbot diagnoses (a wake with the capture), and an incident ticket
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import board

HOME = Path.home()
SESSION = os.environ.get('SWARM_TICK_SESSION', 'agents')
REPORT = Path(os.environ.get('SWARM_PANES_REPORT', str(HOME / '.local' / 'state' / 'swarm-flow' / 'panes.json')))
INTERVAL = int(os.environ.get('SWARM_WATCH_INTERVAL', '60'))
FROZEN_S = int(os.environ.get('SWARM_FROZEN_S', '600'))
COORDINATOR_PREFIXES = tuple(p for p in os.environ.get('SWARM_NO_PARK', 'meth-').split(',') if p)
SHELLS = {'bash', 'sh', 'zsh', 'fish', 'dash'}
PANE_FMT = '\t'.join(['#{pane_id}', '#{window_index}', '#{window_name}', '#{pane_current_command}',
                      '#{pane_current_path}', '#{pane_dead}', '#{pane_pid}'])
TRUST_RE = re.compile(r'Do you trust the files in this folder|trust this folder', re.I)
HANDOFF_WAIT = int(os.environ.get('SWARM_HANDOFF_WAIT', '900'))  # s for the spool to answer a handoff
REPORT_QUIET = int(os.environ.get('SWARM_REPORT_QUIET', '600'))  # s an agent's own report holds the watcher off
NUDGE = ("Coordinator: #{n}: you stopped with {bits} in your worktree. Commit and push your work. "
         "If you need a decision from Michal, call the needs_input tool of the tangentswarm MCP server "
         "instead of asking here.")


# -- pure ------------------------------------------------------------------------------

def screen_hash(screen):
    return hashlib.sha256((screen or '').encode('utf-8', 'replace')).hexdigest()[:16]


def compare(prev, cur, now):
    """Carry the change time: a new screen hash starts a new change_at. Returns cur with
    changed_at and unchanged_s set (prev None: a first capture, unchanged_s None)."""
    h = cur['hash']
    if prev and prev.get('hash') == h:
        cur['changed_at'] = prev.get('changed_at', now)
        cur['unchanged_s'] = now - cur['changed_at']
    else:
        cur['changed_at'] = now
        cur['unchanged_s'] = None if prev is None else 0
    return cur


def asked(screen):
    """The question when the last message asks for a decision (its last line ends with "?",
    or it says it waits for a decision), else None."""
    from . import stophook, tick
    lines = tick.last_message(screen or '')
    if not lines:
        return None
    last = lines[-1].strip().rstrip('*_` ')
    text = '\n'.join(lines)
    if last.endswith('?') or stophook.WAITING_RE.search(text[-600:]):
        return ' '.join(l.lstrip('●- ').strip() for l in lines)[-1200:]
    return None


def classify(screen, blank, claude, unchanged_s, frozen_s=FROZEN_S):
    from . import tick
    if not claude:
        return 'shell'
    if unchanged_s is None:
        return 'new'
    tail = '\n'.join((screen or '').rstrip('\n').split('\n')[-40:])
    if unchanged_s < INTERVAL * 0.5:
        return 'working'
    if tick.BUSY_RE.search(tail):
        return 'frozen' if unchanged_s >= frozen_s else 'working'
    if TRUST_RE.search(tail):
        return 'dialog'
    if tick.MENU_RE.search(tail):
        return 'menu'
    if not blank:  # Codex on PR #51: a typed answer under an old question is input to send
        return 'input'
    if asked(screen):
        return 'asking'
    return 'idle'


def is_worker(p):
    from . import slots
    top = (p.get('wt') or {}).get('top') or p.get('path')
    return slots.is_platform_worktree(top) and not (p.get('window') or '').startswith(COORDINATOR_PREFIXES)


def dirty_bits(wt):
    return ((['uncommitted changes'] if wt.get('dirty') else []) +
            ([f"{wt['ahead']} unpushed commit(s)"] if wt.get('ahead') else []) +
            (['no upstream'] if wt.get('ahead') is None else []))


def decide(p, pr):
    """[(action, detail)] for one pane. p: the pane (cls, wt, screen); pr: its open board PR
    {number, col, draft, head} or None."""
    cls, wt = p.get('cls'), p.get('wt') or {}
    if not is_worker(p) or cls in ('shell', 'new', 'working'):
        return []
    if cls == 'input':
        return [('submit', '')]
    if cls == 'dialog':
        return [('trust', '')]
    if cls in ('menu', 'frozen'):
        return [(cls, '')]
    if pr and pr.get('col') == board.NEEDS_INPUT:  # the question is on the ticket already
        return [('exit', 'card in Needs Input')]
    if cls == 'asking':
        return [('ask', asked(p.get('screen')) or '')]
    bits = dirty_bits(wt)
    if bits:
        return [('nudge', ', '.join(bits))]
    if pr is None:
        return [('exit', 'no open PR for this branch')]
    if wt.get('scaffold'):
        return []  # a new worker before its brief: the cascade's start retry sends it
    if pr.get('col') == board.IN_PROGRESS or pr.get('draft'):
        if wt.get('head') != pr.get('head'):
            return [('nudge', f"local HEAD {(wt.get('head') or '?')[:8]} that is not the PR head")]
        return [('handoff', f"card {pr.get('col')}{', draft' if pr.get('draft') else ''}")]
    return [('exit', f"card in {pr.get('col')}")]


# -- reading tmux ------------------------------------------------------------------------

def _sh(argv, timeout=30):
    from . import tick
    return tick.sh(argv, timeout=timeout)


def list_panes(sh=_sh):
    r = sh(['tmux', 'list-panes', '-s', '-t', SESSION, '-F', PANE_FMT])
    if r.returncode:
        return []
    out = []
    for line in r.stdout.splitlines():
        f = line.split('\t')
        if len(f) < 7 or f[5] not in ('', '0'):
            continue
        out.append(dict(id=f[0], widx=f[1], window=f[2], command=f[3], path=f[4], pane_pid=f[6]))
    return out


def capture(pane_id, sh=_sh):
    from . import agents
    raw = sh(['tmux', 'capture-pane', '-p', '-e', '-t', pane_id]).stdout or ''
    return agents.strip_ansi(raw), agents.claude_prompt_blank(agents.input_view(raw))


def worktree(path):
    """tick.worktree_facts plus 'scaffold' (the head is the "Start #N" commit)."""
    from . import tick
    wt = tick.worktree_facts(path)
    if wt:
        wt['scaffold'] = bool(tick.SCAFFOLD_RE.match(tick.git(wt['top'] or path, 'log', '-1', '--format=%s') or ''))
    return wt


def root_is_shell(pane_pid):
    """True when the pane's own process is a shell: /exit then leaves the window open."""
    try:
        return Path(f'/proc/{int(pane_pid)}/comm').read_text().strip() in SHELLS
    except (OSError, ValueError):
        return False


# -- the watcher -------------------------------------------------------------------------

class PaneWatch:
    """One pass a minute. Every side effect goes through an injectable hook for tests."""

    def __init__(self, dry=False, sh=_sh, panes=None, cap=None, facts=worktree, is_claude=None, tell=None,
                 handoff=None, result=None, route=None, reported=None, wake=None, incident=None, st=None, shell_root=root_is_shell,
                 report=None):
        from . import alerts
        self.dry, self.sh = dry, sh
        self.panes_fn = panes or (lambda: list_panes(self.sh))
        self.cap = cap or (lambda pid: capture(pid, self.sh))
        self.facts = facts
        self.is_claude = is_claude or self._is_claude
        self.tell = tell or self._tell
        self.handoff = handoff or self._handoff
        self.result = result or self._result
        self.inflight = {}  # once-key -> (spool request id, queued at): handoffs not answered yet
        self.route = route or self._route
        self.reported = reported or self._reported
        self.wake = wake or alerts.wake_swarmbot
        self.st = st or alerts.state()
        self.incident = incident or (lambda *a, **k: alerts.incident(*a, st=self.st, **k))
        self.shell_root = shell_root
        self.report = REPORT if report is None else report
        self.latest = {}  # pane id -> the latest capture (reportable at any moment)

    # the default effects
    @staticmethod
    def _is_claude(p):
        from . import tick
        return tick.is_claude(p['id'], p['command'])

    def _tell(self, pane, text):
        from . import agents
        agents.tell_agent(pane, text)

    def _handoff(self, wt):
        """Queue the handoff in the spool; returns the request id (the daemon serves it)."""
        from . import spool
        return spool.submit('handoff', wt)['queued']

    @staticmethod
    def _result(rid):
        from . import spool
        return spool.peek(rid)

    def _route(self, n, question, window):
        """A pane that asks: queue the needs-input report agent_report handles (tangentswarm #53)."""
        from . import agent_report
        out = agent_report.accept('needs-input', n, window, question=question, agent='pane-watch')
        if not out.get('ok'):
            raise RuntimeError(out.get('error') or 'the needs-input report was refused')
        return 'queued as a needs-input report (question on the ticket, card to Needs Input, /exit)'

    @staticmethod
    def _reported(n):
        from . import agent_report
        return agent_report.last_report(n, within=REPORT_QUIET) is not None

    def once(self, prs, now=None):
        """One capture of every pane. prs: {branch: {number, col, draft, head}} of the open
        board PRs. Returns the actions taken, as lines."""
        now = time.time() if now is None else now
        seen, done = {}, []
        for row in self.panes_fn():
            p = dict(row)
            p['claude'] = bool(self.is_claude(p))
            screen, blank = self.cap(p['id']) if p['claude'] else ('', True)
            p.update(screen=screen, blank=blank, hash=screen_hash(screen), captured_at=now)
            prev = self.latest.get(p['id'])
            compare(prev, p, now)
            p['cls'] = classify(screen, blank, p['claude'], p['unchanged_s'])
            if not p['claude']:
                p['wt'] = None
            elif p['cls'] == 'working' and prev and prev.get('wt') and prev.get('path') == p['path']:
                p['wt'] = prev['wt']  # a working pane needs no git reads this minute
            else:
                p['wt'] = self.facts(p['path'])
            pr = prs.get((p['wt'] or {}).get('branch'))
            p['pr'] = pr['number'] if pr else None
            seen[p['id']] = p
            if pr and is_worker(p) and self.reported(pr['number']):
                p['note'] = 'its agent reported recently: left to the report'
                continue
            for action, detail in decide(p, pr):
                try:
                    line = self.act(action, detail, p, pr, now)
                except Exception as e:  # one pane failing never stops the others
                    line = f"{p['id']} {p['window']}: {action} failed: {type(e).__name__}: {str(e)[:200]}"
                if line:
                    done.append(line)
        self.latest = seen
        self.save_report(now)
        return done

    def seen(self, key):
        """True when the action of key already succeeded (persisted)."""
        return self.st.done(key)

    def mark(self, key):
        """Record key only after its action succeeded (Codex on PR #51: a key saved before a
        failed route hid the question for good, so a failure is retried next pass)."""
        if not self.dry:
            self.st.mark(key)

    def keys(self, argv):
        r = self.sh(argv)
        if getattr(r, 'returncode', 0):
            raise RuntimeError(f"{' '.join(argv[:3])}: {(getattr(r, 'stderr', '') or '').strip()[:120] or r.returncode}")

    def act(self, action, detail, p, pr, now):
        n = pr['number'] if pr else None
        where = f"{p['id']} {p['window']}"
        tag = f"#{n} " if n else ''
        if self.dry:
            return f"would {action} {tag}{where}: {detail}".rstrip(': ')
        if action == 'submit':
            key = f"watch-submit:{p['id']}:{p['hash']}"
            if self.seen(key):
                return None
            self.keys(['tmux', 'send-keys', '-t', p['id'], 'Enter'])
            self.mark(key)
            return f"{tag}{where}: submitted the unsent prompt"
        if action == 'trust':
            key = f"watch-trust:{p['id']}:{p['hash']}"
            if self.seen(key):
                return self.diagnose('dialog', p, n, 'the trust dialog stayed after Enter')
            self.keys(['tmux', 'send-keys', '-t', p['id'], 'Enter'])
            self.mark(key)
            return f"{tag}{where}: accepted the trust dialog for its worktree"
        if action in ('menu', 'frozen'):
            what = ('a menu blocks the Claude: ' + self._menu(p['screen'])) if action == 'menu' else \
                f"the busy screen has not changed for {int((p.get('unchanged_s') or 0) // 60)} min"
            return self.diagnose(action, p, n, what)
        if action == 'exit':
            return self.exit(p, tag, where, detail)
        if action == 'ask':
            if not n:
                return self.diagnose('asking', p, n, 'asks in its pane and has no open PR')
            key = f"watch-ask:{n}:{screen_hash(detail)}"
            if self.seen(key):
                return None
            note = self.route(n, detail, p['window'])
            self.mark(key)
            self.incident(f"ask:{n}:{screen_hash(detail)}", f"#{n}: a worker asked in its pane",
                          f"The worker Claude of #{n} ({where}) asked in its pane instead of calling needs_input: "
                          f"{detail[:600]}", fix=f"The pane watcher routed it: {note}")
            return f"{tag}{where}: asked in the pane (a bug): {note}"
        if action == 'handoff':
            head = p['wt'].get('head') or ''
            key = f"watch-handoff:{n}@{head}"
            if self.seen(key):
                return None
            if key in self.inflight:  # Codex on PR #51: done only when the spool says it succeeded
                rid, at = self.inflight[key]
                res = self.result(rid)
                if res is None and now - at < HANDOFF_WAIT:
                    return None
                del self.inflight[key]
                if res is not None and res.get('ok'):
                    self.mark(key)
                    return f"{tag}{where}: the missed handoff ran: {str(res.get('log') or res.get('why') or 'done')[:160]}"
                why = (res or {}).get('error') or f'no answer in {HANDOFF_WAIT} s'
                self.inflight[key] = (self.handoff(p['wt']['top']), now)
                return f"{tag}{where}: the missed handoff failed ({str(why)[:160]}): queued it again"
            self.inflight[key] = (self.handoff(p['wt']['top']), now)
            self.incident(f"missed-handoff:{n}@{head[:12]}", f"#{n}: the Stop hook missed a finished worker",
                          f"The worker of #{n} ({where}) was idle with a clean, pushed head {head[:8]} and the card "
                          f"still {detail}.", fix='The pane watcher queued the handoff.')
            return f"{tag}{where}: finished but {detail}: queued the handoff (the Stop hook missed it)"
        if action == 'nudge':
            key = f"watch-nudge:{p['id']}:{p['wt'].get('head')}:{detail}"
            if self.seen(key):
                return None
            self.tell(p['id'], NUDGE.format(n=n or '?', bits=detail))
            self.mark(key)
            return f"{tag}{where}: idle with {detail}: nudged"
        return None

    def exit(self, p, tag, where, why):
        if not self.shell_root(p.get('pane_pid')):
            if self.seen(f"watch-noexit:{p['id']}"):
                return None
            self.mark(f"watch-noexit:{p['id']}")
            return f"{tag}{where}: idle ({why}), but Claude is the pane's own process: /exit would close the window"
        self.tell(p['id'], '/exit')
        return f"{tag}{where}: idle ({why}): sent /exit, the window stays"

    def diagnose(self, kind, p, n, what):
        key = f"{kind}:{p['id']}:{p['hash']}"
        if self.seen('watch-diagnose:' + key):
            return None
        capture_tail = '\n'.join((p.get('screen') or '').rstrip('\n').split('\n')[-60:])
        why = f"pane {p['id']} {p['window']}{' #' + str(n) if n else ''}: {what}"
        self.wake(why, key, pr=n, detail={'pane': p['id'], 'window': p['window'], 'path': p.get('path'),
                                          'capture': capture_tail})
        self.mark('watch-diagnose:' + key)
        self.incident(key, f"{kind} in {p['window']}", f"{why}.\n\nPane capture:\n```\n{capture_tail[-3000:]}\n```")
        return f"{why}: woke swarmbot to diagnose"

    @staticmethod
    def _menu(screen):
        from . import tick
        return tick.menu_line(screen or '')

    def save_report(self, now):
        if not self.report or self.dry:  # a dry run never replaces the daemon's report
            return
        rows = {k: {f: v for f, v in p.items() if f not in ('wt',)} | {'wt': p.get('wt')}
                for k, p in self.latest.items()}
        try:
            self.report.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.report.with_name(f'.{self.report.name}.{os.getpid()}')
            tmp.write_text(json.dumps({'at': now, 'panes': rows}, default=str))
            os.replace(tmp, self.report)
        except OSError:
            pass


def read_report(path=None):
    try:
        return json.loads((path or REPORT).read_text())
    except (OSError, ValueError):
        return {'at': None, 'panes': {}}
