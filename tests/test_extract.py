from __future__ import annotations

import pytest

from conftest import fixture
from radar import db, extract
from radar.models import AED_PER_USD, ItemStatus, RawItem


def add_filtered(conn, title: str, body: str | None = None) -> int:
    item_id = db.insert_raw_item(
        conn,
        RawItem(
            source_id="s",
            url=f"https://x.test/{abs(hash(title))}",
            url_hash=f"h{abs(hash(title))}",
            title=title,
        ),
    )
    db.update_item(conn, item_id, raw_text=body, passed_filter=True, status=ItemStatus.ENRICHED)
    return item_id


class TestParseValue:
    def test_aed_billions(self):
        value, stated = extract.parse_value_aed("a contract worth AED 1.2 billion")
        assert value == 1_200_000_000
        assert "1.2 billion" in stated

    def test_aed_shorthand(self):
        assert extract.parse_value_aed("worth AED 750m")[0] == 750_000_000

    def test_dirham_abbreviation(self):
        assert extract.parse_value_aed("a Dh500 million deal")[0] == 500_000_000

    def test_thousands_separator_is_not_a_decimal(self):
        assert extract.parse_value_aed("worth AED 1,250,000")[0] == 1_250_000

    def test_usd_is_converted_at_the_peg(self):
        value, stated = extract.parse_value_aed("a $340 million package")
        assert value == pytest.approx(340_000_000 * AED_PER_USD)
        assert "340 million" in stated

    def test_aed_wins_over_usd_when_both_appear(self):
        assert extract.parse_value_aed("AED 1 billion ($272 million)")[0] == 1_000_000_000

    def test_undisclosed_value_stays_none(self):
        """Never estimate a figure the source did not state (brief §13.7)."""
        assert extract.parse_value_aed("the value was not disclosed") == (None, None)

    def test_no_figure_at_all(self):
        assert extract.parse_value_aed("ALEC was appointed main contractor") == (None, None)


class TestDetectors:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("a tower in Business Bay", "Dubai"),
            ("works on Yas Island", "Abu Dhabi"),
            ("a project in Sharjah", "Sharjah"),
            ("a plant in Fujairah", "Fujairah"),
            ("somewhere unspecified", "Unknown"),
        ],
    )
    def test_emirate_detection(self, text, expected):
        assert extract.detect_emirate(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("quay wall and dredging works", "marine"),
            ("a gas processing facility", "oil_gas"),
            ("metro line package", "infrastructure"),
            ("a beachfront resort", "hospitality"),
            ("softscape and hardscape works", "landscape"),
            ("a residential tower of 640 apartments", "residential"),
            ("an office tower", "commercial"),
            ("something unclassifiable", "other"),
        ],
    )
    def test_sector_detection(self, text, expected):
        assert extract.detect_sector(text) == expected

    def test_sector_is_the_article_subject_not_a_passing_mention(self):
        """Caught in a live dry-run: a residential tower was tagged hospitality
        because the closing paragraph mentioned the contractor's past hotel work.
        The dominant sector must win, not whichever appears first in the list."""
        text = (
            "a residential tower comprising 640 apartments and townhouses. "
            "The contractor has also delivered hospitality projects."
        )
        assert extract.detect_sector(text) == "residential"


class TestExtractFromText:
    def test_client_first_sentence_gets_both_sides_right(self):
        result = extract.extract_from_text(
            "Emaar appoints ALEC for Business Bay tower",
            "Emaar Development has appointed ALEC Engineering and Contracting as main "
            "contractor for a residential tower in Business Bay, Dubai, worth AED 1.2 billion.",
        )
        assert result.is_award
        assert result.contractor.startswith("ALEC")
        assert result.developer.startswith("Emaar")
        assert result.value_aed == 1_200_000_000
        assert result.emirate == "Dubai"
        assert result.sector == "residential"

    def test_awarded_to_pattern(self):
        result = extract.extract_from_text(
            "Yas Island villas package awarded",
            "Aldar Properties has awarded the villas package to Trojan General Contracting.",
        )
        assert result.contractor == "Trojan General Contracting"
        assert result.developer.startswith("Aldar")

    def test_winner_first_sentence(self):
        result = extract.extract_from_text(
            "ASGC awarded tower contract",
            "ASGC has been awarded a contract for a residential tower in Dubai.",
        )
        assert result.contractor == "ASGC"
        assert result.is_award

    def test_confidence_never_looks_trustworthy(self):
        """Regex cannot tell the winner from the client (brief §13.3)."""
        result = extract.extract_from_text(
            "Emaar appoints ALEC", "Emaar Development has appointed ALEC as main contractor."
        )
        assert result.confidence <= extract.MAX_REGEX_CONFIDENCE
        assert extract.MAX_REGEX_CONFIDENCE < 0.5

    def test_no_award_sentence_yields_no_contractor(self):
        result = extract.extract_from_text(
            "Construction spending rises in Dubai",
            "Analysts expect more contract awards in the UAE this year.",
        )
        assert not result.is_award
        assert result.contractor is None

    def test_empty_input_is_not_an_award(self):
        assert not extract.extract_from_text(None, None).is_award
        assert not extract.extract_from_text("", "").is_award

    def test_same_name_on_both_sides_drops_the_developer(self):
        result = extract.extract_from_text("ALEC awarded", "ALEC has awarded the package to ALEC.")
        assert result.developer is None

    def test_leading_filler_is_stripped_from_a_name(self):
        result = extract.extract_from_text(
            "Tower contract awarded", "The contract was awarded to ALEC Engineering."
        )
        assert result.contractor is not None
        assert not result.contractor.lower().startswith("the ")

    def test_google_news_publisher_suffix_is_not_read_as_a_company(self):
        """Caught in a live dry-run: the " - The National" suffix that Google
        News appends became the first capitalised run the name regex saw, and
        the developer came out as "National. Emaar"."""
        result = extract.extract_from_text(
            "ASGC awarded Dubai tower contract - The National",
            "Emaar Development has appointed ASGC as main contractor in Dubai.",
        )
        assert result.developer is None or "National" not in result.developer
        assert result.contractor is None or "National" not in result.contractor

    def test_a_company_name_never_spans_a_sentence_boundary(self):
        result = extract.extract_from_text(
            "Contract news",
            "Work continues in Dubai. Emaar has appointed ASGC as main contractor.",
        )
        assert result.developer == "Emaar"

    def test_scope_summary_never_copies_source_sentences(self):
        """Brief §9.6 — store facts, not prose."""
        body = (
            "Emaar Development has appointed ALEC as main contractor for a residential "
            "tower in Business Bay, Dubai, worth AED 1.2 billion."
        )
        result = extract.extract_from_text("Emaar appoints ALEC", body)
        assert result.scope_summary
        assert result.scope_summary not in body
        assert "has appointed ALEC as main contractor" not in result.scope_summary

    def test_works_on_the_real_article_fixture(self):
        from radar import enrich

        text = enrich.extract_text(fixture("article_award.html"))
        result = extract.extract_from_text(
            "Emaar appoints ALEC as main contractor for Business Bay tower", text
        )
        assert result.contractor.startswith("ALEC")
        assert result.value_aed == 1_200_000_000


class TestExtractPending:
    def test_creates_an_award_row_and_marks_the_item(self, conn):
        add_filtered(
            conn,
            "Emaar appoints ALEC",
            "Emaar Development has appointed ALEC as main contractor in Dubai.",
        )
        stats = extract.extract_pending(conn)

        assert stats.awards == 1
        award = conn.execute("SELECT * FROM awards").fetchone()
        assert award["contractor_raw"].startswith("ALEC")
        assert award["extraction_model"] == "regex-v0"
        # Phase 0 does no company matching.
        assert award["contractor_canonical"] is None
        # The award date is not the publish date; NULL beats wrong.
        assert award["award_date"] is None
        assert conn.execute("SELECT status FROM raw_items").fetchone()[0] == "extracted"

    def test_non_award_items_are_filtered_out_not_stored(self, conn):
        add_filtered(
            conn,
            "Construction spending rises in Dubai",
            "Analysts expect more awards this year in the UAE.",
        )
        stats = extract.extract_pending(conn)

        assert stats.awards == 0
        assert stats.not_awards == 1
        assert conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM raw_items").fetchone()[0] == "filtered_out"

    def test_unfiltered_items_are_ignored(self, conn):
        item_id = db.insert_raw_item(
            conn,
            RawItem(
                source_id="s",
                url="https://x.test/a",
                url_hash="ha",
                title="Emaar has appointed ALEC in Dubai",
            ),
        )
        db.update_item(conn, item_id, status=ItemStatus.ENRICHED, passed_filter=False)
        assert extract.extract_pending(conn).considered == 0

    def test_rerunning_creates_no_duplicate_awards(self, conn):
        add_filtered(
            conn,
            "Emaar appoints ALEC",
            "Emaar Development has appointed ALEC as main contractor in Dubai.",
        )
        extract.extract_pending(conn)
        second = extract.extract_pending(conn)

        assert second.considered == 0
        assert conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 1

    def test_limit_caps_the_batch(self, conn):
        for i in range(3):
            add_filtered(
                conn,
                f"Emaar appoints ALEC {i}",
                f"Emaar Development has appointed ALEC {i} in Dubai.",
            )
        assert extract.extract_pending(conn, limit=2).considered == 2
