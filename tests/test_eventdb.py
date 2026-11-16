"""The swarm-webhook event DB (Michal 2026-11-15 1:54 PM ET): append-only, one 'received'
row for every inbound request before the reply, a 'handled' row after."""
import hashlib
import hmac
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from tangentswarm import board, eventdb, webhook as W
from test_webhook import Fake, REPO, pr


def test_the_table_is_append_only(tmp_path):
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    st.add('handled', 'd1', 'pull_request')
    db = sqlite3.connect(tmp_path / 'e.sqlite')
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    for sql in ('UPDATE events SET error=1', 'DELETE FROM events'):
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            db.execute(sql)
    assert st.count() == 1


def test_a_redelivery_gets_its_own_row_marked_duplicate(tmp_path):
    st = eventdb.EventStore(':memory:')
    p = {'action': 'opened', 'repository': REPO, 'pull_request': pr(5), 'sender': {'login': 'tangentstorm'}}
    assert st.add_received('d1', 'pull_request', p, {'X-GitHub-Event': 'pull_request', 'Cookie': 'x'}, b'{}', True,
                           decisions=['card'])[1] is False
    assert st.add_received('d1', 'pull_request', p, {}, b'{}', True, decisions=['card'])[1] is True
    a, b = st.rows()
    assert (a['repo'], a['pr'], a['sender'], a['lane'], a['sig_ok']) == ('tangentcode/platform', 5, 'tangentstorm',
                                                                       'tangentcode/platform#5', 1)
    assert json.loads(a['done']) == ['queued'] and json.loads(a['decisions']) == ['card']
    assert 'Cookie' not in json.loads(a['headers'])
    assert b['reason'] == 'duplicate' and json.loads(b['done']) == ['dropped as duplicate']
    assert st.body_of('d1') == ('pull_request', {})


def test_tags_name_what_was_done():
    assert eventdb.tags(['#7 -> Ready', '#7 relayed to its worker (sent)', 'woke swarmbot: x',
                         'queued the MinBot check of #7', 'cascade queued', 'skip: ping']) == [
        'moved card to Ready', 'relayed to Claude', 'woke swarmbot', 'queued MinBot check', 'queued cascade', 'skipped']


def serve():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', 0), W.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_port}/github'


def test_every_request_has_a_row_before_the_reply_and_log_only_events_do_nothing(tmp_path, monkeypatch):
    (tmp_path / 'secret').write_text('s3cret\n')
    monkeypatch.setattr(W, 'SECRET_FILES', [tmp_path / 'secret'])
    srv, url = serve()

    def post(event, payload, delivery, sig=None):
        body = json.dumps(payload).encode()
        sig = sig or 'sha256=' + hmac.new(b's3cret', body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, method='POST', headers={
            'X-GitHub-Event': event, 'X-GitHub-Delivery': delivery, 'X-Hub-Signature-256': sig})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        # the row exists when the reply arrives
        assert any(r['delivery'] == delivery for r in eventdb.store().rows()), delivery
        return code
    opened = {'action': 'opened', 'repository': REPO, 'pull_request': pr(9, draft=True)}
    try:
        assert post('pull_request', opened, 'a1', sig='sha256=bad') == 401
        assert post('pull_request', opened, 'a2') == 200
        assert post('pull_request', opened, 'a2') == 200
        assert post('sponsorship', {'action': 'created'}, 'u1') == 200  # an unknown type: log-only
        for i, ev in enumerate(W.LOG_ONLY):
            assert post(ev, {'action': 'created', 'repository': REPO}, f'l{i}') == 200
    finally:
        srv.shutdown()
    assert len(list(W.INBOX.glob('*.json'))) == 1  # only a2, once
    W.drain(lambda: Fake())
    rows = eventdb.store().rows()
    by = {(r['delivery'], r['stage'], r['reason']): r for r in rows}
    assert json.loads(by[('a1', 'received', 'bad signature')]['done']) == ['rejected bad signature']
    assert by[('a1', 'received', 'bad signature')]['sig_ok'] == 0 and by[('a1', 'received', 'bad signature')]['pr'] is None
    assert json.loads(by[('a2', 'received', 'duplicate')]['done']) == ['dropped as duplicate']
    handled = by[('a2', 'handled', None)]
    assert json.loads(handled['decisions']) == ['card'] and handled['pr'] == 9
    for i, ev in enumerate(W.LOG_ONLY):
        r = by[(f'l{i}', 'received', 'log-only')]
        assert r['event'] == ev and json.loads(r['decisions']) == ['log-only'] and json.loads(r['done']) == []
    assert json.loads(by[('u1', 'received', 'log-only')]['decisions']) == ['log-only']
    assert not by[('u1', 'received', 'log-only')]['error']
    assert len(rows) == 3 + len(W.LOG_ONLY) + 2
    assert set(W.LOG_ONLY) == {'push', 'commit_comment', 'discussion', 'discussion_comment',
                               'pull_request_review_thread', 'pull_request_review_comment'}
    assert all(a.kind == 'log-only' for ev in W.LOG_ONLY for a in W.classify(ev, {'repository': REPO}))


def test_a_handler_error_is_in_the_handled_row():
    class Boom(Fake):
        def do_card(self, a):
            raise RuntimeError('gh down')
    W.handle('pull_request', {'action': 'opened', 'repository': REPO, 'pull_request': pr(4)}, 'e1', Boom())
    r = eventdb.store().rows("stage='handled'")[0]
    assert 'gh down' in r['error'] and 'error' in json.loads(r['done'])


def test_the_wake_post_retries_with_backoff_and_stops_on_a_permanent_4xx(tmp_path):
    (tmp_path / 'u').write_text('http://wake.invalid/x')
    codes, waits = [503, 429, 200], []

    def urlopen(req, timeout=None):
        c = codes.pop(0)
        if c >= 400:
            raise urllib.error.HTTPError(req.full_url, c, 'x', {'Retry-After': '30'} if c == 429 else {}, None)
        class R:
            status = c
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    W.post_wake({'why': 'x'}, url_file=tmp_path / 'u', urlopen=urlopen, sleep=waits.append, background=False)
    assert waits == [10, 30] and codes == []
    codes[:] = [403, 200]
    W.post_wake({'why': 'y'}, url_file=tmp_path / 'u', urlopen=urlopen, sleep=waits.append, background=False)
    assert codes == [200]
    assert eventdb.backoff(1) == 10 and eventdb.backoff(4) == 80 and eventdb.backoff(20) == 900


def test_a_received_but_unqueued_delivery_is_recovered_and_its_redelivery_queued(tmp_path, monkeypatch):
    """Codex on PR #26: a crash between the receipt row and the inbox write lost the event,
    and GitHub's redelivery was dropped as a duplicate."""
    import time as _t
    (tmp_path / 'secret').write_text('s3cret\n')
    monkeypatch.setattr(W, 'SECRET_FILES', [tmp_path / 'secret'])
    p = {'action': 'opened', 'repository': REPO, 'pull_request': pr(9, draft=True)}
    st = eventdb.store()
    st.add_received('lost-1', 'pull_request', p, {}, json.dumps(p).encode(), True, decisions=['card'],
                    now=_t.time() - 120)
    assert W.recover_unqueued() == ['lost-1'] and W.in_inbox('lost-1')
    assert W.recover_unqueued() == []  # already in the inbox
    W.drain(lambda: Fake())
    assert st.handled('lost-1') and W.recover_unqueued() == []
    # a redelivery of a received-but-unqueued delivery is queued, not dropped
    st.add_received('lost-2', 'pull_request', p, {}, json.dumps(p).encode(), True, decisions=['card'])
    srv, url = serve()
    body = json.dumps(p).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'X-GitHub-Event': 'pull_request', 'X-GitHub-Delivery': 'lost-2',
        'X-Hub-Signature-256': 'sha256=' + hmac.new(b's3cret', body, hashlib.sha256).hexdigest()})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.read() == b'queued\n'
    finally:
        srv.shutdown()
    assert W.in_inbox('lost-2')
