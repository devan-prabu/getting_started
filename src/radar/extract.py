"""EXTRACT — Phase 0 regex/keyword extraction.

This is deliberately rough. The brief expects it to be (§10, Phase 0), and §13.3
is explicit that regex will get contractor and developer backwards on sentences
like "ALEC awards MEP package to Y". So every record this produces carries a low
confidence, capped below the point where anyone should trust it as fact.

Phase 2 replaces the body of `extract_from_text` with an LLM behind a swappable
`Extractor` protocol. The awards table, the alert path and the idempotency
guarantees are all exercised now so that swap is the only change needed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from . import db
from .models import AED_PER_USD, Award, Emirate, ItemStatus, Sector

log = logging.getLogger(__name__)

EXTRACTION_MODEL = "regex-v0"

# Regex cannot resolve who won from who paid, so nothing here is allowed to look
# confident. Anything above this belongs to the Phase 2 LLM stage.
MAX_REGEX_CONFIDENCE = 0.45

_SCALES = {
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
    "m": 1_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
    "k": 1_000,
    "thousand": 1_000,
}

# A company name: a run of capitalised words, allowing & and -. Greedy enough to
# catch "Al Shafar General Contracting", tight enough not to swallow a sentence.
#
# Full stops are deliberately excluded from the token. Allowing them let a name
# run straight across a sentence boundary and produce "National. Emaar" as a
# single company. The cost is that "ALEC L.L.C." captures as "ALEC L", which the
# alias table handles.
_NAME = r"[A-Z][\w&'-]*(?:\s+(?:[A-Z][\w&'-]*|and|of|for|the|Al|El|bin|&)){0,5}"

# Google News appends " - Publisher" to every headline. Left in place it becomes
# the first capitalised run the name regex sees.
_PUBLISHER_SUFFIX = re.compile(r"\s+-\s+[^-]{2,40}$")

_AED_VALUE = re.compile(
    r"(?:AED|Dhs?|dirhams?)\s*([\d]+(?:[.,]\d+)*)\s*"
    r"(billion|bn|million|mn|thousand|[bmk])?\b",
    re.IGNORECASE,
)
_USD_VALUE = re.compile(
    r"(?:US\$|USD|\$)\s*([\d]+(?:[.,]\d+)*)\s*"
    r"(billion|bn|million|mn|thousand|[bmk])?\b",
    re.IGNORECASE,
)

# "<Contractor> has been awarded / wins / has won ..."
_WINNER_FIRST = re.compile(
    rf"\b({_NAME})\s+(?:has\s+been\s+awarded|was\s+awarded|has\s+won|wins|won)\b"
)
# "<Developer> awards/has awarded/appoints ... to <Contractor>"
_CLIENT_FIRST = re.compile(
    rf"\b({_NAME})\s+(?:has\s+)?(?:awarded|awards)\b[^.]{{0,120}}?\bto\s+({_NAME})"
)
# "<Developer> has appointed <Contractor>"
_APPOINTS = re.compile(
    rf"\b({_NAME})\s+(?:has\s+appointed|appoints|has\s+selected|selects)\s+({_NAME})"
)
# "... awarded to <Contractor>" with no visible client
_AWARDED_TO = re.compile(rf"\bawarded\s+to\s+({_NAME})")

_SECTOR_TERMS: list[tuple[str, tuple[str, ...]]] = [
    (Sector.MARINE, ("marine", "dredging", "quay", "breakwater", "port ", "harbour", "jetty")),
    (Sector.OIL_GAS, ("oil", "gas", "refinery", "petrochemical", "adnoc", "pipeline", "lng")),
    (
        Sector.INFRASTRUCTURE,
        (
            "road",
            "bridge",
            "metro",
            "rail",
            "tunnel",
            "sewer",
            "substation",
            "highway",
            "interchange",
            "utility",
            "infrastructure",
            "water treatment",
        ),
    ),
    (Sector.HOSPITALITY, ("hotel", "resort", "hospitality", "serviced apartment")),
    (Sector.LANDSCAPE, ("landscape", "landscaping", "softscape", "hardscape", "park ")),
    (Sector.RESIDENTIAL, ("residential", "villa", "apartment", "housing", "townhouse")),
    (
        Sector.COMMERCIAL,
        ("commercial", "office tower", "mall", "retail", "warehouse", "business park"),
    ),
]

_EMIRATES = [
    (Emirate.ABU_DHABI, ("abu dhabi", "abudhabi", "al ain", "yas island", "saadiyat")),
    (Emirate.DUBAI, ("dubai", "business bay", "jebel ali", "palm jumeirah", "downtown dubai")),
    (Emirate.SHARJAH, ("sharjah",)),
    (Emirate.AJMAN, ("ajman",)),
    (Emirate.RAS_AL_KHAIMAH, ("ras al khaimah", "rak ")),
    (Emirate.FUJAIRAH, ("fujairah",)),
    (Emirate.UMM_AL_QUWAIN, ("umm al quwain",)),
]

# Words that mean the capture grabbed a sentence opener, not a company.
_NOT_A_COMPANY = {
    "the",
    "a",
    "an",
    "it",
    "this",
    "that",
    "these",
    "those",
    "he",
    "she",
    "they",
    "we",
    "i",
    "in",
    "on",
    "at",
    "as",
    "but",
    "and",
    "however",
    "meanwhile",
    "earlier",
    "recently",
    "yesterday",
    "today",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "contract",
    "contractor",
    "project",
    "construction",
    "company",
    "work",
    "works",
    "package",
    "deal",
    "agreement",
    "tender",
}


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = " ".join(name.split()).strip(" ,.;:-&")
    if not name:
        return None
    first = name.split()[0].lower()
    if first in _NOT_A_COMPANY:
        # Drop the leading filler and retry, e.g. "The ALEC" -> "ALEC".
        rest = " ".join(name.split()[1:])
        return _clean_name(rest) if rest else None
    if len(name) < 2:
        return None
    return name


def _to_number(digits: str, scale: str | None) -> float | None:
    # "1,200" is a thousands separator; "1.2" is a decimal. Strip commas, keep dots.
    cleaned = digits.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if scale:
        value *= _SCALES.get(scale.lower(), 1)
    return value


def parse_value_aed(text: str) -> tuple[float | None, str | None]:
    """Return (value in AED, the string the source actually used).

    Never estimates. If no figure is stated, this returns (None, None) and that
    is a perfectly good answer (brief §13.7).
    """
    match = _AED_VALUE.search(text)
    if match:
        value = _to_number(match.group(1), match.group(2))
        return value, match.group(0).strip()

    match = _USD_VALUE.search(text)
    if match:
        value = _to_number(match.group(1), match.group(2))
        if value is not None:
            # Fixed peg, hardcoded on purpose (brief §4).
            return round(value * AED_PER_USD, 2), match.group(0).strip()
    return None, None


def detect_emirate(text: str) -> str:
    lowered = f" {text.lower()} "
    for emirate, terms in _EMIRATES:
        if any(term in lowered for term in terms):
            return str(emirate)
    return str(Emirate.UNKNOWN)


def detect_sector(text: str) -> str:
    """Pick the sector with the most matching terms, not the first in the list.

    First-match-by-list-order tagged a residential tower as hospitality because
    a closing paragraph mentioned the contractor's past hotel work. Counting
    distinct matching terms lets the article's actual subject win, with list
    order only breaking ties.
    """
    lowered = text.lower()
    best_sector = str(Sector.OTHER)
    best_score = 0
    for sector, terms in _SECTOR_TERMS:
        score = sum(1 for term in terms if term in lowered)
        if score > best_score:
            best_score = score
            best_sector = str(sector)
    return best_sector


@dataclass
class ExtractedAward:
    is_award: bool
    developer: str | None = None
    contractor: str | None = None
    value_aed: float | None = None
    value_stated_as: str | None = None
    emirate: str = str(Emirate.UNKNOWN)
    sector: str = str(Sector.OTHER)
    confidence: float = 0.0
    scope_summary: str | None = None


def extract_from_text(title: str | None, body: str | None) -> ExtractedAward:
    """Best-effort structured facts from an article. Rough by design."""
    title = _PUBLISHER_SUFFIX.sub("", (title or "").strip()).strip()
    body = (body or "").strip()
    text = f"{title}. {body}" if title else body
    if not text.strip():
        return ExtractedAward(is_award=False)

    # Only the opening of an article states the award; further down is background
    # that produces false matches.
    head = text[:1500]

    developer: str | None = None
    contractor: str | None = None
    confidence = 0.20

    if (m := _CLIENT_FIRST.search(head)) or (m := _APPOINTS.search(head)):
        developer, contractor = _clean_name(m.group(1)), _clean_name(m.group(2))
        confidence = 0.40
    elif m := _WINNER_FIRST.search(head):
        contractor = _clean_name(m.group(1))
        confidence = 0.35
    elif m := _AWARDED_TO.search(head):
        contractor = _clean_name(m.group(1))
        confidence = 0.30

    if contractor is None:
        # No parseable award sentence. The keyword gate let it through, so keep
        # the row, but say plainly that nothing was identified.
        confidence = 0.10

    if developer and contractor and developer.lower() == contractor.lower():
        # Same name on both sides means the sentence was misread.
        developer = None
        confidence = min(confidence, 0.20)

    value_aed, value_stated = parse_value_aed(head)

    # Never let a regex look confident (brief §13.3).
    confidence = min(confidence, MAX_REGEX_CONFIDENCE)

    return ExtractedAward(
        is_award=contractor is not None,
        developer=developer,
        contractor=contractor,
        value_aed=value_aed,
        value_stated_as=value_stated,
        emirate=detect_emirate(head),
        sector=detect_sector(head),
        confidence=confidence,
        # Phase 0 stores only what the source literally stated about value, not
        # a summary of the article. Never copy sentences (brief §9.6).
        scope_summary=(
            f"Phase 0 regex extraction; value stated as {value_stated}."
            if value_stated
            else "Phase 0 regex extraction; no value stated."
        ),
    )


@dataclass
class ExtractStats:
    considered: int = 0
    awards: int = 0
    not_awards: int = 0

    def as_line(self) -> str:
        return f"considered={self.considered} awards={self.awards} not_awards={self.not_awards}"


def pending_items(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Filtered items with no award row yet."""
    sql = """
        SELECT r.* FROM raw_items r
        WHERE r.passed_filter = 1
          AND r.status = ?
          AND NOT EXISTS (SELECT 1 FROM awards a WHERE a.raw_item_id = r.id)
        ORDER BY r.id
    """
    params: list[object] = [str(ItemStatus.ENRICHED)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def extract_pending(conn: sqlite3.Connection, *, limit: int | None = None) -> ExtractStats:
    """Turn filtered items into award rows. Re-running adds nothing new."""
    stats = ExtractStats()
    for item in pending_items(conn, limit):
        stats.considered += 1
        result = extract_from_text(item["title"], item["raw_text"])

        if not result.is_award:
            stats.not_awards += 1
            db.update_item(conn, item["id"], status=ItemStatus.FILTERED_OUT)
            continue

        award = Award(
            raw_item_id=int(item["id"]),
            developer=result.developer,
            contractor_raw=result.contractor,
            # Phase 0 does no company matching; normalise.py fills this in Phase 2.
            contractor_canonical=None,
            value_aed=result.value_aed,
            emirate=result.emirate,
            sector=result.sector,
            # The award date is not the publish date, and regex cannot tell them
            # apart reliably. NULL rather than wrong (brief §6.4).
            award_date=None,
            scope_summary=result.scope_summary,
            confidence=result.confidence,
            extraction_model=EXTRACTION_MODEL,
        )
        db.insert_award(conn, award)
        db.update_item(conn, item["id"], status=ItemStatus.EXTRACTED)
        stats.awards += 1

    log.info("extract: %s", stats.as_line())
    return stats
