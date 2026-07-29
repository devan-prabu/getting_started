from __future__ import annotations

from datetime import UTC, datetime

from radar import dedupe

# The publisher URLs really encoded inside the fixture feed's article ids.
GNEWS_ASGC = (
    "https://news.google.com/rss/articles/CBMiWWh0dHBzOi8vd3d3LnRoZW5hdGlvbmFsbmV3cy5jb20"
    "vYnVzaW5lc3MvcHJvcGVydHkvMjAyNi8wNy8yNy9hc2djLWF3YXJkZWQtdG93ZXItY29udHJhY3QvMgtDQU"
    "lhQjFKVlFrbEQ?oc=5"
)
ASGC_PUBLISHER = (
    "https://www.thenationalnews.com/business/property/2026/07/27/asgc-awarded-tower-contract/"
)


class TestCanonicalise:
    def test_lowercases_host_but_not_path(self):
        assert dedupe.canonicalise("HTTPS://Www.Example.TEST/Path/To/Story") == (
            "https://www.example.test/Path/To/Story"
        )

    def test_strips_tracking_params(self):
        url = "https://x.test/a?utm_source=googlenews&utm_medium=rss&fbclid=zz&id=7"
        assert dedupe.canonicalise(url) == "https://x.test/a?id=7"

    def test_strips_google_oc_param(self):
        assert dedupe.canonicalise("https://x.test/a?oc=5") == "https://x.test/a"

    def test_drops_fragment_and_trailing_slash(self):
        assert dedupe.canonicalise("https://x.test/a/#section") == "https://x.test/a"

    def test_drops_default_ports(self):
        assert dedupe.canonicalise("https://x.test:443/a") == "https://x.test/a"
        assert dedupe.canonicalise("http://x.test:80/a") == "http://x.test/a"

    def test_bare_root_keeps_slash(self):
        assert dedupe.canonicalise("https://x.test/") == "https://x.test/"

    def test_empty_input(self):
        assert dedupe.canonicalise("") == ""


class TestUrlHash:
    def test_variants_of_one_url_share_a_hash(self):
        a = dedupe.url_hash("https://X.test/story/?utm_source=rss#top")
        b = dedupe.url_hash("https://x.test/story")
        assert a == b

    def test_different_urls_differ(self):
        assert dedupe.url_hash("https://x.test/a") != dedupe.url_hash("https://x.test/b")

    def test_is_sha256_hex(self):
        assert len(dedupe.url_hash("https://x.test/a")) == 64


class TestGoogleNews:
    def test_is_google_news_url(self):
        assert dedupe.is_google_news_url(GNEWS_ASGC)
        assert not dedupe.is_google_news_url("https://gulfnews.com/x")

    def test_decode_google_news_url(self):
        """The whole point of brief §13.1 — resolve offline, before hashing."""
        assert dedupe.decode_google_news_url(GNEWS_ASGC) == ASGC_PUBLISHER

    def test_decode_returns_none_for_non_article_paths(self):
        assert dedupe.decode_google_news_url("https://news.google.com/rss/search?q=x") is None

    def test_decode_returns_none_for_opaque_ids(self):
        """Newer ids carry no URL. Must return None, not crash."""
        url = "https://news.google.com/rss/articles/AAAAAAAAAAAAAAAAAAAAAAAA?oc=5"
        assert dedupe.decode_google_news_url(url) is None

    def test_decode_survives_bad_base64(self):
        assert (
            dedupe.decode_google_news_url("https://news.google.com/rss/articles/!!!not-base64!!!")
            is None
        )

    def test_resolve_url_prefers_offline_decode(self, fake_fetcher):
        fetcher = fake_fetcher()
        assert dedupe.resolve_url(GNEWS_ASGC, fetcher) == ASGC_PUBLISHER
        assert fetcher.resolved == [], "offline decode must avoid the network entirely"

    def test_resolve_url_falls_back_to_redirects(self):
        opaque = "https://news.google.com/rss/articles/AAAAAAAAAAAAAAAA?oc=5"

        class Redirector:
            def resolve(self, url):
                return "https://gulfnews.com/final-story"

        assert dedupe.resolve_url(opaque, Redirector()) == "https://gulfnews.com/final-story"

    def test_resolve_url_keeps_original_when_resolution_fails(self):
        opaque = "https://news.google.com/rss/articles/AAAAAAAAAAAAAAAA?oc=5"

        class Broken:
            def resolve(self, url):
                raise ConnectionError("host down")

        # Losing the item would be worse than storing an unresolved URL.
        assert dedupe.resolve_url(opaque, Broken()) == opaque

    def test_resolve_url_passes_through_non_google_urls(self, fake_fetcher):
        fetcher = fake_fetcher()
        url = "https://gulfnews.com/a"
        assert dedupe.resolve_url(url, fetcher) == url
        assert fetcher.resolved == []

    def test_two_queries_surfacing_one_article_produce_one_hash(self):
        """The actual duplicate-row bug this machinery prevents."""
        from_query_a = dedupe.resolve_url(GNEWS_ASGC, None)
        from_query_b = dedupe.resolve_url(GNEWS_ASGC.replace("?oc=5", "?oc=5&hl=en"), None)
        assert dedupe.url_hash(from_query_a) == dedupe.url_hash(from_query_b)


class TestTitleDedupe:
    def test_normalise_strips_publisher_suffix(self):
        assert (
            dedupe.normalise_title("ASGC awarded AED 1.2bn tower contract - Gulf News")
            == "asgc awarded aed 1.2bn tower contract"
        )

    def test_same_story_two_outlets_is_a_duplicate(self):
        stored = [
            (
                1,
                "ASGC awarded AED 1.2 billion residential tower contract in Dubai - The National",
                "2026-07-27T06:02:00+00:00",
            )
        ]
        found = dedupe.find_duplicate_title(
            "Dubai: ASGC awarded AED 1.2 billion residential tower contract - Construction Week",
            stored,
            published_at="2026-07-27T12:40:00+00:00",
        )
        assert found == 1

    def test_different_awards_are_not_duplicates(self):
        stored = [
            (
                1,
                "ASGC awarded AED 1.2 billion residential tower contract in Dubai - The National",
                "2026-07-27T06:02:00+00:00",
            )
        ]
        found = dedupe.find_duplicate_title(
            "Aldar awards Yas Island villas package to Trojan General Contracting - Zawya",
            stored,
            published_at="2026-07-27T07:00:00+00:00",
        )
        assert found is None

    def test_same_headline_outside_the_window_is_not_a_duplicate(self):
        stored = [
            (1, "ASGC awarded tower contract in Dubai - The National", "2026-01-27T06:02:00+00:00")
        ]
        found = dedupe.find_duplicate_title(
            "ASGC awarded tower contract in Dubai - Gulf News",
            stored,
            published_at="2026-07-27T06:02:00+00:00",
        )
        assert found is None

    def test_missing_timestamp_skips_the_window_check(self):
        stored = [(1, "ASGC awarded tower contract - The National", "2020-01-01T00:00:00+00:00")]
        assert (
            dedupe.find_duplicate_title(
                "ASGC awarded tower contract - Gulf News", stored, published_at=None
            )
            == 1
        )

    def test_empty_title_never_matches(self):
        stored = [(1, "Anything at all", "2026-07-27T06:02:00+00:00")]
        assert dedupe.find_duplicate_title("", stored) is None

    def test_window_start_is_72_hours_back(self):
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert dedupe.window_start(now).startswith("2026-07-24T12:00:00")
