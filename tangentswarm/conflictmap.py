"""The conflict map: git merge-tree answers kept per commit SHA (Michal 2026-11-14 7:16 PM ET).

Whether two commits merge cleanly depends only on the two commits, so the answer for a SHA
pair never changes. Each Staging pull checked every Ready PR against main, work and every
Human Review and Staging head with a fresh git merge-tree, which made one cascade run take
30 s to 75 s. Now each answer is kept:

  pair   'p:<sha>:<sha>' (sorted)       True = merges cleanly
  stack  's:<hash of main, bases, head>' '' = stacks cleanly, else the base it conflicts with
  files  'f:<sha>:<sha>' (sorted)       the paths that conflict (for a rework relay)

Only full 40-hex SHAs are keys: a ref such as refs/heads/main can move, so it is never
cached. The file keeps the newest MAX entries.

    python -m tangentswarm.conflictmap SHA     the SHAs this commit is known to conflict with
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

PATH = Path(os.environ.get('SWARM_CONFLICT_MAP', str(Path.home() / '.cache' / 'swarm-conflict-map.json')))
MAX = int(os.environ.get('SWARM_CONFLICT_MAP_MAX', '20000'))
SHA = re.compile(r'^[0-9a-f]{40}$')


class ConflictMap:
    def __init__(self, path=None, max_entries=None):
        self.path = path or PATH
        self.max = MAX if max_entries is None else max_entries
        self.hits = self.misses = 0
        self.dirty = False
        try:
            d = json.loads(self.path.read_text())
            self.data = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def _get(self, k, fn, enc=lambda v: v, dec=lambda v: v):
        if k in self.data:
            self.hits += 1
            v = self.data.pop(k)
            self.data[k] = v  # newest last: trimming drops the oldest
            return dec(v)
        self.misses += 1
        v = fn()
        self.data[k] = enc(v)
        self.dirty = True
        return v

    def clean(self, a, b, fn):
        """fn() -> bool: does a merge cleanly with b? Kept per SHA pair."""
        if not (SHA.match(a or '') and SHA.match(b or '')):
            return fn()
        return self._get('p:' + ':'.join(sorted((a, b))), lambda: bool(fn()))

    def stack(self, main, bases, head, fn):
        """fn() -> None or the base head conflicts with, for head stacked on main + bases."""
        shas = [main, head] + [s for _, s in bases]
        if not all(SHA.match(s or '') for s in shas):
            return fn()
        k = 's:' + hashlib.sha1(json.dumps([main, [list(b) for b in bases], head]).encode()).hexdigest()
        return self._get(k, fn, enc=lambda v: v or '', dec=lambda v: v or None)

    def files(self, a, b, fn):
        """fn() -> [path]: the files that conflict when a and b merge. Kept per SHA pair."""
        if not (SHA.match(a or '') and SHA.match(b or '')):
            return fn()
        return self._get('f:' + ':'.join(sorted((a, b))), lambda: list(fn()))

    def conflicts_of(self, sha):
        """SHAs known to conflict with sha (pair answers only)."""
        out = []
        for k, v in self.data.items():
            if k.startswith('p:') and v is False:
                a, b = k[2:].split(':')
                if sha in (a, b):
                    out.append(b if a == sha else a)
        return out

    def save(self):
        """Write the map if it changed. Never raises: the cache must not break a pull."""
        if not self.dirty:
            return
        try:
            while len(self.data) > self.max:
                del self.data[next(iter(self.data))]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    m = ConflictMap()
    for sha in argv:
        print(f"{sha[:8]}: " + (', '.join(s[:8] for s in m.conflicts_of(sha)) or 'no known conflicts'))
    if not argv:
        pairs = sum(k.startswith('p:') for k in m.data)
        print(f"{pairs} pair(s), {len(m.data) - pairs} stack answer(s) in {m.path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
