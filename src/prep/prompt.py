"""Builds the system prompt from the profile and the adaptive block.

This module is the answer to "the agent should correct its own context". The
prompt is never stored as a hand-written string that goes stale — it is
*assembled* on every run from:

  - the hand-written half of profile.yml (who you are, what you are aiming at),
  - the machine-maintained `adaptive:` block (what you keep getting wrong),
  - live topic state from the database (scores, staleness),

then hashed. A changed hash means a new row in `context_versions`, and every
question generated afterwards is stamped with that version. So you can always
answer "why did it ask me that, and what did it think of me at the time?"
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from .models import Profile, TopicState

MAX_NOTES = 12


def _line(label: str, value: object | None) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"- {label}: {value}"


def candidate_block(profile: Profile) -> str:
    ident, target = profile.identity, profile.target
    lines = ["## The candidate"]
    for label, value in (
        ("Name", ident.name),
        ("Based in", ident.base),
        ("Current employer", ident.current_employer),
        ("Current title", ident.current_title),
        ("Current project", ident.current_project),
        ("Experience", _years(ident.years_total, ident.years_gcc)),
        ("Education", ident.education),
        ("Languages", ident.languages),
        ("Licences and approvals", ident.licenses),
        ("Notice period", f"{ident.notice_period_days} days" if ident.notice_period_days else None),
    ):
        rendered = _line(label, value)
        if rendered:
            lines.append(rendered)

    lines += ["", "## The role being targeted"]
    for label, value in (
        ("Primary target", target.primary_role),
        ("Secondary target", target.secondary_role),
        ("Level", target.level),
        ("Employer types", target.employer_types),
        ("Locations", target.emirates),
        ("Motivation", target.motivation),
        ("Shortlist (tier 1)", target.companies_tier1[:8]),
    ):
        rendered = _line(label, value)
        if rendered:
            lines.append(rendered)

    comp = target.compensation
    if comp.must_beat_current:
        lines.append(
            "- Hard constraint: any move must beat the current package. Salary questions "
            "must be drilled as a negotiation he can win, not a formality."
        )
    if comp.target_min_aed_month:
        lines.append(f"- Floor he will not go below: AED {comp.target_min_aed_month:,.0f}/month")
    if comp.non_salary_musts:
        lines.append(f"- Non-salary musts: {', '.join(comp.non_salary_musts)}")

    days = target.days_to_interview_window()
    if days is not None:
        when = f"in {days} days" if days > 0 else "already open"
        lines.append(f"- Interview window: {when} ({target.interview_window_from})")

    if profile.strengths:
        lines += ["", "## Already strong (probe, do not teach)"]
        lines += [f"- {s}" for s in profile.strengths]
    if profile.gaps:
        lines += ["", "## Known gaps (this is where the value is)"]
        lines += [f"- {g}" for g in profile.gaps]
    return "\n".join(lines)


def _years(total: float | None, gcc: float | None) -> str | None:
    if total is None and gcc is None:
        return None
    parts = []
    if total is not None:
        parts.append(f"{total:g} years total")
    if gcc is not None:
        parts.append(f"{gcc:g} in the GCC")
    return ", ".join(parts)


def adaptive_block(profile: Profile, states: list[TopicState] | None = None) -> str:
    a = profile.adaptive
    lines = [
        "## Adaptive state (maintained by the agent, edition "
        f"{a.version}{f', updated {a.updated_at[:10]}' if a.updated_at else ''})"
    ]
    if a.updated_reason:
        lines.append(f"- Last change: {a.updated_reason}")
    lines.append(f"- Difficulty dial: {a.difficulty}/5 ({_difficulty_word(a.difficulty)})")
    if a.focus:
        lines.append(f"- Drill hardest, weakest first: {', '.join(a.focus)}")
    if a.resting:
        lines.append(f"- Scoring well, ask only occasionally: {', '.join(a.resting)}")
    for note in a.notes[:MAX_NOTES]:
        lines.append(f"- {note}")

    weak = [s for s in (states or []) if s.avg_score is not None and s.avg_score < 60]
    if weak:
        lines.append(
            "- Measured weak topics: "
            + ", ".join(
                f"{s.topic} ({s.avg_score:.0f}/100 over {s.answered_count})"
                for s in sorted(weak, key=lambda s: s.avg_score or 0)[:6]
            )
        )
    return "\n".join(lines)


def _difficulty_word(level: int) -> str:
    return {
        1: "warm-up, build confidence",
        2: "straightforward, one follow-up",
        3: "a real interview",
        4: "senior panel, expects numbers",
        5: "hostile panel, challenges every claim",
    }.get(level, "a real interview")


ROLE_INSTRUCTIONS = """\
You are the interview coach for one specific candidate, described below. You run
a daily drill over Telegram: a few questions in the evening, he answers from his
phone in a paragraph or two, you grade honestly.

How to write questions:
- Ask what a real interviewer for the TARGET role would ask, not a textbook quiz.
  Consultancy and PMC panels probe judgement and records; contractor panels probe
  execution and speed.
- Ground every question in his actual world — landscape and hardscape packages,
  villa projects, WIR/MIR/NCR registers, UAE authorities, GCC site reality. A
  generic "what is ISO 9001" is a wasted day.
- One question, one idea. It must be answerable in 60–90 seconds of typing.
- Vary the frame: scenario ("your pour is tomorrow and..."), challenge ("convince
  me a contractor QC can supervise"), and plain technical.
- Never repeat a question he has already been asked — a list of recent ones is
  given to you.
- No preamble, no encouragement, no emoji in the question text itself.

How to grade:
- Score 0–100 against what a good answer actually contains, not against length.
- Be honest. A vague answer that gestures at the right area is 40, not 70. He is
  trying to beat other candidates, and a flattering score costs him the job.
- Feedback is at most three short lines: what was missing, the one phrase that
  would have landed it, and — where it applies — how a consultancy panel would
  have heard that answer differently from a contractor panel.
- If he gives a number or a named standard, credit it explicitly. Quantified
  answers are the gap he is closing.
"""


def build_system_prompt(profile: Profile, states: list[TopicState] | None = None) -> str:
    return "\n\n".join(
        [
            ROLE_INSTRUCTIONS.strip(),
            candidate_block(profile),
            adaptive_block(profile, states),
        ]
    )


# The edition number and the timestamp change on every rewrite by definition.
# Hashing them would make every prompt "new", so a version number would stop
# meaning "the context actually changed" (DECISIONS.md D-18).
_VOLATILE = re.compile(r"^(## Adaptive state \(.*|- Last change: .*)$", re.MULTILINE)


def prompt_hash(system_prompt: str) -> str:
    """Hash the *content* of the prompt, ignoring its edition stamp."""
    stable = _VOLATILE.sub("", system_prompt)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def render_context_summary(profile: Profile, states: list[TopicState] | None = None) -> str:
    """The short version shown by `prep context` and sent on /context."""
    a = profile.adaptive
    lines = [
        f"Context edition {a.version}"
        + (f" · updated {a.updated_at[:10]}" if a.updated_at else " · never adapted"),
        f"Target: {profile.target.primary_role}",
        f"Difficulty: {a.difficulty}/5 — {_difficulty_word(a.difficulty)}",
    ]
    if a.focus:
        lines.append(f"Focus: {', '.join(a.focus)}")
    if a.resting:
        lines.append(f"Resting: {', '.join(a.resting)}")
    if a.updated_reason:
        lines.append(f"Last change: {a.updated_reason}")
    days = profile.target.days_to_interview_window(date.today())
    if days is not None and days > 0:
        lines.append(f"Interview window opens in {days} days")
    for note in a.notes[:5]:
        lines.append(f"· {note}")
    return "\n".join(lines)
