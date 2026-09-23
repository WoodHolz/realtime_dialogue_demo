from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend.protocol import EVENT_CHAT_TEXT_QUERY, EVENT_SAY_HELLO
from backend.realtime_client import RealtimeVoiceClient


class _OpenWebSocket:
    closed = False
    close_code = None

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)


class RealtimeTextQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RealtimeVoiceClient("app", "access")
        self.client._ws = _OpenWebSocket()
        self.client._session_id = "session-test"

    def test_text_query_is_event_501_with_content(self) -> None:
        with patch("backend.realtime_client.encode_client_json_event", return_value=b"frame") as encode:
            asyncio.run(self.client.send_text_query("SOP 指导\n用户回答"))

        encode.assert_called_once_with(
            EVENT_CHAT_TEXT_QUERY,
            {"content": "SOP 指导\n用户回答"},
            "session-test",
        )
        self.assertEqual(self.client._ws.sent, [b"frame"])

    def test_opening_is_event_300_with_content(self) -> None:
        with patch("backend.realtime_client.encode_client_json_event", return_value=b"frame") as encode:
            asyncio.run(self.client.say_hello("您好"))

        encode.assert_called_once_with(
            EVENT_SAY_HELLO,
            {"content": "您好"},
            "session-test",
        )


if __name__ == "__main__":
    unittest.main()
