"""Grading an answer.

`ClaudeGrader` is the real one: it sees the question, the answer key and the
same system prompt the question was written under, and returns a score plus
short feedback.

`HeuristicGrader` is what runs with no API key. It measures coverage of the
answer key with fuzzy phrase matching — crude, but it is honest about being
crude, and it produces the one thing the adaptive loop actually needs: a number
that is lower when you missed the point. Its ceiling is capped at 80 so a
heuristic pass can never park a topic in `resting` on its own.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from rapidfuzz import fuzz

from .generate import (
    ANTHROPIC_API,
    ANTHROPIC_VERSION,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    _extract_json,
)
from .models import Grade, Grader

log = logging.getLogger(__name__)

MAX_TOKENS = 700
HEURISTIC_CEILING = 80
# A phrase counts as covered at this partial-ratio. 82 tolerates "95 percent
# compaction" for "95 percent" without matching unrelated text.
PHRASE_HIT = 82
# Below this many characters, no answer is a real answer.
MIN_SERIOUS_ANSWER = 40

_WORD = re.compile(r"[a-z0-9]+")


class GradingError(Exception):
    """The grader was configured but could not produce a verdict."""


class GraderBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def grade(
        self, question: str, expect: list[str], answer: str, *, system_prompt: str = ""
    ) -> Grade: ...


def _normalise(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


@dataclass
class HeuristicGrader:
    """Coverage of the answer key, with no pretence of understanding."""

    name: str = Grader.HEURISTIC

    def available(self) -> bool:
        return True

    def grade(
        self, question: str, expect: list[str], answer: str, *, system_prompt: str = ""
    ) -> Grade:
        body = _normalise(answer)
        if not body:
            return Grade(
                score=0,
                feedback="No answer recorded.",
                missing=list(expect),
                grader=Grader.HEURISTIC,
            )

        if not expect:
            # Nothing to check against: score on effort and say so plainly.
            score = 55 if len(body) >= MIN_SERIOUS_ANSWER else 25
            return Grade(
                score=score,
                feedback=(
                    "No answer key for this question, so this is an effort score only. "
                    "Set ANTHROPIC_API_KEY to get it graded properly."
                ),
                grader=Grader.HEURISTIC,
            )

        hit, missing = [], []
        for phrase in expect:
            target = _normalise(phrase)
            if not target:
                continue
            if fuzz.partial_ratio(target, body) >= PHRASE_HIT:
                hit.append(phrase)
            else:
                missing.append(phrase)

        checked = len(hit) + len(missing)
        coverage = len(hit) / checked if checked else 0.0
        score = int(round(coverage * HEURISTIC_CEILING))
        if len(body) < MIN_SERIOUS_ANSWER:
            score = min(score, 30)

        if not missing:
            feedback = f"Covered all {len(hit)} points the key looks for."
        else:
            shown = ", ".join(missing[:5])
            feedback = (
                f"Hit {len(hit)} of {checked} key points. "
                f"Not mentioned: {shown}"
                f"{'…' if len(missing) > 5 else ''}. "
                "Keyword coverage only — no API key set, so nothing judged the reasoning."
            )
        return Grade(score=score, feedback=feedback, missing=missing, grader=Grader.HEURISTIC)


GRADE_TEMPLATE = """\
Grade this drill answer.

QUESTION:
{question}

ANSWER KEY (what a strong answer contains):
{expect}

HIS ANSWER:
{answer}

Reply with JSON only:
{{"score": 0-100, "feedback": "at most three short lines", "missing": ["point", ...]}}

Score honestly against the key and against what the target-role panel would
accept. Do not inflate. `missing` lists the key points he did not cover.
"""


@dataclass
class ClaudeGrader:
    api_key: str | None = None
    model: str | None = None
    client: httpx.Client | None = None
    name: str = Grader.CLAUDE

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.model = self.model or os.environ.get("PREP_MODEL", DEFAULT_MODEL).strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def grade(
        self, question: str, expect: list[str], answer: str, *, system_prompt: str = ""
    ) -> Grade:
        expect_block = "\n".join(f"- {e}" for e in expect) or "- (no key supplied)"
        payload_text = GRADE_TEMPLATE.format(
            question=question, expect=expect_block, answer=answer.strip()
        )
        client = self.client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        try:
            resp = client.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key": self.api_key or "",
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": MAX_TOKENS,
                    "system": system_prompt or "You grade interview answers honestly.",
                    "messages": [{"role": "user", "content": payload_text}],
                },
            )
        except httpx.HTTPError as exc:
            raise GradingError(f"Anthropic request failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()

        if resp.status_code != 200:
            raise GradingError(f"Anthropic returned {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise GradingError("Anthropic returned non-JSON") from exc

        text = "".join(
            b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
        )
        try:
            data = _extract_json(text)
        except Exception as exc:  # noqa: BLE001 — any parse failure means fall back
            raise GradingError(f"could not parse the verdict: {exc}") from exc

        try:
            score = int(float(data["score"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise GradingError("verdict had no usable score") from exc

        return Grade(
            score=score,
            feedback=str(data.get("feedback", "")).strip() or "No feedback returned.",
            missing=[str(m) for m in (data.get("missing") or [])],
            grader=Grader.CLAUDE,
            model=self.model,
        )


def build_grader(client: httpx.Client | None = None) -> GraderBackend:
    backend = os.environ.get("PREP_GRADER", "claude").strip().lower()
    if backend in ("heuristic", "none", "offline"):
        return HeuristicGrader()
    claude = ClaudeGrader(client=client)
    if not claude.available():
        log.info("ANTHROPIC_API_KEY not set — grading on answer-key coverage only")
        return HeuristicGrader()
    return claude


def grade_answer(
    grader: GraderBackend,
    question: str,
    expect: list[str],
    answer: str,
    *,
    system_prompt: str = "",
    fallback: GraderBackend | None = None,
) -> Grade:
    """Grade, degrading to the heuristic rather than leaving an answer ungraded."""
    try:
        return grader.grade(question, expect, answer, system_prompt=system_prompt)
    except GradingError as exc:
        log.warning("%s grading failed (%s) — falling back to coverage scoring", grader.name, exc)
        return (fallback or HeuristicGrader()).grade(question, expect, answer)


def render_feedback(question: str, grade: Grade, topic_name: str | None = None) -> str:
    """The message sent back to Telegram after grading."""
    bar = _bar(grade.score)
    head = f"{_verdict_emoji(grade.score)} *{grade.score}/100* {bar}"
    lines = [head]
    if topic_name:
        lines.append(f"_{topic_name}_")
    lines += ["", f"❓ {question}", "", grade.feedback]
    if grade.missing:
        lines += ["", "*Not covered:* " + ", ".join(grade.missing[:6])]
    if grade.grader == Grader.HEURISTIC:
        lines += ["", "_Coverage grading — no API key set._"]
    return "\n".join(lines)


def _bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _verdict_emoji(score: int) -> str:
    if score >= 85:
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"


def parse_self_rating(text: str) -> int | None:
    """`/score 70` or a bare `7/10` from the phone, for when you grade yourself."""
    match = re.search(r"\b(\d{1,3})\s*/\s*(10|100)\b", text)
    if match:
        value, scale = int(match.group(1)), int(match.group(2))
        return max(0, min(100, value * 10 if scale == 10 else value))
    match = re.fullmatch(r"\s*(\d{1,3})\s*", text)
    if match:
        return max(0, min(100, int(match.group(1))))
    return None


def json_dumps_missing(grade: Grade) -> str:
    return json.dumps(grade.missing)
