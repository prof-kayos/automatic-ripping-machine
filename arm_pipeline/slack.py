"""Slack notifications (incoming webhook) and owner-only approval by emoji reaction."""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("arm_pipeline.slack")

TIMEOUT_S = 15
MAX_TEXT = 3000


def escape(text: str) -> str:
    """Slack mrkdwn control characters: a hostile disc label or title must not ping @channel or inject links."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
APPROVE = {"white_check_mark", "+1", "heavy_check_mark"}
REJECT = {"x", "no_entry_sign", "-1"}


@dataclass
class Ticket:
    channel: str
    ts: str


class Notifier:
    def __init__(self, cfg, opener=None, api_base: str = "https://slack.com/api",
                 sleep=time.sleep, backoff_s: float = 1.0):
        self.cfg = cfg
        self.opener = opener or urllib.request.build_opener()
        self.api_base = api_base.rstrip("/")
        self._sleep = sleep
        self._backoff = backoff_s

    # ---- webhook notifications -------------------------------------------------
    def post(self, text: str) -> bool:
        """Send a notification. Never raises; returns True on success. Never logs the URL."""
        text = escape(text)[:MAX_TEXT]
        if not self.cfg.slack_url:
            log.info("slack not configured; message: %s", text)
            return False
        body = json.dumps({"text": text}).encode()
        for attempt in range(3):                      # first try + 2 retries
            try:
                req = urllib.request.Request(self.cfg.slack_url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                with self.opener.open(req, timeout=TIMEOUT_S) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except Exception as e:                    # deliberately broad: notifying must not break the pipeline
                # type name only: exception text from urllib can embed the URL
                log.warning("slack webhook attempt %d failed (%s)", attempt + 1, type(e).__name__)
            if attempt < 2:
                self._sleep(self._backoff * (attempt + 1))
        return False

    # ---- approval by reaction --------------------------------------------------
    def _api(self, method: str, payload: dict | None = None, query: dict | None = None) -> dict:
        """Call a Slack Web API method; returns parsed JSON (check 'ok'). Raises on transport errors."""
        url = f"{self.api_base}/{method}"
        headers = {"Authorization": f"Bearer {self.cfg.slack_bot_token}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        with self.opener.open(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())

    def ask_approval(self, text: str) -> Ticket | None:
        """Post a message the owner can react to. None if the bot is not configured or the call fails."""
        if not (self.cfg.slack_bot_token and self.cfg.slack_channel_id):
            return None
        try:
            r = self._api("chat.postMessage", {"channel": self.cfg.slack_channel_id, "text": escape(text)[:MAX_TEXT]})
        except Exception as e:
            log.warning("ask_approval failed (%s)", type(e).__name__)
            return None
        if not r.get("ok") or not r.get("ts"):
            log.warning("ask_approval rejected by Slack: %s", r.get("error"))
            return None
        return Ticket(channel=r.get("channel") or self.cfg.slack_channel_id, ts=r["ts"])

    def poll_approval(self, ticket: Ticket) -> str:
        """'approved' | 'rejected' | 'pending'. Only the owner's reactions count."""
        owner = self.cfg.slack_owner_id
        if not owner:
            log.warning("slack_owner_id not set: refusing to approve anything")
            return "pending"
        try:
            r = self._api("reactions.get", query={"channel": ticket.channel, "timestamp": ticket.ts, "full": "true"})
        except Exception as e:
            log.warning("poll_approval failed (%s)", type(e).__name__)
            return "pending"
        if not r.get("ok"):
            return "pending"
        mine = set()
        for reaction in (r.get("message") or {}).get("reactions", []):
            if owner in reaction.get("users", []):
                mine.add(reaction.get("name", "").split("::")[0])   # strip skin-tone suffix
        if mine & REJECT:          # rejection wins if the owner somehow left both
            return "rejected"
        if mine & APPROVE:
            return "approved"
        return "pending"
