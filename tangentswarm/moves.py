"""The move log: every column move with a time stamp, so wait times from Ready to Human
Review can be compared (for example Staging floor 1 against floor 2; Michal 2026-11-14
7:16 PM ET).

  moves.log   one tab-separated line per move:
              utc time, ET time, #PR, from, to, head, seconds since first seen in Ready
              (empty if never seen), Staging floor, note
  ready-seen  {PR: utc epoch} the first time a PR was seen in Ready. The swarm-promoted
              daemon reads the columns every 30 s (promoted.read_columns), so this is the
              Ready entry time to within a poll.

    python -m tangentswarm.moves      the Ready -> Human Review waits, per floor
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.environ.get('SWARM_FLOW_STATE', str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'pull.json'))).parent
LOG = Path(os.environ.get('SWARM_MOVES_LOG', str(STATE / 'moves.log')))
SEEN = Path(os.environ.get('SWARM_READY_SEEN', str(STATE / 'ready-seen.json')))
KEEP = 14 * 86400  # forget a first-seen time after 14 days


def _load(path):
    try:
        d = json.loads(path.read_text())
        return {str(k): float(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def note_ready(numbers, now=None, path=None):
    """Record the first time each PR is seen in Ready. A PR keeps its time after it leaves
    Ready (the wait runs until Human Review)."""
    path = path or SEEN
    now = time.time() if now is None else now
    seen = _load(path)
    new = {str(n) for n in numbers} - set(seen)
    old = [k for k, t in seen.items() if now - t > KEEP]
    if not new and not old:
        return seen
    for k in old:
        del seen[k]
    for k in new:
        seen[k] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(seen, indent=0, sort_keys=True))
    os.replace(tmp, path)
    return seen


def _et(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def log_move(n, frm, to, head='', note='', floor=None, now=None, log=None, seen=None):
    """Append one move. Never raises: the log must not break a move."""
    now = time.time() if now is None else now
    try:
        first = _load(seen or SEEN).get(str(n))
        wait = '' if first is None else str(int(now - first))
        if floor is None:
            floor = os.environ.get('SWARM_STAGING_FLOOR', '2')
        utc = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        line = '\t'.join([utc, _et(now), f"#{n}", frm, to, (head or '')[:8], wait, str(floor), note.replace('\t', ' ')])
        path = log or LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as f:
            f.write(line + '\n')
        return line
    except Exception:
        return None


def waits(log=None):
    """{floor: [seconds from first seen in Ready to Human Review]} from the move log."""
    out = {}
    try:
        lines = (log or LOG).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split('\t')
        if len(f) >= 8 and f[4] == 'Human Review' and f[6]:
            out.setdefault(f[7], []).append(int(f[6]))
    return out


def main(argv=None):
    for floor, ws in sorted(waits().items()):
        ws = sorted(ws)
        mid = ws[len(ws) // 2]
        print(f"floor {floor}: {len(ws)} PR(s) Ready -> Human Review, median {mid // 60} min, "
              f"max {ws[-1] // 60} min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
