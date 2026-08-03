"""Loaders for config/profile.yml and config/topics.yml.

`ProfileConfig.write_adaptive` is the only path that writes YAML back, and it
touches exactly one key: `adaptive`. Comments elsewhere in the file survive
because the rest of the document is round-tripped untouched by value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import Adaptive, Profile, Topic

CONFIG_DIR = Path(os.environ.get("PREP_CONFIG_DIR", os.environ.get("RADAR_CONFIG_DIR", "config")))

# Keeps the machine-written block visually separate from the hand-written part.
_ADAPTIVE_HEADER = (
    "# ------------------------------------------------------------------\n"
    "# MACHINE-MAINTAINED — rewritten by `prep adapt`. Do not hand-edit.\n"
    "# ------------------------------------------------------------------"
)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


class ProfileConfig:
    """config/profile.yml, parsed, with a narrow write path back."""

    def __init__(self, raw: dict[str, Any], path: Path | None = None):
        self.path = path
        self._raw = raw
        self.profile = Profile(**raw)

    @classmethod
    def load(cls, path: Path | None = None) -> ProfileConfig:
        p = path or (CONFIG_DIR / "profile.yml")
        return cls(_read_yaml(p), p)

    @property
    def adaptive(self) -> Adaptive:
        return self.profile.adaptive

    def write_adaptive(self, adaptive: Adaptive) -> None:
        """Replace the `adaptive:` block in place. Nothing else is rewritten."""
        if self.path is None:
            raise ValueError("cannot write: profile was not loaded from a file")
        self._raw["adaptive"] = adaptive.as_yaml_dict()
        self.profile.adaptive = adaptive
        text = yaml.safe_dump(self._raw, sort_keys=False, allow_unicode=True, width=100)
        # Re-attach the warning banner the dumper cannot carry.
        text = text.replace("\nadaptive:\n", f"\nadaptive:\n{_indent(_ADAPTIVE_HEADER)}\n", 1)
        self.path.write_text(text, encoding="utf-8")


def _indent(block: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in block.splitlines())


class TopicsConfig:
    """config/topics.yml, parsed."""

    def __init__(self, raw: dict[str, Any], path: Path | None = None):
        self.path = path
        self.topics: list[Topic] = [Topic(**t) for t in (raw.get("topics") or [])]
        if not self.topics:
            raise ValueError(f"{path}: no topics defined")

    @classmethod
    def load(cls, path: Path | None = None) -> TopicsConfig:
        p = path or (CONFIG_DIR / "topics.yml")
        return cls(_read_yaml(p), p)

    @property
    def ids(self) -> list[str]:
        return [t.id for t in self.topics]

    def get(self, topic_id: str) -> Topic | None:
        return next((t for t in self.topics if t.id == topic_id), None)

    def for_stage(self, stage: str) -> list[Topic]:
        return [t for t in self.topics if stage in t.stages]

    def seed_count(self) -> int:
        return sum(len(t.seeds) for t in self.topics)
