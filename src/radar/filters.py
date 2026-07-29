"""FILTER — the cheap keyword gate that runs before any LLM spend (brief §6.3).

Rule: pass if >=1 award signal AND >=1 UAE signal AND 0 exclusions.
Expect roughly 15% to pass. The pass rate is logged every run; if it drifts far
from that, the queries in sources.yml need tuning, not this file.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from . import db
from .config import load_keywords
from .models import ItemStatus

log = logging.getLogger(__name__)

EXPECTED_PASS_RATE = 0.15
PASS_RATE_TOLERANCE = 0.10  # log a warning outside 5%-25%


def _compile(term: str) -> re.Pattern[str]:
    r"""Whole-word/phrase matching, case-insensitive.

    Word boundaries matter here: without them "AED" matches inside "faded" and
    "LOA" matches inside "float", and the gate stops filtering anything.
    """
    escaped = r"\s+".join(re.escape(part) for part in term.split())
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


@dataclass
class KeywordGate:
    award_signals: list[re.Pattern[str]]
    uae_signals: list[re.Pattern[str]]
    exclusions: list[re.Pattern[str]]

    @classmethod
    def from_config(cls, keywords: dict[str, list[str]] | None = None) -> KeywordGate:
        kw = keywords if keywords is not None else load_keywords()
        return cls(
            award_signals=[_compile(t) for t in kw["award_signals"]],
            uae_signals=[_compile(t) for t in kw["uae_signals"]],
            exclusions=[_compile(t) for t in kw["exclusions"]],
        )

    @staticmethod
    def _hits(patterns: list[re.Pattern[str]], text: str) -> list[str]:
        return [p.pattern for p in patterns if p.search(text)]

    def evaluate(self, text: str) -> tuple[bool, dict[str, list[str]]]:
        """Return (passed, hits-by-category) so decisions are explainable."""
        text = text or ""
        hits = {
            "award": self._hits(self.award_signals, text),
            "uae": self._hits(self.uae_signals, text),
            "exclusions": self._hits(self.exclusions, text),
        }
        passed = bool(hits["award"]) and bool(hits["uae"]) and not hits["exclusions"]
        return passed, hits

    def passes(self, text: str) -> bool:
        return self.evaluate(text)[0]


@dataclass
class FilterStats:
    evaluated: int = 0
    passed: int = 0
    excluded: int = 0
    hit_counts: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.evaluated if self.evaluated else 0.0

    def as_line(self) -> str:
        return (
            f"evaluated={self.evaluated} passed={self.passed} "
            f"excluded={self.excluded} pass_rate={self.pass_rate:.0%}"
        )


def item_text(item: sqlite3.Row) -> str:
    """Title plus body. Title alone still carries signal when enrich came back
    empty, so it is always included."""
    title = item["title"] or ""
    body = item["raw_text"] or ""
    return f"{title}\n{body}"


def filter_enriched(conn: sqlite3.Connection, gate: KeywordGate | None = None) -> FilterStats:
    """Apply the gate to every enriched item and record the verdict."""
    gate = gate or KeywordGate.from_config()
    stats = FilterStats()

    for item in db.get_items_by_status(conn, ItemStatus.ENRICHED):
        stats.evaluated += 1
        passed, hits = gate.evaluate(item_text(item))
        if hits["exclusions"]:
            stats.excluded += 1
        if passed:
            stats.passed += 1
            # Status stays ENRICHED; the extractor picks these up by
            # passed_filter, so a re-run of extract does not need a re-filter.
            db.update_item(conn, item["id"], passed_filter=True)
            for term in hits["award"]:
                stats.hit_counts[term] = stats.hit_counts.get(term, 0) + 1
        else:
            db.update_item(conn, item["id"], passed_filter=False, status=ItemStatus.FILTERED_OUT)

    log.info("filter: %s", stats.as_line())
    if stats.evaluated >= 20 and abs(stats.pass_rate - EXPECTED_PASS_RATE) > PASS_RATE_TOLERANCE:
        log.warning(
            "filter pass rate %.0f%% is far from the expected ~15%% — "
            "the queries in sources.yml probably need tuning",
            stats.pass_rate * 100,
        )
    return stats
