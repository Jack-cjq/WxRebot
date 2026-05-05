# -*- coding: utf-8 -*-
"""群聊记账机器人控制面板（本地 Qt 桌面界面）。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from examples.messaging.bookkeeping_config import CONFIG_PATH, load_config, save_config


def _split_csv(text: str) -> List[str]:
    raw = str(text or "")
    parts = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    return [item.strip() for item in parts if item.strip()]


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config()

    command_aliases = dict(config.get("command_aliases") or {})
    command_aliases["help"] = _split_csv(str(payload.get("help_aliases_csv", "")))
    command_aliases["rollback"] = _split_csv(str(payload.get("rollback_aliases_csv", "")))
    command_aliases["bill"] = _split_csv(str(payload.get("bill_aliases_csv", "")))
    command_aliases["record_prefix"] = _split_csv(str(payload.get("record_prefix_csv", "")))
    config["command_aliases"] = command_aliases
    return config


def _append_manual_group(name: str) -> Dict[str, Any]:
    group_name = (name or "").strip()
    if not group_name:
        raise ValueError("群聊名称不能为空")

    config = load_config()
    manual_groups = list(config.get("manual_groups") or [])
    if group_name not in manual_groups:
        manual_groups.append(group_name)
    config["manual_groups"] = manual_groups
    save_config(config)
    return config


def _to_form_data(config: Dict[str, Any]) -> Dict[str, Any]:
    command_aliases = dict(config.get("command_aliases") or {})
    runtime = dict(config.get("runtime") or {})
    return {
        "help_aliases_csv": ", ".join(command_aliases.get("help") or []),
        "rollback_aliases_csv": ", ".join(command_aliases.get("rollback") or []),
        "bill_aliases_csv": ", ".join(command_aliases.get("bill") or []),
        "record_prefix_csv": ", ".join(command_aliases.get("record_prefix") or []),
        "runtime": runtime,
    }


def _fill_list_widget(widget: Any, items: List[str]) -> None:
    widget.clear()
    if items:
        widget.addItems(items)
    else:
        widget.addItem("暂无")


class BookkeepingPanelWindow:
    def __init__(self) -> None:
        from PySide6.QtWidgets import (
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QVBoxLayout,
            QWidget,
        )

        self._win = QMainWindow()
        self._win.setWindowTitle("微信群记账机器人控制面板")
        self._win.resize(920, 680)

        central = QWidget()
        root = QVBoxLayout(central)

        title = QLabel("微信群记账机器人控制面板")
        f = title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        path_hint = QLabel(f"配置文件：{CONFIG_PATH}")
        path_hint.setStyleSheet("color: #64748b; font-size: 12px;")
        path_hint.setWordWrap(True)
        root.addWidget(path_hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        cols = QHBoxLayout(scroll_content)

        left_col = QVBoxLayout()
        right_col = QVBoxLayout()

        # --- 监听状态（仅当前已监听 + 添加） ---
        gb_listen = QGroupBox("监听状态")
        gl = QVBoxLayout(gb_listen)
        gl.addWidget(QLabel("当前已监听的群（由机器人写入，重新加载可刷新）"))
        self._listening = QListWidget()
        self._listening.setMinimumHeight(200)
        gl.addWidget(self._listening)

        row_add = QHBoxLayout()
        self._add_group_name = QLineEdit()
        self._add_group_name.setPlaceholderText("输入群聊名称")
        btn_add = QPushButton("添加监听")
        btn_add.clicked.connect(self._on_add_listen)
        row_add.addWidget(self._add_group_name)
        row_add.addWidget(btn_add)
        gl.addLayout(row_add)

        left_col.addWidget(gb_listen)

        # --- 指令触发词 ---
        gb_cmd = QGroupBox("指令触发词")
        gc = QFormLayout(gb_cmd)
        self._help_aliases = QLineEdit()
        self._rollback_aliases = QLineEdit()
        self._bill_aliases = QLineEdit()
        self._record_prefix = QLineEdit()
        gc.addRow("帮助指令（逗号分隔）", self._help_aliases)
        gc.addRow("撤销指令（逗号分隔）", self._rollback_aliases)
        gc.addRow("账单指令前缀（逗号分隔）", self._bill_aliases)
        gc.addRow("记账前缀（逗号分隔）", self._record_prefix)
        hint_rp = QLabel(
            "记账前缀不能仅为四则运算符 +、-、*、/。\n"
            "「算式 + 分隔符 + 后缀」里的分隔符必须是上面配置的某一个前缀（可多选逗号分隔），"
            "例如配置了 # 则用 +1200*5.8#备注。"
        )
        hint_rp.setStyleSheet("color: #64748b; font-size: 12px;")
        hint_rp.setWordWrap(True)
        gc.addRow(hint_rp)

        right_col.addWidget(gb_cmd)

        # --- 操作 ---
        gb_ops = QGroupBox("操作")
        go = QVBoxLayout(gb_ops)
        row_btn = QHBoxLayout()
        btn_save = QPushButton("保存配置")
        btn_save.clicked.connect(self._on_save)
        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(self._load_into_ui)
        row_btn.addWidget(btn_save)
        row_btn.addWidget(btn_reload)
        row_btn.addStretch()
        go.addLayout(row_btn)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #15803d; font-size: 13px;")
        go.addWidget(self._status)

        right_col.addWidget(gb_ops)
        right_col.addStretch()

        left_w = QWidget()
        left_w.setLayout(left_col)
        left_w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        right_w = QWidget()
        right_w.setLayout(right_col)
        right_w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        cols.addWidget(left_w, 1)
        cols.addWidget(right_w, 1)
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, 1)

        self._win.setCentralWidget(central)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            "color: #b91c1c; font-size: 13px;" if error else "color: #15803d; font-size: 13px;"
        )

    def _load_into_ui(self) -> None:
        try:
            config = load_config()
            d = _to_form_data(config)
        except Exception as exc:
            self._set_status(f"读取配置失败：{exc}", error=True)
            return

        rt = d.get("runtime") or {}
        _fill_list_widget(self._listening, list(rt.get("current_listening_groups") or []))

        self._help_aliases.setText(str(d.get("help_aliases_csv") or ""))
        self._rollback_aliases.setText(str(d.get("rollback_aliases_csv") or ""))
        self._bill_aliases.setText(str(d.get("bill_aliases_csv") or ""))
        self._record_prefix.setText(str(d.get("record_prefix_csv") or ""))
        self._set_status("配置已加载")

    def _payload_from_ui(self) -> Dict[str, Any]:
        return {
            "help_aliases_csv": self._help_aliases.text(),
            "rollback_aliases_csv": self._rollback_aliases.text(),
            "bill_aliases_csv": self._bill_aliases.text(),
            "record_prefix_csv": self._record_prefix.text(),
        }

    def _on_save(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        try:
            config = _normalize_payload(self._payload_from_ui())
            save_config(config)
        except Exception as exc:
            self._set_status(str(exc), error=True)
            QMessageBox.warning(self._win, "保存失败", str(exc))
            return
        self._set_status("配置已保存")
        self._load_into_ui()

    def _on_add_listen(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        name = self._add_group_name.text().strip()
        if not name:
            self._set_status("请先输入群聊名称", error=True)
            return
        try:
            _append_manual_group(name)
        except Exception as exc:
            self._set_status(str(exc), error=True)
            QMessageBox.warning(self._win, "添加失败", str(exc))
            return
        self._add_group_name.clear()
        self._set_status("已添加监听，机器人将自动重载")
        self._load_into_ui()

    def show(self) -> None:
        self._load_into_ui()
        self._win.show()


def main() -> None:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("未安装 PySide6，请先执行：pip install PySide6")
        sys.exit(1)

    app = QApplication(sys.argv)
    panel = BookkeepingPanelWindow()
    panel.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
