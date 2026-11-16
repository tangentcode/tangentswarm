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
        from . import advance as A
        try:  # the whole MinBot gate again, on a fresh read: anything may have changed since the pass
            why = gate_problem(A.codex_details(adv.g['gql'], p['number'], p['repo']), p['head'], p['repo'])
        except Exception as e:
            why = f"not readable now: {type(e).__name__}"
        if why:
            _say(adv, f"{p['repo']}#{p['number']} stays in Ready: {why}")
            continue
        if adv.dry:
            _say(adv, f"would move {p['repo']}#{p['number']} Ready -> Human Review (board {b.number})")
        else:
            board.move_card(adv.g, p['item'], board.HUMAN_REVIEW, n=p['number'], b=b)
            _say(adv, f"{p['repo']}#{p['number']} Ready -> Human Review (board {b.number}, {hr + 1}/{HR_TARGET})")
        moved.append(p['number'])
        hr += 1
    if not moved:
        _say(adv, f"board {b.number}: Human Review {hr}/{HR_TARGET}, Ready {len(ready)}: nothing to promote")
    return moved


def gate_problem(d, head, repo):
    """Why PR d (advance.codex_details) does not pass the MinBot gate at this head, or None.
    The ONE gate of a board without staging: the MinBot check runs it to reach Ready, and
    promote() runs it again on a fresh read before Human Review (Codex on PR #47 found, one
    round at a time, each condition promote() did not repeat)."""
    from . import advance as A
    now = d.get('headRefOid') or ''
    if now != head:
        return f"head moved to {now[:8]}"
    if d.get('baseRefName') != 'main':  # mergeable is against the PR's own base
        return f"targets {d.get('baseRefName')}, not main"
    if d.get('isDraft'):
        return 'is a draft'
    q = A.open_question(d)
    if q:
        return q
    if d.get('mergeable') != 'MERGEABLE':  # UNKNOWN is not a pass
        return ('conflicts with main (GitHub mergeable)' if d.get('mergeable') == 'CONFLICTING'
                else f"GitHub has not confirmed it merges cleanly yet (mergeable {d.get('mergeable')})")
    if not A.codex_clean(d):
        return f"not Codex-clean on {head[:8]}"
    return A.ci_problem(A.check_runs(head, repo))


def minbot_repo(adv, n, repo, promote_after=True):
    """The MinBot check of PR n in repo. Returns True when it passed (card to Ready); then
    promote() runs, unless the caller promotes once after a batch (promote_after False)."""
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
    why = gate_problem(d, card['head'], repo)
    if why:
        _say(adv, f"{repo}#{n} stays in Auto Review: {why}")
        return False
    if adv.dry:
        _say(adv, f"would MinBot-pass {repo}#{n} at {head[:8]}: pass comment, Auto Review -> Ready")
        return True
    mark = A.pass_text(head).split(' (')[0]
    if not any(mark in (c.get('body') or '') for c in (d.get('comments') or {}).get('nodes') or []):
        adv.minbot('evidence', n, '--repo', repo, '--title', 'MinBot pass', '--commit', head, '--body', A.pass_text(head))
    board.move_card(adv.g, card['item'], board.READY, n=n, b=b)
    _say(adv, f"MinBot-pass {repo}#{n} at {head[:8]}: pass comment, Auto Review -> Ready")
    if promote_after:
        promote(adv, b)
    return True


def board_pass(adv, b):
    """The periodic pass of a board without staging (swarm-promoted queues it every
    BOARD_EVERY s): the MinBot check of each Auto Review PR, then promote. It retries what an
    event could not finish, such as GitHub's mergeable still UNKNOWN (Codex on PR #47)."""
    for p in boards.open_prs(adv.g['gql'], b).get(board.AUTO_REVIEW, []):
        if not p['draft']:  # Codex on PR #47: check them all first, then promote in pick order
            minbot_repo(adv, p['number'], p['repo'], promote_after=False)
    return promote(adv, b)


def main(a, adv):
    if a.promote_board:
        b = next((x for x in board.BOARDS if x.number == a.promote_board), None)
        if b is None:
            _say(adv, f"board {a.promote_board} is not configured")
            return 2
        if getattr(a, 'board_pass', False):
            board_pass(adv, b)
        else:
            promote(adv, b)
        return 0
    minbot_repo(adv, a.minbot, a.repo)
    return 0
