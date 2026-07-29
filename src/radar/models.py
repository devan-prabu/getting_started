"""Pydantic models. These define the shape of everything downstream.

Language is a field, not an assumption (brief §13.5) — Arabic sources are out of
scope for v1 but must not require a rewrite to add.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The AED/USD peg is fixed by the UAE Central Bank. Hardcoded on purpose
# (brief §4): a live FX lookup would add a network dependency and a source of
# drift for no benefit.
AED_PER_USD = 3.6725


class Emirate(StrEnum):
    DUBAI = "Dubai"
    ABU_DHABI = "Abu Dhabi"
    SHARJAH = "Sharjah"
    AJMAN = "Ajman"
    RAS_AL_KHAIMAH = "Ras Al Khaimah"
    FUJAIRAH = "Fujairah"
    UMM_AL_QUWAIN = "Umm Al Quwain"
    UNKNOWN = "Unknown"


class Sector(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    INFRASTRUCTURE = "infrastructure"
    MARINE = "marine"
    OIL_GAS = "oil_gas"
    HOSPITALITY = "hospitality"
    LANDSCAPE = "landscape"
    OTHER = "other"


class ItemStatus(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    FILTERED_OUT = "filtered_out"
    EXTRACTED = "extracted"
    FAILED = "failed"


class SourceStatus(StrEnum):
    UNVERIFIED = "unverified"
    LIVE = "live"
    DEAD = "dead"


def utcnow_iso() -> str:
    """Timestamps are ISO 8601 UTC strings everywhere (brief §4)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Source(BaseModel):
    """One entry from config/sources.yml."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    url: str | None = None
    query: str | None = None
    enabled: bool = True
    status: SourceStatus = SourceStatus.UNVERIFIED
    last_checked: str | None = None
    notes: str | None = None

    @field_validator("notes", "url", "query", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def model_post_init(self, _ctx: Any) -> None:
        if self.type == "google_news" and not self.query:
            raise ValueError(f"source {self.id}: type=google_news requires a `query`")
        if self.type == "rss" and not self.url:
            raise ValueError(f"source {self.id}: type=rss requires a `url`")


class RawItem(BaseModel):
    """A collected article before any interpretation. The durable log.

    Everything downstream is derived and re-computable from this, so a failure
    in a later stage must never destroy one of these (brief §2).
    """

    id: int | None = None
    source_id: str
    url: str
    url_hash: str
    title: str | None = None
    published_at: str | None = None
    fetched_at: str = Field(default_factory=utcnow_iso)
    raw_text: str | None = None
    passed_filter: bool = False
    status: ItemStatus = ItemStatus.NEW
    language: str = "en"


class Award(BaseModel):
    """A structured contract-award record extracted from a RawItem.

    `value_aed` is None whenever the source did not state a figure. Never
    estimate it (brief §4, §13.7).
    """

    id: int | None = None
    raw_item_id: int
    developer: str | None = None
    contractor_raw: str | None = None
    contractor_canonical: str | None = None
    project_name: str | None = None
    value_aed: float | None = None
    emirate: str = Emirate.UNKNOWN
    sector: str = Sector.OTHER
    award_date: str | None = None
    scope_summary: str | None = None
    confidence: float = 0.0
    relevance_score: int | None = None
    hiring_window_start: str | None = None
    hiring_window_end: str | None = None
    extraction_model: str | None = None
    created_at: str = Field(default_factory=utcnow_iso)

    @field_validator("scope_summary")
    @classmethod
    def _cap_summary(cls, v: str | None) -> str | None:
        # Hard cap rather than a validation error: we store facts, not prose
        # (brief §9.6), and a slightly long summary is not worth losing a row.
        if v is None:
            return None
        v = " ".join(v.split())
        return v[:200]

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("value_aed")
    @classmethod
    def _reject_nonsense_value(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            return None
        return v

    @field_validator("award_date")
    @classmethod
    def _validate_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            date.fromisoformat(v)
        except ValueError:
            return None
        return v


class Company(BaseModel):
    """One entry from config/companies.yml."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    careers_url: str | None = None
    ats: str | None = None
    tier: int | None = None
    is_watchlist: bool = False
    parent: str | None = None
    notes: str | None = None


class AlertPayload(BaseModel):
    """What actually gets sent, independent of channel."""

    award_id: int
    title: str
    contractor: str | None = None
    developer: str | None = None
    project_name: str | None = None
    value_aed: float | None = None
    emirate: str | None = None
    sector: str | None = None
    award_date: str | None = None
    published_at: str | None = None
    source_url: str
    careers_url: str | None = None
    hiring_window_start: str | None = None
    hiring_window_end: str | None = None
    relevance_score: int | None = None
    confidence: float | None = None
