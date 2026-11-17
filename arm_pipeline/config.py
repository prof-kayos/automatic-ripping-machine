"""Runtime configuration, read from the environment (never from git). All secrets come from env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # --- paths as seen by THIS machine
    movies_dir: str = "/mnt/movies"
    scratch_dir: str = "/scratch"
    state_dir: str = "~/.arm-pipeline"
    canary_name: str = ".arm-canary"            # must exist in movies_dir; proves the share is the real mount
    # --- drive
    sr_dev: str = "/dev/sr0"
    sg_dev: str = "/dev/sg1"
    # --- tools
    makemkvcon: str = "makemkvcon"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    # --- notifications / approvals
    slack_url: str = field(default="", repr=False)                          # incoming webhook: notifications
    slack_bot_token: str = field(default="", repr=False)                    # optional: enables approval by reaction
    slack_channel_id: str = ""
    slack_owner_id: str = ""                     # only this user's reaction counts
    # --- media servers (paths are as THE SERVER sees them, which can differ from movies_dir)
    jellyfin_url: str = ""
    jellyfin_token: str = field(default="", repr=False)
    jellyfin_movies_path: str = "/mnt/nas/movies"
    plex_url: str = ""
    plex_token: str = field(default="", repr=False)
    plex_section_id: str = "1"
    plex_movies_path: str = "/mnt/nas/movies"
    # --- metadata
    tmdb_api_key: str = field(default="", repr=False)
    # --- behaviour
    auto_replace: bool = False                   # False = a replacement always waits for approval
    verify_full_decode: bool = False             # True = also run the slow full decode every time
    sample_windows: int = 12                     # sampled-decode windows spread across the file
    sample_seconds: int = 20                     # length of each sampled window
    approval_timeout_days: int = 7
    poll_seconds: int = 15
    scan_timeout_s: int = 900
    server_wait_s: int = 300                     # how long to wait for Plex/Jellyfin to reflect a change
    # --- dashboard (web UI); the token is required for the dashboard to start
    dashboard_token: str = field(default="", repr=False)
    dashboard_bind: str = "127.0.0.1"
    dashboard_allowed_hosts: str = ""            # comma list of Host names; default 127.0.0.1, localhost, bind IP
    dashboard_allow_any_bind: bool = False       # needed to bind 0.0.0.0 / ::
    dashboard_allow_public: bool = False         # accept clients outside loopback/private/link-local ranges
    dashboard_allowed_clients: str = ""          # optional comma list of client IPs/CIDRs; when set ONLY these (+ loopback) may connect
    dashboard_port: int = 8088
    dashboard_secure_cookie: bool = False        # True when served behind HTTPS

    @property
    def state_path(self) -> str:
        return os.path.expanduser(self.state_dir)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        e = os.environ if env is None else env

        def g(name, default=""):
            return e.get(name, default) or default

        def i(name, default):
            try:
                return int(e.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            movies_dir=g("ARM_MOVIES_DIR", cls.movies_dir), scratch_dir=g("ARM_SCRATCH_DIR", cls.scratch_dir),
            state_dir=g("ARM_STATE_DIR", cls.state_dir), canary_name=g("ARM_CANARY", cls.canary_name),
            sr_dev=g("ARM_SR_DEV", cls.sr_dev), sg_dev=g("ARM_SG_DEV", cls.sg_dev),
            makemkvcon=g("ARM_MAKEMKVCON", cls.makemkvcon), ffmpeg=g("ARM_FFMPEG", cls.ffmpeg),
            ffprobe=g("ARM_FFPROBE", cls.ffprobe),
            slack_url=g("SLACK_URL"), slack_bot_token=g("SLACK_BOT_TOKEN"),
            slack_channel_id=g("SLACK_CHANNEL_ID"), slack_owner_id=g("SLACK_OWNER_ID"),
            jellyfin_url=g("JELLYFIN_URL"), jellyfin_token=g("JELLYFIN_TOKEN"),
            jellyfin_movies_path=g("ARM_JELLYFIN_MOVIES_PATH", cls.jellyfin_movies_path),
            plex_url=g("PLEX_URL"), plex_token=g("PLEX_TOKEN"), plex_section_id=g("PLEX_SECTION_ID", "1"),
            plex_movies_path=g("ARM_PLEX_MOVIES_PATH", cls.plex_movies_path),
            tmdb_api_key=g("TMDB_API_KEY"),
            auto_replace=_b(e.get("ARM_AUTO_REPLACE")), verify_full_decode=_b(e.get("ARM_VERIFY_FULL_DECODE")),
            sample_windows=i("ARM_SAMPLE_WINDOWS", 12), sample_seconds=i("ARM_SAMPLE_SECONDS", 20),
            approval_timeout_days=i("ARM_APPROVAL_TIMEOUT_DAYS", 7), poll_seconds=i("ARM_POLL_SECONDS", 15),
            scan_timeout_s=i("ARM_SCAN_TIMEOUT_S", 900), server_wait_s=i("ARM_SERVER_WAIT_S", 300),
            dashboard_token=g("DASHBOARD_TOKEN"), dashboard_bind=g("ARM_DASHBOARD_BIND", "127.0.0.1"),
            dashboard_allowed_hosts=g("ARM_DASHBOARD_ALLOWED_HOSTS"),
            dashboard_allow_any_bind=_b(e.get("ARM_DASHBOARD_ALLOW_ANY_BIND")),
            dashboard_allow_public=_b(e.get("ARM_DASHBOARD_ALLOW_PUBLIC")),
            dashboard_allowed_clients=g("ARM_DASHBOARD_ALLOWED_CLIENTS"),
            dashboard_port=i("ARM_DASHBOARD_PORT", 8088),
            dashboard_secure_cookie=_b(e.get("ARM_DASHBOARD_SECURE_COOKIE")),
        )
