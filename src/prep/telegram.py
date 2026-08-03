"""Telegram Bot API client — send, poll and parse.

A plain httpx POST, no SDK, matching the choice already made for the radar
alerts (DECISIONS.md D-02). Two directions matter here that did not for the
radar: this bot is also *read*, because your answers arrive as replies.

Long polling is not used. `get_updates` takes an offset from the database and
returns whatever is waiting, so a CI job can poll and exit without holding a
connection open (DECISIONS.md D-19).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import Command

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_TIMEOUT = 20.0
# Telegram rejects anything longer; the drill is never near this, but a long
# graded feedback plus a quoted answer can be.
MAX_MESSAGE = 4096

_COMMAND = re.compile(r"^/(?P<name>[a-zA-Z_]+)(?:@\w+)?\s*(?P<arg>.*)$", re.DOTALL)
_MD_UNSAFE = re.compile(r"[*_`\[\]]")


class TelegramError(Exception):
    """The bot is configured but the API call failed."""


def escape(value: str | None) -> str:
    """Strip legacy-Markdown control characters rather than escaping them."""
    return _MD_UNSAFE.sub("", value or "").strip()


def parse_command(text: str) -> Command | None:
    """`/goal move to Parsons by October` -> Command(goal, 'move to ...')."""
    match = _COMMAND.match(text.strip())
    if not match:
        return None
    return Command(name=match.group("name").lower(), argument=match.group("arg").strip())


@dataclass
class Update:
    """One inbound message, flattened to what this agent cares about."""

    update_id: int
    text: str
    message_id: int | None = None
    reply_to_message_id: int | None = None
    chat_id: str | None = None

    @property
    def command(self) -> Command | None:
        cmd = parse_command(self.text)
        if cmd is not None:
            cmd.message_id = self.message_id
        return cmd


@dataclass
class TelegramClient:
    token: str | None = None
    chat_id: str | None = None
    client: httpx.Client | None = None
    name: str = "telegram"
    _owned: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.token = self.token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = self.chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=DEFAULT_TIMEOUT)
            self._owned = True
        return self.client

    def close(self) -> None:
        if self.client is not None and self._owned:
            self.client.close()
            self.client = None
            self._owned = False

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            raise TelegramError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        try:
            resp = self._http().post(f"{TELEGRAM_API}/bot{self.token}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} request failed: {exc}") from exc
        if resp.status_code != 200:
            raise TelegramError(f"{method} returned {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON: {resp.text[:200]}") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method} not ok: {str(body)[:300]}")
        return body

    def send(self, text: str, *, markdown: bool = True) -> int | None:
        """Send a message; returns the Telegram message_id.

        The id is stored against the question so a reply can be attached to the
        exact question it answers rather than to whatever was most recent.
        """
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE],
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"
        body = self._call("sendMessage", payload)
        result = body.get("result") or {}
        message_id = result.get("message_id")
        return int(message_id) if message_id is not None else None

    def get_updates(self, offset: int | None = None, limit: int = 50) -> list[Update]:
        """Fetch waiting messages. `offset` is the last seen update_id + 1."""
        payload: dict[str, Any] = {"limit": limit, "timeout": 0}
        if offset is not None:
            payload["offset"] = offset
        body = self._call("getUpdates", payload)

        updates: list[Update] = []
        for raw in body.get("result") or []:
            message = raw.get("message") or raw.get("edited_message") or {}
            text = (message.get("text") or message.get("caption") or "").strip()
            if not text:
                continue
            chat = str((message.get("chat") or {}).get("id", ""))
            # A group the bot was added to is not your drill.
            if self.chat_id and chat and chat != str(self.chat_id):
                log.debug("ignoring update from chat %s", chat)
                continue
            reply_to = (message.get("reply_to_message") or {}).get("message_id")
            updates.append(
                Update(
                    update_id=int(raw["update_id"]),
                    text=text,
                    message_id=message.get("message_id"),
                    reply_to_message_id=int(reply_to) if reply_to is not None else None,
                    chat_id=chat or None,
                )
            )
        return updates


@dataclass
class StdoutClient:
    """Dry-run stand-in. Prints, records nothing, never reads."""

    name: str = "stdout"
    sent: list[str] = field(default_factory=list)

    def configured(self) -> bool:
        return True

    def send(self, text: str, *, markdown: bool = True) -> int | None:
        self.sent.append(text)
        print(text)
        print("-" * 72)
        return None

    def get_updates(self, offset: int | None = None, limit: int = 50) -> list[Update]:
        return []

    def close(self) -> None:
        return None

    def __enter__(self) -> StdoutClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
