"""Tests for the ethics-critical module. respx intercepts httpx; no sockets open.

Brief §9 is a hard constraint, so these tests exist to make a regression here
loud: robots is obeyed, hosts are rate limited, the UA is honest, and 403/404 is
never retried.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from conftest import fixture
from radar import fetcher as fetcher_mod
from radar.fetcher import Fetcher, RobotsDisallowed, user_agent


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client(timeout=5.0, follow_redirects=True, headers={"User-Agent": user_agent()})


def build(client: httpx.Client, **kw) -> Fetcher:
    kw.setdefault("min_interval", 0.0)
    return Fetcher(client=client, **kw)


class TestUserAgent:
    def test_identifies_the_tool_and_is_not_a_browser(self, monkeypatch):
        monkeypatch.delenv("CONTACT_EMAIL", raising=False)
        ua = user_agent()
        assert ua.startswith("UAE-Project-Radar/0.1")
        # Brief §9.3: no browser impersonation, ever.
        assert "Mozilla" not in ua
        assert "Chrome" not in ua

    def test_includes_contact_email_when_set(self, monkeypatch):
        monkeypatch.setenv("CONTACT_EMAIL", "me@example.test")
        assert "me@example.test" in user_agent()

    def test_prompts_when_contact_is_missing(self, monkeypatch):
        monkeypatch.setenv("CONTACT_EMAIL", "")
        assert "set CONTACT_EMAIL" in user_agent()


class TestRobots:
    @respx.mock
    def test_disallowed_path_raises_and_is_not_fetched(self, client):
        respx.get("https://blocked.test/robots.txt").mock(
            return_value=httpx.Response(200, text=fixture("robots_disallow.txt"))
        )
        page = respx.get("https://blocked.test/news")
        with build(client) as f, pytest.raises(RobotsDisallowed):
            f.get("https://blocked.test/news")
        assert not page.called, "a disallowed URL must never be requested"

    @respx.mock
    def test_allowed_path_is_fetched(self, client):
        respx.get("https://feeds.test/robots.txt").mock(
            return_value=httpx.Response(200, text=fixture("robots_allow.txt"))
        )
        respx.get("https://feeds.test/rss/awards").mock(
            return_value=httpx.Response(200, text="<rss/>")
        )
        with build(client) as f:
            result = f.get("https://feeds.test/rss/awards")
        assert result.status_code == 200

    @respx.mock
    def test_disallowed_subpath_of_an_otherwise_open_site(self, client):
        respx.get("https://feeds.test/robots.txt").mock(
            return_value=httpx.Response(200, text=fixture("robots_allow.txt"))
        )
        with build(client) as f:
            assert f.allowed("https://feeds.test/rss/awards")
            assert not f.allowed("https://feeds.test/admin/panel")

    @respx.mock
    def test_missing_robots_is_treated_as_allow_all(self, client):
        respx.get("https://norobots.test/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://norobots.test/feed").mock(return_value=httpx.Response(200, text="ok"))
        with build(client) as f:
            assert f.get("https://norobots.test/feed").text == "ok"

    @respx.mock
    def test_unreachable_robots_does_not_crash_the_run(self, client):
        respx.get("https://flaky.test/robots.txt").mock(side_effect=httpx.ConnectError("no route"))
        respx.get("https://flaky.test/feed").mock(return_value=httpx.Response(200, text="ok"))
        with build(client) as f:
            assert f.get("https://flaky.test/feed").status_code == 200

    @respx.mock
    def test_robots_is_fetched_once_per_host_per_run(self, client):
        robots = respx.get("https://feeds.test/robots.txt").mock(
            return_value=httpx.Response(200, text=fixture("robots_allow.txt"))
        )
        respx.get("https://feeds.test/rss/a").mock(return_value=httpx.Response(200, text="a"))
        respx.get("https://feeds.test/rss/b").mock(return_value=httpx.Response(200, text="b"))
        with build(client) as f:
            f.get("https://feeds.test/rss/a")
            f.get("https://feeds.test/rss/b")
        assert robots.call_count == 1


class TestRateLimit:
    def test_throttle_enforces_the_minimum_gap(self, monkeypatch):
        """The 3s floor is brief §9.2 and is not negotiable."""
        slept: list[float] = []
        clock = {"t": 0.0}
        monkeypatch.setattr(fetcher_mod.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: slept.append(s))

        throttle = fetcher_mod._HostThrottle(min_interval=3.0)
        throttle.wait("a.test")
        assert slept == []
        throttle.wait("a.test")
        assert slept == [3.0]

    def test_throttle_is_per_host(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(fetcher_mod.time, "monotonic", lambda: 0.0)
        monkeypatch.setattr(fetcher_mod.time, "sleep", lambda s: slept.append(s))

        throttle = fetcher_mod._HostThrottle(min_interval=3.0)
        throttle.wait("a.test")
        throttle.wait("b.test")
        assert slept == [], "different hosts do not wait on each other"

    def test_default_interval_is_at_least_three_seconds(self):
        assert fetcher_mod.MIN_HOST_INTERVAL >= 3.0


class TestRetries:
    @respx.mock
    def test_retries_on_500_then_succeeds(self, client):
        route = respx.get("https://flaky.test/feed").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, text="finally")]
        )
        with build(client, respect_robots=False) as f:
            assert f.get("https://flaky.test/feed").text == "finally"
        assert route.call_count == 2

    @respx.mock
    def test_gives_up_after_three_attempts(self, client):
        route = respx.get("https://down.test/feed").mock(return_value=httpx.Response(500))
        with build(client, respect_robots=False) as f, pytest.raises(fetcher_mod.RetryableStatus):
            f.get("https://down.test/feed")
        assert route.call_count == fetcher_mod.RETRY_ATTEMPTS

    @respx.mock
    def test_404_is_never_retried(self, client):
        route = respx.get("https://gone.test/feed").mock(return_value=httpx.Response(404))
        with build(client, respect_robots=False) as f:
            result = f.get("https://gone.test/feed")
        assert result.status_code == 404
        assert route.call_count == 1, "a 404 is an answer, not a failure to retry"

    @respx.mock
    def test_403_is_never_retried(self, client):
        route = respx.get("https://forbidden.test/feed").mock(return_value=httpx.Response(403))
        with build(client, respect_robots=False) as f:
            assert f.get("https://forbidden.test/feed").status_code == 403
        assert route.call_count == 1

    @respx.mock
    def test_retries_on_timeout(self, client):
        route = respx.get("https://slow.test/feed").mock(
            side_effect=[httpx.ReadTimeout("too slow"), httpx.Response(200, text="ok")]
        )
        with build(client, respect_robots=False) as f:
            assert f.get("https://slow.test/feed").text == "ok"
        assert route.call_count == 2


class TestResolve:
    @respx.mock
    def test_resolve_follows_redirects(self, client):
        respx.head("https://short.test/x").mock(
            return_value=httpx.Response(301, headers={"Location": "https://publisher.test/story"})
        )
        respx.head("https://publisher.test/story").mock(return_value=httpx.Response(200))
        with build(client, respect_robots=False) as f:
            assert f.resolve("https://short.test/x") == "https://publisher.test/story"

    @respx.mock
    def test_resolve_falls_back_to_get_when_head_is_refused(self, client):
        respx.head("https://picky.test/x").mock(return_value=httpx.Response(405))
        respx.get("https://picky.test/x").mock(return_value=httpx.Response(200, text="body"))
        with build(client, respect_robots=False) as f:
            assert f.resolve("https://picky.test/x") == "https://picky.test/x"

    @respx.mock
    def test_resolve_keeps_the_url_when_the_host_is_dead(self, client):
        respx.head("https://dead.test/x").mock(side_effect=httpx.ConnectError("down"))
        with build(client, respect_robots=False) as f:
            assert f.resolve("https://dead.test/x") == "https://dead.test/x"

    @respx.mock
    def test_resolve_respects_robots(self, client):
        respx.get("https://blocked.test/robots.txt").mock(
            return_value=httpx.Response(200, text=fixture("robots_disallow.txt"))
        )
        with build(client) as f, pytest.raises(RobotsDisallowed):
            f.resolve("https://blocked.test/x")
