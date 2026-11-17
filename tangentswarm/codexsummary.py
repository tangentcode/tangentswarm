"""The one parser for Codex's review-state summary comment (tangentswarm #75).

Codex edits one summary comment while it reviews. Its hidden state JSON goes from
"status":"running" with null findings flags to "status":"completed" with real flags. A
reader that used `not has_any_findings` took the running state as clean (methodology #96,
tangentswarm #74, 2026-11-16). advance.codex_clean, advance.codex_decision and
tick.codex_status all read the summary through this module, so they cannot drift apart.
"""
import json
import re

STATE_RE = re.compile(r'codex-review-state:v1\s*(\{.*?\})\s*-->', re.S)

CLEAN, FINDINGS, PENDING = 'clean', 'findings', 'pending'


def state(body):
    """The code_review dict of a summary comment body, or None when it has no state."""
    m = STATE_RE.search(body or '')
    if not m:
        return None
    try:
        st = json.loads(m.group(1))
    except ValueError:
        return None
    cr = st.get('code_review') if isinstance(st, dict) else None
    return cr if isinstance(cr, dict) else None


def result(body, head):
    """'clean' | 'findings' | 'pending' for one summary body on this head, or None when the
    body has no review state. Completed on the head with both flags exactly false is clean.
    Completed on the head with either flag true is findings. Anything else (running, null
    flags, another sha, no head) is pending, never clean."""
    st = state(body)
    if st is None:
        return None
    if not head or st.get('commit_sha') != head or st.get('status') != 'completed':
        return PENDING
    flags = (st.get('has_any_findings'), st.get('has_unresolved_findings'))
    if True in flags:
        return FINDINGS
    if flags == (False, False):
        return CLEAN
    return PENDING


def on_head(comments, head, is_codex):
    """The summary result for the head over all Codex comments: findings in any summary win,
    then clean, then pending; None when no Codex comment has a review state."""
    seen = {result(c.get('body'), head) for c in comments or [] if is_codex(c)} - {None}
    for r in (FINDINGS, CLEAN, PENDING):
        if r in seen:
            return r
    return None
