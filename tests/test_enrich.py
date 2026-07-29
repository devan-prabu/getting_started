from __future__ import annotations

from conftest import FakeFetcher, fixture
from radar import db, enrich
from radar.models import ItemStatus, RawItem

AWARD_URL = "https://gulfnews.com/business/property/emaar-appoints-alec"
PAYWALL_URL = "https://paywalled.test/story"


def add_item(conn, url=AWARD_URL, title="Emaar appoints ALEC") -> int:
    return db.insert_raw_item(
        conn,
        RawItem(source_id="gnews_test", url=url, url_hash=f"h-{url}", title=title),
    )


class TestExtractText:
    def test_pulls_body_and_drops_chrome(self):
        text = enrich.extract_text(fixture("article_award.html"), url=AWARD_URL)
        assert text is not None
        assert "appointed ALEC Engineering and Contracting" in text
        assert "Copyright 2026" not in text
        assert "Home" not in text.split("\n")[0]

    def test_returns_none_for_a_paywall_stub(self):
        assert enrich.extract_text(fixture("article_paywall.html"), url=PAYWALL_URL) is None

    def test_returns_none_for_empty_input(self):
        assert enrich.extract_text("") is None
        assert enrich.extract_text("   ") is None

    def test_truncates_very_long_documents(self):
        html = "<html><body><article>" + ("word " * 20000) + "</article></body></html>"
        text = enrich.extract_text(html)
        assert text is not None
        assert len(text) <= enrich.MAX_TEXT_CHARS


class TestEnrichNew:
    def test_fills_raw_text_and_marks_enriched(self, conn):
        add_item(conn)
        fetcher = FakeFetcher({AWARD_URL: (200, fixture("article_award.html"))})
        stats = enrich.enrich_new(conn, fetcher)

        assert stats.enriched == 1
        assert stats.coverage == 1.0
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert "ALEC Engineering and Contracting" in row["raw_text"]
        assert row["status"] == "enriched"

    def test_paywall_stub_keeps_the_row_with_no_text(self, conn):
        add_item(conn, url=PAYWALL_URL, title="Subscribe to continue")
        fetcher = FakeFetcher({PAYWALL_URL: (200, fixture("article_paywall.html"))})
        stats = enrich.enrich_new(conn, fetcher)

        assert stats.empty == 1
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        # The title still carries signal, so the item is kept, not failed.
        assert row["status"] == "enriched"
        assert row["raw_text"] is None

    def test_robots_disallowed_article_is_kept_title_only(self, conn):
        add_item(conn)
        fetcher = FakeFetcher({AWARD_URL: (200, "x")}, disallowed={AWARD_URL})
        stats = enrich.enrich_new(conn, fetcher)

        assert stats.skipped_robots == 1
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert row["status"] == "enriched"
        assert row["raw_text"] is None

    def test_http_error_marks_the_item_failed(self, conn):
        add_item(conn)
        fetcher = FakeFetcher({AWARD_URL: (404, "gone")})
        stats = enrich.enrich_new(conn, fetcher)

        assert stats.failed == 1
        assert conn.execute("SELECT status FROM raw_items").fetchone()[0] == "failed"

    def test_network_error_does_not_stop_the_batch(self, conn):
        add_item(conn, url="https://dead.test/a", title="Dead")
        add_item(conn)
        fetcher = FakeFetcher({AWARD_URL: (200, fixture("article_award.html"))})
        stats = enrich.enrich_new(conn, fetcher)

        assert stats.failed == 1
        assert stats.enriched == 1

    def test_only_new_items_are_touched(self, conn):
        item_id = add_item(conn)
        db.update_item(conn, item_id, status=ItemStatus.ENRICHED)
        stats = enrich.enrich_new(conn, FakeFetcher({}))
        assert stats.attempted == 0

    def test_rerunning_enrich_is_a_noop(self, conn):
        add_item(conn)
        fetcher = FakeFetcher({AWARD_URL: (200, fixture("article_award.html"))})
        enrich.enrich_new(conn, fetcher)
        second = enrich.enrich_new(conn, fetcher)
        assert second.attempted == 0

    def test_limit_caps_the_batch(self, conn):
        for i in range(3):
            add_item(conn, url=f"https://x.test/{i}", title=f"Item {i}")
        fetcher = FakeFetcher(
            {f"https://x.test/{i}": (200, fixture("article_award.html")) for i in range(3)}
        )
        stats = enrich.enrich_new(conn, fetcher, limit=2)
        assert stats.attempted == 2

    def test_coverage_meets_the_phase_0_target(self, conn):
        """Phase 0 acceptance: raw_text on >=80% of items."""
        urls = [f"https://x.test/{i}" for i in range(5)]
        for i, url in enumerate(urls):
            add_item(conn, url=url, title=f"Item {i}")
        responses = {u: (200, fixture("article_award.html")) for u in urls[:4]}
        responses[urls[4]] = (200, fixture("article_paywall.html"))
        stats = enrich.enrich_new(conn, FakeFetcher(responses))
        assert stats.coverage >= 0.8
