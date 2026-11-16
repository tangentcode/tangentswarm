"""Alerts to Michal, swarmbot wakes and incident tickets (tangentswarm #40)."""
from tangentswarm import alerts, eventdb, webhook


def test_tell_michal_posts_once_per_key_and_logs_a_row():
    sent = []
    assert alerts.tell_michal('Codex outage', key='k1', post=sent.append)
    assert not alerts.tell_michal('Codex outage', key='k1', post=sent.append)
    assert len(sent) == 1 and sent[0]['to'] == 'michal' and sent[0]['text'] == 'Codex outage'
    rows = eventdb.store().rows("stage='watch'")
    assert len(rows) == 1 and 'Codex outage' in rows[0]['notes']


def test_the_alert_goes_to_the_methbot_url_not_the_swarmbot_url(monkeypatch):
    seen = []
    monkeypatch.setattr(webhook, 'post_wake', lambda p, url_file=None, key_file=None: seen.append(url_file))
    alerts.tell_michal('x', key='k2')
    assert seen == [alerts.ALERT_URL_FILE]


def test_a_swarmbot_wake_carries_the_detail():
    posts = []
    ex = webhook.Executor(wake_post=posts.append)
    alerts.wake_swarmbot('hung pytest killed', 'hung:1', pr=7, detail={'capture': 'screen'}, ex=ex)
    assert posts and posts[0]['detail'] == {'capture': 'screen'} and posts[0]['why'] == 'hung pytest killed'


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
