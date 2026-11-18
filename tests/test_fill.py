"""tangentswarm #144: one fill routine for Human Review, Staging and Ready, top down, the same
for every trigger. No network: the board and the gate are fakes."""
import pytest

from tangentswarm import board, fill, flow, promoted as P, webhook as W

from test_advance import CascadeGate, cascade, pr
from test_refill import G, Scratch, make, pull, ready_card

HR, ST, RD, IP = board.HUMAN_REVIEW, board.STAGING, board.READY, board.IN_PROGRESS


def cols(hr=0, st=0, rd=0):
    return {HR: [pr(100 + i, HR) for i in range(hr)], ST: [pr(200 + i, ST) for i in range(st)],
            RD: [pr(300 + i, RD) for i in range(rd)]}


def item_move(frm, to):
    return {'action': 'edited', 'sender': {'login': W.MICHAL},
            'projects_v2_item': {'content_node_id': 'PR_x', 'project_node_id': board.PROJECT_ID},
            'changes': {'field_value': {'field_node_id': board.STATUS_FIELD, 'from': {'id': board.OPTION[frm]},
                                        'to': {'id': board.OPTION[to]}}}}


@pytest.mark.parametrize('hr,st,flying,want', [
    (0, 0, 0, (0, 2, 4)),   # both empty: Ready goes straight to Human Review, then Staging
    (0, 2, 0, (2, 0, 2)),   # Staging full, Human Review empty: promote both, refill Staging
    (1, 0, 0, (0, 1, 3)),   # a reject or merge left one slot and Staging is empty
    (1, 2, 0, (1, 0, 1)),   # a merge with Staging full: promote one, pull one
    (2, 1, 0, (0, 0, 1)),   # a hand move Staging -> Human Review: Staging is short by one
    (2, 2, 0, (0, 0, 0)),   # full
    (0, 0, 3, (0, 2, 1)),   # pulls in flight count
    (3, 6, 0, (0, 0, 0)),   # over the targets: never negative
])
def test_the_plan_fills_top_down(hr, st, flying, want):
    pl = fill.plan(cols(hr, st), flying, hr_target=2, staging_target=2)
    assert (pl.promote, pl.direct, pl.pull) == want


def test_landing_is_human_review_only_while_staging_cannot_fill_it():
    assert fill.landing(cols(0, 0), hr_target=2) == HR
    assert fill.landing(cols(1, 0), hr_target=2) == HR
    assert fill.landing(cols(0, 1), hr_target=2) == HR    # one Staging PR fills one slot, one is left
    assert fill.landing(cols(0, 2), hr_target=2) == ST    # Staging is ahead of it
    assert fill.landing(cols(2, 0), hr_target=2) == ST


# -- Human Review and Staging both empty -----------------------------------------------------------

def test_both_empty_the_pulled_ready_prs_go_straight_to_human_review_then_staging(monkeypatch):
    """Michal 2026-11-18 1:59 PM ET: with Human Review and Staging both empty, a Ready PR went into
    Staging and waited there. Now the first two land in Human Review and the third in Staging."""
    monkeypatch.setattr(flow, 'HR_TARGET', 2)
    g = G(files=('docs/x.md',), body='Where to see: docs only')
    f = make(g)
    prs = {n: ready_card(n) for n in (7, 8, 9)}
    for n in (7, 8, 9):
        f.advance(pull(n), prs, scratch=Scratch())
    assert g.moves == [('I7', HR), ('I8', HR), ('I9', ST)] and flow.load_pulls() == []
    assert [a[1] for a in g.logged] == [7, 8, 9]  # each one gets its gate pass line
    assert f.did[0] == 'did: #7 Ready -> Human Review (pre-merged tip in work; no gate re-run)'
    assert f.did[2] == 'did: #9 Ready -> Staging (pre-merged tip in work; no gate re-run)'


def test_both_empty_the_cascade_pulls_for_human_review_and_staging(monkeypatch, tmp_path):
    out, pulled = cascade(monkeypatch, tmp_path, {HR: [], RD: [pr(n, RD) for n in (4, 5, 6, 7, 8)]}, CascadeGate())
    assert pulled == [4, 5, 6, 7]  # the pick order: the two Human Review slots and the two Staging slots
    assert any(l.endswith('2 go straight to Human Review') for l in out)
    assert 'would run hr-gate --apply 4 (Ready -> Human Review when it passes)' in out
    assert 'would run hr-gate --apply 6 (Ready -> Staging when it passes)' in out


def test_both_empty_the_daemon_queues_the_fill_and_promotes_nothing():
    p = P.Promoter.__new__(P.Promoter)
    p.last_hr, p.cascade_at, p.last_main = 1, 0.0, None
    assert not P.needs_promotion(cols(0, 0))
    assert p.cascade_due(cols(0, 0), False, now=1) == 'Human Review dropped to 0 (was 1)'


# -- Staging full but Human Review empty ------------------------------------------------------------

def test_staging_full_and_human_review_empty_promotes_and_lands_the_next_pull_in_staging(monkeypatch, tmp_path):
    c = cols(0, 2, 3)
    assert P.needs_promotion(c) and fill.plan(c, hr_target=2, staging_target=2).promote == 2
    assert fill.landing(c, hr_target=2) == ST
    p = P.Promoter.__new__(P.Promoter)
    p.last_hr, p.cascade_at, p.last_main = 2, 0.0, None
    assert p.cascade_due(c, False, now=1) is None  # the promote fills Human Review; it queues the restock


# -- a reject ---------------------------------------------------------------------------------------

def test_a_reject_runs_the_fill_first_and_an_empty_staging_sends_the_next_ready_pr_to_human_review():
    assert [a.kind for a in W.classify('projects_v2_item', item_move(HR, IP))] == ['cascade', 'reject']
    c = cols(1, 0, 2)
    p = P.Promoter.__new__(P.Promoter)
    p.last_hr, p.cascade_at, p.last_main = 2, 0.0, None
    assert p.cascade_due(c, False, now=1) == 'Human Review dropped to 1 (was 2) and Staging cannot fill it'
    assert fill.landing(c, hr_target=2) == HR


# -- a merge ----------------------------------------------------------------------------------------

def test_a_merge_promotes_from_staging_and_pulls_one_for_staging(monkeypatch):
    monkeypatch.setattr(P, 'PULL', True)
    c = cols(1, 2, 3)  # the merged PR left Human Review
    pl = fill.plan(c, hr_target=2, staging_target=2)
    assert P.needs_promotion(c) and (pl.promote, pl.direct, pl.pull) == (1, 0, 1)
    assert P.needs_pull(c, None)


def test_a_merge_with_staging_empty_lands_the_pulled_pr_in_human_review(monkeypatch):
    monkeypatch.setattr(flow, 'HR_TARGET', 2)
    g = G(files=('docs/x.md',), body='Where to see: docs only')
    prs = {1: dict(ready_card(1), col=HR), 7: ready_card(7)}  # #2 merged: one Human Review slot is free
    make(g).advance(pull(7), prs, scratch=Scratch())
    assert g.moves == [('I7', HR)]


# -- a direct move from Staging to Human Review ----------------------------------------------------

def test_a_hand_move_from_staging_to_human_review_refills_staging():
    assert [a.kind for a in W.classify('projects_v2_item', item_move(ST, HR))] == ['cascade']
    c = cols(2, 1, 2)
    pl = fill.plan(c, hr_target=2, staging_target=2)
    assert (pl.promote, pl.direct, pl.pull) == (0, 0, 1)
    assert fill.landing(c, hr_target=2) == ST


def test_the_cascade_pulls_one_after_a_hand_move_from_staging(monkeypatch, tmp_path):
    c = {HR: [pr(1, HR), pr(2, HR)], ST: [pr(3, ST)], RD: [pr(n, RD) for n in (8, 9)]}
    out, pulled = cascade(monkeypatch, tmp_path, c, CascadeGate())
    assert pulled == [8] and 'would run hr-gate --apply 8 (Ready -> Staging when it passes)' in out


# -- only the top Ready PR (Michal 2026-11-18 2:09 PM ET) ------------------------------------------

def test_the_top_is_the_first_ready_pr_in_the_pick_order_not_in_a_pull():
    c = {RD: [pr(5, RD), pr(4, RD, draft=True), pr(3, RD), pr(9, RD, rank=board.FAST_TRACK)]}
    assert fill.top(c)['number'] == 9
    assert fill.top(c, {9})['number'] == 3
    assert fill.top({RD: []}) is None


def test_a_top_conflict_fixes_only_the_top_in_place_and_the_fill_waits(monkeypatch, tmp_path):
    c = {HR: [], RD: [pr(n, RD) for n in (4, 5, 6)]}
    out, pulled = cascade(monkeypatch, tmp_path, c, CascadeGate(), main_dirty={4, 5})
    assert pulled == []
    assert 'NEEDS_FIX #4 f4.ts' in out and not any(('#5' in l or '#6' in l) and 'STALL' not in l for l in out)
    assert any(l.startswith('fill: #4 (the top Ready PR) is fixed in place (pre-merging): the fill waits')
               for l in out)
