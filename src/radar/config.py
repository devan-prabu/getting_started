"""Loaders for the YAML files in config/.

Not in the brief's file list, but collect/filters/cli all need to read the same
files and duplicating the parsing three times would be worse (DECISIONS.md D-04).
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from .models import Company, Source

CONFIG_DIR = Path(os.environ.get("RADAR_CONFIG_DIR", "config"))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


class SourcesConfig:
    """config/sources.yml, parsed."""

    def __init__(self, raw: dict[str, Any], path: Path | None = None):
        self.path = path
        self.google_news_base: str = raw.get(
            "google_news_base", "https://news.google.com/rss/search"
        )
        self.google_news_params: dict[str, str] = raw.get("google_news_params", {}) or {}
        self.sources: list[Source] = [Source(**s) for s in (raw.get("sources") or [])]
        self._raw = raw

    @classmethod
    def load(cls, path: Path | None = None) -> SourcesConfig:
        p = path or (CONFIG_DIR / "sources.yml")
        return cls(_read_yaml(p), p)

    def enabled(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def get(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def build_url(self, source: Source) -> str:
        """The URL a source is actually fetched from.

        Google News queries are built here rather than hand-written into the
        YAML, so there is no chance of a typo'd URL silently returning nothing
        (brief §5.1).
        """
        if source.type == "google_news":
            params = {"q": source.query or "", **self.google_news_params}
            return f"{self.google_news_base}?{urllib.parse.urlencode(params)}"
        if not source.url:
            raise ValueError(f"source {source.id}: no url and type is not google_news")
        return source.url

    def write_status(self, updates: dict[str, tuple[str, str, str | None]]) -> None:
        """Rewrite status/last_checked/notes in place for the given source ids.

        `updates` maps source_id -> (status, last_checked, note_suffix).
        Used by `radar sources check` so the file always reflects reality
        (brief §0.2).
        """
        if self.path is None:
            raise ValueError("cannot write status: config was not loaded from a file")
        for entry in self._raw.get("sources") or []:
            upd = updates.get(entry.get("id"))
            if not upd:
                continue
            status, checked, note = upd
            entry["status"] = status
            entry["last_checked"] = checked
            if note:
                entry["notes"] = note
        with self.path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self._raw, fh, sort_keys=False, allow_unicode=True, width=100)


def load_companies(path: Path | None = None) -> list[Company]:
    p = path or (CONFIG_DIR / "companies.yml")
    raw = _read_yaml(p)
    return [Company(**c) for c in (raw.get("companies") or [])]


def load_keywords(path: Path | None = None) -> dict[str, list[str]]:
    p = path or (CONFIG_DIR / "keywords.yml")
    raw = _read_yaml(p)
    return {
        "award_signals": list(raw.get("award_signals") or []),
        "uae_signals": list(raw.get("uae_signals") or []),
        "exclusions": list(raw.get("exclusions") or []),
    }
