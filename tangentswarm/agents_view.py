"""The Agents screen of swarm-events (tangentswarm #44): one line per tmux pane of the swarm,
from a livestate sample, under a header with the host load and the start capacity.

  #  window                  claude   state      screen  mem    cpu   PR    card          processes
"""
import re

from . import launches

RED, GREEN, YELLOW, DIM, BOLD, RESET = '\033[31m', '\033[32m', '\033[33m', '\033[2m', '\033[1m', '\033[0m'
MB = 1024 ** 2


ANSI = re.compile(r'\x1b\[[0-9;]*m')


def clip(text, width):
    """text cut to width visible characters (color codes kept, and closed when cut), so a
    line never wraps on the terminal (Codex on PR #48)."""
    out, seen, i = [], 0, 0
    while i < len(text):
        m = ANSI.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if seen == width:
            return ''.join(out) + (RESET if ANSI.search(text) else '')
        out.append(text[i])
        seen += 1
        i += 1
    return text


def ago(s):
    """A short age: 45s, 12m, 3h, 2d ('' for None)."""
    if s is None:
        return ''
    s = int(s)
    for unit, n in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def mb(v):
    return '?' if v is None else f"{v // MB}M"


def header(st):
    """The two header lines: the host load, memory and Claude count; the start capacity."""
    h = st['host']
    load = ' '.join(f"{x:.2f}" for x in h['load'])
    parts = [f"load {load} on {h['cpus']} CPUs (holds at {launches.load_max(h['cpus']):.1f})"]
    m = h.get('mem')
    if m:
        room = launches.headroom(m)
        parts.append(f"workers {mb(m['current'])} of {mb(m['high'] or m['max'])} (room {mb(room)})")
        parts.append(f"host free {mb(m['avail'])}")
    else:
        parts.append(f"worker memory unknown: {h.get('mem_error') or '?'}"[:80])
    n = h.get('claudes')
    parts.append(f"Claudes {'?' if n is None else n}/{h['max_claudes']}")
    cap = st['capacity']
    capline = f"capacity: {cap['free']} start(s) free" if cap['free'] else f"capacity: none: {cap['why']}"
    return ['  '.join(parts), capline]


INTERPRETERS = {'python', 'python3', 'bash', 'sh', 'node', 'nodejs', 'bun', 'deno', 'timeout', 'env', 'npx', 'uv'}


def short(cmd):
    """A short name of a command line: the program, or the script an interpreter runs."""
    words = [w.rsplit('/', 1)[-1] for w in cmd.split(' ') if w]
    i = 0
    while i < len(words) - 1:
        w = words[i]
        if w == '-m':
            i += 1
            break
        if not (w in INTERPRETERS or w.startswith(('python', '-')) or w.rstrip('smh').isdigit()):
            break
        i += 1
    return ' '.join(words[i:i + 2])[:30]


def procs_text(r):
    def names(ps):
        return ', '.join(short(cmd) for _, cmd in ps[:3]) + (f" +{len(ps) - 3}" if len(ps) > 3 else '')
    out = []
    if r['tests']:
        out.append(f"tests: {names(r['tests'])}")
    if r['other']:
        out.append(f"{len(r['other'])} other: {names(r['other'])}")
    return '; '.join(out)


CARD_NAMES = {'Pending': 'Staging'}  # board.STAGING keeps the internal key "Pending"


def row(r, width=200):
    """The plain text line of one pane."""
    state = r['state']
    if state == 'idle' and r.get('idle_s') is not None:
        state = f"idle {ago(r['idle_s'])}"
    cols = [f"{r['index']!s:>3}", f"{r['window'][:30]:<30}", f"{r['claude'] or '-'!s:>8}", f"{state:<10}",
            f"{ago(r.get('screen_s')):>6}", f"{mb(r['mem']):>6}", f"{r['cpu']:>5.0f}%" if r.get('cpu') is not None else f"{'-':>6}",
            f"{('#' + str(r['pr'])) if r.get('pr') else '':<6}",
            f"{CARD_NAMES.get(r.get('card'), r.get('card') or '')[:13]:<13}", procs_text(r)]
    return ' '.join(cols).rstrip()[:width]


TITLE = '  # window                            claude state      screen    mem   cpu PR     card          processes'


def render(st, color=True, width=200, height=None, top=0):
    """The lines of the Agents screen. With a height, the header and the summary always show,
    the pane rows scroll (top is the first row shown, clamped), and no line is wider than
    width."""
    head = header(st)
    if color:
        cap = st['capacity']
        head[1] = (GREEN if cap['free'] else RED) + head[1] + RESET
    head += ['', (BOLD + TITLE + RESET) if color else TITLE]
    body = []
    for r in st['agents']:
        text = row(r, width)
        if color:
            if r['tests']:
                text = YELLOW + text + RESET
            elif r['state'] == 'active':
                text = BOLD + text + RESET
            elif r['state'] == 'no claude':
                text = DIM + text + RESET
        body.append(text)
    n = sum(1 for r in st['agents'] if r['claude'])
    act = sum(1 for r in st['agents'] if r['state'] == 'active')
    tests = sum(1 for r in st['agents'] if r['tests'])
    summary = f"{len(st['agents'])} panes; {n} with a Claude ({act} active); {tests} running tests"
    keys = '    keys: e events  a agents  q quit'
    if not height:
        return head + body + ['', summary + keys]
    room = page(height)
    top = clamp(top, len(body), height)
    if len(body) > room:
        summary += f"; rows {top + 1}-{min(top + room, len(body))} of {len(body)}"
        keys += '  j/k or space/b scroll'
    # every line is one terminal line, so page() counts what is drawn
    return [clip(l, width) for l in head + body[top:top + room] + ['', summary + keys]]


def page(height):
    """How many pane rows one screen of this height shows: all but the 4 header and 2 summary lines."""
    return max(1, height - 6)


def clamp(top, rows, height):
    """The first row shown: top, kept so that the last page is full."""
    return max(0, min(top, rows - page(height)))
