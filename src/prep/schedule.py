"""Which topics get asked today, and when the drill fires.

Selection is a weighted priority, not a rotation. In order of pull:

  1. `adaptive.focus` — what adapt decided you are weakest at.
  2. Measured low scores from `topic_state`.
  3. Spaced repetition — a topic answered well backs off; one answered badly
     comes back tomorrow.
  4. Staleness — a topic never asked outranks one asked yesterday.
  5. Stage weighting near the interview window: behavioural, HR and
     company-specific climb as the window approaches, because that is what you
     will actually face first.

`resting` topics are not banned. They surface roughly one day in five, because
an interviewer will open on your strongest ground and a cold strength is a risk.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import TopicsConfig
from .models import Profile, TopicState, today_iso

log = logging.getLogger(__name__)

RESTING_PULL = 0.2
# Stages that matter more the closer the interview window gets.
LATE_STAGES = ("hr_screen", "behavioural", "client_facing")
LATE_WINDOW_DAYS = 45


def local_now(profile: Profile, now: datetime | None = None) -> datetime:
    """Now in the profile's timezone. CI runs in UTC; you do not live there."""
    tz_name = profile.delivery.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r — falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    base = now or datetime.now(tz)
    return base.astimezone(tz)


def should_send_now(
    profile: Profile,
    already_sent_today: int,
    now: datetime | None = None,
) -> bool:
    """True when it is at or past the local ask hour and nothing went out today.

    The workflow fires every couple of hours; this is what stops it from
    sending a second drill at 22:00.
    """
    if already_sent_today > 0:
        return False
    return local_now(profile, now).hour >= profile.delivery.ask_hour_local


def questions_today(profile: Profile, now: datetime | None = None) -> int:
    return profile.questions_for(local_now(profile, now).strftime("%A"))


def _staleness(state: TopicState, today: date) -> float:
    if state.last_asked is None:
        return 14.0
    try:
        gap = (today - date.fromisoformat(state.last_asked)).days
    except ValueError:
        return 14.0
    return float(min(gap, 30))


def topic_priority(
    topic_id: str,
    state: TopicState,
    profile: Profile,
    base_weight: float,
    stages: list[str],
    today: date | None = None,
) -> float:
    """Higher wins. Kept deliberately readable — this is tuned by hand."""
    today = today or date.today()
    score = base_weight

    adaptive = profile.adaptive
    if topic_id in adaptive.focus:
        # Position matters: focus is written weakest-first.
        score += 3.0 - 0.3 * adaptive.focus.index(topic_id)
    if topic_id in adaptive.resting:
        score *= RESTING_PULL

    if state.avg_score is not None:
        # 30/100 average adds 1.4; 90/100 subtracts 0.8.
        score += (65 - state.avg_score) / 25.0
    if state.last_score is not None and state.last_score < 60:
        score += 0.8

    if not state.is_due(today.isoformat()):
        score *= 0.35

    score += _staleness(state, today) / 10.0

    days = profile.target.days_to_interview_window(today)
    if days is not None and days <= LATE_WINDOW_DAYS:
        closeness = 1.0 - max(days, 0) / LATE_WINDOW_DAYS
        if any(s in LATE_STAGES for s in stages):
            score += 1.5 * closeness
        elif days <= 0:
            score += 0.2 * closeness

    return score


def choose_topics(
    topics_cfg: TopicsConfig,
    states: dict[str, TopicState],
    profile: Profile,
    count: int,
    today: date | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    """Pick `count` topic ids, best first, without repeating one in a day."""
    today = today or date.today()
    rng = rng or random.Random()
    ranked: list[tuple[float, str]] = []
    for topic in topics_cfg.topics:
        state = states.get(topic.id) or TopicState(topic=topic.id)
        priority = topic_priority(topic.id, state, profile, topic.weight, topic.stages, today)
        # A small jitter stops the same three topics from owning every evening.
        ranked.append((priority + rng.uniform(0, 0.4), topic.id))

    ranked.sort(reverse=True)
    return [topic_id for _, topic_id in ranked[:count]]


def next_due(score: int, state: TopicState, today: date | None = None) -> TopicState:
    """SM-2, cut down to what a daily drill actually needs.

    Badly answered comes back tomorrow. Well answered doubles the interval, up
    to a fortnight — beyond that you have simply stopped being tested on it.
    """
    today = today or date.today()
    updated = state.model_copy(deep=True)
    updated.answered_count += 1
    updated.last_score = score
    updated.avg_score = (
        score
        if updated.avg_score is None
        else round(
            (updated.avg_score * (updated.answered_count - 1) + score) / updated.answered_count, 1
        )
    )

    if score < 60:
        updated.interval_days = 1
        updated.strong_streak = 0
    elif score < 85:
        updated.interval_days = max(2, min(7, updated.interval_days + 1))
        updated.strong_streak = 0
    else:
        updated.strong_streak += 1
        updated.interval_days = min(14, max(2, updated.interval_days * 2))

    updated.due_on = (today + timedelta(days=updated.interval_days)).isoformat()
    return updated


def mark_topic_asked(state: TopicState, on_date: str | None = None) -> TopicState:
    updated = state.model_copy(deep=True)
    updated.asked_count += 1
    updated.last_asked = on_date or today_iso()
    return updated
