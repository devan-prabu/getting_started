"""Where questions come from.

Two backends behind one protocol:

  - `ClaudeGenerator` — the Messages API, given the assembled system prompt, the
    topic, the difficulty dial and the list of recently asked questions. Returns
    JSON, validated, with one repair retry.
  - `BankGenerator` — the seeded questions in config/topics.yml, chosen by
    difficulty distance and least-recently-asked.

The bank is not a stub. If the API key is missing, the network is down, the
account is out of credit or the model returns junk twice, the drill still fires
(DECISIONS.md D-16). A silent skipped day is the one failure mode that kills
this kind of tool.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from .config import TopicsConfig
from .models import Question, QuestionSource, Topic

log = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT = 60.0
MAX_TOKENS = 1024

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class GenerationError(Exception):
    """The backend was configured but could not produce a usable question."""


class Generator(Protocol):
    name: str

    def available(self) -> bool: ...

    def generate(
        self,
        topic: Topic,
        *,
        system_prompt: str,
        difficulty: int,
        avoid: list[str],
        stage: str | None = None,
    ) -> Question: ...


def _extract_json(text: str) -> dict[str, Any]:
    match = _JSON_BLOCK.search(text)
    if not match:
        raise GenerationError("no JSON object in the response")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise GenerationError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GenerationError("JSON was not an object")
    return data


@dataclass
class BankGenerator:
    """Seeded questions from config/topics.yml. Always available."""

    rng: random.Random = field(default_factory=random.Random)
    name: str = "bank"

    def available(self) -> bool:
        return True

    def generate(
        self,
        topic: Topic,
        *,
        system_prompt: str = "",
        difficulty: int = 3,
        avoid: list[str] | None = None,
        stage: str | None = None,
    ) -> Question:
        avoid_keys = {" ".join(a.lower().split()) for a in (avoid or [])}
        unseen = [s for s in topic.seeds if " ".join(s.q.lower().split()) not in avoid_keys]
        # Everything in the topic has been asked recently: reuse rather than
        # skip the topic. Re-asking a question you got wrong is the point.
        pool = unseen or list(topic.seeds)
        if not pool:
            raise GenerationError(f"topic {topic.id} has no seed questions")

        # Prefer seeds near the requested difficulty, then pick at random among
        # the closest so the same seed does not lead every time.
        closest = min(abs(s.difficulty - difficulty) for s in pool)
        candidates = [s for s in pool if abs(s.difficulty - difficulty) == closest]
        seed = self.rng.choice(candidates)
        return Question(
            topic=topic.id,
            text=seed.q,
            expect=list(seed.expect),
            difficulty=seed.difficulty,
            stage=stage or (topic.stages[0] if topic.stages else None),
            source=QuestionSource.BANK,
        )


USER_TEMPLATE = """\
Write ONE interview question for tonight's drill.

Topic: {topic_name} ({topic_id})
Interview stage: {stage}
Difficulty: {difficulty}/5

{avoid_block}

Reply with JSON only, no prose around it:
{{"question": "...", "expect": ["key point", "key point", ...], "difficulty": {difficulty}}}

`expect` is the answer key: 5-10 short phrases a strong answer would contain.
They are used to grade him, so make them specific and checkable, not generic.
"""

REPAIR_SUFFIX = (
    "\n\nYour previous reply could not be parsed ({error}). "
    "Reply with the JSON object only — no explanation, no code fence."
)


@dataclass
class ClaudeGenerator:
    """The Anthropic Messages API over plain httpx."""

    api_key: str | None = None
    model: str | None = None
    client: httpx.Client | None = None
    name: str = "claude"

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.model = self.model or os.environ.get("PREP_MODEL", DEFAULT_MODEL).strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, system_prompt: str, user_text: str) -> str:
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
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_text}],
                },
            )
        except httpx.HTTPError as exc:
            raise GenerationError(f"Anthropic request failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()

        if resp.status_code != 200:
            raise GenerationError(f"Anthropic returned {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise GenerationError("Anthropic returned non-JSON") from exc
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise GenerationError("Anthropic returned an empty message")
        return text

    def generate(
        self,
        topic: Topic,
        *,
        system_prompt: str,
        difficulty: int = 3,
        avoid: list[str] | None = None,
        stage: str | None = None,
    ) -> Question:
        avoid = avoid or []
        avoid_block = (
            "Do NOT ask any of these — he has had them recently:\n"
            + "\n".join(f"- {a}" for a in avoid[:25])
            if avoid
            else "He has not been asked anything on this topic yet."
        )
        user_text = USER_TEMPLATE.format(
            topic_name=topic.name,
            topic_id=topic.id,
            stage=stage or (topic.stages[0] if topic.stages else "technical"),
            difficulty=difficulty,
            avoid_block=avoid_block,
        )

        last_error = ""
        for attempt in (1, 2):
            prompt = (
                user_text if attempt == 1 else user_text + REPAIR_SUFFIX.format(error=last_error)
            )
            try:
                data = _extract_json(self._post(system_prompt, prompt))
                return Question(
                    topic=topic.id,
                    text=str(data.get("question", "")),
                    expect=[str(e) for e in (data.get("expect") or [])],
                    difficulty=int(data.get("difficulty", difficulty)),
                    stage=stage or (topic.stages[0] if topic.stages else None),
                    source=QuestionSource.CLAUDE,
                    model=self.model,
                )
            except (GenerationError, ValidationError, ValueError, TypeError) as exc:
                last_error = str(exc)[:200]
                log.warning("claude generation attempt %d failed: %s", attempt, last_error)
        raise GenerationError(f"two attempts failed; last error: {last_error}")


def build_generator(client: httpx.Client | None = None) -> Generator:
    """Claude when a key is present, the bank otherwise."""
    backend = os.environ.get("PREP_GENERATOR", "claude").strip().lower()
    if backend in ("bank", "none", "offline"):
        return BankGenerator()
    claude = ClaudeGenerator(client=client)
    if not claude.available():
        log.info("ANTHROPIC_API_KEY not set — using the seeded question bank")
        return BankGenerator()
    return claude


def generate_question(
    generator: Generator,
    topics_cfg: TopicsConfig,
    topic_id: str,
    *,
    system_prompt: str,
    difficulty: int,
    avoid: list[str],
    stage: str | None = None,
    fallback: Generator | None = None,
) -> Question:
    """Generate for one topic, degrading to the bank rather than failing."""
    topic = topics_cfg.get(topic_id)
    if topic is None:
        raise GenerationError(f"unknown topic {topic_id!r}")
    try:
        return generator.generate(
            topic,
            system_prompt=system_prompt,
            difficulty=difficulty,
            avoid=avoid,
            stage=stage,
        )
    except GenerationError as exc:
        log.warning(
            "%s failed on %s (%s) — falling back to the bank", generator.name, topic_id, exc
        )
        return (fallback or BankGenerator()).generate(
            topic, system_prompt=system_prompt, difficulty=difficulty, avoid=avoid, stage=stage
        )
