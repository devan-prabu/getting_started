from __future__ import annotations

import sqlite3

import pytest

from radar import db
from radar.models import Award, Company, ItemStatus, RawItem


def make_item(**kw) -> RawItem:
    base = {
        "source_id": "gnews_test",
        "url": "https://example.test/a",
        "url_hash": "hash-a",
        "title": "Something was awarded in Dubai",
        "published_at": "2026-07-27T08:00:00+00:00",
    }
    base.update(kw)
    return RawItem(**base)


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "r.db"
    db.init_db(path)
    db.init_db(path)  # must not raise
    with db.connect(path) as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"raw_items", "awards", "companies", "alerts_sent", "source_health"} <= tables


def test_wal_mode_enabled(tmp_path):
    path = tmp_path / "r.db"
    db.init_db(path)
    with db.connect(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_migration_adds_language_and_parent(tmp_path):
    """The extra columns must exist even on a DB created from the bare DDL."""
    path = tmp_path / "r.db"
    db.init_db(path)
    with db.connect(path) as conn:
        raw_cols = {r["name"] for r in conn.execute("PRAGMA table_info(raw_items)")}
        company_cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    assert "language" in raw_cols
    assert "parent" in company_cols


def test_insert_raw_item_dedupes_on_url_hash(conn):
    first = db.insert_raw_item(conn, make_item())
    second = db.insert_raw_item(conn, make_item(url="https://example.test/a?x=1"))
    assert isinstance(first, int)
    assert second is None, "same url_hash must not create a second row"
    assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 1


def test_url_hash_exists(conn):
    db.insert_raw_item(conn, make_item())
    assert db.url_hash_exists(conn, "hash-a")
    assert not db.url_hash_exists(conn, "hash-missing")


def test_update_item_sets_fields(conn):
    item_id = db.insert_raw_item(conn, make_item())
    db.update_item(conn, item_id, raw_text="body", passed_filter=True, status=ItemStatus.ENRICHED)
    row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (item_id,)).fetchone()
    assert row["raw_text"] == "body"
    assert row["passed_filter"] == 1
    assert row["status"] == "enriched"


def test_update_item_with_nothing_is_a_noop(conn):
    item_id = db.insert_raw_item(conn, make_item())
    db.update_item(conn, item_id)
    row = conn.execute("SELECT status FROM raw_items WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "new"


def test_get_items_by_status_respects_limit(conn):
    for i in range(3):
        db.insert_raw_item(conn, make_item(url=f"https://e.test/{i}", url_hash=f"h{i}"))
    assert len(db.get_items_by_status(conn, ItemStatus.NEW)) == 3
    assert len(db.get_items_by_status(conn, ItemStatus.NEW, limit=2)) == 2


def test_alerts_sent_unique_constraint_blocks_double_alerting(conn):
    item_id = db.insert_raw_item(conn, make_item())
    award_id = db.insert_award(conn, Award(raw_item_id=item_id, contractor_raw="ALEC"))

    assert db.mark_alert_sent(conn, award_id, "telegram") is True
    assert db.mark_alert_sent(conn, award_id, "telegram") is False
    # A different channel is a different alert and is allowed.
    assert db.mark_alert_sent(conn, award_id, "email") is True

    count = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert count == 2


def test_unalerted_awards_excludes_sent_ones(conn):
    item_id = db.insert_raw_item(conn, make_item())
    a1 = db.insert_award(conn, Award(raw_item_id=item_id, contractor_raw="ALEC"))
    a2 = db.insert_award(conn, Award(raw_item_id=item_id, contractor_raw="ASGC"))

    assert {r["id"] for r in db.unalerted_awards(conn, "telegram")} == {a1, a2}
    db.mark_alert_sent(conn, a1, "telegram")
    assert {r["id"] for r in db.unalerted_awards(conn, "telegram")} == {a2}
    # Still unsent on email.
    assert {r["id"] for r in db.unalerted_awards(conn, "email")} == {a1, a2}


def test_unalerted_awards_joins_source_url(conn):
    item_id = db.insert_raw_item(conn, make_item())
    db.insert_award(conn, Award(raw_item_id=item_id, contractor_raw="ALEC"))
    row = db.unalerted_awards(conn, "telegram")[0]
    assert row["source_url"] == "https://example.test/a"
    assert row["source_title"] == "Something was awarded in Dubai"


def test_award_requires_existing_raw_item(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_award(conn, Award(raw_item_id=9999, contractor_raw="Ghost"))


def test_load_companies_upserts(conn):
    companies = [
        Company(
            canonical_name="ALEC",
            aliases=["ALEC LLC"],
            tier=1,
            is_watchlist=True,
            careers_url="https://alec.test/jobs",
        )
    ]
    assert db.load_companies(conn, companies) == 1
    db.load_companies(conn, companies)  # again
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1

    row = db.get_company(conn, "ALEC")
    assert row["aliases"] == '["ALEC LLC"]'
    assert row["is_watchlist"] == 1


def test_source_health_tracks_consecutive_failures(conn):
    db.record_source_failure(conn, "gnews_x", "HTTP 500")
    db.record_source_failure(conn, "gnews_x", "HTTP 503")
    row = conn.execute("SELECT * FROM source_health WHERE source_id='gnews_x'").fetchone()
    assert row["consecutive_failures"] == 2

    db.record_source_success(conn, "gnews_x", items=7)
    row = conn.execute("SELECT * FROM source_health WHERE source_id='gnews_x'").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["items_last_run"] == 7
    assert row["last_success"] is not None


def test_recent_titles_respects_window(conn):
    db.insert_raw_item(conn, make_item(published_at="2026-07-27T08:00:00+00:00"))
    db.insert_raw_item(
        conn,
        make_item(
            url="https://e.test/old",
            url_hash="h-old",
            title="Old news",
            published_at="2026-01-01T00:00:00+00:00",
        ),
    )
    titles = db.recent_titles(conn, "2026-07-01T00:00:00+00:00")
    assert [t[1] for t in titles] == ["Something was awarded in Dubai"]


def test_counts_reports_status_breakdown(conn):
    item_id = db.insert_raw_item(conn, make_item())
    db.update_item(conn, item_id, status=ItemStatus.ENRICHED)
    totals = db.counts(conn)
    assert totals["raw_items"] == 1
    assert totals["raw_items.enriched"] == 1
