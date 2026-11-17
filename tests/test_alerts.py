"""Alerts to Michal, swarmbot wakes and incident tickets (tangentswarm #40)."""
from tangentswarm import alerts, eventdb, webhook


HEAD = 'a' * 40


def test_an_alert_wakes_swarmbot_once_per_key_and_logs_an_alert_row():
    sent = []
    assert alerts.alert('Codex outage', key='k1', post=sent.append)
    assert not alerts.alert('Codex outage', key='k1', post=sent.append)
    assert len(sent) == 1 and sent[0]['why'] == 'Codex outage' and sent[0]['source'] == 'swarm-watch'
    rows = eventdb.store().rows("source='watch'")
    assert len(rows) == 1 and rows[0]['event'] == 'alert' and 'Codex outage' in rows[0]['notes']


def test_an_alert_never_posts_to_the_methbot_url(monkeypatch):
    """tangentswarm #19: a Codex outage, a stalled promote or a Needs Input card wakes
    swarmbot, never MethBot."""
    seen = []
    monkeypatch.setattr(webhook, 'post_wake', lambda p, url_file=None, key_file=None, **k: seen.append(url_file))
    alerts.alert('x', key='k2')
    assert seen == [None]  # post_wake's default: swarmbot's WAKE_URL_FILE
    assert alerts.ALERT_URL_FILE not in seen


def test_methbot_done_posts_the_spec_payload_once_per_pr_and_head():
    sent = []
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=sent.append,
                               now=lambda: 1_763_400_000.0) is True
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=sent.append) == 'already sent'
    assert sent == [{'source': 'swarm', 'kind': 'done', 'repo': 'tangentcode/tangentswarm', 'pr': 71,
                     'head': HEAD, 'at': '2025-11-17T12:20:00-05:00'}]
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head='b' * 40, post=sent.append) is True
    assert alerts.tell_methbot('done', 'tangentcode/methodology', 71, head=HEAD, post=sent.append) is True
    assert len(sent) == 3
    rows = eventdb.store().rows("source='watch'")
    assert [r['event'] for r in rows] == ['methbot-done'] * 3 and rows[0]['repo'] == 'tangentcode/tangentswarm'


def test_methbot_needs_input_posts_the_question():
    sent = []
    assert alerts.tell_methbot('needs-input', 'tangentcode/methodology', 9, question=' Which flag? ',
                               post=sent.append, now=lambda: 1_763_400_000.0) is True
    assert alerts.tell_methbot('needs-input', 'tangentcode/methodology', 9, question='Which flag?',
                               post=sent.append) == 'already sent'
    assert sent == [{'source': 'swarm', 'kind': 'needs-input', 'repo': 'tangentcode/methodology', 'pr': 9,
                     'question': 'Which flag?', 'at': '2025-11-17T12:20:00-05:00'}]
    assert alerts.tell_methbot('needs-input', 'tangentcode/methodology', 9, question='Another?',
                               post=sent.append) is True


def test_methbot_is_only_for_project_6_and_its_two_events():
    sent = []
    assert 'not a Project 6 repo' in alerts.tell_methbot('done', 'tangentcode/platform', 1, head=HEAD,
                                                         post=sent.append)
    assert 'not a Project 6 repo' in alerts.tell_methbot('needs-input', 'tangentcode/platform', 1, question='q?',
                                                         post=sent.append)
    assert 'not a MethBot event' in alerts.tell_methbot('blocked', 'tangentcode/tangentswarm', 1, post=sent.append)
    assert 'full head sha' in alerts.tell_methbot('done', 'tangentcode/tangentswarm', 1, head='abc',
                                                  post=sent.append)
    assert 'question' in alerts.tell_methbot('needs-input', 'tangentcode/tangentswarm', 1, question=' ',
                                             post=sent.append)
    assert sent == [] and eventdb.store().rows("source='watch'") == []


def test_methbot_goes_to_the_methbot_url_with_its_key_synchronously(monkeypatch):
    """Codex on PR #71: the spool serve process exits after the tool answer, so the POST
    must not run in a daemon thread."""
    seen = []
    monkeypatch.setattr(webhook, 'post_wake', lambda p, url_file=None, key_file=None, background=True:
                        seen.append((url_file, key_file, background)) or True)
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD) is True
    assert seen == [(alerts.ALERT_URL_FILE, alerts.ALERT_KEY_FILE, False)]


def test_a_failed_methbot_post_is_not_marked_sent_and_a_repeat_tries_again():
    """Codex on PR #71: the once-key was marked before the POST, so a lost POST was never sent."""
    tries = []
    r = alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=lambda p: tries.append(p) is not None)
    assert 'failed' in r
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=lambda p: tries.append(p) or True) is True
    assert alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=tries.append) == 'already sent'
    assert len(tries) == 2
    q = alerts.tell_methbot('needs-input', 'tangentcode/methodology', 9, question='q?', post=lambda p: False)
    assert 'failed' in q
    assert alerts.tell_methbot('needs-input', 'tangentcode/methodology', 9, question='q?', post=lambda p: True) is True
    events = [r['event'] for r in eventdb.store().rows("source='watch'")]
    assert events == ['methbot-done-failed', 'methbot-done', 'methbot-needs-input-failed', 'methbot-needs-input']


def test_no_methbot_url_is_a_failed_post_not_a_sent_one():
    assert not alerts.ALERT_URL_FILE.exists()
    assert 'failed' in alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD)
    assert not alerts.state().done(f"methbot:done:tangentcode/tangentswarm#71@{HEAD}")


def test_the_methbot_post_sends_the_bearer_key_to_the_url(tmp_path):
    """The real post_wake with an injected urlopen: no network."""
    alerts.ALERT_URL_FILE.write_text('https://methbot.example/wake\n')
    alerts.ALERT_KEY_FILE.write_text('sekrit\n')
    reqs = []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import json
    webhook.post_wake({'source': 'swarm', 'kind': 'done'}, url_file=alerts.ALERT_URL_FILE,
                      key_file=alerts.ALERT_KEY_FILE, urlopen=lambda req, timeout=None: reqs.append(req) or R(),
                      background=False)
    assert len(reqs) == 1 and reqs[0].full_url == 'https://methbot.example/wake'
    assert reqs[0].get_header('Authorization') == 'Bearer sekrit'
    assert json.loads(reqs[0].data) == {'source': 'swarm', 'kind': 'done'}


def test_a_synchronous_post_answers_whether_it_was_delivered():
    import urllib.error
    alerts.ALERT_URL_FILE.write_text('https://methbot.example/wake\n')

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, 'no', {}, None)
    assert webhook.post_wake({'k': 1}, url_file=alerts.ALERT_URL_FILE, urlopen=refuse, background=False) is False
    assert webhook.post_wake({'k': 1}, url_file=alerts.ALERT_URL_FILE.with_name('none'), background=False) is False


def test_drain_posts_waits_for_the_background_posts():
    """Codex on PR #71, the same bug in swarm-watch --once: its wake POST threads are
    daemon threads, so the process drains them before it exits."""
    import threading
    alerts.ALERT_URL_FILE.write_text('https://swarmbot.example/wake\n')
    go, done = threading.Event(), []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def slow(req, timeout=None):
        go.wait(5)
        done.append(1)
        return R()
    assert webhook.post_wake({'k': 1}, url_file=alerts.ALERT_URL_FILE, urlopen=slow) is None
    assert webhook.drain_posts(timeout=0.05) == 1
    go.set()
    assert webhook.drain_posts(timeout=5) == 0 and done == [1]


def test_a_swarmbot_wake_carries_the_detail():
    posts = []
    alerts.wake_swarmbot('hung pytest killed', 'hung:1', pr=7, detail={'capture': 'screen'}, post=posts.append)
    assert posts and posts[0]['detail'] == {'capture': 'screen'} and posts[0]['why'] == 'hung pytest killed'
    assert posts[0]['source'] == 'swarm-watch'


def test_an_incident_ticket_is_opened_once_per_key_and_capped_per_hour(monkeypatch):
    monkeypatch.setattr(alerts, 'INCIDENTS_PER_HOUR', 2)
    calls = []
    sh = lambda argv: calls.append(argv) or 'https://github.com/tangentcode/methodology/issues/90\n'  # noqa: E731
    t = [1000.0]
    url = alerts.incident('a', 'menu in mn-7', 'blocked', sh=sh, now=lambda: t[0])
    assert url.endswith('/issues/90')
    assert calls[0][:4] == ['gh', 'issue', 'create', '--repo'] and calls[0][4] == alerts.INCIDENT_REPO
    body = calls[0][calls[0].index('--body') + 1]
    assert all(h in body for h in ('## What happened', '## Why', '## Fix', '## Prevention'))
    assert calls[1][:3] == ['gh', 'project', 'item-add'] and calls[1][3] == '6'
    assert alerts.incident('a', 'menu in mn-7', 'blocked', sh=sh, now=lambda: t[0]) == 'dup'
    alerts.incident('b', 'x', 'y', sh=sh, now=lambda: t[0])
    assert alerts.incident('c', 'x', 'y', sh=sh, now=lambda: t[0]) == 'capped'
    t[0] += 3601
    assert alerts.incident('c', 'x', 'y', sh=sh, now=lambda: t[0]).startswith('https://')


def test_overlapping_methbot_reports_post_once():
    """Codex on PR #71: two serve processes of the same report both posted. The check, the
    POST and the mark run under one interprocess lock, and the check reads the file again."""
    import threading
    other = alerts.state()  # loaded before the first report marks the key, like a second process
    inside, release, posts = threading.Event(), threading.Event(), []

    def slow(p):
        posts.append(p)
        inside.set()
        release.wait(5)
        return True
    first = []
    t = threading.Thread(target=lambda: first.append(
        alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, post=slow)))
    t.start()
    assert inside.wait(5)
    second = []
    t2 = threading.Thread(target=lambda: second.append(
        alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD, st=other, post=slow)))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()  # waits on the lock while the first POST runs
    release.set()
    t.join(5)
    t2.join(5)
    assert first == [True] and second == ['already sent'] and len(posts) == 1


def test_a_stale_state_does_not_repeat_an_alert_or_an_incident():
    """The same class: swarm-watch --once beside the daemon, each with its own loaded state."""
    a, b = alerts.state(), alerts.state()
    sent = []
    assert alerts.alert('Codex outage', key='k9', st=a, post=sent.append) is True
    assert alerts.alert('Codex outage', key='k9', st=b, post=sent.append) is False
    assert len(sent) == 1
    calls = []
    sh = lambda argv: calls.append(argv) or 'https://github.com/tangentcode/methodology/issues/91\n'  # noqa: E731
    c, d = alerts.state(), alerts.state()
    assert alerts.incident('z', 't', 'w', st=c, sh=sh).endswith('/issues/91')
    assert alerts.incident('z', 't', 'w', st=d, sh=sh) == 'dup'
    assert len(calls) == 2  # one issue create and one item-add


def test_methbot_exception_and_pull_post_the_reason_once_per_pr_head_and_key():
    """tangentswarm #89: a done report the swarm could not merge, and the Backlog pull."""
    sent = []
    repo = 'tangentcode/tangentswarm'
    assert alerts.tell_methbot('exception', repo, 90, head=HEAD, reason=' draft ', post=sent.append,
                               now=lambda: 1_763_400_000.0) is True
    assert alerts.tell_methbot('exception', repo, 90, head=HEAD, reason='other', post=sent.append) == 'already sent'
    assert sent == [{'source': 'swarm', 'kind': 'exception', 'repo': repo, 'pr': 90, 'head': HEAD,
                     'reason': 'draft', 'at': '2025-11-17T12:20:00-05:00'}]
    assert alerts.tell_methbot('exception', repo, 90, head=HEAD, reason='dirty', key='cleanup', post=sent.append) is True
    assert alerts.tell_methbot('exception', repo, 90, head='b' * 40, reason='draft', post=sent.append) is True
    assert alerts.tell_methbot('pull', repo, 90, head=HEAD, reason='start #95', post=sent.append) is True
    assert [p['kind'] for p in sent] == ['exception'] * 3 + ['pull']
    assert 'full head sha' in alerts.tell_methbot('exception', repo, 90, head='abc', reason='x', post=sent.append)
    assert 'needs the reason' in alerts.tell_methbot('pull', repo, 90, head=HEAD, reason=' ', post=sent.append)
    assert 'not a Project 6 repo' in alerts.tell_methbot('exception', 'tangentcode/platform', 1, head=HEAD,
                                                         reason='x', post=sent.append)
    assert len(sent) == 4
