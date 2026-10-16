from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.jev_control_demo.controller import JevSopController


class _Choice:
    def __init__(self, *, instructions: str, criteria: dict[str, str]) -> None:
        self.instructions = instructions
        self.criteria = criteria


def _answer(choice: str, confidence: float = 0.95):
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


class _Client:
    next_local = "7-36"
    next_global = "NONE"
    last_state = None

    def __init__(self, *, model: str) -> None:
        self.model = model

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def system_one(self, *, state, questions, model):
        self.__class__.last_state = state
        assert self.__class__.next_local in questions["stage_branch"].criteria
        assert self.__class__.next_global in questions["global_side_branch"].criteria
        return SimpleNamespace(
            choices={
                "stage_branch": _answer(self.__class__.next_local),
                "global_side_branch": _answer(self.__class__.next_global),
            }
        )


FAKE_SDK = SimpleNamespace(Choice=_Choice, TypeSafeClient=_Client)


class JevSopControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        _Client.next_local = "7-36"
        _Client.next_global = "NONE"
        _Client.last_state = None

    def _route(self, controller: JevSopController, text: str, history=None):
        with patch.dict("sys.modules", {"typesafe_sdk": FAKE_SDK}):
            return asyncio.run(controller.route(text, history or []))

    def test_local_branch_advances_stage_and_builds_controlled_query(self) -> None:
        controller = JevSopController(initial_stage="DELIVERY")
        decision = self._route(controller, "我收到了，但是还没填", ["坐席: 收到短信了吗？"])

        self.assertEqual(decision.sop_id, "7-36")
        self.assertEqual(decision.stage_before, "DELIVERY")
        self.assertEqual(decision.stage_after, "UNFINISHED")
        self.assertFalse(decision.is_global)
        self.assertIn("我收到了，但是还没填", decision.query)
        self.assertIn("收到未填", decision.query)
        self.assertEqual(_Client.last_state["recent_dialogue"], ["坐席: 收到短信了吗？"])

    def test_non_terminal_global_branch_preserves_main_stage(self) -> None:
        _Client.next_global = "7-11"
        controller = JevSopController(initial_stage="DELIVERY")
        decision = self._route(controller, "你们是不是诈骗？")

        self.assertTrue(decision.is_global)
        self.assertEqual(decision.sop_id, "7-11")
        self.assertEqual(decision.stage_after, "DELIVERY")

    def test_terminal_global_branch_closes_main_flow(self) -> None:
        _Client.next_local = "OUT_OF_SCOPE"
        _Client.next_global = "7-18"
        controller = JevSopController(initial_stage="COMMITMENT")
        decision = self._route(controller, "我正在开车，先不说了")

        self.assertEqual(decision.stage_after, "DONE")
        self.assertEqual(controller.stage, "DONE")

    def test_troubleshooting_branch_holds_stage(self) -> None:
        _Client.next_local = "7-51"
        controller = JevSopController(initial_stage="UNFINISHED")
        decision = self._route(controller, "人脸识别总是失败")

        self.assertEqual(decision.stage_after, "UNFINISHED")

    def test_opening_confirmation_advances_to_delivery(self) -> None:
        _Client.next_local = "7-6"
        controller = JevSopController()
        decision = self._route(controller, "是我本人，现在方便")

        self.assertEqual(decision.stage_before, "OPENING")
        self.assertEqual(decision.stage_after, "DELIVERY")
        self.assertIn("询问是否收到背调授权通知", decision.query)

    def test_async_progress_requires_evidence_before_advancing(self) -> None:
        _Client.next_local = "7-6"
        controller = JevSopController(async_progress=True)
        decision = self._route(controller, "是我本人，现在方便")
        self.assertEqual(decision.stage_after, "OPENING")
        invalid = {"completed": True, "evidence": "编造的证据"}
        self.assertFalse(asyncio.run(controller.apply_progress(1, invalid, "收到通知了吗？")))
        self.assertEqual(controller.stage, "OPENING")
        patch = {"completed": True, "evidence": "收到通知了吗？", "facts": {"identity_confirmed": True}}
        self.assertTrue(asyncio.run(controller.apply_progress(1, patch, "收到通知了吗？")))
        self.assertEqual(controller.stage, "DELIVERY")
        self.assertNotIn("identity_confirmed", controller.status)

    def test_late_progress_cannot_update_new_turn(self) -> None:
        _Client.next_local = "7-6"
        controller = JevSopController(async_progress=True)
        self._route(controller, "是我本人，现在方便")
        self._route(controller, "是我本人，现在方便")
        patch = {"completed": True, "evidence": "现在方便"}
        self.assertFalse(asyncio.run(controller.apply_progress(1, patch, "")))
        self.assertEqual(controller.stage, "OPENING")


if __name__ == "__main__":
    unittest.main()
