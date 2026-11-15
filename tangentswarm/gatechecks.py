"""Two hr-gate conditions that let a broken PR into Human Review (platform #479).

Condition 7a (screenshots). hr-gate's IMG_RE took any markdown image, so a bare relative
name such as ![x](pr479-attr-gui.png) passed. It never renders on GitHub. Now every image
in the body must be an absolute https URL that resolves:

  https://github.com/user-attachments/assets/<id>    issued by GitHub's own upload. In a
                                                      private repo only a signed-in reader
                                                      can open it, so the form is checked,
                                                      not fetched (no user token is used).
  swarm-shots branch of the PR's repo, as
    https://github.com/<repo>/blob/swarm-shots/<path>?raw=true
    https://github.com/<repo>/raw/swarm-shots/<path>
    https://raw.githubusercontent.com/<repo>/swarm-shots/<path>
                                                      the file must exist on swarm-shots
                                                      (contents API, app quota)
  any other https URL                                 GET answers 200 with an image/* type

Condition 6 (deployed + served). It was best-effort and always passed. For a PR that
touches served files (web/, app/, api/), ENTRY (into Staging or Human Review, and the
instant promote) now needs the static .deploy-sha to contain the head. A re-gate of a card
already in Human Review (a merge-only re-gate) stays lenient. Entry also fails when
origin/work itself is broken (work_problem): it holds conflict markers, or its latest
Redeploy run failed. Work commit 0d249b69 (#447) reached work with markers at 1:19 AM ET,
its Redeploy failed, and the gate still passed.

Condition 6 checks EVERY staging site (check_served_sites). Each site serves the platform
work branch, so a PR with served files reaches all of them. Platform #493 entered Human
Review while one staging site still served 95f38a27, which did not contain its head
37ceb8f4; the gate had read only the one site that site_for picked.
"""
import re
import urllib.request

SHOTS_BRANCH = 'swarm-shots'

# The one rule every coder gets (launch line, gate relays). Keep it one line.
SHOTS_RULE = ("Screenshots in the PR body must be absolute https URLs that render on GitHub: drag the "
              "image into a GitHub comment box and paste the github.com/user-attachments/assets/... URL, "
              f"or push it to the {SHOTS_BRANCH} branch and use https://github.com/<owner>/<repo>/blob/"
              f"{SHOTS_BRANCH}/<path>?raw=true. Never a relative file name such as ![x](shot.png); "
              "hr-gate fails it.")
SERVED_RE = re.compile(r'^(web|api|app)/')

_MD = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_TAG = re.compile(r'<img\s[^>]*?src\s*=\s*["\']?([^"\'\s>]+)', re.I)
_ATTACH = re.compile(r'https://github\.com/user-attachments/assets/[0-9a-fA-F-]{16,}/?')


def image_urls(body):
    """Image URLs in a PR body (markdown and <img>), in order, without repeats."""
    found = sorted([(m.start(), m.group(1)) for m in _MD.finditer(body or '')] +
                   [(m.start(), m.group(1)) for m in _TAG.finditer(body or '')])
    out = []
    for _, u in found:
        if u not in out:
            out.append(u)
    return out


def shots_path(url, repo):
    """The file path if url is a swarm-shots URL of repo, else None."""
    r = re.escape(repo)
    b = re.escape(SHOTS_BRANCH)
    for pat in (rf'https://github\.com/{r}/blob/{b}/([^?#]+)\?raw=true$',
                rf'https://github\.com/{r}/raw/{b}/([^?#]+)$',
                rf'https://raw\.githubusercontent\.com/{r}/{b}/([^?#]+)$'):
        m = re.match(pat, url)
        if m:
            return m.group(1)
    return None


def probe_image(url, timeout=15, opener=urllib.request.urlopen):
    """(ok, why) for a public https image URL: GET answers 200 with an image/* content type."""
    req = urllib.request.Request(url, headers={'User-Agent': 'hr-gate', 'Range': 'bytes=0-0'})
    try:
        with opener(req, timeout=timeout) as r:
            kind = (r.headers.get('Content-Type') or '').split(';')[0].strip()
            code = getattr(r, 'status', 200)
    except Exception as e:  # HTTPError, URLError, timeouts
        return False, f"{url[:80]} does not load ({getattr(e, 'code', None) or type(e).__name__})"
    if code not in (200, 206) or not kind.startswith('image/'):
        return False, f"{url[:80]} is not an image ({code} {kind or 'no type'})"
    return True, ''


UI_RE = re.compile(r'^web/|\.(css|html|mts|tsx?|jsx?|svg)$')
NO_SHOTS_PREFIXES = ('api/', 'docs/', '.github/', 'scripts/', 'tests/', 'test/')
CODE_ONLY_RE = re.compile(r'code[- ]only|tooling[- ]only|no deployable surface|nothing (?:to )?deploy', re.I)


def needs_screenshots(paths, body=''):
    """True when condition 7a applies: the PR changes a visible UI file. api-only and
    docs-only PRs (and tooling: .github/, scripts/, tests/) are exempt, and so is a body
    that says code-only (Michal 2026-11-15: #519 was demoted for screenshots it did not
    need a worker for)."""
    if CODE_ONLY_RE.search(body or '') and not any(p.startswith(('web/', 'app/')) for p in paths):
        return False
    return any(UI_RE.search(p) and not p.startswith(NO_SHOTS_PREFIXES) and not p.endswith('.md')
               for p in paths)


def check_images(body, repo, exists, probe=probe_image):
    """(ok, why) for condition 7a. exists(path) -> bool says if a file is on swarm-shots."""
    urls = image_urls(body)
    if not urls:
        return False, 'no image in body'
    bad = []
    for u in urls:
        if not u.lower().startswith('https://'):
            bad.append(f"{u[:60]} is not an absolute https URL (a relative name never renders)")
        elif _ATTACH.fullmatch(u):
            continue
        elif shots_path(u, repo) is not None:
            if not exists(shots_path(u, repo)):
                bad.append(f"{u[:80]} is not on the {SHOTS_BRANCH} branch")
        else:
            ok, why = probe(u)
            if not ok:
                bad.append(why)
    if bad:
        return False, f"{len(bad)} of {len(urls)} image(s) do not render: " + '; '.join(bad[:3])
    return True, f"{len(urls)} image(s), all absolute https and resolving"


MARKER_GREP = ['grep', '-l', '--all-match', '-e', '^<<<<<<< ', '-e', '^>>>>>>> ']  # git grep args


def work_problem(marker_files, redeploy=None):
    """Why origin/work is broken, or None. marker_files: files with both <<<<<<< and >>>>>>>
    lines (git <MARKER_GREP> origin/work). redeploy: the latest work Redeploy run
    {sha, status, conclusion} or None. Only a FAILED run counts (skipped is the webhook mode)."""
    if marker_files:
        more = f" and {len(marker_files) - 3} more" if len(marker_files) > 3 else ''
        return f"origin/work has conflict markers in {', '.join(marker_files[:3])}{more}"
    if redeploy and (redeploy.get('conclusion') or '').lower() in ('failure', 'timed_out', 'startup_failure'):
        return f"the latest work Redeploy run failed (work {(redeploy.get('sha') or '')[:8]})"
    return None


def check_served(paths, entering, static_sha, has_head, err=None, tips=(), contains=None, work_bad=None):
    """(ok, why) for condition 6. has_head(sha) -> bool: the deployed commit contains the head.
    entering: the card is not already in Human Review (strict). Else lenient (a re-gate).
    tips: [(name, sha)] the stack tips condition 9 picked (a -vN branch). The staging pull
    merges that tip into work, not the plain head, so a deployed commit that contains the
    tip serves the PR. contains(tip_sha, static_sha) -> bool checks it.
    work_bad: work_problem()'s reason; it fails entry before any SHA check."""
    if not any(SERVED_RE.search(p) for p in paths):
        return True, 'n/a (no served files: web/, api/, app/)'
    if work_bad:
        return (False, work_bad) if entering else (True, 'lenient re-gate: ' + work_bad)
    if err or not static_sha:
        why = f"cannot read the static .deploy-sha ({err or 'empty'})"
        return (False, why) if entering else (True, 'lenient re-gate: ' + why)
    if has_head(static_sha):
        return True, f"static .deploy-sha {static_sha[:8]} contains the head"
    for name, sha in tips:
        if sha and contains and contains(sha, static_sha):
            return True, f"static .deploy-sha {static_sha[:8]} contains {name} {sha[:8]} (the tip condition 9 picked)"
    why = f"static .deploy-sha {static_sha[:8]} LACKS the head: staging does not serve it yet"
    return (False, why) if entering else (True, 'lenient re-gate: ' + why)


def check_served_sites(paths, entering, sites, has_head, tips=(), contains=None, work_bad=None):
    """(ok, why) for condition 6 on EVERY staging site. sites: [(host, static_sha, err)].
    Each site must serve the head (or the -vN tip condition 9 picked); one site that lags
    fails entry (platform #493: ptw lagged while the other two served the head).
    A re-gate (not entering) stays lenient, and names the sites that lag."""
    if not any(SERVED_RE.search(p) for p in paths):
        return True, 'n/a (no served files: web/, api/, app/)'
    if work_bad:
        return (False, work_bad) if entering else (True, 'lenient re-gate: ' + work_bad)
    if not sites:
        return (False, 'no staging site to check') if entering else (True, 'lenient re-gate: no staging site')
    res = [(host, *check_served(paths, entering, sha, has_head, err, tips, contains)) for host, sha, err in sites]
    lag = 'lenient re-gate: '
    bad = [f"{h}: {w[len(lag):] if w.startswith(lag) else w}" for h, ok, w in res if not ok or w.startswith(lag)]
    if bad:
        why = f"{len(bad)} of {len(res)} staging sites do not serve it: " + '; '.join(bad)
        return (False, why) if entering else (True, lag + why)
    return True, f"all {len(res)} staging sites serve it: " + '; '.join(f"{h}: {w}" for h, _, w in res)


# Condition 4 (Michal 2026-11-14 7:22 PM ET): the MinBot-passed label is retired. The pass is
# a MinBot comment that names the commit it passed: "### MinBot pass" with "Commit: `<sha>`"
# (minbot evidence --title "MinBot pass" --commit SHA), or "### MinBot pass on <sha>".
MINBOT_LOGIN = 'tangentcode-minbot'
_PASS_TITLE = re.compile(r'^\s*###\s*MinBot pass\b')
_HEX = re.compile(r'\b[0-9a-f]{8,40}\b')


def minbot_pass(comments, shas):
    """(ok, why) for condition 4. shas: [(label, sha)] such as [('head', H)] or the -vN tip.
    Passes when a MinBot pass comment names one of them (8 or more hex characters of it)."""
    for c in reversed(list(comments or [])):
        login = ((c.get('author') or {}).get('login') or '').replace('[bot]', '')
        body = c.get('body') or ''
        if login != MINBOT_LOGIN or not _PASS_TITLE.match(body):
            continue
        named = _HEX.findall(body)
        for label, sha in shas:
            if sha and any(sha.startswith(t) for t in named):
                return True, f"MinBot pass comment names {label} {sha[:8]}"
    want = ', '.join(f"{label} {sha[:8]}" for label, sha in shas if sha)
    return False, f"no MinBot pass comment names {want}"
