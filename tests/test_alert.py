from __future__ import annotations

import httpx
import pytest
import respx

from radar import alert, db
from radar.models import AlertPayload, Award, RawItem

PAYLOAD = AlertPayload(
    award_id=1,
    title="ASGC awarded AED 1.2 billion residential tower contract in Dubai",
    contractor="ASGC Construction",
    developer="Emaar Development",
    project_name="Tower X, Business Bay",
    value_aed=1_200_000_000,
    emirate="Dubai",
    sector="residential",
    award_date="2026-07-27",
    source_url="https://www.thenationalnews.com/business/asgc-tower",
    careers_url="https://www.asgcgroup.com/careers",
    hiring_window_start="2026-08-24",
    hiring_window_end="2026-10-25",
    relevance_score=85,
    confidence=0.9,
)


def seed_award(conn, contractor="ASGC Construction", url="https://x.test/a") -> int:
    item_id = db.insert_raw_item(
        conn,
        RawItem(
            source_id="s",
            url=url,
            url_hash=f"h-{url}",
            title=f"{contractor} awarded a Dubai contract",
        ),
    )
    return db.insert_award(
        conn,
        Award(
            raw_item_id=item_id,
            contractor_raw=contractor,
            value_aed=1_200_000_000,
            emirate="Dubai",
            sector="residential",
            confidence=0.4,
        ),
    )


class TestFormatValue:
    def test_billions(self):
        assert alert.format_value(1_200_000_000) == "AED 1.20bn"

    def test_round_billions_drop_the_decimals(self):
        assert alert.format_value(2_000_000_000) == "AED 2bn"

    def test_millions(self):
        assert alert.format_value(750_000_000) == "AED 750m"

    def test_small_values(self):
        assert alert.format_value(450_000) == "AED 450,000"

    def test_undisclosed_is_stated_plainly(self):
        assert alert.format_value(None) == "undisclosed"


class TestRender:
    def test_includes_the_facts_and_the_source_link(self):
        text = alert.render_markdown(PAYLOAD)
        assert "ASGC Construction" in text
        assert "AED 1.20bn" in text
        assert "residential · Dubai" in text
        assert PAYLOAD.source_url in text

    def test_includes_the_hiring_window(self):
        """The hiring window is the whole point of the tool (brief §6.6)."""
        text = alert.render_markdown(PAYLOAD)
        assert "2026-08-24" in text and "2026-10-25" in text
        assert "mobilisation hiring" in text

    def test_includes_the_careers_link(self):
        assert PAYLOAD.careers_url in alert.render_markdown(PAYLOAD)

    def test_omits_phase_3_lines_when_absent(self):
        p = PAYLOAD.model_copy(
            update={
                "hiring_window_start": None,
                "hiring_window_end": None,
                "careers_url": None,
                "relevance_score": None,
            }
        )
        text = alert.render_markdown(p)
        assert "mobilisation hiring" not in text
        assert "Careers" not in text
        assert "Relevance" not in text

    def test_low_confidence_is_flagged_to_the_reader(self):
        p = PAYLOAD.model_copy(update={"confidence": 0.4})
        assert "guessed by regex" in alert.render_markdown(p)

    def test_high_confidence_carries_no_warning(self):
        assert "guessed by regex" not in alert.render_markdown(PAYLOAD)

    def test_markdown_metacharacters_in_names_cannot_break_the_message(self):
        p = PAYLOAD.model_copy(update={"contractor": "A*B_C [Ltd]"})
        text = alert.render_markdown(p)
        assert "*A*B_C [Ltd]*" not in text
        assert text.count("*") % 2 == 0

    def test_missing_contractor_says_so_rather_than_inventing_one(self):
        p = PAYLOAD.model_copy(update={"contractor": None})
        assert "Unidentified contractor" in alert.render_markdown(p)

    def test_undisclosed_value_is_rendered_honestly(self):
        p = PAYLOAD.model_copy(update={"value_aed": None})
        assert "undisclosed" in alert.render_markdown(p)

    def test_falls_back_to_report_date_when_the_award_date_is_unknown(self):
        p = PAYLOAD.model_copy(
            update={"award_date": None, "published_at": "2026-07-27T08:14:00+00:00"}
        )
        text = alert.render_markdown(p)
        assert "Reported:* 2026-07-27" in text

    def test_plaintext_drops_markdown_markers(self):
        text = alert.render_plaintext(PAYLOAD)
        assert "*" not in text and "_" not in text
        assert "ASGC Construction" in text

    def test_never_forwards_article_prose(self):
        """Brief §9.6 — link to the source, do not mirror it."""
        p = PAYLOAD.model_copy(update={"title": "A headline"})
        assert len(alert.render_markdown(p)) < 800


class TestStdoutChannel:
    def test_prints_and_is_always_configured(self, capsys):
        channel = alert.StdoutChannel()
        assert channel.configured()
        channel.send(PAYLOAD)
        assert "ASGC Construction" in capsys.readouterr().out


class TestTelegramChannel:
    def test_unconfigured_without_credentials(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert not alert.TelegramChannel().configured()

    @respx.mock
    def test_posts_to_the_bot_api(self):
        route = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        with httpx.Client() as client:
            alert.TelegramChannel(token="123:abc", chat_id="42", client=client).send(PAYLOAD)

        assert route.called
        body = route.calls[0].request.content.decode()
        assert "ASGC Construction" in body
        assert '"chat_id":"42"' in body.replace(", ", ",").replace(": ", ":")

    @respx.mock
    def test_api_error_raises_alert_error(self):
        respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            return_value=httpx.Response(400, text="chat not found")
        )
        with httpx.Client() as client, pytest.raises(alert.AlertError):
            alert.TelegramChannel(token="123:abc", chat_id="42", client=client).send(PAYLOAD)

    @respx.mock
    def test_network_error_raises_alert_error(self):
        respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            side_effect=httpx.ConnectError("down")
        )
        with httpx.Client() as client, pytest.raises(alert.AlertError):
            alert.TelegramChannel(token="123:abc", chat_id="42", client=client).send(PAYLOAD)


class FakeSMTP:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class TestEmailChannel:
    def test_unconfigured_without_smtp_settings(self, monkeypatch):
        for var in ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO", "SMTP_USER"):
            monkeypatch.delenv(var, raising=False)
        assert not alert.EmailChannel().configured()

    def test_builds_a_readable_message(self):
        channel = alert.EmailChannel(host="smtp.test", sender="me@test", recipient="you@test")
        msg = channel.build_message(PAYLOAD)
        assert "ASGC Construction" in msg["Subject"]
        assert msg["To"] == "you@test"
        assert "AED 1.20bn" in msg.get_content()

    def test_sends_through_the_smtp_factory(self):
        FakeSMTP.sent.clear()
        channel = alert.EmailChannel(
            host="smtp.test", sender="me@test", recipient="you@test", smtp_factory=FakeSMTP
        )
        channel.send(PAYLOAD)
        assert len(FakeSMTP.sent) == 1

    def test_smtp_failure_raises_alert_error(self):
        def boom():
            raise OSError("connection refused")

        channel = alert.EmailChannel(
            host="smtp.test", sender="me@test", recipient="you@test", smtp_factory=boom
        )
        with pytest.raises(alert.AlertError):
            channel.send(PAYLOAD)


class TestChannelsFromEnv:
    def test_reads_the_channel_list(self, monkeypatch):
        monkeypatch.setenv("ALERT_CHANNELS", "telegram,email")
        assert [c.name for c in alert.channels_from_env()] == ["telegram", "email"]

    def test_single_channel(self, monkeypatch):
        monkeypatch.setenv("ALERT_CHANNELS", "telegram")
        assert [c.name for c in alert.channels_from_env()] == ["telegram"]

    def test_unknown_channel_is_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("ALERT_CHANNELS", "telegram,carrier-pigeon")
        assert [c.name for c in alert.channels_from_env()] == ["telegram"]


class RecordingChannel:
    def __init__(self, name="telegram", fail=False):
        self.name = name
        self.fail = fail
        self.sent: list[AlertPayload] = []

    def configured(self) -> bool:
        return True

    def send(self, payload: AlertPayload) -> None:
        if self.fail:
            raise alert.AlertError("nope")
        self.sent.append(payload)


class TestSendAlerts:
    def test_sends_each_award_once(self, conn):
        seed_award(conn)
        channel = RecordingChannel()
        stats = alert.send_alerts(conn, [channel])

        assert stats.sent == 1
        assert len(channel.sent) == 1

    def test_rerunning_sends_nothing(self, conn):
        """Phase 0 acceptance: zero duplicate alerts on a second run."""
        seed_award(conn)
        channel = RecordingChannel()
        alert.send_alerts(conn, [channel])
        second = alert.send_alerts(conn, [channel])

        assert second.sent == 0
        assert len(channel.sent) == 1
        assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 1

    def test_each_channel_alerts_independently(self, conn):
        seed_award(conn)
        telegram, email = RecordingChannel("telegram"), RecordingChannel("email")
        alert.send_alerts(conn, [telegram, email])

        assert len(telegram.sent) == 1 and len(email.sent) == 1
        assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 2

    def test_a_failed_send_is_not_recorded_and_retries_next_run(self, conn):
        seed_award(conn)
        failing = RecordingChannel(fail=True)
        stats = alert.send_alerts(conn, [failing])

        assert stats.failed == 1
        assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0

        working = RecordingChannel()
        assert alert.send_alerts(conn, [working]).sent == 1

    def test_dry_run_prints_and_records_nothing(self, conn, capsys):
        seed_award(conn)
        channel = RecordingChannel()
        stats = alert.send_alerts(conn, [channel], dry_run=True)

        assert "ASGC Construction" in capsys.readouterr().out
        assert stats.sent == 1
        assert channel.sent == [], "dry-run must not touch the real channel"
        assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0

    def test_dry_run_can_be_repeated(self, conn, capsys):
        seed_award(conn)
        alert.send_alerts(conn, [], dry_run=True)
        capsys.readouterr()
        assert alert.send_alerts(conn, [], dry_run=True).sent == 1

    def test_unconfigured_channel_is_reported_not_fatal(self, conn):
        seed_award(conn)

        class Unconfigured(RecordingChannel):
            def configured(self):
                return False

        stats = alert.send_alerts(conn, [Unconfigured("telegram")])
        assert stats.unconfigured == ["telegram"]
        assert stats.sent == 0

    def test_limit_caps_the_batch(self, conn):
        for i in range(3):
            seed_award(conn, contractor=f"Contractor {i}", url=f"https://x.test/{i}")
        assert alert.send_alerts(conn, [RecordingChannel()], limit=2).sent == 2
