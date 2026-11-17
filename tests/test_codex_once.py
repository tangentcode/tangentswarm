"""One "@codex review" per PR head SHA (tangentswarm #63): every poster goes through
codexrounds.ask(), which is idempotent on (repo, PR, head) and retries only after a
"Something went wrong" reply or a timeout. No network: gh is a list that records calls."""
import json
import threading
import time

from tangentswarm import board, codexrounds as CR, eventdb, tick, webhook as W

from test_webhook import Fake, codex_pr

H = 'a' * 40
ASK = ['gh', 'pr', 'comment', '7', '-R', CR.REPO, '--body', '@codex review']
CODEX = {'login': 'chatgpt-codex-connector'}


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def full(head=H, comments=(), reviews=(), committed=None):
    pr = codex_pr(head)
    pr['comments'] = {'nodes': list(comments)}
    pr['reviews'] = {'nodes': list(reviews)}
    if committed is not None:
        pr['commits'] = {'nodes': [{'commit': {'committedDate': iso(committed)}}]}
    return pr


def ask(pr=None, now=None, **kw):
    calls = kw.pop('calls', [])
    res = CR.ask(7, H, 'test', pr or full(), sh=calls.append, now=now, **kw)
    return res, calls


def rows(event):
    return eventdb.store().rows("stage='event' AND event=?", (event,))


def test_the_first_ask_is_posted_and_logged_with_its_head_and_repo():
    res, calls = ask()
    assert res['posted'] and res['kind'] == 'ask' and res['round'] == 1 and calls == [ASK]
    (r,) = rows(CR.ROUND)
    assert json.loads(r['body'])['head'] == H and r['repo'] == CR.REPO and r['pr'] == 7
    assert tick.State(tick.STATE_DIR / 'state.json').done(CR.once_key(7, H))


def test_a_second_ask_for_the_same_head_is_skipped_while_pending():
    t0 = time.time()
    ask(now=t0)
    res, calls = ask(now=t0 + 60)
    assert not res['posted'] and calls == [] and 'still pending' in res['why']
    assert len(rows(CR.ROUND)) == 1 and rows(CR.RETRY) == []


def test_a_new_head_is_a_new_round():
    ask()
    res = CR.ask(7, 'b' * 40, 'test', full('b' * 40), sh=lambda a: None)
    assert res['posted'] and res['kind'] == 'ask' and res['round'] == 2


def test_skip_when_codex_reviewed_the_head():
    review = {'author': CODEX, 'commit': {'oid': H}, 'submittedAt': iso(time.time())}
    res, calls = ask(full(reviews=[review]))
    assert not res['posted'] and calls == [] and 'answered' in res['why']


def test_skip_when_a_codex_receipt_names_the_head():
    receipt = {'author': CODEX, 'body': f"Reviewed commit {H[:8]}. Didn't find any major issues.",
               'createdAt': iso(time.time())}
    res, calls = ask(full(comments=[receipt]))
    assert not res['posted'] and calls == []


def test_skip_when_a_codex_review_of_the_head_is_running():
    state = json.dumps({'code_review': {'commit_sha': H, 'status': 'running'}})
    c = {'author': CODEX, 'body': f'<!-- codex-review-state:v1 {state} -->', 'createdAt': iso(time.time())}
    res, calls = ask(full(comments=[c]))
    assert not res['posted'] and calls == [] and 'running' in res['why']


def test_a_person_ask_on_github_after_the_head_counts_as_pending():
    now = time.time()
    c = {'author': {'login': 'tangentstorm'}, 'body': '@codex review', 'createdAt': iso(now - 120)}
    res, calls = ask(full(comments=[c], committed=now - 600), now=now)
    assert not res['posted'] and calls == [] and 'still pending' in res['why']


def test_an_ask_before_the_head_commit_does_not_count():
    now = time.time()
    c = {'author': {'login': 'tangentstorm'}, 'body': '@codex review', 'createdAt': iso(now - 900)}
    res, _ = ask(full(comments=[c], committed=now - 600), now=now)
    assert res['posted'] and res['kind'] == 'ask'


def test_an_old_once_key_counts_as_an_ask():
    st = tick.State(tick.STATE_DIR / 'state.json')
    st.mark(CR.once_key(7, H))
    res, calls = ask(state=st)
    assert not res['posted'] and calls == []


def test_retry_after_something_went_wrong_is_logged_and_not_a_new_round():
    t0 = time.time() - 120
    ask(now=t0)
    err = {'author': CODEX, 'body': 'Something went wrong. Try again later.', 'createdAt': iso(t0 + 30)}
    res, calls = ask(full(comments=[err]), now=t0 + 60)
    assert res['posted'] and res['kind'] == 'retry' and calls == [ASK] and res['round'] == 1
    (r,) = rows(CR.RETRY)
    assert json.loads(r['body'])['head'] == H and 'Something went wrong' in json.loads(r['body'])['why']
    assert len(CR.rounds(7)) == 1
    # the retry is the latest ask: the same error reply does not trigger another one
    res, calls = ask(full(comments=[err]), now=t0 + 90)
    assert not res['posted'] and calls == []


def test_retry_after_the_timeout_only():
    t0 = time.time()
    ask(now=t0)
    assert not ask(now=t0 + CR.ASK_WAIT - 1)[0]['posted']
    res, calls = ask(now=t0 + CR.ASK_WAIT)
    assert res['posted'] and res['kind'] == 'retry' and calls == [ASK]
    assert not ask(now=t0 + CR.ASK_WAIT + 60)[0]['posted']


def test_a_dry_run_decides_but_posts_and_records_nothing():
    res, calls = ask(dry=True)
    assert res['kind'] == 'ask' and not res['posted'] and calls == [] and rows(CR.ROUND) == []


def test_retry_only_never_makes_a_first_ask():
    res, calls = ask(retry_only=True)
    assert not res['posted'] and calls == []


def test_a_stale_read_of_another_head_is_skipped():
    res, calls = ask(full('c' * 40))
    assert not res['posted'] and calls == []


def test_the_same_pr_number_in_two_repos_does_not_collide():
    ask()
    calls = []
    res = CR.ask(7, H, 'test', full(), repo='tangentcode/tangentswarm', sh=calls.append)
    assert res['posted'] and res['round'] == 1 and calls[0][5] == 'tangentcode/tangentswarm'
    assert CR.once_key(7, H, 'tangentcode/tangentswarm') == f"codex-request:tangentcode/tangentswarm#7@{H}"


def test_concurrent_posters_post_once():
    """Two asks in the same second (platform#441): the lock makes the second see the first."""
    calls, go = [], threading.Barrier(4)

    def slow(argv):
        time.sleep(0.2)
        calls.append(argv)

    def one():
        go.wait()
        CR.ask(7, H, 'test', full(), sh=slow)
    ts = [threading.Thread(target=one) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls == [ASK] and len(rows(CR.ROUND)) == 1


def test_the_webhook_handoff_skips_a_head_the_stop_hook_already_asked_for():
    CR.ask(9, H, 'stop-hook', full(), sh=lambda a: None)
    ex = Fake(col=board.AUTO_REVIEW, full=dict(codex_pr(), isDraft=False))
    ex.run(W.Action('handoff', 9, 'new head', dict(head=H, push=True)))
    assert not any('@codex review' in c for c in ex.calls)
    assert any('no Codex ask' in d and 'still pending' in d for d in ex.done)


def test_the_webhook_retries_on_a_something_went_wrong_reply():
    t0 = time.time() - 300
    CR.ask(9, H, 'stop-hook', full(), sh=lambda a: None, now=t0)
    err = {'author': CODEX, 'body': 'Something went wrong.', 'createdAt': iso(t0 + 60)}
    ex = Fake(col=board.AUTO_REVIEW, full=full(comments=[err]))
    ex.run(W.Action('codex', 9, 'Codex comment (summary or receipt)'))
    assert ['gh', 'pr', 'comment', '9', '-R', W.REPO, '--body', '@codex review'] in ex.calls
    assert len(rows(CR.RETRY)) == 1


def test_a_pending_codex_comment_with_no_earlier_ask_posts_nothing():
    ex = Fake(col=board.AUTO_REVIEW, full=full())
    ex.run(W.Action('codex', 9, 'Codex comment (summary or receipt)'))
    assert not any('@codex review' in c for c in ex.calls)
