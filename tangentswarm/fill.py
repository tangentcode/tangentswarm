"""The one fill routine for Ready, Staging and Human Review (tangentswarm #144).

Michal 2026-11-18 2:01 PM ET: "we should just have one copy of this logic that just scans it
from top to bottom and just pulls anything that needs to happen ... just fills the whole
thing." Every trigger (a merge, a reject, a close, any move out of Human Review, the
swarm-promoted poll and its SIGUSR1, the cascade, swarm-flow step, swarm-advance) asks
plan() what to do, and a pulled Ready PR asks landing() where to go. No trigger has its own
count or its own target.

From the top down:
  1. Human Review holds HR_TARGET (2). Fill it from Staging first (promote), in the pick order.
  2. A Human Review slot that no Staging PR can fill is filled straight from Ready: the next
     pulled Ready PR lands in Human Review, not in Staging (direct).
  3. Then Staging holds STAGING_TARGET (2), pulled from Ready.
  4. So the pulls in flight make up the whole gap: (HR_TARGET + STAGING_TARGET) minus what
     Human Review, Staging and the pulls in flight already hold (pull).
  5. Ready short of its target: the cascade's signal step starts new work (slots).
Only the TOP Ready PR is looked at (Michal 2026-11-18 2:09 PM ET: "we should just do the first
ticket. In ready."): it is the only one checked against main. After every merge into main it
is updated onto the new main (its -vN) and its tests run again there; when it merges cleanly
and they pass, it moves up. tangentswarm #146: when it conflicts or its tests fail, it stays in
Ready (label pre-merging) and its worker fixes it in place; it never goes to In Progress. The
fill WAITS on it: it never skips ahead to the second Ready PR (top()). No PR further down
Ready is checked against main or reworked until it reaches the top. When Human Review cannot
be kept at its target, the cascade sends a stall notice that names the blocked PR
(tangentswarm.stall).
The pick order is the shared one (board.pick_key): fast-track first, then milestone, then the
lowest PR number.
"""
import os
from collections import namedtuple

from . import board

HR_TARGET = int(os.environ.get('SWARM_HR_TARGET', '2'))
STAGING_TARGET = int(os.environ.get('SWARM_STAGING_FLOOR', os.environ.get('SWARM_STAGING_TARGET', '2')))

Plan = namedtuple('Plan', 'hr staging flying promote direct pull')


def _n(x):
    return x if isinstance(x, int) else len(x or [])


def plan(cols, flying=0, hr_target=None, staging_target=None):
    """What the board needs now, top down. cols: {column: [cards]}; flying: the pulls in
    flight (a count or a collection) that are not in Staging or Human Review yet.
      promote  Staging PRs to move into Human Review now
      direct   Human Review slots no Staging PR fills: that many next pulled PRs land there
      pull     Ready PRs to pull now (Human Review gap + Staging gap - pulls in flight)"""
    hr_t = HR_TARGET if hr_target is None else hr_target
    st_t = STAGING_TARGET if staging_target is None else staging_target
    hr, st, fly = _n(cols.get(board.HUMAN_REVIEW)), _n(cols.get(board.STAGING)), _n(flying)
    promote = min(max(0, hr_t - hr), st)
    direct = max(0, hr_t - hr - promote)
    pull = max(0, hr_t + st_t - (hr + promote) - (st - promote) - fly)
    return Plan(hr, st, fly, promote, direct, pull)


def top(cols, flying=()):
    """The one Ready PR the fill looks at: the first not-draft Ready PR not in a pull, in the
    shared pick order (fast-track, milestone, lowest number), or None."""
    from . import flow
    skip = set(flying or ())
    ready = [p for p in cols.get(board.READY, []) if not p.get('draft') and p['number'] not in skip]
    return flow.ready_order(ready)[0] if ready else None


def landing(cols, hr_target=None):
    """The column a pulled Ready PR goes into when it is ready to move: Human Review when it
    has a slot that no Staging PR fills (Staging PRs are ahead of it), else Staging."""
    hr_t = HR_TARGET if hr_target is None else hr_target
    hr, st = _n(cols.get(board.HUMAN_REVIEW)), _n(cols.get(board.STAGING))
    return board.HUMAN_REVIEW if hr + st < hr_t else board.STAGING


def columns_of(prs, moved=None):
    """{column: [cards]} of the open, not parked cards in prs ({n: card}), with this pass's
    own moves ({n: column}) applied (the board read does not show them yet)."""
    cols = {}
    for n, p in (prs or {}).items():
        if p.get('state', 'OPEN') != 'OPEN' or p.get('parked'):
            continue
        col = (moved or {}).get(n, p.get('col'))
        cols.setdefault(col, []).append(p)
    return cols


def text(pl, hr_target=None, staging_target=None):
    """One line for the logs."""
    hr_t = HR_TARGET if hr_target is None else hr_target
    st_t = STAGING_TARGET if staging_target is None else staging_target
    fl = f" + {pl.flying} in flight" if pl.flying else ''
    return (f"Human Review {pl.hr}/{hr_t}, Staging {pl.staging}/{st_t}{fl}: promote {pl.promote}, "
            f"straight to Human Review {pl.direct}, pull {pl.pull}")
