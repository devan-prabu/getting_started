"""ALERT — push unsent awards to Telegram and/or email.

Two channels ship in Phase 0 because both were asked for (DECISIONS.md D-02).
`alerts_sent` has a UNIQUE(award_id, channel) constraint, so an award is alerted
exactly once per channel, ever (brief §6.7).

`--dry-run` prints to stdout and records nothing, so previewing an alert never
burns it.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from . import db
from .models import AlertPayload

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_CHANNELS = "telegram,email"

# Legacy Telegram Markdown breaks on unbalanced markup inside interpolated
# values, so strip the characters rather than trying to escape them.
_MD_UNSAFE = re.compile(r"[*_`\[\]]")


class AlertError(Exception):
    """The channel was configured but the send failed."""


def _md(value: str | None) -> str:
    return _MD_UNSAFE.sub("", value or "").strip()


def format_value(value_aed: float | None) -> str:
    """Undisclosed is a real answer and is shown as such (brief §13.7)."""
    if value_aed is None:
        return "undisclosed"
    if value_aed >= 1_000_000_000:
        return f"AED {value_aed / 1_000_000_000:.2f}bn".replace(".00bn", "bn")
    if value_aed >= 1_000_000:
        return f"AED {value_aed / 1_000_000:.0f}m"
    return f"AED {value_aed:,.0f}"


def row_to_payload(row: sqlite3.Row) -> AlertPayload:
    return AlertPayload(
        award_id=int(row["id"]),
        title=row["source_title"] or row["project_name"] or "Untitled item",
        contractor=row["contractor_canonical"] or row["contractor_raw"],
        developer=row["developer"],
        project_name=row["project_name"],
        value_aed=row["value_aed"],
        emirate=row["emirate"],
        sector=row["sector"],
        award_date=row["award_date"],
        published_at=row["source_published_at"],
        source_url=row["source_url"],
        hiring_window_start=row["hiring_window_start"],
        hiring_window_end=row["hiring_window_end"],
        relevance_score=row["relevance_score"],
        confidence=row["confidence"],
    )


def render_markdown(p: AlertPayload) -> str:
    """The message body. Facts and a link — never paragraphs of the article."""
    headline = _md(p.contractor) or "Unidentified contractor"
    lines = [f"🏗 *{headline}* — possible contract award", ""]

    lines.append(f"*Headline:* {_md(p.title)}")
    if p.project_name:
        lines.append(f"*Project:* {_md(p.project_name)}")
    if p.developer:
        lines.append(f"*Client:* {_md(p.developer)}")
    lines.append(f"*Value:* {format_value(p.value_aed)}")

    where = " · ".join(x for x in [_md(p.sector), _md(p.emirate)] if x and x != "Unknown")
    if where:
        lines.append(f"*Sector:* {where}")
    when = p.award_date or (p.published_at[:10] if p.published_at else None)
    if when:
        label = "Awarded" if p.award_date else "Reported"
        lines.append(f"*{label}:* {when}")

    if p.hiring_window_start and p.hiring_window_end:
        lines += [
            "",
            f"📈 *Likely mobilisation hiring: {p.hiring_window_start} → {p.hiring_window_end}*",
        ]
    if p.careers_url:
        lines.append(f"🔗 Careers: {p.careers_url}")
    lines.append(f"📰 Source: {p.source_url}")

    footer = []
    if p.relevance_score is not None:
        footer.append(f"Relevance {p.relevance_score}/100")
    if p.confidence is not None:
        footer.append(f"confidence {p.confidence:.2f}")
    if footer:
        lines += ["", f"_{' · '.join(footer)}_"]

    if p.confidence is not None and p.confidence < 0.5:
        lines.append("_Phase 0: contractor guessed by regex — check the source._")

    return "\n".join(lines)


def render_plaintext(p: AlertPayload) -> str:
    return re.sub(r"[*_]", "", render_markdown(p))


class Channel(Protocol):
    name: str

    def configured(self) -> bool: ...

    def send(self, payload: AlertPayload) -> None: ...


@dataclass
class StdoutChannel:
    """Dry-run output. Never records to alerts_sent."""

    name: str = "stdout"

    def configured(self) -> bool:
        return True

    def send(self, payload: AlertPayload) -> None:
        print(render_plaintext(payload))
        print("-" * 72)


@dataclass
class TelegramChannel:
    token: str | None = None
    chat_id: str | None = None
    client: httpx.Client | None = None
    name: str = "telegram"

    def __post_init__(self) -> None:
        self.token = self.token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = self.chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, payload: AlertPayload) -> None:
        if not self.configured():
            raise AlertError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        client = self.client or httpx.Client(timeout=20.0)
        try:
            resp = client.post(
                f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": render_markdown(payload),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
            )
            if resp.status_code != 200:
                raise AlertError(f"Telegram returned {resp.status_code}: {resp.text[:300]}")
        except httpx.HTTPError as exc:
            raise AlertError(f"Telegram request failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()


@dataclass
class EmailChannel:
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    sender: str | None = None
    recipient: str | None = None
    # Injection point for tests; production leaves it None and uses smtplib.
    smtp_factory: Callable[[], Any] | None = None
    name: str = "email"

    def __post_init__(self) -> None:
        env = os.environ.get
        self.host = self.host or env("SMTP_HOST", "").strip()
        self.port = self.port or int(env("SMTP_PORT", "587") or 587)
        self.user = self.user or env("SMTP_USER", "").strip()
        self.password = self.password or env("SMTP_PASSWORD", "").strip()
        self.sender = self.sender or env("EMAIL_FROM", "").strip() or self.user
        self.recipient = self.recipient or env("EMAIL_TO", "").strip()

    def configured(self) -> bool:
        return bool(self.host and self.sender and self.recipient)

    def build_message(self, payload: AlertPayload) -> EmailMessage:
        msg = EmailMessage()
        who = payload.contractor or "Unidentified contractor"
        msg["Subject"] = f"[Radar] {who} — possible contract award"
        msg["From"] = self.sender or ""
        msg["To"] = self.recipient or ""
        msg.set_content(render_plaintext(payload))
        return msg

    def send(self, payload: AlertPayload) -> None:
        if not self.configured():
            raise AlertError("SMTP_HOST, EMAIL_FROM or EMAIL_TO is not set")
        msg = self.build_message(payload)
        try:
            if self.smtp_factory is not None:
                smtp = self.smtp_factory()
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=20)
                smtp.starttls()
                if self.user and self.password:
                    smtp.login(self.user, self.password)
            with smtp:
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise AlertError(f"SMTP send failed: {exc}") from exc


def channels_from_env() -> list[Channel]:
    names = [
        n.strip().lower()
        for n in os.environ.get("ALERT_CHANNELS", DEFAULT_CHANNELS).split(",")
        if n.strip()
    ]
    built: list[Channel] = []
    for name in names:
        if name == "telegram":
            built.append(TelegramChannel())
        elif name == "email":
            built.append(EmailChannel())
        else:
            log.warning("unknown alert channel %r in ALERT_CHANNELS — ignoring", name)
    return built


@dataclass
class AlertStats:
    sent: int = 0
    skipped_already_sent: int = 0
    failed: int = 0
    unconfigured: list[str] = field(default_factory=list)

    def as_line(self) -> str:
        return f"sent={self.sent} already_sent={self.skipped_already_sent} failed={self.failed}"


def send_alerts(
    conn: sqlite3.Connection,
    channels: list[Channel],
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> AlertStats:
    """Send every unsent award on every configured channel.

    In dry-run nothing is recorded, so the same preview can be run repeatedly.
    """
    stats = AlertStats()
    if dry_run:
        channels = [StdoutChannel()]

    for channel in channels:
        if not channel.configured():
            log.warning("channel %s is not configured — skipping", channel.name)
            stats.unconfigured.append(channel.name)
            continue

        pending = db.unalerted_awards(conn, channel.name)
        if limit is not None:
            pending = pending[:limit]

        for row in pending:
            payload = row_to_payload(row)
            try:
                channel.send(payload)
            except AlertError as exc:
                stats.failed += 1
                log.error(
                    "alert failed on %s for award %s: %s", channel.name, payload.award_id, exc
                )
                continue

            if dry_run:
                stats.sent += 1
                continue

            # Recorded only after a successful send, so a failure re-alerts next
            # run rather than silently dropping the award.
            if db.mark_alert_sent(conn, payload.award_id, channel.name):
                stats.sent += 1
            else:
                stats.skipped_already_sent += 1

    log.info("alert: %s", stats.as_line())
    return stats
