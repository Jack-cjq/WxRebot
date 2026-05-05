# -*- coding: utf-8 -*-
"""历史视图 → 回到底部 resync 状态机的模拟测试。"""

from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.messaging.listener import (  # noqa: E402
    MESSAGE_CLASSES,
    TIME_CLASS,
    _ListenSession,
    _VisibleItem,
    _contiguous_new_message_suffix_from_bottom,
    _mark_visible_items_seen,
    _stable_message_signature,
    _update_bottom_anchors,
)


def _msg(name: str, rid: int) -> _VisibleItem:
    cls = next(iter(MESSAGE_CLASSES))
    return _VisibleItem(
        kind="message",
        name=name,
        class_name=cls,
        runtime_id=(rid,),
        control=None,
    )


def _time_row(name: str = "12:00", rid: int = 0) -> _VisibleItem:
    return _VisibleItem(
        kind="time/system",
        name=name,
        class_name=TIME_CLASS,
        runtime_id=(99, rid),
        control=None,
    )


def _session_stub() -> _ListenSession:
    return _ListenSession(
        group="模拟群",
        hwnd=0,
        root=None,
        msg_list=None,
        seen=set(),
        boot_wall_time=0.0,
        boot_monotonic_ns=0,
        bootstrap_completed_wall_time=0.0,
        historic_ui_keys=set(),
        bottom_anchor_sigs=deque(maxlen=30),
    )


class HistoryResyncSimulationTests(unittest.TestCase):
    def test_mark_visible_marks_messages_and_time(self) -> None:
        s = _session_stub()
        items = [_time_row(), _msg("A", 1), _msg("B", 2)]
        n = _mark_visible_items_seen(s, items)
        self.assertEqual(n, 2)
        self.assertEqual(len(s.seen), 3)
        self.assertEqual(len(s.historic_ui_keys), 2)

    def test_resync_then_new_rid_same_content_not_in_new_suffix(self) -> None:
        """回到底部后旧消息以新 RuntimeId 出现：先 mark 进 seen，再算 suffix 应截断在 seen 处。"""
        s = _session_stub()
        old_a = _msg("指令A", 100)
        old_b = _msg("指令B", 101)
        for it in (old_a, old_b):
            s.seen.add(it.key)
            s.historic_ui_keys.add(it.key)
        _update_bottom_anchors(s, [old_a, old_b])

        # 虚拟化回流：同文案新 Rid
        new_a = _msg("指令A", 200)
        new_b = _msg("指令B", 201)
        items_full = [new_a, new_b]
        _mark_visible_items_seen(s, items_full)
        self.assertIn(new_a.key, s.seen)
        suffix = _contiguous_new_message_suffix_from_bottom(items_full, s.seen)
        self.assertEqual(suffix, [])

    def test_after_resync_only_truly_new_message_in_suffix(self) -> None:
        s = _session_stub()
        baseline = [_msg("C", 1), _msg("B", 2), _msg("A", 3)]
        for it in baseline:
            s.seen.add(it.key)
            s.historic_ui_keys.add(it.key)
        for it in baseline:
            sig = _stable_message_signature(it)
            if sig not in s.bottom_anchor_sigs:
                s.bottom_anchor_sigs.append(sig)

        # 回到底部 resync：新 Rid 的 A/B/C
        resync_items = [_msg("C", 10), _msg("B", 11), _msg("A", 12)]
        _mark_visible_items_seen(s, resync_items)

        # 又来真新消息 D
        d = _msg("新指令D", 20)
        bottom_snapshot = resync_items + [d]
        suffix = _contiguous_new_message_suffix_from_bottom(bottom_snapshot, s.seen)
        self.assertEqual(len(suffix), 1)
        self.assertEqual(suffix[0].name, "新指令D")


if __name__ == "__main__":
    unittest.main()
