from __future__ import annotations

import pytest

from conftest import fixture
from radar import db, filters
from radar.config import load_keywords
from radar.models import ItemStatus, RawItem


@pytest.fixture
def gate() -> filters.KeywordGate:
    """Built from the real config/keywords.yml, so a bad edit fails the suite."""
    return filters.KeywordGate.from_config()


def add_enriched(conn, title: str, body: str | None = None) -> int:
    item_id = db.insert_raw_item(
        conn,
        RawItem(
            source_id="s",
            url=f"https://x.test/{abs(hash(title))}",
            url_hash=f"h{abs(hash(title))}",
            title=title,
        ),
    )
    db.update_item(conn, item_id, raw_text=body, status=ItemStatus.ENRICHED)
    return item_id


class TestKeywordGate:
    def test_award_plus_uae_passes(self, gate):
        assert gate.passes("ASGC awarded a construction contract in Dubai")

    def test_award_without_uae_fails(self, gate):
        assert not gate.passes("Skanska awarded a construction contract in Stockholm")

    def test_uae_without_award_fails(self, gate):
        assert not gate.passes("Dubai weather forecast for the weekend")

    def test_exclusion_vetoes_an_otherwise_passing_item(self, gate):
        assert not gate.passes("Hiring now: QA/QC engineer for an awarded Dubai contract")

    def test_sponsored_content_is_excluded(self, gate):
        assert not gate.passes("ASGC awarded Dubai contract. Sponsored content.")

    def test_evaluate_explains_the_verdict(self, gate):
        passed, hits = gate.evaluate("Emaar has appointed ALEC in Abu Dhabi")
        assert passed
        assert hits["award"] and hits["uae"]
        assert hits["exclusions"] == []

    def test_word_boundaries_prevent_substring_matches(self, gate):
        """Without boundaries 'AED' matches 'faded' and the gate stops filtering."""
        assert not gate.passes("The faded float was unremarkable in Dubai")

    def test_aed_matches_as_a_whole_word(self, gate):
        assert gate.passes("Contract worth AED 1.2 billion in Dubai")

    def test_multiword_phrases_tolerate_extra_whitespace(self, gate):
        assert gate.passes("Named as\n  main   contractor for the Dubai tower")

    def test_case_insensitive(self, gate):
        assert gate.passes("asgc AWARDED a contract in dubai")

    def test_empty_text_fails_safely(self, gate):
        assert not gate.passes("")

    def test_known_gap_split_wins_contract_phrasing(self, gate):
        """Documents a real miss in the brief's keyword list (DECISIONS.md D-07).

        "wins contract" is a fixed phrase, so the common headline shape
        "X wins <project> contract" does not match it. Such items are still
        caught when the body says "awarded", which is why this is a Phase 1
        tuning item against a measured pass rate rather than a blind fix now:
        loosening it to bare "wins" would drag in every sports and awards story.
        """
        assert not gate.passes("NMDC wins Abu Dhabi marine works package")
        # The body almost always rescues it.
        assert gate.passes(
            "NMDC wins Abu Dhabi marine works package\n"
            "The company was awarded the package by the client in Abu Dhabi."
        )

    def test_real_award_article_passes(self, gate):
        from radar import enrich

        text = enrich.extract_text(fixture("article_award.html"))
        assert gate.passes(text or "")

    def test_horoscope_article_is_rejected(self, gate):
        from radar import enrich

        text = enrich.extract_text(fixture("article_noise.html"))
        assert not gate.passes(text or "")


class TestKeywordsConfig:
    def test_shipped_keywords_yml_has_all_three_lists(self):
        kw = load_keywords()
        assert kw["award_signals"] and kw["uae_signals"] and kw["exclusions"]


class TestFilterEnriched:
    def test_passing_items_are_flagged_and_stay_enriched(self, conn, gate):
        add_enriched(conn, "ASGC awarded AED 1.2bn Dubai tower contract")
        stats = filters.filter_enriched(conn, gate)

        assert stats.passed == 1
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert row["passed_filter"] == 1
        assert row["status"] == "enriched"

    def test_failing_items_are_marked_filtered_out(self, conn, gate):
        add_enriched(conn, "Dubai weather forecast")
        stats = filters.filter_enriched(conn, gate)

        assert stats.passed == 0
        row = conn.execute("SELECT * FROM raw_items").fetchone()
        assert row["passed_filter"] == 0
        assert row["status"] == "filtered_out"

    def test_title_alone_is_enough_when_enrich_came_back_empty(self, conn, gate):
        add_enriched(conn, "NMDC awarded Abu Dhabi marine contract", body=None)
        assert filters.filter_enriched(conn, gate).passed == 1

    def test_body_can_carry_the_signal_the_title_lacks(self, conn, gate):
        add_enriched(
            conn, "Big news today", body="The developer awarded the main contract in Sharjah."
        )
        assert filters.filter_enriched(conn, gate).passed == 1

    def test_rerunning_filter_is_a_noop(self, conn, gate):
        add_enriched(conn, "Dubai weather forecast")
        filters.filter_enriched(conn, gate)
        # Filtered-out items are no longer 'enriched', so they are not re-read.
        assert filters.filter_enriched(conn, gate).evaluated == 0

    def test_pass_rate_is_reported(self, conn, gate):
        add_enriched(conn, "ASGC awarded a Dubai contract")
        add_enriched(conn, "Dubai weather forecast")
        add_enriched(conn, "Sharjah traffic update")
        stats = filters.filter_enriched(conn, gate)
        assert stats.evaluated == 3
        assert stats.pass_rate == pytest.approx(1 / 3)

    def test_pass_rate_of_an_empty_run_is_zero_not_an_error(self, conn, gate):
        assert filters.filter_enriched(conn, gate).pass_rate == 0.0
