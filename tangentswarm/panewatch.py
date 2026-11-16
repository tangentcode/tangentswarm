"""The pane watcher (tangentswarm #40, Michal 2026-11-15 6:38 PM ET): once a minute the
server captures every pane of the swarm's tmux session, keeps the latest capture as state
(PANES, for reports and the Agents screen, #44), and compares it with the previous one.

A working Claude shows a running timer (and so does one with a subworker), so a pane whose
screen has not changed for a minute has probably finished or stopped. Only then is it
classified (tick.classify_screen: idle at the prompt, asking, a menu, unsent input) and
acted on, with no LLM:

  input    real unsent text at the prompt: submit it (Enter), once per screen
  idle     a Claude idle at the prompt in a worktree whose PR card is In Progress: the Stop
           hook may have missed it, so queue the same handoff (spool op 'stophook'), once
           per screen
  asking   a Claude that asks in its pane (a bug: it should use needs_input): wake swarmbot
  menu     a blocked dialog: wake swarmbot to diagnose
  frozen   a Claude that still shows its busy line but has not changed for FROZEN s: wake
           swarmbot

Every class change is a row in the events DB (source 'panes').
"""
import hashlib
import json
import os
import time
from pathlib import Path

from . import tick

PANES = Path(os.environ.get('SWARM_PANES_STATE', str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'panes.json')))
QUIET = 60      # s without a change before a pane is classified
FROZEN = 300    # s a busy screen may stay unchanged before it counts as frozen
FMT = '\t'.join(['#{pane_id}', '#{window_name}', '#{pane_current_command}', '#{pane_current_path}', '#{pane_dead}'])


def list_panes(session=None, sh=None):
    """[{id, window, command, path}] of the live panes of the swarm session."""
    sh = sh or tick.sh
    r = sh(['tmux', 'list-panes', '-s', '-t', session or tick.SESSION, '-F', FMT], timeout=30)
    if r.returncode:
        return []
    out = []
    for line in (r.stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) == 5 and parts[4] in ('', '0'):
            out.append(dict(id=parts[0], window=parts[1], command=parts[2], path=parts[3]))
    return out


def load(path=None):
    try:
        d = json.loads((path or PANES).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d, path=None):
    path = path or PANES
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, path)


def observe(prev, pane, screen, blank, claude, now):
    """The pane's new record from its capture (pure). Its class is 'working' while the
    screen changes, and the screen's class once it has been quiet for QUIET s."""
    h = hashlib.sha256(screen.encode()).hexdigest()[:16]
    changed = not prev or prev.get('hash') != h
    since = now if changed or prev.get('changed_at') is None else float(prev['changed_at'])
    quiet = now - since
    if not claude:
        cls = 'shell'
    elif quiet < QUIET:
        cls = 'working'
    else:
        cls = tick.classify_screen(screen, blank)
        if cls == 'working' and quiet >= FROZEN:
            cls = 'frozen'
        elif cls == 'working':
            cls = 'quiet'  # busy line shown, not changed yet: wait for FROZEN
    lines = [l for l in screen.rstrip('\n').split('\n') if l.strip()]
    return dict(id=pane['id'], window=pane['window'], command=pane['command'], path=pane['path'],
                pr=tick.window_pr(pane['window']), claude=claude, hash=h, changed_at=since, seen=now,
                quiet=int(quiet), cls=cls, tail=lines[-12:], acted=(prev or {}).get('acted'))


def actions(rec, card_col=None):
    """[(kind, detail)] to do for a pane record (pure), at most once per screen hash."""
    if rec.get('acted') == rec['hash'] or not rec['claude']:
        return []
    cls = rec['cls']
    if cls == 'input':
        return [('enter', None)]
    if cls == 'idle' and card_col == 'In Progress':
        return [('stophook', rec['path'])]
    if cls in ('asking', 'menu', 'frozen'):
        what = {'asking': 'asks in its pane instead of using needs_input',
                'menu': 'is blocked at a dialog', 'frozen': f'shows a busy line but has not changed for {rec["quiet"]} s'}[cls]
        return [('wake', f"pane {rec['window']} ({rec['id']}) {what}: " + ' | '.join(rec['tail'][-3:])[:300])]
    return []


def transcript_of(worktree, projects=None):
    """The newest Claude transcript (JSONL) of a worktree, or None (slots.latest_session)."""
    from . import slots
    sid = slots.latest_session(worktree, projects)
    if not sid:
        return None
    import re
    return str((projects or slots.PROJECTS) / re.sub(r'[^A-Za-z0-9]', '-', str(worktree)) / f'{sid}.jsonl')


def _log(event, **kw):
    try:
        from . import eventdb
        if hasattr(eventdb, 'log_event'):
            eventdb.log_event(event, source='panes', **kw)
    except Exception:
        pass


def _wake(pr, why, key):
    from . import webhook
    if hasattr(webhook, 'wake_exception'):
        return webhook.wake_exception(pr, why, key=key, source='swarm-promoted')
    return webhook.Executor().wake(pr, why, key=key)


class PaneWatch:
    def __init__(self, sh=None, capture=None, is_claude=None, cols=None, submit=None, wake=None,
                 path=None, out=print, dry=False, transcript=None):
        self.sh = sh or tick.sh
        self.capture = capture or tick.capture
        self.is_claude = is_claude or tick.is_claude
        self.cols = cols  # () -> {PR number: column}; read only when a pane needs it
        self.submit = submit
        self.wake = wake or _wake
        self.path, self.out, self.dry = path, out, dry
        self.transcript = transcript

    def card_cols(self):
        if self.cols:
            return self.cols()
        from . import advance, promoted
        return {p['number']: col for col, ps in promoted.read_columns(advance.hr_gate()['gql'], max_age=60).items()
                for p in ps}

    def scan(self, now=None):
        """One pass over every pane. Returns the new state {pane id: record}."""
        now = time.time() if now is None else now
        prev = load(self.path)
        state, cols = {}, None
        for p in list_panes(sh=self.sh):
            try:
                screen, blank = self.capture(p['id'])
                claude = self.is_claude(p['id'], p['command'])
            except Exception:
                continue
            rec = observe(prev.get(p['id']), p, screen, blank, claude, now)
            old = (prev.get(p['id']) or {}).get('cls')
            if rec['cls'] != old and rec['claude']:
                _log('pane', action=rec['cls'], pr=rec['pr'], actor=rec['window'], notes=rec['tail'][-2:])
            todo = actions(rec)
            if not todo and rec['claude'] and rec['cls'] == 'idle' and rec['acted'] != rec['hash'] and rec['pr']:
                try:  # Codex on PR #52: a board outage must not stop the other panes' observations
                    cols = cols if cols is not None else self.card_cols()
                    todo = actions(rec, cols.get(rec['pr']))
                except Exception as e:
                    self.out(f"panes: board read failed for {rec['window']}: {type(e).__name__}: {str(e)[:120]}")
                    cols = {}
            for kind, detail in todo:
                self.act(rec, kind, detail)
            state[p['id']] = rec
        if not self.dry:
            save(state, self.path)
        return state

    def act(self, rec, kind, detail):
        if self.dry:
            return self.out(f"panes: would {kind} {rec['window']} ({rec['id']})")
        rec['acted'] = rec['hash']
        if kind == 'enter':
            self.sh(['tmux', 'send-keys', '-t', rec['id'], 'Enter'])
        elif kind == 'stophook':
            # Codex on PR #52: the hook's own safety check (a question, partial scope, a pending
            # decision goes to Needs Input, never ready) needs the worker's transcript
            t = (self.transcript or transcript_of)(detail)
            if not t:
                kind, detail = 'wake', (f"pane {rec['window']} ({rec['id']}) is idle with its card In Progress, "
                                        "but no transcript was found for the Stop-hook check")
                self.wake(rec['pr'], detail, f"pane:{rec['id']}:{rec['hash']}")
            else:
                from . import spool
                (self.submit or spool.submit)('stophook', detail, transcript=t)
        elif kind == 'wake':
            self.wake(rec['pr'], detail, f"pane:{rec['id']}:{rec['hash']}")
        _log('pane-action', action=kind, pr=rec['pr'], actor=rec['window'], notes=[str(detail or '')[:300]])
        self.out(f"panes: {kind} {rec['window']} ({rec['id']})")
