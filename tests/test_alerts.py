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


def test_methbot_goes_to_the_methbot_url_with_its_key(monkeypatch):
    seen = []
    monkeypatch.setattr(webhook, 'post_wake', lambda p, url_file=None, key_file=None, **k: seen.append((url_file, key_file)))
    alerts.tell_methbot('done', 'tangentcode/tangentswarm', 71, head=HEAD)
    assert seen == [(alerts.ALERT_URL_FILE, alerts.ALERT_KEY_FILE)]


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
