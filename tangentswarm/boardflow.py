"""The same flow on a board without a staging site (tangentswarm #45, Michal 2026-11-15
7:20 PM ET: Project 6 with the tangentswarm and methodology repos, no special flow).

  minbot_repo   the MinBot check of one Auto Review PR of such a repo: not a draft, merges
                cleanly (GitHub mergeable), Codex clean on the exact head, CI green on the
                head, no open question for Michal. A pass posts the MinBot pass comment and
                moves the card to Ready, then promote() runs.
  promote       Ready -> Human Review while Human Review holds fewer than HR_TARGET, in the
                shared pick order (board.pick_key). There is no Staging step: the repo has no
                staging site, and MethBot takes PRs out of Human Review and merges them.

Run as `swarm-advance --minbot N --repo OWNER/NAME` and `swarm-advance --promote-board N`
(queued by swarm-webhook), under the serial lock.
"""
from . import board, boards

HR_TARGET = 2


def _say(adv, line):
    adv.out(line)


def promote(adv, b):
    """Move Ready PRs to Human Review in pick order while it holds fewer than HR_TARGET.
    Returns the moved PR numbers."""
    if b.staging:
        _say(adv, f"board {b.number} has a staging site: its promote is the cascade's")
        return []
    cols = boards.open_prs(adv.g['gql'], b)
    hr = len(cols.get(board.HUMAN_REVIEW, []))
    # Codex on PR #47: the active fast-track ticket is a Project 4 number; on this board a
    # PR is fast-track by its own labels (or its issues'), never by a matching number
    ready = sorted((p for p in cols.get(board.READY, []) if not p['draft']),
                   key=lambda p: board.pick_key(p, None))
    moved = []
    for p in ready:
        if hr >= HR_TARGET:
            break
        if p.get('mergeable') != 'MERGEABLE':  # Codex on PR #47: a merge since its pass may conflict now
            _say(adv, f"{p['repo']}#{p['number']} stays in Ready: GitHub mergeable is {p.get('mergeable')} "
                      "(a merge of main is needed, or GitHub has not computed it yet)")
            continue
        if adv.dry:
            _say(adv, f"would move {p['repo']}#{p['number']} Ready -> Human Review (board {b.number})")
        else:
            board.move_card(adv.g, p['item'], board.HUMAN_REVIEW, b=b)
            _say(adv, f"{p['repo']}#{p['number']} Ready -> Human Review (board {b.number}, {hr + 1}/{HR_TARGET})")
        moved.append(p['number'])
        hr += 1
    if not moved:
        _say(adv, f"board {b.number}: Human Review {hr}/{HR_TARGET}, Ready {len(ready)}: nothing to promote")
    return moved


def minbot_repo(adv, n, repo):
    """The MinBot check of PR n in repo. Returns True when it passed (card to Ready)."""
    from . import advance as A
    b = board.for_repo(repo)
    if b is None:
        _say(adv, f"{repo} is not on a swarm board: no MinBot check")
        return False
    card = next((p for p in boards.open_prs(adv.g['gql'], b).get(board.AUTO_REVIEW, []) if p['number'] == n
                 and p['repo'] == repo), None)
    if card is None:
        _say(adv, f"{repo}#{n} is not in Auto Review: no MinBot check")
        return False
    d = A.codex_details(adv.g['gql'], n, repo)
    head = d.get('headRefOid') or ''
    why = None
    if head != card['head']:
        why = f"head moved to {head[:8]}"
    elif d.get('isDraft'):
        why = 'is a draft'
    elif A.open_question(d):
        why = A.open_question(d)
    elif d.get('mergeable') != 'MERGEABLE':  # Codex on PR #47: UNKNOWN is not a pass
        why = ('conflicts with main (GitHub mergeable)' if d.get('mergeable') == 'CONFLICTING'
               else f"GitHub has not confirmed it merges cleanly yet (mergeable {d.get('mergeable')})")
    elif not A.codex_clean(d):
        why = f"not Codex-clean on {head[:8]}"
    else:
        why = A.ci_problem(A.check_runs(head, repo))
    if why:
        _say(adv, f"{repo}#{n} stays in Auto Review: {why}")
        return False
    if adv.dry:
        _say(adv, f"would MinBot-pass {repo}#{n} at {head[:8]}: pass comment, Auto Review -> Ready")
        return True
    mark = A.pass_text(head).split(' (')[0]
    if not any(mark in (c.get('body') or '') for c in (d.get('comments') or {}).get('nodes') or []):
        adv.minbot('evidence', n, '--repo', repo, '--title', 'MinBot pass', '--commit', head, '--body', A.pass_text(head))
    board.move_card(adv.g, card['item'], board.READY, b=b)
    _say(adv, f"MinBot-pass {repo}#{n} at {head[:8]}: pass comment, Auto Review -> Ready")
    promote(adv, b)
    return True


def main(a, adv):
    if a.promote_board:
        b = next((x for x in board.BOARDS if x.number == a.promote_board), None)
        if b is None:
            _say(adv, f"board {a.promote_board} is not configured")
            return 2
        promote(adv, b)
        return 0
    minbot_repo(adv, a.minbot, a.repo)
    return 0
