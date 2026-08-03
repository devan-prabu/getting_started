from __future__ import annotations

import httpx
import pytest
import respx

from prep.telegram import TELEGRAM_API, StdoutClient, TelegramClient, TelegramError, parse_command

TOKEN = "123:abc"
CHAT = "555"


def client(**kw) -> TelegramClient:
    return TelegramClient(token=TOKEN, chat_id=CHAT, **kw)


def update(
    update_id: int, text: str, *, chat: str = CHAT, reply_to: int | None = None, message_id: int = 9
) -> dict:
    message: dict = {"message_id": message_id, "chat": {"id": int(chat)}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


class TestParseCommand:
    def test_plain_command(self):
        cmd = parse_command("/harder")
        assert cmd is not None and cmd.name == "harder" and cmd.argument == ""

    def test_command_with_argument(self):
        cmd = parse_command("/goal move to Parsons by October")
        assert cmd.name == "goal"
        assert cmd.argument == "move to Parsons by October"

    def test_bot_suffix_is_stripped(self):
        assert parse_command("/status@prepbot").name == "status"

    def test_case_is_normalised(self):
        assert parse_command("/GOAL something").name == "goal"

    def test_multiline_argument_survives(self):
        cmd = parse_command("/cv line one\nline two")
        assert "line two" in cmd.argument

    def test_a_plain_answer_is_not_a_command(self):
        assert parse_command("The RE leads site supervision.") is None

    def test_a_bare_slash_is_not_a_command(self):
        assert parse_command("/ 123") is None


class TestSend:
    @respx.mock
    def test_returns_the_message_id(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        )
        assert client().send("hello") == 77

    @respx.mock
    def test_an_http_error_raises(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(400, text="Bad Request: chat not found")
        )
        with pytest.raises(TelegramError, match="400"):
            client().send("hello")

    @respx.mock
    def test_ok_false_raises(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": False, "description": "blocked"})
        )
        with pytest.raises(TelegramError, match="not ok"):
            client().send("hello")

    @respx.mock
    def test_a_network_failure_raises_rather_than_hanging(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/sendMessage").mock(
            side_effect=httpx.ConnectError("no route")
        )
        with pytest.raises(TelegramError, match="request failed"):
            client().send("hello")

    @respx.mock
    def test_overlong_messages_are_truncated_to_the_api_limit(self):
        route = respx.post(f"{TELEGRAM_API}/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        client().send("x" * 9000)
        assert len(route.calls[0].request.read().decode()) < 6000

    def test_unconfigured_is_refused_before_any_request(self):
        with pytest.raises(TelegramError, match="not set"):
            TelegramClient(token="", chat_id="").send("hello")


class TestGetUpdates:
    @respx.mock
    def test_flattens_messages(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": [update(10, "my answer", reply_to=77)]},
            )
        )
        updates = client().get_updates()
        assert len(updates) == 1
        assert updates[0].update_id == 10
        assert updates[0].reply_to_message_id == 77
        assert updates[0].command is None

    @respx.mock
    def test_messages_from_another_chat_are_ignored(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": [update(11, "hello", chat="999")]}
            )
        )
        assert client().get_updates() == []

    @respx.mock
    def test_non_text_updates_are_skipped(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 12,
                            "message": {"message_id": 1, "chat": {"id": int(CHAT)}, "sticker": {}},
                        },
                    ],
                },
            )
        )
        assert client().get_updates() == []

    @respx.mock
    def test_the_offset_is_passed_through(self):
        route = respx.post(f"{TELEGRAM_API}/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        client().get_updates(offset=42)
        assert '"offset":42' in route.calls[0].request.content.decode().replace(" ", "")

    @respx.mock
    def test_a_command_update_exposes_the_command(self):
        respx.post(f"{TELEGRAM_API}/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": [update(13, "/focus concrete")]}
            )
        )
        cmd = client().get_updates()[0].command
        assert cmd.name == "focus" and cmd.argument == "concrete"


class TestStdoutClient:
    def test_it_records_and_never_reads(self, capsys):
        stub = StdoutClient()
        assert stub.send("drill one") is None
        assert stub.get_updates() == []
        assert stub.sent == ["drill one"]
        assert "drill one" in capsys.readouterr().out
