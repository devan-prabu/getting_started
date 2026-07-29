"""End-to-end pipeline test.

This is the Phase 0 acceptance criterion that matters most: `radar run` twice in
a row must produce zero duplicate rows and zero duplicate alerts (brief §7).
Everything runs against fixtures; no socket is opened.
"""

from __future__ import annotations

import contextlib

from conftest import FakeFetcher, fixture
from radar import alert as alert_mod
from radar import collect, db, enrich, extract, filters
from radar.config import SourcesConfig

AWARD_HTML = (200, fixture("article_award.html"))
NOISE_HTML = (200, fixture("article_noise.html"))

# The publisher URLs the fixture feed's Google News ids decode to.
ARTICLES = {
    "https://gulfnews.com/business/property/"
    "emaar-appoints-alec-for-business-bay-tower-1.9988776": AWARD_HTML,
    "https://www.thenationalnews.com/business/property/2026/07/27/"
    "asgc-awarded-tower-contract/": AWARD_HTML,
    "https://www.zawya.com/en/projects/nmdc-wins-abu-dhabi-marine-package-x9f2k3": AWARD_HTML,
    "https://www.khaleejtimes.com/business/dubai-metro-blue-line-package-awarded": AWARD_HTML,
    "https://example-outlet.test/horoscope/27-july"
    "?utm_source=googlenews&utm_medium=rss": NOISE_HTML,
}


class RecordingChannel:
    def __init__(self, name: str = "telegram"):
        self.name = name
        self.sent: list = []

    def configured(self) -> bool:
        return True

    def send(self, payload) -> None:
        self.sent.append(payload)


def make_cfg() -> SourcesConfig:
    return SourcesConfig(
        {
            "google_news_base": "https://news.google.com/rss/search",
            "google_news_params": {"hl": "en", "gl": "AE", "ceid": "AE:en"},
            "sources": [{"id": "gnews_test", "type": "google_news", "query": "test"}],
        }
    )


def make_fetcher(cfg: SourcesConfig) -> FakeFetcher:
    responses = {cfg.build_url(cfg.get("gnews_test")): (200, fixture("google_news_awards.xml"))}
    # The horoscope URL is stored canonicalised, so serve both spellings.
    responses.update(ARTICLES)
    responses["https://example-outlet.test/horoscope/27-july"] = NOISE_HTML
    return FakeFetcher(responses)


def run_once(conn, cfg, fetcher, channels, *, dry_run=False):
    c = collect.collect_all(conn, cfg, fetcher)
    e = enrich.enrich_new(conn, fetcher)
    f = filters.filter_enriched(conn)
    x = extract.extract_pending(conn)
    a = alert_mod.send_alerts(conn, channels, dry_run=dry_run)
    return c, e, f, x, a


def test_full_pipeline_produces_alerts(conn):
    cfg = make_cfg()
    fetcher = make_fetcher(cfg)
    channel = RecordingChannel()

    c, e, f, x, a = run_once(conn, cfg, fetcher, [channel])

    assert c.inserted == 5
    # Phase 0 acceptance: raw_text on >=80% of collected items.
    assert e.coverage >= 0.8
    # The horoscope is excluded; the four award items pass.
    assert f.passed == 4
    assert x.awards >= 1
    assert a.sent == x.awards
    assert len(channel.sent) == a.sent


def test_running_twice_creates_no_duplicate_rows_or_alerts(conn):
    """The headline Phase 0 guarantee."""
    cfg = make_cfg()
    fetcher = make_fetcher(cfg)
    channel = RecordingChannel()

    run_once(conn, cfg, fetcher, [channel])
    counts_after_first = db.counts(conn)
    alerts_after_first = len(channel.sent)

    c2, e2, f2, x2, a2 = run_once(conn, cfg, fetcher, [channel])

    assert c2.inserted == 0
    assert e2.attempted == 0
    assert f2.evaluated == 0
    assert x2.considered == 0
    assert a2.sent == 0

    assert db.counts(conn)["raw_items"] == counts_after_first["raw_items"]
    assert db.counts(conn)["awards"] == counts_after_first["awards"]
    assert db.counts(conn)["alerts_sent"] == counts_after_first["alerts_sent"]
    assert len(channel.sent) == alerts_after_first


def test_third_run_is_still_clean(conn):
    cfg = make_cfg()
    fetcher = make_fetcher(cfg)
    channel = RecordingChannel()
    for _ in range(3):
        run_once(conn, cfg, fetcher, [channel])

    assert db.counts(conn)["raw_items"] == 5
    assert len(channel.sent) == db.counts(conn)["awards"]


def test_no_google_redirect_urls_survive_into_the_database(conn):
    cfg = make_cfg()
    run_once(conn, cfg, make_fetcher(cfg), [RecordingChannel()])
    urls = [r["url"] for r in conn.execute("SELECT url FROM raw_items")]
    assert not any("news.google.com" in u for u in urls)


def test_dry_run_leaves_the_alert_ledger_empty(conn, capsys):
    cfg = make_cfg()
    fetcher = make_fetcher(cfg)
    run_once(conn, cfg, fetcher, [], dry_run=True)

    out = capsys.readouterr().out
    assert "Source:" in out
    assert db.counts(conn)["alerts_sent"] == 0

    # And a later real run still delivers everything.
    channel = RecordingChannel()
    assert alert_mod.send_alerts(conn, [channel]).sent == db.counts(conn)["awards"]


def test_a_failing_extract_does_not_lose_the_collected_item(conn, monkeypatch):
    """raw_items is the durable log; downstream failures must be recoverable."""
    cfg = make_cfg()
    fetcher = make_fetcher(cfg)
    collect.collect_all(conn, cfg, fetcher)
    enrich.enrich_new(conn, fetcher)
    filters.filter_enriched(conn)

    def boom(*args, **kwargs):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(extract, "extract_from_text", boom)
    with contextlib.suppress(RuntimeError):
        extract.extract_pending(conn)

    assert db.counts(conn)["raw_items"] == 5

    # With the extractor restored, the run recovers on its own.
    monkeypatch.undo()
    assert extract.extract_pending(conn).awards >= 1


def test_second_source_reporting_the_same_award_adds_no_row(conn):
    cfg = SourcesConfig(
        {
            "google_news_base": "https://news.google.com/rss/search",
            "google_news_params": {"hl": "en"},
            "sources": [
                {"id": "gnews_a", "type": "google_news", "query": "a"},
                {"id": "gnews_b", "type": "google_news", "query": "b"},
            ],
        }
    )
    fetcher = FakeFetcher(
        {
            cfg.build_url(cfg.get("gnews_a")): (200, fixture("google_news_awards.xml")),
            cfg.build_url(cfg.get("gnews_b")): (200, fixture("google_news_duplicate.xml")),
            **ARTICLES,
            "https://example-outlet.test/horoscope/27-july": NOISE_HTML,
            "https://www.zawya.com/en/projects/aldar-awards-yas-villas-trojan-a1b2c3": AWARD_HTML,
        }
    )
    stats = collect.collect_all(conn, cfg, fetcher)

    # Both ASGC re-reports are dropped by title similarity.
    assert stats.duplicate_title == 2
    titles = [r["title"] for r in conn.execute("SELECT title FROM raw_items")]
    assert sum("ASGC" in t for t in titles) == 1
