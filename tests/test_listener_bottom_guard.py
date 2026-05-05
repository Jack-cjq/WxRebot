# -*- coding: utf-8 -*-
"""listener 底部判定与锚点逻辑的单元测试（不依赖微信 / UIA）。"""

from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.messaging.listener import (  # noqa: E402
    _VisibleItem,
    _looks_like_history_view,
    _stable_message_signature,
    _update_bottom_anchors,
)


def _msg(name: str, rid: int = 1) -> _VisibleItem:
    return _VisibleItem(
        kind="message",
        name=name,
        class_name="mmui::ChatBubbleItemView",
        runtime_id=(42, rid),
        control=None,
    )


class BottomGuardTests(unittest.TestCase):
    def test_stable_signature_ignores_runtime_id(self) -> None:
        a = _msg("  hello  ", rid=1)
        b = _msg("  hello  ", rid=99)
        self.assertEqual(_stable_message_signature(a), _stable_message_signature(b))

    def test_normal_bottom_tail_overlaps_anchors(self) -> None:
        """底部新增一条时，尾部通常仍含旧签名，不应判为 history_view。"""
        session = SimpleNamespace(
            bottom_anchor_sigs=deque(
                [
                    ("mmui::ChatBubbleItemView", "m1"),
                    ("mmui::ChatBubbleItemView", "m2"),
                ],
                maxlen=30,
            )
        )
        items = [_msg("m1"), _msg("m2"), _msg("m3 new")]
        self.assertFalse(_looks_like_history_view(session, items))

    def test_history_view_when_tail_unrelated_to_anchors(self) -> None:
        """上翻后可见尾部与底部锚点完全无交集 → 视为历史区域。"""
        session = SimpleNamespace(
            bottom_anchor_sigs=deque(
                [("mmui::ChatBubbleItemView", "recent a")],
                maxlen=30,
            )
        )
        items = [_msg("ancient x"), _msg("ancient y")]
        self.assertTrue(_looks_like_history_view(session, items))

    def test_empty_anchors_never_history_view(self) -> None:
        session = SimpleNamespace(bottom_anchor_sigs=deque(maxlen=30))
        items = [_msg("anything")]
        self.assertFalse(_looks_like_history_view(session, items))

    def test_replay_same_instruction_at_bottom_not_history_view(self) -> None:
        """复制旧指令再发：正文与锚点重复，尾部命中锚点 → 非 history_view（仍需 at_bottom 才入队）。"""
        cmd = "支出 18 餐饮"
        session = SimpleNamespace(
            bottom_anchor_sigs=deque(
                [
                    ("mmui::ChatBubbleItemView", cmd),
                ],
                maxlen=30,
            )
        )
        items = [_msg(cmd)]
        self.assertFalse(_looks_like_history_view(session, items))

    def test_update_bottom_anchors_dedupes(self) -> None:
        session = SimpleNamespace(bottom_anchor_sigs=deque(maxlen=30))
        items = [_msg("a"), _msg("b")]
        _update_bottom_anchors(session, items)
        _update_bottom_anchors(session, items)
        self.assertEqual(len(session.bottom_anchor_sigs), 2)


if __name__ == "__main__":
    unittest.main()
