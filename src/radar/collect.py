"""COLLECT — pull feeds into raw_items.

Phase 0 handles Google News RSS and plain RSS. Nothing here interprets an
article; the job is only to get a deduped, durable row into raw_items so that a
failure further down the pipeline cannot lose it (brief §2).
"""

from __future__ import annotations

import calendar
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import feedparser

from . import db, dedupe
from .config import SourcesConfig
from .fetcher import Fetcher, RobotsDisallowed
from .models import ItemStatus, RawItem, Source

log = logging.getLogger(__name__)

# Following redirects costs one throttled request each, so cap it per run.
# Offline id decoding handles most items without any network at all.
DEFAULT_MAX_RESOLVE = 50


@dataclass
class CollectStats:
    fetched: int = 0
    inserted: int = 0
    duplicate_url: int = 0
    duplicate_title: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def as_line(self) -> str:
        return (
            f"fetched={self.fetched} inserted={self.inserted} "
            f"dup_url={self.duplicate_url} dup_title={self.duplicate_title} "
            f"errors={len(self.errors)}"
        )


def _published_iso(entry) -> str | None:
    """RFC 822 -> ISO 8601 UTC. feedparser has already done the parsing."""
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    try:
        return (
            datetime.fromtimestamp(calendar.timegm(tm), tz=UTC).replace(microsecond=0).isoformat()
        )
    except (ValueError, OverflowError, TypeError):
        return None


def parse_feed(text: str) -> list[dict]:
    """Parse feed XML into plain dicts. Kept separate so tests need no network."""
    parsed = feedparser.parse(text)
    # An HTML error page parses without complaint and yields zero entries, which
    # would otherwise be indistinguishable from a healthy but empty feed and
    # would leave a dead source looking fine in source_health. `version` is
    # empty only when the document is not a feed at all.
    if not parsed.get("version"):
        detail = parsed.get("bozo_exception") or "response is not a feed"
        raise ValueError(f"unparseable feed: {detail}")
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
    out = []
    for entry in parsed.entries:
        link = (getattr(entry, "link", "") or "").strip()
        if not link:
            continue
        out.append(
            {
                "url": link,
                "title": (getattr(entry, "title", "") or "").strip() or None,
                "published_at": _published_iso(entry),
            }
        )
    return out


def collect_source(
    conn: sqlite3.Connection,
    source: Source,
    cfg: SourcesConfig,
    fetcher: Fetcher,
    *,
    max_resolve: int = DEFAULT_MAX_RESOLVE,
    stats: CollectStats | None = None,
) -> CollectStats:
    """Fetch one source and store its new items."""
    stats = stats or CollectStats()
    url = cfg.build_url(source)

    try:
        result = fetcher.get(url)
    except RobotsDisallowed:
        msg = "robots.txt disallows this feed"
        log.warning("[%s] %s", source.id, msg)
        db.record_source_failure(conn, source.id, msg)
        stats.errors[source.id] = msg
        return stats
    except Exception as exc:  # noqa: BLE001 - one dead host must not stall the run
        msg = f"{type(exc).__name__}: {exc}"
        log.warning("[%s] fetch failed: %s", source.id, msg)
        db.record_source_failure(conn, source.id, msg)
        stats.errors[source.id] = msg
        return stats

    if result.status_code != 200:
        msg = f"HTTP {result.status_code}"
        log.warning("[%s] %s", source.id, msg)
        db.record_source_failure(conn, source.id, msg)
        stats.errors[source.id] = msg
        return stats

    try:
        entries = parse_feed(result.text)
    except ValueError as exc:
        msg = str(exc)
        log.warning("[%s] %s", source.id, msg)
        db.record_source_failure(conn, source.id, msg)
        stats.errors[source.id] = msg
        return stats

    inserted_here = 0
    resolved_count = 0
    seen_titles = db.recent_titles(conn, dedupe.window_start())

    for entry in entries:
        stats.fetched += 1
        raw_url = entry["url"]

        # Resolve before hashing, or the same article gets stored once per query
        # that surfaced it (brief §5.1).
        if dedupe.is_google_news_url(raw_url):
            decoded = dedupe.decode_google_news_url(raw_url)
            if decoded:
                final_url = decoded
            elif resolved_count < max_resolve:
                final_url = dedupe.resolve_url(raw_url, fetcher)
                resolved_count += 1
            else:
                log.info("[%s] resolve budget spent; keeping Google URL", source.id)
                final_url = raw_url
        else:
            final_url = raw_url

        h = dedupe.url_hash(final_url)
        if db.url_hash_exists(conn, h):
            stats.duplicate_url += 1
            continue

        title = entry["title"]
        dup_id = dedupe.find_duplicate_title(
            title or "", seen_titles, published_at=entry["published_at"]
        )
        if dup_id is not None:
            # Same story, different outlet. Keep the first one we saw; brief §13.2
            # says keep the earliest, and the stored row is by definition earlier.
            log.debug("[%s] title-duplicate of raw_item %s: %s", source.id, dup_id, title)
            stats.duplicate_title += 1
            continue

        item = RawItem(
            source_id=source.id,
            url=final_url,
            url_hash=h,
            title=title,
            published_at=entry["published_at"],
            status=ItemStatus.NEW,
        )
        new_id = db.insert_raw_item(conn, item)
        if new_id is None:
            stats.duplicate_url += 1
            continue
        stats.inserted += 1
        inserted_here += 1
        if title:
            seen_titles.append((new_id, title, item.published_at or item.fetched_at))

    db.record_source_success(conn, source.id, inserted_here)
    log.info("[%s] %s new items", source.id, inserted_here)
    return stats


def collect_all(
    conn: sqlite3.Connection,
    cfg: SourcesConfig,
    fetcher: Fetcher,
    *,
    source_id: str | None = None,
    max_resolve: int = DEFAULT_MAX_RESOLVE,
) -> CollectStats:
    stats = CollectStats()
    if source_id:
        source = cfg.get(source_id)
        if source is None:
            raise KeyError(f"no such source: {source_id}")
        sources = [source]
    else:
        sources = cfg.enabled()

    for source in sources:
        collect_source(conn, source, cfg, fetcher, max_resolve=max_resolve, stats=stats)
    return stats
