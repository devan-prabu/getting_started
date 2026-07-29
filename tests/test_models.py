from __future__ import annotations

import pytest
from pydantic import ValidationError

from radar.config import SourcesConfig, load_companies, load_keywords
from radar.models import AED_PER_USD, Award, Company, Source, utcnow_iso


class TestAward:
    def test_scope_summary_is_capped_at_200_chars(self):
        """Brief §9.6 — an original short summary, never paragraphs of prose."""
        award = Award(raw_item_id=1, scope_summary="x" * 500)
        assert len(award.scope_summary) == 200

    def test_scope_summary_whitespace_is_collapsed(self):
        award = Award(raw_item_id=1, scope_summary="a\n\n  b   c")
        assert award.scope_summary == "a b c"

    def test_confidence_is_clamped(self):
        assert Award(raw_item_id=1, confidence=5.0).confidence == 1.0
        assert Award(raw_item_id=1, confidence=-2.0).confidence == 0.0

    def test_undisclosed_value_stays_none(self):
        assert Award(raw_item_id=1).value_aed is None

    def test_nonsense_values_become_none_rather_than_a_wrong_number(self):
        assert Award(raw_item_id=1, value_aed=0).value_aed is None
        assert Award(raw_item_id=1, value_aed=-5).value_aed is None

    def test_invalid_award_date_is_dropped_not_guessed(self):
        assert Award(raw_item_id=1, award_date="last Tuesday").award_date is None
        assert Award(raw_item_id=1, award_date="2026-07-27").award_date == "2026-07-27"

    def test_defaults_are_unknown_not_invented(self):
        award = Award(raw_item_id=1)
        assert award.emirate == "Unknown"
        assert award.sector == "other"
        assert award.contractor_canonical is None

    def test_aed_peg_is_the_official_rate(self):
        assert AED_PER_USD == 3.6725


class TestSource:
    def test_google_news_requires_a_query(self):
        with pytest.raises(ValidationError):
            Source(id="x", type="google_news")

    def test_rss_requires_a_url(self):
        with pytest.raises(ValidationError):
            Source(id="x", type="rss")

    def test_blank_strings_normalise_to_none(self):
        source = Source(id="x", type="rss", url="https://a.test/feed", notes="  ")
        assert source.notes is None

    def test_unknown_keys_are_rejected(self):
        """A typo in sources.yml should fail loudly, not be silently ignored."""
        with pytest.raises(ValidationError):
            Source(id="x", type="rss", url="https://a.test/f", enbaled=True)


class TestCompany:
    def test_aliases_default_to_empty(self):
        assert Company(canonical_name="ALEC").aliases == []

    def test_unknown_keys_are_rejected(self):
        with pytest.raises(ValidationError):
            Company(canonical_name="ALEC", careers=None)


class TestTimestamps:
    def test_utcnow_is_iso_utc_without_microseconds(self):
        value = utcnow_iso()
        assert value.endswith("+00:00")
        assert "." not in value


class TestShippedConfig:
    def test_companies_yml_parses(self):
        companies = load_companies()
        assert len(companies) >= 16

    def test_watchlist_contractors_are_present(self):
        names = {c.canonical_name for c in load_companies()}
        for expected in (
            "ALEC Engineering and Contracting",
            "ASGC Construction",
            "NMDC Group",
            "Trojan General Contracting",
        ):
            assert expected in names

    def test_brief_required_aliases_are_covered(self):
        """Brief §6.5 names these explicitly as the ones that must resolve."""
        by_name = {c.canonical_name: c for c in load_companies()}
        assert "ALEC" in by_name["ALEC Engineering and Contracting"].aliases
        assert "CCC" in by_name["Consolidated Contractors Company"].aliases
        assert "L&T" in by_name["Larsen & Toubro"].aliases
        assert "Larsen and Toubro" in by_name["Larsen & Toubro"].aliases
        assert "NMDC" in by_name["NMDC Group"].aliases
        assert "National Marine Dredging Company" in by_name["NMDC Group"].aliases

    def test_canonical_names_are_unique(self):
        names = [c.canonical_name for c in load_companies()]
        assert len(names) == len(set(names))

    def test_parent_links_are_recorded_where_they_matter(self):
        """Brief §13.6 — Trojan hires under the subsidiary, not the parent."""
        trojan = next(
            c for c in load_companies() if c.canonical_name == "Trojan General Contracting"
        )
        assert trojan.parent == "Alpha Dhabi Holding"

    def test_keywords_yml_parses(self):
        kw = load_keywords()
        assert "awarded" in kw["award_signals"]
        assert "Dubai" in kw["uae_signals"]
        assert "horoscope" in kw["exclusions"]

    def test_sources_yml_parses(self):
        assert len(SourcesConfig.load().sources) >= 3


class TestSourcesConfigWriting:
    def test_write_status_updates_the_file_in_place(self, tmp_path):
        path = tmp_path / "sources.yml"
        path.write_text(
            "google_news_base: https://news.google.com/rss/search\n"
            "google_news_params: {hl: en}\n"
            "sources:\n"
            "  - id: s1\n"
            "    type: google_news\n"
            "    query: test\n"
            "    enabled: true\n"
            "    status: unverified\n"
            "    last_checked: null\n"
            "    notes: a note\n",
            encoding="utf-8",
        )
        cfg = SourcesConfig.load(path)
        cfg.write_status({"s1": ("dead", "2026-07-29", "a note [checked: 404]")})

        reloaded = SourcesConfig.load(path)
        assert reloaded.sources[0].status == "dead"
        assert reloaded.sources[0].last_checked == "2026-07-29"
        assert "404" in reloaded.sources[0].notes

    def test_build_url_encodes_the_query(self):
        cfg = SourcesConfig(
            {
                "google_news_base": "https://news.google.com/rss/search",
                "google_news_params": {"hl": "en", "gl": "AE", "ceid": "AE:en"},
                "sources": [
                    {
                        "id": "s",
                        "type": "google_news",
                        "query": '("awarded contract") (UAE OR Dubai)',
                    }
                ],
            }
        )
        url = cfg.build_url(cfg.get("s"))
        assert "%22awarded+contract%22" in url
        assert " " not in url

    def test_build_url_uses_explicit_urls_for_plain_rss(self):
        cfg = SourcesConfig(
            {"sources": [{"id": "s", "type": "rss", "url": "https://a.test/feed.xml"}]}
        )
        assert cfg.build_url(cfg.get("s")) == "https://a.test/feed.xml"
