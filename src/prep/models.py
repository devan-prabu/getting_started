"""Pydantic models for the prep agent.

The profile is split in two on purpose: everything the user writes lives in
`Profile`, and everything the agent maintains lives in `Adaptive`. Only the
second is ever written back to disk, so a rewrite can never eat a hand-edited
career goal (DECISIONS.md D-17).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Answers below this are treated as "did not know it" by the scheduler.
WEAK_SCORE = 60
# Two answers at or above this park a topic in `resting`.
STRONG_SCORE = 85


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def days_from_today(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class Stage(StrEnum):
    HR_SCREEN = "hr_screen"
    TECHNICAL = "technical"
    CONSULTANT_PANEL = "consultant_panel"
    CLIENT_FACING = "client_facing"
    BEHAVIOURAL = "behavioural"


class QuestionSource(StrEnum):
    BANK = "bank"
    CLAUDE = "claude"


class Grader(StrEnum):
    CLAUDE = "claude"
    HEURISTIC = "heuristic"
    SELF = "self"


class Seed(BaseModel):
    """One banked question from config/topics.yml."""

    model_config = ConfigDict(extra="forbid")

    q: str
    expect: list[str] = Field(default_factory=list)
    difficulty: int = 3

    @field_validator("difficulty")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(1, min(5, v))


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    weight: float = 1.0
    stages: list[str] = Field(default_factory=list)
    seeds: list[Seed] = Field(default_factory=list)


class Compensation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current_aed_month: float | None = None
    target_min_aed_month: float | None = None
    target_stretch_aed_month: float | None = None
    must_beat_current: bool = True
    non_salary_musts: list[str] = Field(default_factory=list)


class Identity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "you"
    base: str | None = None
    current_employer: str | None = None
    current_title: str | None = None
    current_project: str | None = None
    years_total: float | None = None
    years_gcc: float | None = None
    education: str | None = None
    languages: list[str] = Field(default_factory=list)
    licenses: list[str] = Field(default_factory=list)
    notice_period_days: int | None = None


class Target(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_role: str = "QA/QC Engineer"
    secondary_role: str | None = None
    level: str | None = None
    employer_types: list[str] = Field(default_factory=list)
    emirates: list[str] = Field(default_factory=list)
    motivation: str | None = None
    compensation: Compensation = Field(default_factory=Compensation)
    interview_window_from: str | None = None
    companies_tier1: list[str] = Field(default_factory=list)
    companies_tier2: list[str] = Field(default_factory=list)

    @field_validator("interview_window_from")
    @classmethod
    def _valid_date(cls, v: str | None) -> str | None:
        if not v:
            return None
        try:
            date.fromisoformat(str(v))
        except ValueError:
            return None
        return str(v)

    def days_to_interview_window(self, today: date | None = None) -> int | None:
        if not self.interview_window_from:
            return None
        return (date.fromisoformat(self.interview_window_from) - (today or date.today())).days


class Delivery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timezone: str = "Asia/Dubai"
    ask_hour_local: int = 20
    questions_per_day: int = 3
    weekend_questions: int = 5
    weekend_days: list[str] = Field(default_factory=lambda: ["Saturday", "Sunday"])

    @field_validator("ask_hour_local")
    @classmethod
    def _clamp_hour(cls, v: int) -> int:
        return max(0, min(23, v))

    @field_validator("questions_per_day", "weekend_questions")
    @classmethod
    def _clamp_count(cls, v: int) -> int:
        # A drill you cannot finish on a phone is a drill you stop answering.
        return max(1, min(10, v))


class Adaptive(BaseModel):
    """The machine-maintained block of profile.yml.

    This is the whole point of the agent: it is rewritten from measured
    performance and from the commands sent to the bot, and the system prompt is
    rebuilt from it every run.
    """

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    updated_at: str | None = None
    updated_reason: str | None = None
    focus: list[str] = Field(default_factory=list)
    resting: list[str] = Field(default_factory=list)
    difficulty: int = 3
    notes: list[str] = Field(default_factory=list)

    @field_validator("difficulty")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(1, min(5, v))

    def as_yaml_dict(self) -> dict[str, Any]:
        """Written straight back into profile.yml under `adaptive:`."""
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "updated_reason": self.updated_reason,
            "focus": list(self.focus),
            "resting": list(self.resting),
            "difficulty": self.difficulty,
            "notes": list(self.notes),
        }


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    identity: Identity = Field(default_factory=Identity)
    target: Target = Field(default_factory=Target)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    delivery: Delivery = Field(default_factory=Delivery)
    adaptive: Adaptive = Field(default_factory=Adaptive)

    def questions_for(self, day_name: str) -> int:
        d = self.delivery
        if day_name in d.weekend_days:
            return d.weekend_questions
        return d.questions_per_day


class Question(BaseModel):
    """A question to ask, whether banked or generated."""

    id: int | None = None
    topic: str
    text: str
    expect: list[str] = Field(default_factory=list)
    difficulty: int = 3
    stage: str | None = None
    source: str = QuestionSource.BANK
    model: str | None = None
    context_version: int | None = None
    asked_on: str | None = None
    created_at: str = Field(default_factory=utcnow_iso)

    @field_validator("text")
    @classmethod
    def _tidy(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("question text is empty")
        return v

    @field_validator("difficulty")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(1, min(5, v))

    def dedupe_key(self) -> str:
        """Lowercased text, so the bank never asks the same thing twice in a row."""
        return " ".join(self.text.lower().split())


class Grade(BaseModel):
    """The verdict on one answer."""

    score: int
    feedback: str
    missing: list[str] = Field(default_factory=list)
    grader: str = Grader.HEURISTIC
    model: str | None = None

    @field_validator("score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("feedback")
    @classmethod
    def _cap(cls, v: str) -> str:
        # It is read on a phone. Long feedback does not get read.
        v = " ".join(v.split())
        return v[:900]


class Command(BaseModel):
    """A `/command` parsed out of a Telegram message."""

    name: str
    argument: str = ""
    message_id: int | None = None


class TopicState(BaseModel):
    """Spaced-repetition state for one topic."""

    topic: str
    asked_count: int = 0
    answered_count: int = 0
    avg_score: float | None = None
    last_score: int | None = None
    last_asked: str | None = None
    due_on: str | None = None
    interval_days: int = 1
    strong_streak: int = 0

    def is_due(self, today: str | None = None) -> bool:
        if self.due_on is None:
            return True
        return self.due_on <= (today or today_iso())
