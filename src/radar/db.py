"""SQLite access. Plain sqlite3, no ORM (brief §4).

The schema is created idempotently, so `radar init` is safe to re-run. Simple
additive migrations are handled by `_migrate`, which adds missing columns rather
than dropping anything — `raw_items` is the durable log and must survive
upgrades.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Award, Company, ItemStatus, RawItem, utcnow_iso

DEFAULT_DB_PATH = "data/radar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    url_hash      TEXT NOT NULL UNIQUE,
    title         TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    raw_text      TEXT,
    passed_filter INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_items(status);

CREATE TABLE IF NOT EXISTS awards (
    id                   INTEGER PRIMARY KEY,
    raw_item_id          INTEGER NOT NULL REFERENCES raw_items(id),
    developer            TEXT,
    contractor_raw       TEXT,
    contractor_canonical TEXT,
    project_name         TEXT,
    value_aed            REAL,
    emirate              TEXT,
    sector               TEXT,
    award_date           TEXT,
    scope_summary        TEXT,
    confidence           REAL,
    relevance_score      INTEGER,
    hiring_window_start  TEXT,
    hiring_window_end    TEXT,
    extraction_model     TEXT,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_awards_contractor ON awards(contractor_canonical);
CREATE INDEX IF NOT EXISTS idx_awards_date ON awards(award_date);

CREATE TABLE IF NOT EXISTS companies (
    canonical_name TEXT PRIMARY KEY,
    aliases        TEXT,
    careers_url    TEXT,
    ats            TEXT,
    tier           INTEGER,
    is_watchlist   INTEGER DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id       INTEGER PRIMARY KEY,
    award_id INTEGER NOT NULL REFERENCES awards(id),
    channel  TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    UNIQUE(award_id, channel)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id     TEXT PRIMARY KEY,
    last_success  TEXT,
    last_error    TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    items_last_run INTEGER
);
"""

# Columns added after the brief's original DDL. (column, table, DDL fragment)
_MIGRATIONS: list[tuple[str, str, str]] = [
    # Brief §13.5: keep language a field, not an assumption, so Arabic sources
    # can be added later without a schema rewrite.
    ("raw_items", "language", "ALTER TABLE raw_items ADD COLUMN language TEXT DEFAULT 'en'"),
    # companies.yml carries parent/subsidiary links (brief §13.6); careers-page
    # routing needs them.
    ("companies", "parent", "ALTER TABLE companies ADD COLUMN parent TEXT"),
]


def db_path() -> Path:
    return Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with WAL and foreign keys on. Commits on clean exit."""
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def init_db(path: str | Path | None = None) -> Path:
    """Create the schema. Idempotent."""
    p = Path(path) if path is not None else db_path()
    with connect(p) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return p


# --- raw_items ------------------------------------------------------------


def insert_raw_item(conn: sqlite3.Connection, item: RawItem) -> int | None:
    """Insert an item, or return None if its url_hash is already stored.

    The UNIQUE constraint on url_hash is what makes `radar collect` re-runnable.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO raw_items
            (source_id, url, url_hash, title, published_at, fetched_at,
             raw_text, passed_filter, status, language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.source_id,
            item.url,
            item.url_hash,
            item.title,
            item.published_at,
            item.fetched_at,
            item.raw_text,
            int(item.passed_filter),
            str(item.status),
            item.language,
        ),
    )
    if cur.rowcount == 0:
        return None
    return int(cur.lastrowid)


def url_hash_exists(conn: sqlite3.Connection, url_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM raw_items WHERE url_hash = ? LIMIT 1", (url_hash,)).fetchone()
    return row is not None


def get_items_by_status(
    conn: sqlite3.Connection, status: str | ItemStatus, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM raw_items WHERE status = ? ORDER BY id"
    params: list[object] = [str(status)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def recent_titles(conn: sqlite3.Connection, since_iso: str) -> list[tuple[int, str, str]]:
    """(id, title, published_at) for items in the fuzzy-dedupe window."""
    rows = conn.execute(
        """
        SELECT id, title, COALESCE(published_at, fetched_at) AS ts
        FROM raw_items
        WHERE title IS NOT NULL AND COALESCE(published_at, fetched_at) >= ?
        """,
        (since_iso,),
    )
    return [(int(r["id"]), r["title"], r["ts"]) for r in rows]


def update_item(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    raw_text: str | None = None,
    passed_filter: bool | None = None,
    status: str | ItemStatus | None = None,
) -> None:
    sets: list[str] = []
    params: list[object] = []
    if raw_text is not None:
        sets.append("raw_text = ?")
        params.append(raw_text)
    if passed_filter is not None:
        sets.append("passed_filter = ?")
        params.append(int(passed_filter))
    if status is not None:
        sets.append("status = ?")
        params.append(str(status))
    if not sets:
        return
    params.append(item_id)
    conn.execute(f"UPDATE raw_items SET {', '.join(sets)} WHERE id = ?", params)


# --- awards ---------------------------------------------------------------


def insert_award(conn: sqlite3.Connection, award: Award) -> int:
    cur = conn.execute(
        """
        INSERT INTO awards
            (raw_item_id, developer, contractor_raw, contractor_canonical,
             project_name, value_aed, emirate, sector, award_date, scope_summary,
             confidence, relevance_score, hiring_window_start, hiring_window_end,
             extraction_model, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            award.raw_item_id,
            award.developer,
            award.contractor_raw,
            award.contractor_canonical,
            award.project_name,
            award.value_aed,
            award.emirate,
            award.sector,
            award.award_date,
            award.scope_summary,
            award.confidence,
            award.relevance_score,
            award.hiring_window_start,
            award.hiring_window_end,
            award.extraction_model,
            award.created_at,
        ),
    )
    return int(cur.lastrowid)


def award_exists_for_item(conn: sqlite3.Connection, raw_item_id: int) -> bool:
    """One award row per raw item in Phase 0 — keeps `radar run` idempotent."""
    row = conn.execute(
        "SELECT 1 FROM awards WHERE raw_item_id = ? LIMIT 1", (raw_item_id,)
    ).fetchone()
    return row is not None


def unalerted_awards(conn: sqlite3.Connection, channel: str) -> list[sqlite3.Row]:
    """Awards with no alerts_sent row for this channel, newest first."""
    return list(
        conn.execute(
            """
            SELECT a.*, r.url AS source_url, r.title AS source_title,
                   r.published_at AS source_published_at
            FROM awards a
            JOIN raw_items r ON r.id = a.raw_item_id
            WHERE NOT EXISTS (
                SELECT 1 FROM alerts_sent s
                WHERE s.award_id = a.id AND s.channel = ?
            )
            ORDER BY COALESCE(a.award_date, r.published_at, a.created_at) DESC, a.id DESC
            """,
            (channel,),
        )
    )


def mark_alert_sent(conn: sqlite3.Connection, award_id: int, channel: str) -> bool:
    """Record a sent alert. False means it was already sent — never send twice.

    The UNIQUE(award_id, channel) constraint is the hard guarantee (brief §6.7);
    this is just the polite path to it.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO alerts_sent (award_id, channel, sent_at) VALUES (?, ?, ?)",
        (award_id, channel, utcnow_iso()),
    )
    return cur.rowcount > 0


# --- companies ------------------------------------------------------------


def load_companies(conn: sqlite3.Connection, companies: Iterable[Company]) -> int:
    n = 0
    for c in companies:
        conn.execute(
            """
            INSERT INTO companies
                (canonical_name, aliases, careers_url, ats, tier, is_watchlist, notes, parent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_name) DO UPDATE SET
                aliases = excluded.aliases,
                careers_url = excluded.careers_url,
                ats = excluded.ats,
                tier = excluded.tier,
                is_watchlist = excluded.is_watchlist,
                notes = excluded.notes,
                parent = excluded.parent
            """,
            (
                c.canonical_name,
                json.dumps(c.aliases),
                c.careers_url,
                c.ats,
                c.tier,
                int(c.is_watchlist),
                c.notes,
                c.parent,
            ),
        )
        n += 1
    return n


def get_company(conn: sqlite3.Connection, canonical_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM companies WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()


# --- source_health --------------------------------------------------------


def record_source_success(conn: sqlite3.Connection, source_id: str, items: int) -> None:
    conn.execute(
        """
        INSERT INTO source_health (source_id, last_success, last_error,
                                   consecutive_failures, items_last_run)
        VALUES (?, ?, NULL, 0, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            last_success = excluded.last_success,
            consecutive_failures = 0,
            items_last_run = excluded.items_last_run
        """,
        (source_id, utcnow_iso(), items),
    )


def record_source_failure(conn: sqlite3.Connection, source_id: str, error: str) -> None:
    conn.execute(
        """
        INSERT INTO source_health (source_id, last_success, last_error,
                                   consecutive_failures, items_last_run)
        VALUES (?, NULL, ?, 1, 0)
        ON CONFLICT(source_id) DO UPDATE SET
            last_error = excluded.last_error,
            consecutive_failures = source_health.consecutive_failures + 1,
            items_last_run = 0
        """,
        (source_id, error[:500]),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Quick totals for the CLI summary lines."""
    out: dict[str, int] = {}
    for table in ("raw_items", "awards", "alerts_sent", "companies"):
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    for row in conn.execute("SELECT status, COUNT(*) AS c FROM raw_items GROUP BY status"):
        out[f"raw_items.{row['status']}"] = int(row["c"])
    return out
