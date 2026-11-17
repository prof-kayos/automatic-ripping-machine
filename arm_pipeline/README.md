# arm_pipeline - hands-off disc -> media library

Standard-library Python that wraps MakeMKV. Insert a disc and it: scans -> identifies the movie (TMDb) -> decides with
`arm_policy` (new / replace-if-better / skip / ask) -> rips the main feature (English + original language only) to scratch
-> verifies (tiered, fast) -> copies to the share as `*.mkv.part` with a checksum read-back -> places or replaces ->
tells Plex and Jellyfin -> posts to Slack -> ejects.

## Safety rules (enforced in code and tests)
* The library is never touched until the new file is verified and staged on the same share as `.part` (ignored by servers).
* Replace order: approval -> hide old (`.arm-old`) -> scan both servers, wait until both dropped it -> rename new into place
  -> scan again (shows as NEW in "Recently Added") -> delete hidden old. File renames only, never directories.
* A crash mid-replace is rolled back on the next start (the hidden old file is renamed back) and reported.
* A replacement waits for approval (CLI or Slack reaction by one owner id) unless `ARM_AUTO_REPLACE=1` and the match is exact.
* A handled disc is never re-ripped (ledger); a restart never auto-resumes a half-done job.
* The disc is ejected when its data is safely off it, or skipped/failed; a disc needing a human answer stays in.

## Verification tiers (why a 4K rip does not take twice as long)
Always: MakeMKV log scan, ffprobe sanity (duration/streams/size), demux integrity pass and a sampled decode (12 x 20 s
windows) run concurrently with the share copy. Full decode only if something looks suspicious or `ARM_VERIFY_FULL_DECODE=1`.

## Dashboard (monitor and fix problems from a browser)
`python3 -m arm_pipeline.dashboard` (unit: `deploy/arm-dashboard.service`) serves a single-page UI + JSON API, stdlib only.
* **Needs attention**: rule-based issues with one-click fixes (a disc waiting for a title gets a TMDb search + Resume; failed jobs
  Retry/Dismiss; replacements Approve/Reject; duplicate records Dismiss; service/share/drive/scratch/integration problems).
* **Now processing**: live phase, percent, rate, ETA and GB from `progress.json` (the daemon writes it while ripping/copying).
* **Activity** feed (every Slack message and operator action, from `events.jsonl`) and **History** with a per-disc detail drawer.
* Security model: one shared token (`DASHBOARD_TOKEN`, >= 32 chars, required), HttpOnly SameSite=Strict session cookie, per-session
  CSRF header + required Origin on every POST, Host allowlist (anti DNS-rebinding), private-network clients only, loopback bind by
  default, connection caps and timeouts, strict CSP (no inline script/style), all text rendered with `textContent`, every action
  audited as an event, secrets never returned or logged. It is for a trusted LAN over plain HTTP; put it behind TLS + SSO before any
  wider exposure (set `ARM_DASHBOARD_SECURE_COOKIE=1` and the proxy host in `ARM_DASHBOARD_ALLOWED_HOSTS`).
* Optional SSO (`ARM_DASHBOARD_SSO=1`, off by default; see `deploy/arm-pipeline.conf.example`): when the peer address is exactly
  `ARM_DASHBOARD_TRUSTED_PROXY` and `X-authentik-username` is in `ARM_DASHBOARD_SSO_USERS` (or `X-authentik-groups` contains a group in
  `ARM_DASHBOARD_SSO_GROUPS`), `/api/session` issues the normal session cookie + CSRF token, so every POST protection still applies.
  The proxy must keep vouching for the same user on every request or the session is dropped. Logout leaves a "Sign in with SSO" button.
  Token login remains as a fallback. Group membership is an exact match against the allowed groups; malformed or over-long segments in the
  header are skipped (and logged once), not fatal.
  **Deployment warning:** SSO trust is only as strong as "every proxy route to this backend carries the forward-auth middleware". A second
  route without it lets clients forge the `X-authentik-*` headers through the trusted proxy address.
* Preview with fake data: `python3 tools/dashboard_mock.py --port 8099` (token `demo`; `?scenario=attention|busy|down|clear`).
* The daemon does not trust the dashboard: identity answers and approvals are command files that the daemon re-validates (regular
  file, owner, size, strict title/year rules, state check) before acting.

## CLI
From the install directory with the env loaded (`cd /opt/arm && set -a && . ~/.env && set +a`; a bare `python3 -m arm_pipeline` elsewhere fails with "No module named arm_pipeline"): `python3 -m arm_pipeline status | approve FP | reject FP | identify FP "Title (Year)" | retry FP | skip FP`. `retry` also undoes a skip (then re-insert the disc); the dashboard's job drawer has the same button.

## Deploy (Linux, systemd)
1. Copy `arm_policy/` and `arm_pipeline/` to `/opt/arm/` (keep the previous copy as `/opt/arm.prev` for rollback).
2. `/etc/arm-pipeline/secrets.env` (mode 600) and `/etc/arm-pipeline/arm-pipeline.conf` from the `deploy/*.example` files.
3. `deploy/arm-pipeline.service` -> `/etc/systemd/system/`; `systemctl daemon-reload && systemctl enable --now arm-pipeline`.
4. Needs: `makemkvcon` (registered), `ffmpeg`/`ffprobe`, `sg3-utils` (eject inside an unprivileged container uses the sg node),
   a canary file `.arm-canary` in the movies directory (proves the share is the real mount).
Rollback: `systemctl disable --now arm-pipeline`; check no `*.arm-old` files remain on the share; swap `/opt/arm.prev` back.

Tests: `python3 -m unittest discover -s test/unittest/pipeline -t test/unittest/pipeline` (needs ffmpeg for the end-to-end tests).
