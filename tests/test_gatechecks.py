"""gatechecks: screenshots must render (7a); entry needs staging to serve the head (6)."""
import urllib.error

from tangentswarm import gatechecks as G

REPO = 'owner/repo'
ATTACH = 'https://github.com/user-attachments/assets/0c5b998d-7a0c-4e80-bc87-a74779e4169f'
BLOB = f'https://github.com/{REPO}/blob/swarm-shots/shots/pr1-a.png?raw=true'
RAW = f'https://raw.githubusercontent.com/{REPO}/swarm-shots/shots/pr1-b.png'


def never(*a):
    raise AssertionError('must not be called')


def test_image_urls_reads_markdown_and_img_tags_in_order():
    body = f'x ![a]({ATTACH}) <img width=3 src="{RAW}"> ![b](<{BLOB}> "t") ![a]({ATTACH})'
    assert G.image_urls(body) == [ATTACH, RAW, BLOB]


def test_a_relative_name_fails_like_platform_479():
    ok, why = G.check_images('![x](pr479-attr-gui.png)', REPO, exists=never, probe=never)
    assert not ok and 'not an absolute https URL' in why


def test_no_image_fails():
    assert G.check_images('just text, a .png word', REPO, exists=never, probe=never) == (False, 'no image in body')


def test_user_attachments_pass_on_form_without_a_fetch():
    assert G.check_images(f'![a]({ATTACH})', REPO, exists=never, probe=never)[0]
    ok, why = G.check_images('![a](https://github.com/user-attachments/assets/x)', REPO,
                             exists=never, probe=lambda u: (False, 'probe: 404'))
    assert not ok and 'probe: 404' in why        # not the GitHub-issued form: it is probed


def test_swarm_shots_urls_must_exist_on_the_branch():
    seen = []
    ok, _ = G.check_images(f'![a]({BLOB}) ![b]({RAW})', REPO, exists=lambda p: seen.append(p) or True, probe=never)
    assert ok and seen == ['shots/pr1-a.png', 'shots/pr1-b.png']
    ok, why = G.check_images(f'![a]({BLOB})', REPO, exists=lambda p: False, probe=never)
    assert not ok and 'not on the swarm-shots branch' in why
    assert G.shots_path(f'https://github.com/{REPO}/raw/swarm-shots/s/x.png', REPO) == 's/x.png'
    assert G.shots_path(f'https://github.com/other/repo/raw/swarm-shots/s/x.png', REPO) is None


def test_other_https_hosts_must_load_as_an_image():
    class R:
        def __init__(self, kind, status=200):
            self.headers, self.status = {'Content-Type': kind}, status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    assert G.probe_image('https://x/y.png', opener=lambda req, timeout: R('image/png'))[0]
    ok, why = G.probe_image('https://x/y.png', opener=lambda req, timeout: R('text/html'))
    assert not ok and 'not an image' in why

    def gone(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, 'nf', {}, None)
    ok, why = G.probe_image('https://x/y.png', opener=gone)
    assert not ok and '404' in why


def test_served_check_is_strict_on_entry_and_lenient_on_a_regate():
    web = ['web/a.ts', 'README.md']
    assert G.check_served(['README.md'], True, None, never)[0]
    assert G.check_served(web, True, 'abc12345', lambda s: True)[0]
    ok, why = G.check_served(web, True, 'abc12345', lambda s: False)
    assert not ok and 'LACKS the head' in why
    ok, why = G.check_served(web, False, 'abc12345', lambda s: False)
    assert ok and why.startswith('lenient re-gate')
    assert not G.check_served(['api/x.py'], True, None, never, err='cannot read')[0]
    assert G.check_served(['app/x'], False, None, never, err='cannot read')[0]


def test_served_accepts_the_vn_tip_condition_9_picked():
    """platform #447: work 95f38a27 has ptw/cursor-item-end-v2 06347b83, not head 0a8b0f7a."""
    web = ['web/ptw/cursor.ts']
    inside = {('06347b83', '95f38a27')}
    contains = lambda anc, desc: (anc, desc) in inside
    ok, why = G.check_served(web, True, '95f38a27', lambda s: False,
                             tips=[('ptw/cursor-item-end-v2', '06347b83')], contains=contains)
    assert ok and 'contains ptw/cursor-item-end-v2 06347b83' in why
    ok, why = G.check_served(web, True, '95f38a27', lambda s: False,
                             tips=[('ptw/cursor-item-end-v1', 'deadbeef')], contains=contains)
    assert not ok and 'LACKS the head' in why


def test_a_broken_work_branch_fails_entry():
    files = ['web/js/ptw/ChessTrackParser.mts', 'web/js/ptw/MenuTrackParser.mts', 'web/tc/AnimCompiler.mts',
             'web/tc/MnAnimPlugin.playhead.test.mts']
    why = G.work_problem(files)
    assert why == ('origin/work has conflict markers in web/js/ptw/ChessTrackParser.mts, '
                   'web/js/ptw/MenuTrackParser.mts, web/tc/AnimCompiler.mts and 1 more')
    failed = {'sha': '0d249b69d06d', 'status': 'completed', 'conclusion': 'failure'}
    assert G.work_problem([], failed) == 'the latest work Redeploy run failed (work 0d249b69)'
    assert G.work_problem([], dict(failed, conclusion='skipped')) is None
    assert G.work_problem([], dict(failed, conclusion='success')) is None
    assert G.work_problem([], None) is None
    web = ['web/a.ts']
    ok, why = G.check_served(web, True, 'abc12345', lambda s: True, work_bad='origin/work has conflict markers in x')
    assert not ok and why.startswith('origin/work has conflict markers')
    assert G.check_served(web, False, 'abc12345', lambda s: True, work_bad='w')[1] == 'lenient re-gate: w'
    assert G.check_served(['README.md'], True, None, never, work_bad='w')[0]   # no served files


def test_every_staging_site_must_serve_the_head():
    """platform #493: tangentcode and minavo served 37ceb8f4, ptw still served 95f38a27."""
    web = ['web/tc/DbItemList.mts', 'api/tcode/db_bp.py']
    has = lambda s: s != '95f38a27'
    sites = [('work.example.test', '4fe539ad', None), ('minavo.example.test', '4fe539ad', None),
             ('ptw.example.test', '95f38a27', None)]
    ok, why = G.check_served_sites(web, True, sites, has)
    assert not ok
    assert why == ('1 of 3 staging sites do not serve it: ptw.example.test: static .deploy-sha 95f38a27 '
                   'LACKS the head: staging does not serve it yet')
    ok, why = G.check_served_sites(web, False, sites, has)
    assert ok and why.startswith('lenient re-gate: 1 of 3 staging sites') and 'pawntakesworld' in why
    ok, why = G.check_served_sites(web, True, sites[:2], has)
    assert ok and why.startswith('all 2 staging sites serve it: work.example.test: static .deploy-sha 4fe539ad')


def test_every_site_accepts_the_vn_tip_and_an_unreadable_site_fails_entry():
    web = ['web/ptw/cursor.ts']
    sites = [('a', 'aaaaaaaa', None), ('b', 'bbbbbbbb', None)]
    contains = lambda tip, desc: (tip, desc) in {('06347b83', 'aaaaaaaa'), ('06347b83', 'bbbbbbbb')}
    assert G.check_served_sites(web, True, sites, lambda s: False, tips=[('x-v2', '06347b83')], contains=contains)[0]
    ok, why = G.check_served_sites(web, True, sites + [('c', None, 'cannot read /x')], lambda s: True)
    assert not ok and 'c: cannot read the static .deploy-sha (cannot read /x)' in why
    assert G.check_served_sites(['README.md'], True, [], never)[0]
    assert not G.check_served_sites(web, True, [], never)[0]
    assert G.check_served_sites(web, True, sites, never, work_bad='origin/work has markers') == \
        (False, 'origin/work has markers')


def pass_comment(body, login='tangentcode-minbot'):
    return {'author': {'login': login}, 'body': body}


def test_minbot_pass_comment_must_name_the_exact_head():
    head = '39144fac99549b3b2e7e10db7d945da62d342648'
    old = pass_comment('### MinBot pass\n\nCommit: `49cf308210a01c8161614d55485f12cf8ca9a5ff`\n\nCodex clean')
    new = pass_comment(f'### MinBot pass\n\nCommit: `{head}`\n\nCodex clean on 39144fac99')
    assert G.minbot_pass([old], [('head', head)]) == (False, 'no MinBot pass comment names head 39144fac')
    assert G.minbot_pass([old, new], [('head', head)]) == (True, 'MinBot pass comment names head 39144fac')
    short = pass_comment('### MinBot pass\n\nCommit: `b1849519fd`\n\nExact-head pass on b1849519fd')
    assert G.minbot_pass([short], [('head', 'b1849519fd' + '0' * 30)])[0]
    assert G.minbot_pass([pass_comment('### MinBot pass on 39144fac99')], [('head', head)])[0]


def test_minbot_pass_accepts_the_vn_tip_but_not_other_authors_or_titles():
    tip, head = '06347b83' + 'a' * 32, '0a8b0f7a' + 'b' * 32
    c = pass_comment(f'### MinBot pass\n\nCommit: `{tip}`')
    assert not G.minbot_pass([c], [('head', head)])[0]
    assert G.minbot_pass([c], [('head', head), ('ptw/x-v2', tip)]) == (True, 'MinBot pass comment names ptw/x-v2 06347b83')
    assert G.minbot_pass([pass_comment(f'### MinBot pass\n\nCommit: `{head}`', login='tangentcode-minbot[bot]')],
                         [('head', head)])[0]
    assert not G.minbot_pass([pass_comment(f'### MinBot pass\n\nCommit: `{head}`', login='someone')], [('head', head)])[0]
    assert not G.minbot_pass([pass_comment(f'### Evidence\n\nCommit: `{head}`')], [('head', head)])[0]
    assert not G.minbot_pass([pass_comment('### MinBot pass\n\nCommit: `0a8b0f7`')], [('head', head)])[0]  # 7 chars
