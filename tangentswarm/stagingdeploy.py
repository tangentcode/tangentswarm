"""Start the PTW staging deploy at once after a push to platform work (tangentswarm #136).

Before, ptw-staging-deploy.timer polled origin/work every 2 min, so a pull waited about 49 s
(up to 2 min) before its deploy started. Michal (2026-11-17 1:06 PM ET): "It doesn't need to
be a webhook. As long as it happens quickly. Make it so." The swarm starts the unit itself
after each push to work; the timer stays as a backup.

cornerbot (2026-11-17 1:07 PM ET) allowed only this exact line for memnar (sudoers):

    sudo -n /usr/bin/systemctl start --no-block ptw-staging-deploy.service

Plain systemctl without sudo -n asks polkit for a password, so never use it. When a deploy
already runs, the start is a no-op: swarm-promoted starts the unit again when that deploy ends
and origin/work moved past what it built (promoted.Promoter.deploy_watch). The deploy script
does nothing when the served .deploy-sha is origin/work already, so an extra start is cheap.

SWARM_STAGING_DEPLOY_START=0 turns the start off (the timer alone, as before).
"""
import os

from . import proc

ARGV = ('sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block', 'ptw-staging-deploy.service')
SITE = 'ptw'  # the hr-gate SITES key this unit deploys
TIMEOUT = 20


def enabled():
    return os.environ.get('SWARM_STAGING_DEPLOY_START', '1') != '0'


def start(why='', sh=None):
    """Start ptw-staging-deploy.service now (no wait for the deploy). Returns one note line.
    Never raises: the timer is the backup."""
    if not enabled():
        return f"staging deploy start is off (SWARM_STAGING_DEPLOY_START=0); the timer deploys ({why})"
    try:
        r = (sh or proc.sh)(*ARGV, check=False, timeout=TIMEOUT, who='staging-deploy')
    except Exception as e:
        return f"staging deploy start failed: {type(e).__name__}: {str(e)[:160]}; the timer deploys ({why})"
    if r.returncode:
        err = ' '.join(((r.stderr or '') + (r.stdout or '')).split())[:160]
        return f"staging deploy start failed: rc={r.returncode} {err}; the timer deploys ({why})"
    return f"started ptw-staging-deploy ({why})"
