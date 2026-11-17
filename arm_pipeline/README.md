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

## CLI
`python3 -m arm_pipeline status | approve FP | reject FP | identify FP "Title (Year)" | retry FP | skip FP`

## Deploy (Linux, systemd)
1. Copy `arm_policy/` and `arm_pipeline/` to `/opt/arm/` (keep the previous copy as `/opt/arm.prev` for rollback).
2. `/etc/arm-pipeline/secrets.env` (mode 600) and `/etc/arm-pipeline/arm-pipeline.conf` from the `deploy/*.example` files.
3. `deploy/arm-pipeline.service` -> `/etc/systemd/system/`; `systemctl daemon-reload && systemctl enable --now arm-pipeline`.
4. Needs: `makemkvcon` (registered), `ffmpeg`/`ffprobe`, `sg3-utils` (eject inside an unprivileged container uses the sg node),
   a canary file `.arm-canary` in the movies directory (proves the share is the real mount).
Rollback: `systemctl disable --now arm-pipeline`; check no `*.arm-old` files remain on the share; swap `/opt/arm.prev` back.

Tests: `python3 -m unittest discover -s test/unittest/pipeline -t test/unittest/pipeline` (needs ffmpeg for the end-to-end tests).
