# -*- coding: utf-8 -*-
"""控制面板监听群配置操作测试。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.messaging import group_bookkeeping_panel as panel  # noqa: E402


class PanelGroupConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_load = panel.load_config
        self._saved_save = panel.save_config
        self.config: Dict[str, Any] = {
            "manual_groups": ["测试群"],
            "exclude_groups": ["旧群"],
            "command_aliases": {},
            "runtime": {},
        }

        def load_config() -> Dict[str, Any]:
            return copy.deepcopy(self.config)

        def save_config(config: Dict[str, Any]) -> None:
            self.config = copy.deepcopy(config)

        panel.load_config = load_config
        panel.save_config = save_config

    def tearDown(self) -> None:
        panel.load_config = self._saved_load
        panel.save_config = self._saved_save

    def test_add_manual_group_removes_exclusion(self) -> None:
        panel._append_manual_group("旧群")

        self.assertEqual(self.config["manual_groups"], ["测试群", "旧群"])
        self.assertEqual(self.config["exclude_groups"], [])

    def test_add_manual_groups_dedupes_and_removes_exclusions(self) -> None:
        panel._append_manual_groups(["测试群", "旧群", "新群"])

        self.assertEqual(self.config["manual_groups"], ["测试群", "旧群", "新群"])
        self.assertEqual(self.config["exclude_groups"], [])

    def test_remove_listen_group_excludes_and_removes_manual(self) -> None:
        panel._remove_listen_group("测试群")

        self.assertEqual(self.config["manual_groups"], [])
        self.assertEqual(self.config["exclude_groups"], ["旧群", "测试群"])


if __name__ == "__main__":
    unittest.main()
