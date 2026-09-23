from __future__ import annotations

import asyncio
import unittest

from backend.server import urge_auth_t2s_page, t2sbg_voice_page


class JevUrgeAuthPageTest(unittest.TestCase):
    def test_dedicated_page_uses_dedicated_websocket(self) -> None:
        response = asyncio.run(urge_auth_t2s_page())
        body = response.body.decode("utf-8")

        self.assertIn("/ws/urge-auth/t2s", body)
        self.assertIn('value="livekit_turn_v1" checked', body)
        self.assertNotIn("Smart Turn v3（Pipecat", body)
        self.assertNotIn("LiveKit text EOU（旧版", body)

    def test_existing_t2sbg_page_keeps_original_websocket(self) -> None:
        response = asyncio.run(t2sbg_voice_page())
        body = response.body.decode("utf-8")

        self.assertIn("const url = protocol + '//' + host + '/ws';", body)
        self.assertNotIn("/ws/urge-auth/t2s", body)


if __name__ == "__main__":
    unittest.main()
