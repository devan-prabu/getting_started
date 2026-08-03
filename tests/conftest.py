"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from radar import db as db_mod
from radar.config import SourcesConfig
from radar.fetcher import Fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path: Path):
    """A real SQLite DB on disk with the full schema."""
    path = tmp_path / "radar.db"
    db_mod.init_db(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


class FakeFetcher:
    """Stands in for Fetcher, serving fixture bodies from a URL map.

    Deliberately not a subclass: if the real Fetcher grows a method the code
    depends on, these tests should fail loudly rather than inherit it.
    """

    def __init__(
        self,
        responses: dict[str, tuple[int, str]] | None = None,
        disallowed: set[str] | None = None,
    ):
        self.responses = responses or {}
        self.disallowed = disallowed or set()
        self.requested: list[str] = []
        self.resolved: list[str] = []

    def allowed(self, url: str) -> bool:
        return url not in self.disallowed

    def get(self, url: str):
        from radar.fetcher import FetchResult, RobotsDisallowed

        self.requested.append(url)
        if url in self.disallowed:
            raise RobotsDisallowed(url)
        if url not in self.responses:
            raise ConnectionError(f"no fixture registered for {url}")
        status, body = self.responses[url]
        ctype = "application/xml" if body.lstrip().startswith("<?xml") else "text/html"
        return FetchResult(url=url, status_code=status, text=body, content_type=ctype)

    def resolve(self, url: str) -> str:
        self.resolved.append(url)
        return url

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def fake_fetcher():
    return FakeFetcher


@pytest.fixture
def sources_cfg() -> SourcesConfig:
    """The real config/sources.yml, so a broken query is caught by the suite."""
    return SourcesConfig.load(Path(__file__).parents[1] / "config" / "sources.yml")


@pytest.fixture
def offline_fetcher() -> Fetcher:
    """A real Fetcher with robots checks off and no throttle, for unit tests."""
    return Fetcher(respect_robots=False, min_interval=0.0)


# --- prep agent -----------------------------------------------------------

CONFIG = Path(__file__).parents[1] / "config"


@pytest.fixture
def prep_conn(tmp_path: Path):
    """A real prep DB on disk with the full schema."""
    from prep import db as prep_db

    path = tmp_path / "prep.db"
    prep_db.init_db(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def topics_cfg():
    """The real config/topics.yml, so a broken seed is caught by the suite."""
    from prep.config import TopicsConfig

    return TopicsConfig.load(CONFIG / "topics.yml")


@pytest.fixture
def profile_cfg(tmp_path: Path):
    """A writable copy of the real profile, so write_adaptive can be tested."""
    from prep.config import ProfileConfig

    target = tmp_path / "profile.yml"
    target.write_text((CONFIG / "profile.yml").read_text(encoding="utf-8"), encoding="utf-8")
    return ProfileConfig.load(target)


class FakeTelegram:
    """Stands in for TelegramClient. Records sends, replays scripted updates."""

    def __init__(self, updates=None, fail_on_send: bool = False):
        self.name = "telegram"
        self.sent: list[str] = []
        self.updates = list(updates or [])
        self.fail_on_send = fail_on_send
        self.next_message_id = 100

    def configured(self) -> bool:
        return True

    def send(self, text: str, *, markdown: bool = True) -> int | None:
        from prep.telegram import TelegramError

        if self.fail_on_send:
            raise TelegramError("simulated outage")
        self.sent.append(text)
        self.next_message_id += 1
        return self.next_message_id

    def get_updates(self, offset=None, limit=50):
        pending = [u for u in self.updates if offset is None or u.update_id >= offset]
        return pending

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def fake_telegram():
    return FakeTelegram
