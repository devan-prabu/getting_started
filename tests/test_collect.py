from __future__ import annotations

import pytest

from conftest import FakeFetcher, fixture
from radar import collect
from radar.config import SourcesConfig
from radar.models import Source

FEED_URL = "https://news.google.com/rss/search?q=test&hl=en&gl=AE&ceid=AE%3Aen"


@pytest.fixture
def cfg() -> SourcesConfig:
    return SourcesConfig(
        {
            "google_news_base": "https://news.google.com/rss/search",
            "google_news_params": {"hl": "en", "gl": "AE", "ceid": "AE:en"},
            "sources": [
                {"id": "gnews_test", "type": "google_news", "query": "test"},
                {"id": "gnews_dup", "type": "google_news", "query": "dup"},
            ],
        }
    )


@pytest.fixture
def source(cfg) -> Source:
    return cfg.get("gnews_test")


def fetcher_for(cfg, body_by_source: dict[str, str], **kw) -> FakeFetcher:
    responses = {cfg.build_url(cfg.get(sid)): (200, body) for sid, body in body_by_source.items()}
    return FakeFetcher(responses, **kw)


class TestParseFeed:
    def test_parses_entries(self):
        entries = collect.parse_feed(fixture("google_news_awards.xml"))
        assert len(entries) == 5
        assert entries[0]["title"].startswith("Emaar appoints ALEC")

    def test_normalises_pubdate_to_utc_iso(self):
        entries = collect.parse_feed(fixture("google_news_awards.xml"))
        assert entries[0]["published_at"] == "2026-07-27T08:14:00+00:00"

    def test_empty_feed_yields_nothing(self):
        assert collect.parse_feed(fixture("google_news_empty.xml")) == []

    def test_malformed_body_raises_rather_than_returning_junk(self):
        with pytest.raises(ValueError):
            collect.parse_feed(fixture("malformed.xml"))


class TestCollectSource:
    def test_stores_items_with_resolved_publisher_urls(self, conn, cfg, source):
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        stats = collect.collect_source(conn, source, cfg, fetcher)

        assert stats.fetched == 5
        assert stats.inserted == 5
        urls = [r["url"] for r in conn.execute("SELECT url FROM raw_items ORDER BY id")]
        # Google redirect URLs must never reach the database (brief §13.1).
        assert not any("news.google.com" in u for u in urls)
        assert "gulfnews.com" in urls[0]

    def test_offline_decoding_avoids_network_resolution(self, conn, cfg, source):
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        collect.collect_source(conn, source, cfg, fetcher)
        assert fetcher.resolved == []

    def test_rerunning_inserts_nothing_new(self, conn, cfg, source):
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        collect.collect_source(conn, source, cfg, fetcher)
        second = collect.collect_source(conn, source, cfg, fetcher)

        assert second.inserted == 0
        assert second.duplicate_url == 5
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 5

    def test_same_story_from_another_outlet_is_dropped(self, conn, cfg):
        """Brief §13.2 — one award, six outlets, one row."""
        fetcher = fetcher_for(
            cfg,
            {
                "gnews_test": fixture("google_news_awards.xml"),
                "gnews_dup": fixture("google_news_duplicate.xml"),
            },
        )
        collect.collect_source(conn, cfg.get("gnews_test"), cfg, fetcher)
        stats = collect.collect_source(conn, cfg.get("gnews_dup"), cfg, fetcher)

        # Two ASGC re-reports dropped; the Aldar/Trojan story is genuinely new.
        assert stats.duplicate_title == 2
        assert stats.inserted == 1
        titles = [r["title"] for r in conn.execute("SELECT title FROM raw_items")]
        assert sum("ASGC" in t for t in titles) == 1

    def test_records_source_health_on_success(self, conn, cfg, source):
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        collect.collect_source(conn, source, cfg, fetcher)
        row = conn.execute("SELECT * FROM source_health WHERE source_id='gnews_test'").fetchone()
        assert row["items_last_run"] == 5
        assert row["consecutive_failures"] == 0

    def test_dead_host_is_recorded_and_does_not_raise(self, conn, cfg, source):
        fetcher = FakeFetcher({})  # no fixture registered -> ConnectionError
        stats = collect.collect_source(conn, source, cfg, fetcher)

        assert stats.inserted == 0
        assert "gnews_test" in stats.errors
        row = conn.execute("SELECT * FROM source_health WHERE source_id='gnews_test'").fetchone()
        assert row["consecutive_failures"] == 1

    def test_robots_disallowed_source_is_skipped_not_retried(self, conn, cfg, source):
        url = cfg.build_url(source)
        fetcher = FakeFetcher({url: (200, "<rss/>")}, disallowed={url})
        stats = collect.collect_source(conn, source, cfg, fetcher)
        assert "robots" in stats.errors["gnews_test"]
        assert stats.inserted == 0

    def test_non_200_is_recorded_as_a_failure(self, conn, cfg, source):
        fetcher = FakeFetcher({cfg.build_url(source): (429, "slow down")})
        stats = collect.collect_source(conn, source, cfg, fetcher)
        assert stats.errors["gnews_test"] == "HTTP 429"

    def test_malformed_feed_does_not_lose_the_run(self, conn, cfg, source):
        fetcher = FakeFetcher({cfg.build_url(source): (200, fixture("malformed.xml"))})
        stats = collect.collect_source(conn, source, cfg, fetcher)
        assert stats.inserted == 0
        assert "gnews_test" in stats.errors

    def test_resolve_budget_keeps_google_urls_when_spent(self, conn, cfg, source):
        """With no offline decode available, the budget caps network calls."""
        feed = fixture("google_news_awards.xml").replace(
            "CBMiWWh0dHBzOi8vd3d3LnRoZW5hdGlvbmFsbmV3cy5jb20"
            "vYnVzaW5lc3MvcHJvcGVydHkvMjAyNi8wNy8yNy9hc2djLWF3YXJkZWQtdG93ZXItY29udHJhY3QvMgtDQU"
            "lhQjFKVlFrbEQ",
            "AAAAAAAAAAAAAAAAAAAA",
        )
        fetcher = FakeFetcher({cfg.build_url(source): (200, feed)})
        collect.collect_source(conn, source, cfg, fetcher, max_resolve=0)
        assert fetcher.resolved == []
        urls = [r["url"] for r in conn.execute("SELECT url FROM raw_items")]
        assert any("news.google.com" in u for u in urls)


class TestCollectAll:
    def test_collects_every_enabled_source(self, conn, cfg):
        fetcher = fetcher_for(
            cfg,
            {
                "gnews_test": fixture("google_news_awards.xml"),
                "gnews_dup": fixture("google_news_duplicate.xml"),
            },
        )
        stats = collect.collect_all(conn, cfg, fetcher)
        assert stats.fetched == 8
        assert stats.inserted == 6  # 5 + 1 new, 2 title-duplicates

    def test_disabled_sources_are_skipped(self, conn, cfg):
        cfg.sources[1].enabled = False
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        stats = collect.collect_all(conn, cfg, fetcher)
        assert stats.fetched == 5

    def test_unknown_source_id_is_an_error(self, conn, cfg):
        with pytest.raises(KeyError):
            collect.collect_all(conn, cfg, FakeFetcher({}), source_id="nope")

    def test_one_dead_source_does_not_stop_the_others(self, conn, cfg):
        fetcher = fetcher_for(cfg, {"gnews_test": fixture("google_news_awards.xml")})
        stats = collect.collect_all(conn, cfg, fetcher)  # gnews_dup has no fixture
        assert stats.inserted == 5
        assert "gnews_dup" in stats.errors


class TestRealConfig:
    def test_shipped_sources_yml_parses_and_builds_urls(self, sources_cfg):
        assert len(sources_cfg.sources) >= 3, "Phase 0 needs at least 3 queries"
        for source in sources_cfg.sources:
            url = sources_cfg.build_url(source)
            assert url.startswith("https://news.google.com/rss/search?q=")
            assert "hl=en" in url and "gl=AE" in url

    def test_every_shipped_source_is_honestly_marked(self, sources_cfg):
        """Nothing may claim to be live until `radar sources check` has run."""
        for source in sources_cfg.sources:
            assert source.status in {"unverified", "live", "dead"}
            if source.status == "unverified":
                assert source.last_checked is None

    def test_source_ids_are_unique(self, sources_cfg):
        ids = [s.id for s in sources_cfg.sources]
        assert len(ids) == len(set(ids))
