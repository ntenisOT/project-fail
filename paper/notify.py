"""Best-effort Telegram push for the paper trader.

Conventions borrowed from project-magic (not its code): secrets are ENV-ONLY,
sends are access-controlled to explicit chat ids, and a Telegram failure is
swallowed with a WARNING — it must NEVER crash or block the trading loop.
KISS: a direct Bot API call (no heavy python-telegram-bot dependency).

Env:
  TELEGRAM_BOT_TOKEN         bot token (required to actually send)
  TELEGRAM_PUSH_CHAT_IDS     comma-separated chat ids to push to
  TELEGRAM_ALLOWED_USER_IDS  fallback chat ids if PUSH ids unset
If unconfigured, messages are logged instead of sent — so the paper trader runs
fine with Telegram off (visibility without setup).
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("paper.notify")


def _chat_ids() -> list[str]:
    raw = os.environ.get("TELEGRAM_PUSH_CHAT_IDS") or os.environ.get("TELEGRAM_ALLOWED_USER_IDS") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


class Notifier:
    """Fire-and-forget Telegram sender; degrades to logging when unconfigured."""

    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chats = _chat_ids()
        self.enabled = bool(self.token and self.chats)
        if not self.enabled:
            log.info("telegram push disabled (set TELEGRAM_BOT_TOKEN + chat ids to enable); logging only")

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram-off] %s", text)
            return
        for chat in self.chats:
            try:
                data = urllib.parse.urlencode(
                    {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
                ).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{self.token}/sendMessage", data=data
                )
                urllib.request.urlopen(req, timeout=8).read()
            except Exception as e:  # best-effort: never propagate
                log.warning("telegram send failed (%s): %s", chat, e.__class__.__name__)


# module-level convenience singleton
_default: Notifier | None = None


def notifier() -> Notifier:
    global _default
    if _default is None:
        _default = Notifier()
    return _default
