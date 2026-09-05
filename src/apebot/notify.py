"""Notifications: Telegram (optional) + log."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str | None, chat_id: str | None, enabled: bool = True):
        self.enabled = bool(enabled and bot_token and chat_id)
        self.token = bot_token
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=10) if self.enabled else None

    def send(self, text: str, level: str = "info") -> None:
        getattr(log, level if level in ("info", "warning", "error") else "info")("NOTIFY: %s", text)
        if not self.enabled:
            return
        try:
            self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})
        except Exception as e:  # never let notifications break trading
            log.warning("telegram send failed: %s", e)
