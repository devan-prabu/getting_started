"""ENRICH — fetch each article and pull clean text with trafilatura.

Only the extracted text is stored, and only so the keyword gate and the Phase 2
extractor have something to work on. Alerts always link to the source rather
than reproducing it (brief §9.6).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import trafilatura

from . import db
from .fetcher import Fetcher, RobotsDisallowed
from .models import ItemStatus

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
# Paywall stubs and JS-only pages still yield a word or two ("Subscribe"), which
# is worse than nothing: it looks like a successful enrich while carrying no
# signal. Anything this short is treated as no text at all.
MIN_TEXT_CHARS = 200


@dataclass
class EnrichStats:
    attempted: int = 0
    enriched: int = 0
    empty: int = 0
    skipped_robots: int = 0
    failed: int = 0

    @property
    def coverage(self) -> float:
        return self.enriched / self.attempted if self.attempted else 0.0

    def as_line(self) -> str:
        return (
            f"attempted={self.attempted} enriched={self.enriched} "
            f"empty={self.empty} robots_skipped={self.skipped_robots} "
            f"failed={self.failed} coverage={self.coverage:.0%}"
        )


def extract_text(html: str, url: str | None = None) -> str | None:
    """trafilatura's main-content extraction. None when there is nothing usable."""
    if not html or not html.strip():
        return None
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text:
        return None
    text = text.strip()
    if len(text) < MIN_TEXT_CHARS:
        return None
    return text[:MAX_TEXT_CHARS]


def enrich_item(
    conn: sqlite3.Connection, item: sqlite3.Row, fetcher: Fetcher, stats: EnrichStats
) -> None:
    stats.attempted += 1
    url = item["url"]
    try:
        result = fetcher.get(url)
    except RobotsDisallowed:
        # A disallowed article is not a failure of ours; retitle it so it is not
        # retried forever, and let the title-only keyword gate still see it.
        stats.skipped_robots += 1
        db.update_item(conn, item["id"], status=ItemStatus.ENRICHED)
        log.info("robots disallows %s; keeping title only", url)
        return
    except Exception as exc:  # noqa: BLE001 - a dead host must not stall the run
        stats.failed += 1
        db.update_item(conn, item["id"], status=ItemStatus.FAILED)
        log.warning("enrich failed for %s: %s", url, exc)
        return

    if result.status_code != 200:
        stats.failed += 1
        db.update_item(conn, item["id"], status=ItemStatus.FAILED)
        log.warning("enrich got HTTP %s for %s", result.status_code, url)
        return

    text = extract_text(result.text, url=result.url)
    if text:
        stats.enriched += 1
        db.update_item(conn, item["id"], raw_text=text, status=ItemStatus.ENRICHED)
    else:
        # Paywall stub, JS-only page, or a video item. The title still carries
        # signal, so keep the row and move on.
        stats.empty += 1
        db.update_item(conn, item["id"], status=ItemStatus.ENRICHED)
        log.info("no extractable text at %s", url)


def enrich_new(
    conn: sqlite3.Connection, fetcher: Fetcher, *, limit: int | None = None
) -> EnrichStats:
    stats = EnrichStats()
    for item in db.get_items_by_status(conn, ItemStatus.NEW, limit=limit):
        enrich_item(conn, item, fetcher, stats)
    return stats
