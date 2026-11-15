# Preview gate (design)

Status: design. Approved plan: methodology #67 (maintainer, 2026-11-14 11:28 PM ET).
Nothing here is built yet. The swarm side waits for the preview hosts (see "Needs from
cornerbot" below).

## Why

Platform PR #476 reached Human Review with screenshots from a local Vite page that
rendered the component outside the app. The feature did not work in the real app.
Screenshots and a feature check must come from a deploy of the real app, BEFORE the PR
enters Ready. Staging -> Human Review must stay instant: it gets no new step.

## Where the gate sits

    In Progress -> Auto Review -> [minbot pass] -> [PREVIEW] -> Ready -> Staging -> Human Review
                                                     new          unchanged from here on

- The minbot pass (Codex clean, not a draft, merges onto main, Linux CI green) stays as it
  is. Today it moves the card to Ready. With the gate, a PR that needs a preview stays in
  Auto Review and goes into the preview queue. Only a preview pass moves it to Ready.
- No new board column. While the preview runs, the card stays in Auto Review. The state
  is in the preview state file and in one PR comment.
- Promote (Staging -> Human Review), the fill (Ready -> Staging) and hr-gate condition 6
  do not change. They never wait on the preview lane.

A PR needs a preview when it changes UI files (hr-gate UI_RE) and the body does not mark
it code-only. Other PRs go from the minbot pass to Ready as today.

## The preview lane

One PR at a time, in the background, under its own lock (`~/.cache/swarm-preview.lock`).
It never takes the serial merge lock (`swarm-serial.lock`), so it cannot slow a pull or a
promote. A new command `swarm-preview` runs one step and exits, like `swarm-advance`.
The swarm-promoted daemon starts it when the queue is not empty and the lane is free.

One run for PR n at head H:

1. **Pick.** The oldest queued PR whose minbot pass names its current head. A PR whose
   head moved since it was queued goes back to the minbot step.
2. **Build the preview branch.** In a scratch clone: `preview-<app>` = origin/work, then
   a merge commit of H (`git merge --no-ff`). The PR branch is not changed. On a
   conflict: no deploy, a `NEEDS_REWORK #n <files>` relay (the same path as the main
   check in the cascade), and the card goes to In Progress. The merge commit is
   force-pushed to `preview-<app>`. `<app>` is the site of the PR (hr-gate site_for:
   ptw, minavo or work).
3. **Deploy.** The push starts the preview deploy (see cornerbot item 2). The lane polls
   the preview site's `.deploy-sha` until it names the merge commit (limit 15 min).
   - Timeout: the run stops with `PREVIEW_BLOCKED: <app> preview does not serve <sha>
     after 15m`. That line is printed again on every run until the site serves the
     commit.
4. **Smoke.** The generic smoke drive of the site, as staging-checks.sh runs it against
   staging, but against the preview profile. A failure here is a preview failure. It is
   not a fault of the PR when the same smoke also fails on plain work; then the lane
   reports `PREVIEW_BROKEN` and keeps the PR queued.
5. **Feature check and screenshots.** Playwright (headless Chromium) runs the PR's
   preview block (below) against the preview site, logged in with the preview login.
   Each `shot` step saves a PNG. Any failed `expect` step fails the check. The page must
   also log no uncaught JS errors.
6. **Publish.** The PNGs go to the `swarm-shots` branch at `preview/<n>/<head8>-<k>.png`.
   minbot posts one comment on the PR:

       Preview pass: head <H> on work <W> (merge <M>), site <app> preview.
       Checks: <k> of <k> passed. Shots: <urls>

   or `Preview FAIL: ...` with the failed step, its error and the last screenshot.
   The swarm also writes the shots into the PR body, in a section between markers
   (`<!-- swarm-preview -->`), so the Human Review reader sees them. The coder's own
   text is not changed.
7. **Result.**
   - Pass: the card moves Auto Review -> Ready (option id, through hr-gate move).
   - Fail: the card moves to In Progress. The PR's Claude gets a one-line relay
     (`slots.revive`, or a `RELAY:` line when no rework slot is free) with the failed
     step and the screenshot URL.
8. **Reset.** `preview-<app>` is force-pushed back to origin/work and deployed again.
   The lane is free when the reset push is done. It does not wait for the reset deploy:
   the next run builds its own merge commit from work, so a slow reset cannot hold the
   lane. A new work commit also resets an idle preview branch, so the site mirrors work.

Lane state: `~/.local/state/swarm-flow/preview.json`
{pr, head, work, merge, app, step, started, asked}. One run at a time. A crash leaves the
state, and the next run continues from the saved step.

## The preview block (written by the coder)

A UI PR describes how to see and check its feature. This is a fenced block in the PR
body, so it is visible to the reviewer too:

    ```preview
    open /path/in/the/app
    click text=Add item
    type #name "demo"
    press Enter
    expect text=demo visible
    shot after-add
    ```

Verbs: `open PATH`, `click SELECTOR`, `type SELECTOR TEXT`, `press KEY`,
`wait SELECTOR`, `expect SELECTOR visible|hidden|text "..."`, `shot NAME`. Selectors are
Playwright selectors. Each step has a 15 s limit. At least one `expect` and one `shot`
are required.

- A UI PR without a preview block fails the gate with a clear note. Before the gate is
  live, the coder brief and the gate relays get one new line about the block, like
  gatechecks.SHOTS_RULE.
- Local Vite-harness screenshots are fine during In Progress. They no longer count as
  review evidence. hr-gate 7a will need a preview pass for the current head (later step;
  7a is not changed now).

## Rules kept

- Staging -> Human Review: no change, no wait.
- No GraphQL in the lane loop. It reads the shared board snapshot. The comment, the body
  edit and the card move use one request each.
- The lane never touches work or main. It never commits PR code. The preview branches
  hold only the work tip plus one merge commit, and are reset after each run.
- A rate limit stops the run with exit 75 and keeps the state, as in the cascade.

## Build order (after cornerbot delivers item 1 to 4)

1. `tangentswarm/preview.py`: the preview block parser, the lane state, the steps. A
   test of each step with fakes, and one test with a real git scratch repo for the merge.
2. `swarm-preview [--dry-run] [PR]`: one step, its own lock, exit 75 on a rate limit.
3. The minbot step: a UI PR goes to the preview queue instead of Ready.
4. Daemon: start `swarm-preview` when the queue is not empty and the lane is free.
5. Live proof on one real UI PR, end to end, while the Staging -> Human Review path is
   watched for any delay.
6. Then (separate change) hr-gate 7a asks for the preview pass.

## Needs from cornerbot

The swarm does not create hosts, nginx sites, DNS, certificates, users or units. It needs
these, one row per app (ptw first, because #476 was a ptw PR; then work and minavo):

1. **A preview site per app.** The same app build as that app's work staging site, from
   branch `preview-<app>` of the platform repo. Its own checkout, docroot, user and API
   unit, as a new row in `redeploy.sh` (for example `preview-ptw`). cornerbot picks
   the host names and tells swarmbot.
2. **A deploy that a push starts, with no person in the loop.** The deploy hook deploys
   `preview-<app>` on a push to `refs/heads/preview-<app>`. The branch is force-pushed
   (reset to work after each run), so the checkout must follow it with
   `git fetch` + `git reset --hard origin/preview-<app>`, not `git pull --ff-only`. The
   deploy writes `.deploy-sha` the same way as the staging sites. The swarm must be able
   to read it (an https URL is enough).
3. **Its own data.** The preview API must not write to the work staging database. A
   copy of the work data, or an empty seeded database, is enough. Please say which one.
   It is fine if the data resets when the branch resets.
4. **A login for automation.** One test account on each preview site, and its
   credentials in the same place as the staging logins (an mna profile
   `<app>-preview`), so that Playwright and the smoke drive can sign in. No credentials
   go into the repo or the PR.
5. **The same smoke target.** The preview site answers the same smoke checks as staging
   (`/` redirects to `/login`, and `/login` answers 200).
6. **No search indexing**, and access the same as the staging sites.

When item 1 to 4 are live for one app, the swarm side starts with build step 1.
