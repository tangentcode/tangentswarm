"""swarm-events: a console viewer of the swarm-webhook event DB (Michal 2026-11-15 1:56 PM ET).

Opens the event DB read-only, prints the rows since a day (today ET by default), then
follows new rows every second. One line per row:

  ET time  stage  event/action  repo#pr  sender  sig  decision -> actions done  error

An error is red; duplicate, log-only and rejected rows are dim. -v prints the full row as
JSON (the raw body decoded) under each line.

  swarm-events [--since today|all|YYYY-MM-DD] [-v] [--once] [--db PATH] [--color|--no-color]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

from . import eventdb

RED, DIM, BOLD, RESET = '\033[31m', '\033[2m', '\033[1m', '\033[0m'


def since_iso(since, now=None):
    """The received_at lower bound for --since (ISO with the ET offset), or '' for all."""
    if since == 'all':
        return ''
    now = now or datetime.now(eventdb.ET)
    if since == 'today':
        day = now.date()
    else:
        day = datetime.strptime(since, '%Y-%m-%d').date()
    return datetime(day.year, day.month, day.day, tzinfo=eventdb.ET).isoformat(timespec='seconds')


def _list(text):
    try:
        v = json.loads(text or '[]')
    except ValueError:
        return [str(text)]
    return v if isinstance(v, list) else [v]


def line(r, color=True):
    """One console line for a row (a dict of the events columns)."""
    t = (r.get('received_at') or '')[11:19]
    ev = r.get('event') or '-'
    if r.get('action'):
        ev += '/' + r['action']
    where = (r.get('repo') or '-') + (f"#{r['pr']}" if r.get('pr') else '')
    sig = {1: 'sig ok', 0: 'SIG BAD', None: 'sig -'}.get(r.get('sig_ok'), 'sig ?')
    decision = ','.join(_list(r.get('decisions'))) or '-'
    done = ', '.join(_list(r.get('done'))) or '-'
    reason = f" ({r['reason']})" if r.get('reason') else ''
    out = f"{t} {r.get('stage', ''):<8} {ev:<34} {where:<28} {r.get('sender') or '-':<20} {sig:<7} {decision}{reason} -> {done}"
    err = r.get('error')
    if not color:
        return out + (f"  ERROR: {err}" if err else '')
    if err:
        return out + f"  {RED}ERROR: {err}{RESET}"
    if r.get('reason') in ('duplicate', 'log-only', 'bad signature') or r.get('sig_ok') == 0:
        return DIM + out + RESET
    return out


def full(r):
    d = dict(r)
    body = d.get('body')
    if isinstance(body, (bytes, bytearray)):
        try:
            d['body'] = json.loads(body)
        except ValueError:
            d['body'] = body.decode('utf-8', 'replace')
    for k in ('headers', 'decisions', 'done', 'notes'):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return json.dumps(d, indent=2, default=str)


def connect(path):
    """Read-only: the viewer never writes (and the DB is append-only anyway)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def rows_after(db, last_id, since=''):
    return [dict(r) for r in db.execute('SELECT * FROM events WHERE id > ? AND received_at >= ? ORDER BY id',
                                        (last_id, since))]


def run(path, since='today', verbose=False, once=False, color=None, out=sys.stdout, sleep=time.sleep,
        polls=None):
    color = out.isatty() if color is None else color
    lower = since_iso(since)
    while not os.path.exists(path):  # the server makes it on its first request
        if once:
            print(f"swarm-events: no event DB at {path} yet", file=out)
            return 1
        sleep(1)
    db = connect(path)
    last, n = 0, 0
    while True:
        try:
            got = rows_after(db, last, lower)
        except sqlite3.OperationalError as e:  # e.g. a busy WAL checkpoint: try again
            got = []
            print(f"swarm-events: {e}", file=sys.stderr)
        for r in got:
            print(line(r, color), file=out)
            if verbose:
                print(full(r), file=out)
            last = r['id']
        out.flush()
        n += 1
        if once or (polls is not None and n >= polls):
            return 0
        sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-events', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--since', default='today', help='today (ET, default), all, or YYYY-MM-DD')
    ap.add_argument('-v', '--verbose', action='store_true', help='print the full row as JSON')
    ap.add_argument('--once', action='store_true', help='print the rows and exit (no follow)')
    ap.add_argument('--db', default=eventdb.PATH)
    ap.add_argument('--color', dest='color', action='store_true', default=None)
    ap.add_argument('--no-color', dest='color', action='store_false')
    a = ap.parse_args(argv)
    try:
        since_iso(a.since)
    except ValueError:
        ap.error('--since: today, all or YYYY-MM-DD')
    try:
        return run(a.db, a.since, a.verbose, a.once, a.color)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
