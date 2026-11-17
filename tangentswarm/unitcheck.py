"""swarm-unit-check: every swarm service must import tangentswarm from flow-live (methodology #84).

Incident (methodology #84): swarm-mcp-apikey and swarm-mcp-oauth ran from ~/srv/swarm-tmux
while the other swarm services ran from ~/ver/tangentswarm.flow-live, so a deploy to
flow-live did not reach them. This check makes that drift fail loudly.

For each swarm user unit (systemctl --user, swarm*.service) that is enabled or active, it
reads WorkingDirectory, Environment (PYTHONPATH) and ExecStart, and finds where Python would
import tangentswarm from, in the order Python uses:

  1. the first sys.path entry: the working directory for `python -m` / `-c`, or the
     directory of the script (a venv console script such as swarm-mcp: the venv bin);
  2. the PYTHONPATH entries, in order;
  3. else the installed copy in the venv (an editable install points at some other
     checkout: that is how the #84 units loaded ~/srv/swarm-tmux).

The first of 1 and 2 that holds a tangentswarm package must be flow-live. The unit fails also
when its WorkingDirectory, or a path in ExecStart, is inside a tangentswarm checkout that is
not flow-live. When the unit runs, its process is checked the same way, with the cwd and
PYTHONPATH from /proc (a drop-in that is not restarted yet shows there).

A disabled unit that is not active is skipped. Template units (name@.service) are skipped.

    swarm-unit-check          print one line per unit; exit 1 if any unit fails
    swarm-unit-check --json   the results as JSON

The flow-live path is $SWARM_FLOW_LIVE, default ~/ver/tangentswarm.flow-live.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

PATTERN = 'swarm*.service'
SKIP_STATES = ('disabled', 'masked', 'masked-runtime', 'bad', 'not-found')
PROPS = ('Id', 'UnitFileState', 'ActiveState', 'WorkingDirectory', 'Environment', 'ExecStart', 'MainPID')


def live_path():
    return os.path.realpath(os.path.expanduser(
        os.environ.get('SWARM_FLOW_LIVE') or '~/ver/tangentswarm.flow-live'))


def systemctl(*args):
    """Run systemctl --user; returns stdout. Raises on a failed call."""
    r = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl --user {' '.join(args)}: exit {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def has_pkg(d):
    """True when directory d holds a tangentswarm package (a checkout, or a copy of one)."""
    return bool(d) and os.path.isfile(os.path.join(d, 'tangentswarm', '__init__.py'))


def _norm(p):
    return os.path.realpath(p) if p else p


# -- reading systemd --------------------------------------------------------------------------

def unit_files(run=systemctl):
    """[(unit, state)] of the swarm*.service unit files."""
    out = run('list-unit-files', '--type=service', '--no-legend', '--plain', '--no-pager', PATTERN)
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith('.service'):
            rows.append((parts[0], parts[1]))
    return rows


def show(unit, run=systemctl):
    """The PROPS of one unit as a dict (systemctl show prints Key=value lines)."""
    out = run('show', unit, '--no-pager', *(f'-p{p}' for p in PROPS))
    props = {}
    for line in out.splitlines():
        k, sep, v = line.partition('=')
        if sep:
            props.setdefault(k, v)  # ExecStart can repeat in old systemd output; the first wins
    return props


def exec_argvs(value):
    """The argv lists in a systemctl show ExecStart value: '{ path=... ; argv[]=a b c ; ... }'."""
    out = []
    for chunk in value.split('argv[]=')[1:]:
        argv = chunk.split(' ; ', 1)[0].strip()
        try:
            out.append(shlex.split(argv))
        except ValueError:
            out.append(argv.split())
    return out


def environment(value):
    """A systemctl show Environment value (KEY=v pairs, quoted when they hold spaces) as a dict."""
    try:
        words = shlex.split(value or '')
    except ValueError:
        words = (value or '').split()
    return dict(w.split('=', 1) for w in words if '=' in w)


# -- where Python imports from ----------------------------------------------------------------

def path0(argv, cwd, env):
    """(first sys.path entry or None, PYTHONPATH honoured) for this argv, like CPython does."""
    if not argv:
        return None, True
    safe = bool(env.get('PYTHONSAFEPATH'))
    use_env = True
    if not os.path.basename(argv[0]).startswith('python'):
        return os.path.dirname(_norm(argv[0])), True  # a script (a venv console script)
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == '-' or not a.startswith('-'):
            if a == '-':
                return (None if safe else cwd), use_env
            return (None if safe else os.path.dirname(_norm(os.path.join(cwd or '/', a)))), use_env
        if a.startswith('--'):
            i += 1
            continue
        flags = a[1:]
        for j, c in enumerate(flags):
            if c == 'I':
                use_env, safe = False, True
            elif c == 'E':
                use_env = False
            elif c == 'P':
                safe = True
            elif c in 'mc':
                return (None if safe else cwd), use_env
            elif c in 'WX':
                if j == len(flags) - 1:
                    i += 1  # the value is the next word
                break
        i += 1
    return (None if safe else cwd), use_env


def import_root(argv, cwd, env, has=has_pkg):
    """The directory tangentswarm is imported from, or None (then the venv's installed copy loads)."""
    first, use_env = path0(argv, cwd, env)
    entries = [first] if first else []
    if use_env:
        entries += [p for p in (env.get('PYTHONPATH') or '').split(':') if p]
    for e in entries:
        e = _norm(os.path.join(cwd or '/', e))
        if has(e):
            return e
    return None


def checkout_of(path, has=has_pkg):
    """The tangentswarm checkout that holds path (path or a parent has the package), or None."""
    p = _norm(path)
    while p and p != '/':
        if has(p):
            return p
        p = os.path.dirname(p)
    return None


def problems(argv, cwd, env, live, has=has_pkg, where='unit'):
    """The reasons this argv/cwd/env does not run flow-live code ([] when it does)."""
    out = []
    root = import_root(argv, cwd, env, has)
    if root is None:
        out.append(f"{where}: no sys.path entry holds flow-live (PYTHONPATH={env.get('PYTHONPATH') or 'unset'}); "
                   f"the venv's installed tangentswarm loads, not {live}")
    elif root != live:
        out.append(f"{where}: imports tangentswarm from {root}, not {live}")
    co = checkout_of(cwd, has) if cwd else None
    if co and co != live:
        out.append(f"{where}: WorkingDirectory {cwd} is in the checkout {co}, not {live}")
    for a in argv:
        if a.startswith('/'):
            co = checkout_of(a, has)
            if co and co != live:
                out.append(f"{where}: ExecStart runs {a} from the checkout {co}, not {live}")
    return out


# -- processes --------------------------------------------------------------------------------

def proc_info(pid, proc=Path('/proc')):
    """(argv, cwd, env) of a live process, or None when it is gone or not readable."""
    base = Path(proc) / str(pid)
    try:
        argv = [a for a in (base / 'cmdline').read_bytes().decode(errors='replace').split('\0') if a]
        cwd = os.readlink(base / 'cwd').removesuffix(' (deleted)')
        raw = (base / 'environ').read_bytes().decode(errors='replace').split('\0')
    except OSError:
        return None
    return argv, cwd, dict(w.split('=', 1) for w in raw if '=' in w)


# -- the check --------------------------------------------------------------------------------

def check_unit(unit, props, live, has=has_pkg, proc=Path('/proc')):
    """One result dict: unit, state, active, pid, ok, problems."""
    env = environment(props.get('Environment'))
    cwd = props.get('WorkingDirectory') or ''
    cwd = cwd.lstrip('!-+')  # the systemd prefixes: '!' (ignore the user), '-' (may be absent)
    if cwd in ('', '~'):
        cwd = os.path.expanduser('~')  # a user unit runs in $HOME when it has none ('~' is $HOME too)
    found = []
    argvs = exec_argvs(props.get('ExecStart') or '')
    if not argvs:
        found.append(f"unit: cannot read ExecStart ({(props.get('ExecStart') or 'empty')[:80]})")
    for argv in argvs:
        found += problems(argv, cwd, env, live, has, 'unit')
    pid = int(props.get('MainPID') or 0)
    if pid > 0:
        info = proc_info(pid, proc)
        if info is None:
            found.append(f"process {pid}: cannot read /proc/{pid} (cmdline, cwd, environ)")
        else:
            found += problems(info[0], info[1], info[2], live, has, f"process {pid}")
    return dict(unit=unit, state=props.get('UnitFileState') or '', active=props.get('ActiveState') or '',
                pid=pid, ok=not found, problems=found)


def check(run=systemctl, live=None, has=has_pkg, proc=Path('/proc'), units=None):
    """The results for every swarm unit that is enabled or active. units: names to check
    instead of the swarm*.service list (each one is checked whatever its state).
    Raises when systemctl cannot be read."""
    live = _norm(live or live_path())
    out = []
    if not has(live):
        out.append(dict(unit='(flow-live)', state='', active='', pid=0, ok=False,
                        problems=[f"{live} does not hold a tangentswarm package"]))
    names = [(u, None) for u in units] if units else unit_files(run)
    for unit, state in names:
        if unit.endswith('@.service'):
            continue
        props = show(unit, run)
        if units is None:
            st = props.get('UnitFileState') or state or ''
            if st in SKIP_STATES and props.get('ActiveState') not in ('active', 'activating', 'reloading'):
                continue
        out.append(check_unit(unit, props, live, has, proc))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-unit-check', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--json', action='store_true', help='print the results as JSON')
    ap.add_argument('--live', help='the flow-live checkout (default: $SWARM_FLOW_LIVE or ~/ver/tangentswarm.flow-live)')
    ap.add_argument('units', nargs='*', help='check these units instead of every swarm*.service unit')
    a = ap.parse_args(argv)
    try:
        res = check(live=a.live, units=a.units or None)
    except Exception as e:
        print(f"swarm-unit-check: FAIL: cannot read the units: {e}", file=sys.stderr)
        return 2
    bad = [r for r in res if not r['ok']]
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        for r in res:
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['unit']} ({r['state'] or '-'}, {r['active'] or '-'})")
            for p in r['problems']:
                print(f"     {p}")
        if not res:
            print("no swarm units found")
    if bad:
        print(f"swarm-unit-check: FAIL: {len(bad)} unit(s) do not run from {_norm(a.live or live_path())}: "
              + ', '.join(r['unit'] for r in bad), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
