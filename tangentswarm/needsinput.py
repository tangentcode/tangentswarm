"""New Needs Input cards ping Michal in MethBot's chat (tangentswarm #40 item 4c).

The question itself goes through agent_report (tangentswarm #53): report_status or the
worker tool needs_input posts it on the ticket, moves the card and closes the Claude, and a
pane that asks is routed there by the pane watcher.
"""
import time

REPO = 'tangentcode/platform'


def new_cards(st, numbers, ping, titles=None, first_run_quiet=True, now=time.time):
    """Ping Michal for each card new in Needs Input (numbers: the PRs and issues there now).
    The first look only records them. Returns the numbers pinged."""
    numbers = sorted(set(numbers))
    if 'needs_input_seen' not in st.data and first_run_quiet:
        st.data['needs_input_seen'] = numbers
        st.save()
        return []
    seen = set(st.data.get('needs_input_seen') or [])
    out = []
    t = now()
    for n in numbers:
        if n in seen:
            continue
        title = (titles or {}).get(n, '')
        ping(f"Needs Input: #{n} {title} https://github.com/{REPO}/issues/{n}".replace('  ', ' '),
             key=f"needs-input-card:{n}:{int(t)}", pr=n, st=st)
        out.append(n)
    st.data['needs_input_seen'] = numbers
    st.save()
    return out
