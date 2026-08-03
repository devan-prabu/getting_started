"""Self-correction: rewrite the agent's own context from evidence.

Three inputs, in this order of authority:

  1. **What you told it.** `/goal`, `/target`, `/focus`, `/drop`, `/harder`,
     `/easier`, `/cv` arrive as events from Telegram. You outrank the data.
  2. **What you scored.** Topics below the weak line get pushed into `focus`,
     topics scoring consistently well get parked in `resting`, and the
     difficulty dial follows your rolling average.
  3. **Where you are in time.** As the interview window approaches, behavioural
     and HR drills climb. If you have stopped answering, difficulty and volume
     drop rather than the agent shouting into a void.

The result is written back to `profile.yml` under `adaptive:` and recorded as a
new row in `context_versions` — but only if the assembled system prompt actually
changed. An unchanged prompt writes nothing, so the version number means
something (DECISIONS.md D-18).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from . import db
from .config import ProfileConfig, TopicsConfig
from .models import STRONG_SCORE, WEAK_SCORE, Adaptive, TopicState, utcnow_iso
from .prompt import build_system_prompt, prompt_hash

log = logging.getLogger(__name__)

# Rolling window the difficulty dial and the focus list are computed over.
LOOKBACK_DAYS = 21
MIN_ANSWERS_FOR_DIFFICULTY = 4
MAX_FOCUS = 5
MAX_NOTES = 12
# No answers for this long and the agent eases off instead of piling on.
STALE_DAYS = 10

# Commands that write into the adaptive block. Anything else is answered
# inline by the poller and never reaches here.
GOAL_COMMANDS = {"goal", "target", "cv", "note"}
FOCUS_COMMANDS = {"focus", "drop"}
DIAL_COMMANDS = {"harder", "easier"}


@dataclass
class AdaptReport:
    version: int | None = None
    changed: bool = False
    reason: str = "no change"
    focus: list[str] = field(default_factory=list)
    resting: list[str] = field(default_factory=list)
    difficulty: int = 3
    applied_events: int = 0

    def as_line(self) -> str:
        head = f"version={self.version} changed={self.changed} difficulty={self.difficulty}"
        return f"{head} focus={','.join(self.focus) or '—'} events={self.applied_events}"


def _since_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


def measured_state(conn: sqlite3.Connection, topics_cfg: TopicsConfig) -> dict[str, TopicState]:
    return {t.id: db.get_topic_state(conn, t.id) for t in topics_cfg.topics}


def _rolling_average(conn: sqlite3.Connection) -> tuple[float | None, int]:
    rows = db.scores_since(conn, _since_iso(LOOKBACK_DAYS))
    if not rows:
        return None, 0
    scores = [int(r["score"]) for r in rows]
    return sum(scores) / len(scores), len(scores)


def compute_focus(
    states: dict[str, TopicState], profile_gaps: list[str], topics_cfg: TopicsConfig
) -> tuple[list[str], list[str]]:
    """(focus, resting) from measured scores, weakest first."""
    answered = [s for s in states.values() if s.answered_count > 0 and s.avg_score is not None]

    weak = sorted(
        (s for s in answered if s.avg_score is not None and s.avg_score < WEAK_SCORE),
        key=lambda s: s.avg_score or 0,
    )
    focus = [s.topic for s in weak][:MAX_FOCUS]

    # Untested topics belong in focus too, but behind the measured failures: a
    # known weakness beats an unknown one. "Untested" means no evidence at all —
    # a topic answered well is not untested just because the counters differ.
    if len(focus) < MAX_FOCUS:
        untouched = [
            t.id
            for t in topics_cfg.topics
            if states[t.id].asked_count == 0 and states[t.id].answered_count == 0
        ]
        focus += [t for t in untouched if t not in focus][: MAX_FOCUS - len(focus)]

    resting = [
        s.topic
        for s in answered
        if s.avg_score is not None
        and s.avg_score >= STRONG_SCORE
        and s.strong_streak >= 2
        and s.topic not in focus
    ]
    return focus, resting


def compute_difficulty(current: int, average: float | None, sample: int) -> int:
    """Move one notch at a time. A dial that swings is a dial you stop trusting."""
    if average is None or sample < MIN_ANSWERS_FOR_DIFFICULTY:
        return current
    if average >= 85:
        return min(5, current + 1)
    if average < 50:
        return max(1, current - 1)
    return current


def _event_notes(events: list[sqlite3.Row]) -> tuple[list[str], int, list[str], list[str]]:
    """Turn inbound commands into notes, a difficulty delta and focus edits."""
    notes: list[str] = []
    delta = 0
    pin: list[str] = []
    drop: list[str] = []

    for row in events:
        payload = json.loads(row["payload"] or "{}")
        name = payload.get("command", row["kind"])
        argument = (payload.get("argument") or "").strip()
        stamp = (row["created_at"] or "")[:10]

        if name in GOAL_COMMANDS and argument:
            label = {
                "goal": "Stated goal",
                "target": "Target company",
                "cv": "CV update",
                "note": "Note",
            }.get(name, "Note")
            notes.append(f"{label} ({stamp}): {argument}")
        elif name in FOCUS_COMMANDS and argument:
            (pin if name == "focus" else drop).append(argument.lower().replace(" ", "_"))
            notes.append(
                f"{'Asked for more' if name == 'focus' else 'Asked to drop'} ({stamp}): {argument}"
            )
        elif name in DIAL_COMMANDS:
            delta += 1 if name == "harder" else -1
    return notes, delta, pin, drop


def _time_notes(profile_target_days: int | None, days_since_answer: int | None) -> list[str]:
    notes: list[str] = []
    if profile_target_days is not None:
        if profile_target_days <= 0:
            notes.append(
                "Interview window is OPEN — weight behavioural, HR and company-specific "
                "questions above deep technical ones."
            )
        elif profile_target_days <= 45:
            notes.append(
                f"Interview window opens in {profile_target_days} days — start mixing in "
                "behavioural, salary and company-specific questions."
            )
    if days_since_answer is not None and days_since_answer >= STALE_DAYS:
        notes.append(
            f"No answers for {days_since_answer} days — keep questions short and "
            "concrete until the habit restarts."
        )
    return notes


def _days_since_last_answer(conn: sqlite3.Connection) -> int | None:
    last = db.last_answer_at(conn)
    if not last:
        return None
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).days


def build_adaptive(
    conn: sqlite3.Connection,
    profile_cfg: ProfileConfig,
    topics_cfg: TopicsConfig,
    events: list[sqlite3.Row],
    today: date | None = None,
) -> tuple[Adaptive, str]:
    """The new adaptive block and a one-line reason for the change."""
    profile = profile_cfg.profile
    current = profile.adaptive
    states = measured_state(conn, topics_cfg)

    focus, resting = compute_focus(states, profile.gaps, topics_cfg)
    average, sample = _rolling_average(conn)
    difficulty = compute_difficulty(current.difficulty, average, sample)

    event_notes, dial_delta, pinned, dropped = _event_notes(events)
    if dial_delta:
        difficulty = max(1, min(5, difficulty + dial_delta))

    # An explicit /focus outranks the measured ordering; /drop parks a topic.
    known = set(topics_cfg.ids)
    for topic_id in pinned:
        if topic_id in known and topic_id not in focus:
            focus.insert(0, topic_id)
    focus = [t for t in focus if t not in dropped][:MAX_FOCUS]
    resting = sorted({*resting, *[d for d in dropped if d in known]} - set(focus))

    # Notes the user wrote are kept; notes the agent computed are regenerated,
    # so a stale "interview in 40 days" never lingers for a month.
    kept = [n for n in current.notes if _is_user_note(n)]
    computed = _time_notes(
        profile.target.days_to_interview_window(today or date.today()),
        _days_since_last_answer(conn),
    )
    if average is not None and sample >= MIN_ANSWERS_FOR_DIFFICULTY:
        computed.append(
            f"Rolling average over the last {LOOKBACK_DAYS} days: {average:.0f}/100 "
            f"across {sample} answers."
        )
    notes = _dedupe(kept + event_notes + computed)[:MAX_NOTES]

    reason = _reason(current, focus, resting, difficulty, len(event_notes))
    adaptive = Adaptive(
        version=current.version + 1,
        updated_at=utcnow_iso(),
        updated_reason=reason,
        focus=focus,
        resting=resting,
        difficulty=difficulty,
        notes=notes,
    )
    return adaptive, reason


# Agent-computed notes all start with one of these; anything else came from you.
_COMPUTED_PREFIXES = ("Interview window", "No answers for", "Rolling average")


def _is_user_note(note: str) -> bool:
    return not note.startswith(_COMPUTED_PREFIXES)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _reason(
    current: Adaptive, focus: list[str], resting: list[str], difficulty: int, event_count: int
) -> str:
    bits: list[str] = []
    if event_count:
        bits.append(f"{event_count} command{'s' if event_count > 1 else ''} from Telegram")
    if focus != current.focus:
        gained = [t for t in focus if t not in current.focus]
        if gained:
            bits.append(f"weak on {', '.join(gained[:3])}")
        else:
            bits.append("focus reordered")
    if difficulty != current.difficulty:
        bits.append(f"difficulty {current.difficulty}→{difficulty}")
    if resting != current.resting:
        gained = [t for t in resting if t not in current.resting]
        if gained:
            bits.append(f"resting {', '.join(gained[:3])}")
    return "; ".join(bits) or "periodic refresh"


def adapt(
    conn: sqlite3.Connection,
    profile_cfg: ProfileConfig,
    topics_cfg: TopicsConfig,
    *,
    dry_run: bool = False,
    today: date | None = None,
) -> AdaptReport:
    """Consume events, recompute the context, persist it if it really changed."""
    events = db.unapplied_events(conn)
    adaptive, reason = build_adaptive(conn, profile_cfg, topics_cfg, events, today)

    candidate = profile_cfg.profile.model_copy(deep=True)
    candidate.adaptive = adaptive
    states = list(measured_state(conn, topics_cfg).values())
    new_prompt = build_system_prompt(candidate, states)
    new_hash = prompt_hash(new_prompt)

    latest = db.latest_context(conn)
    unchanged = latest is not None and latest["prompt_hash"] == new_hash

    report = AdaptReport(
        version=int(latest["version"]) if latest is not None else None,
        changed=not unchanged,
        reason=reason if not unchanged else "no change",
        focus=adaptive.focus,
        resting=adaptive.resting,
        difficulty=adaptive.difficulty,
        applied_events=len(events),
    )

    if dry_run:
        return report

    if unchanged:
        # Still consume the events — they were reflected in the prompt already,
        # and leaving them unapplied would re-log them forever.
        db.mark_events_applied(conn, [int(e["id"]) for e in events])
        return report

    version = db.record_context(
        conn,
        system_prompt=new_prompt,
        prompt_hash=new_hash,
        adaptive_json=json.dumps(adaptive.as_yaml_dict()),
        reason=reason,
    )
    adaptive.version = version or adaptive.version
    profile_cfg.write_adaptive(adaptive)
    db.mark_events_applied(conn, [int(e["id"]) for e in events])

    report.version = version
    log.info("adapt: %s", report.as_line())
    return report


def current_context(
    conn: sqlite3.Connection, profile_cfg: ProfileConfig, topics_cfg: TopicsConfig
) -> tuple[int, str]:
    """The live system prompt and its version, recording it if it is new.

    Called before every generation run, so a hand edit to profile.yml is picked
    up and versioned without waiting for the next adapt.
    """
    states = list(measured_state(conn, topics_cfg).values())
    system_prompt = build_system_prompt(profile_cfg.profile, states)
    digest = prompt_hash(system_prompt)
    version = db.record_context(
        conn,
        system_prompt=system_prompt,
        prompt_hash=digest,
        adaptive_json=json.dumps(profile_cfg.adaptive.as_yaml_dict()),
        reason="profile or state changed outside adapt",
    )
    if version is None:
        latest = db.latest_context(conn)
        version = int(latest["version"]) if latest is not None else 1
    return version, system_prompt
