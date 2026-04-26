# -*- coding: utf-8 -*-
"""群聊记账机器人配置读写（单文件 JSON）。"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 多线程下「读-改-写」同一 JSON 会丢更新；所有 load/save 必须串行
_config_io_lock = threading.RLock()

# 记账前缀不能仅为四则运算符（保存配置时拒绝）
_OPERATOR_ONLY_PREFIX_RE = re.compile(r"^[+\-*/]+$")


CONFIG_PATH = Path(__file__).resolve().parent / "group_bookkeeping_config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "auto_discover_groups": False,
    "discover_interval_seconds": 60,
    "manual_groups": [],
    "exclude_groups": [],
    "group_admins": {
        "*": ["张三", "李四"]
    },
    "command_aliases": {
        "help": ["记账帮助", "账单帮助", "help", "帮助"],
        "rollback": ["撤销记账", "撤销", "\\撤回"],
        "query": ["\\查账"],
        "clear": ["\\清账"],
        "bill": ["账单"],
        "record_prefix": ["$"],
    },
    "runtime": {
        "last_discovered_groups": [],
        "current_listening_groups": [],
        "desired_listening_groups": [],
        "group_balances": {},
        "updated_at": "",
    },
}


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in incoming.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def is_forbidden_operator_only_record_prefix(prefix: str) -> bool:
    t = (prefix or "").strip()
    return bool(t and _OPERATOR_ONLY_PREFIX_RE.fullmatch(t))


def sanitize_record_prefixes(prefixes: Any) -> List[str]:
    """去掉非法前缀；若全部非法或为空则回退为 $。"""
    if not isinstance(prefixes, list):
        return ["$"]
    out: List[str] = []
    for raw in prefixes:
        p = str(raw or "").strip()
        if not p or is_forbidden_operator_only_record_prefix(p):
            continue
        out.append(p)
    return out if out else ["$"]


def validate_record_prefixes_for_save(prefixes: Any) -> None:
    """保存配置时校验：记账标识不能仅为四则运算符。"""
    if prefixes is None:
        return
    if not isinstance(prefixes, list):
        raise ValueError("command_aliases.record_prefix 必须是字符串数组。")
    if not prefixes:
        raise ValueError("记账标识至少填写一个有效前缀。")
    for raw in prefixes:
        p = str(raw or "").strip()
        if not p:
            raise ValueError("记账标识不能为空（请检查逗号分隔是否有多余逗号）。")
        if is_forbidden_operator_only_record_prefix(p):
            raise ValueError(
                f"记账标识「{p}」不能仅为四则运算符（+、-、*、/），请更换为其它符号或文字。"
            )


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    with _config_io_lock:
        if not path.exists():
            save_config(copy.deepcopy(DEFAULT_CONFIG), path=path)
            return copy.deepcopy(DEFAULT_CONFIG)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            save_config(copy.deepcopy(DEFAULT_CONFIG), path=path)
            return copy.deepcopy(DEFAULT_CONFIG)

        merged = _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
        aliases = dict(merged.get("command_aliases") or {})
        aliases["record_prefix"] = sanitize_record_prefixes(aliases.get("record_prefix"))
        merged["command_aliases"] = aliases
        save_config(merged, path=path)
        return merged


def save_config(config: Dict[str, Any], path: Path = CONFIG_PATH) -> None:
    with _config_io_lock:
        aliases = config.get("command_aliases")
        if isinstance(aliases, dict) and "record_prefix" in aliases:
            validate_record_prefixes_for_save(aliases.get("record_prefix"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")


def update_runtime(
    *,
    current_listening_groups: Optional[list] = None,
    desired_listening_groups: Optional[list] = None,
    last_discovered_groups: Optional[list] = None,
    path: Path = CONFIG_PATH,
) -> Dict[str, Any]:
    # 与 load/save 同一临界区，避免「读配置后、写回前」被其它线程改写
    with _config_io_lock:
        config = load_config(path=path)
        runtime = dict(config.get("runtime") or {})
        if current_listening_groups is not None:
            runtime["current_listening_groups"] = list(dict.fromkeys(current_listening_groups))
        if desired_listening_groups is not None:
            runtime["desired_listening_groups"] = list(dict.fromkeys(desired_listening_groups))
        if last_discovered_groups is not None:
            runtime["last_discovered_groups"] = list(dict.fromkeys(last_discovered_groups))
        runtime["updated_at"] = datetime.now().isoformat(timespec="seconds")
        config["runtime"] = runtime
        save_config(config, path=path)
        return config
