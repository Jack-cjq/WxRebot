# -*- coding: utf-8 -*-
r"""微信群聊记账机器人（自动发现群 + 指令控制）。

启动目标：
1. 自动发现当前微信左侧会话里的群聊并监听。
2. 检测到记账指令后自动回复并执行。
3. 每个群单独记账。
注意：
- wx4py 当前版本的 MessageEvent 没有稳定 sender 字段。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import ast
import operator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src import CallbackHandler, WeChatClient
from src.core import uiautomation as uia
from src.features.messaging.listener import MessageEvent
from src.utils.logger import get_logger
from examples.messaging.bookkeeping_config import (
    is_forbidden_operator_only_record_prefix,
    load_config,
    save_config,
    update_runtime,
)


logger = get_logger(__name__)


def _resolve_data_dir() -> Path:
    """开发态：wx4py/examples/data/group_ledgers；打包运行时：包根目录下 data/group_ledgers。"""
    raw = (os.environ.get("WX_BOOKKEEPING_BUNDLE_DIR") or "").strip()
    if raw:
        return Path(raw).resolve() / "data" / "group_ledgers"
    return Path(__file__).resolve().parent.parent / "data" / "group_ledgers"


# 数据目录
DATA_DIR = _resolve_data_dir()

# 发送者解析格式：`昵称: 指令`
SENDER_PREFIX_RE = re.compile(r"^([^\n:：﹕꞉∶]{1,64})[:：﹕꞉∶]\s*(.+)$")


@dataclass(frozen=True)
class CommandEnvelope:
    sender: Optional[str]
    command_text: str
    raw_text: str


@dataclass(frozen=True)
class LedgerEntry:
    timestamp: float
    direction: str  # "收入" | "支出"
    amount: float
    category: str
    note: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "amount": round(self.amount, 2),
            "category": self.category,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict) -> "LedgerEntry":
        return LedgerEntry(
            timestamp=float(data.get("timestamp", 0.0)),
            direction=str(data.get("direction", "支出")),
            amount=float(data.get("amount", 0.0)),
            category=str(data.get("category", "未分类")),
            note=str(data.get("note", "")),
        )


class GroupLedgerStore:
    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _group_file(self, group: str) -> Path:
        normalized = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", group).strip("_")
        if not normalized:
            normalized = "group"
        suffix = sha1(group.encode("utf-8")).hexdigest()[:8]
        return self._data_dir / f"{normalized}_{suffix}.jsonl"

    def _read_all(self, group: str) -> List[LedgerEntry]:
        path = self._group_file(group)
        if not path.exists():
            return []
        entries: List[LedgerEntry] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(LedgerEntry.from_dict(json.loads(line)))
                except Exception:
                    continue
        return entries

    def add(self, group: str, entry: LedgerEntry) -> None:
        path = self._group_file(group)
        payload = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(payload + "\n")

    def list_by_period(self, group: str, period: str) -> List[LedgerEntry]:
        entries = self._read_all(group)
        if period == "全部":
            return entries

        now = datetime.now()
        start = datetime(now.year, now.month, now.day)
        if period == "本月":
            start = datetime(now.year, now.month, 1)
        start_ts = start.timestamp()
        return [entry for entry in entries if entry.timestamp >= start_ts]

    def pop_last(self, group: str) -> Optional[LedgerEntry]:
        with self._lock:
            entries = self._read_all(group)
            if not entries:
                return None
            removed = entries.pop()
            path = self._group_file(group)
            with path.open("w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            return removed

    def clear(self, group: str) -> None:
        path = self._group_file(group)
        with self._lock:
            if path.exists():
                path.write_text("", encoding="utf-8")


class BookkeepingBot:
    ADD_RE = re.compile(
        r"^(?:记账\s+)?(收入|支出)\s+([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$"
    )
    ADD_RE_ALT = re.compile(
        r"^支出\s+([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$"
    )
    BILL_RE = re.compile(r"^账单(?:\s+(今日|本月|全部))?$")
    MATH_RE = re.compile(r"^[\d\.\+\-\*/\(\)\s]+$")
    # 机器人回复第二行形如「+1200*5.8=6960$后缀」；「=金额」不在合法算式字符集内，需剥离且勿重复入账
    _ECHO_EQUALS_SUFFIX_RE = re.compile(r"^(.+?)=(\d+(?:\.\d+)?)$")
    # More tolerant add-command parser (less strict about spaces).
    ADD_RE_RELAXED = re.compile(
        r"^(?:\u8bb0\u8d26\s*)?(\u6536\u5165|\u652f\u51fa)\s*([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$"
    )
    ADD_RE_ALT_RELAXED = re.compile(
        r"^\u652f\u51fa\s*([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$"
    )
    _ALLOWED_BIN_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }
    _ALLOWED_UNARY_OPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }
    _ROLLBACK_COMMANDS = {"\\撤回", "/撤回"}
    _QUERY_COMMANDS = {"\\查账", "/查账"}
    _CLEAR_COMMANDS = {"\\清账", "/清账"}
    # 整条指令仅含四则运算符与空白时，不作为有效指令（避免误触）
    _OPERATOR_ONLY_COMMAND_RE = re.compile(r"^[+\-*/\s]+$")
    def __init__(
        self,
        store: GroupLedgerStore,
        command_aliases: Dict[str, List[str]],
    ):
        self._store = store
        self._command_aliases = {
            key: [item.strip() for item in values if item and item.strip()]
            for key, values in command_aliases.items()
        }

    @staticmethod
    def _is_operator_only_command_text(text: str) -> bool:
        s = (text or "").strip()
        return bool(s and BookkeepingBot._OPERATOR_ONLY_COMMAND_RE.fullmatch(s))

    @staticmethod
    def _is_forbidden_operator_only_record_prefix(prefix: str) -> bool:
        return is_forbidden_operator_only_record_prefix(prefix)

    _MAX_MULTILINE_COMMANDS = 30

    def _try_plus_one_repeat_command(self, lines: List[str]) -> Optional[str]:
        """引用一条记账指令后另起一行发「+1」时，从引用区解析出要再执行一次的指令正文。

        UIA 常把引用预览与回复拼成多行：自下而上取第一条可解析为记账指令的行。
        """
        if len(lines) < 2:
            return None
        last = self._normalize_command_text(lines[-1])
        if not last or not re.fullmatch(r"[+＋]\s*1", last):
            return None
        for line in reversed(lines[:-1]):
            env = self._extract_command(line, fallback_sender=None)
            if env:
                return env.command_text
        return None

    def _try_plus_one_inline_quoted_command(self, raw: str) -> Optional[str]:
        """微信引用后回复 +1 时，整行可能合成「+1#<被引用的完整记账指令>」。

        此时应执行内层指令（再记一笔），而不是把「+1#+算式#…」当成一条怪指令。
        """
        s = (raw or "").replace("\u2005", " ").replace("\xa0", " ").strip()
        if not s:
            return None
        s = self._merge_wechat_inline_quote(s)
        s = self._normalize_command_text(s)
        if not s.startswith("+1"):
            return None
        # 避免把 +12、+1.5 等误判为「+1 + 后缀」
        if len(s) > 2 and s[2] not in " \t#＃$＄":
            return None
        rest = s[2:].lstrip()
        if not rest:
            return None
        prefs = self._sorted_record_prefixes()
        if not prefs:
            return None
        inner: Optional[str] = None
        for p in prefs:
            if rest.startswith(p):
                inner = rest[len(p) :].strip()
                break
        if inner is None:
            return None
        if not inner:
            return None
        env = self._extract_command(inner, fallback_sender=None)
        if not env:
            return None
        return env.command_text

    def handle(self, event: MessageEvent) -> str:
        text = (event.content or "").strip()
        logger.info(
            "收到消息: group=%s event_sender=%s content=%s",
            event.group,
            event.sender or "<none>",
            text,
        )
        if not text:
            return ""

        lines = [ln.strip() for ln in re.split(r"\r\n|\n|\r", text) if ln.strip()]
        # 单行：+1#<被引用完整指令>（微信引用合成一行）
        inline_repeat = None
        for probe_inline in lines[:5]:
            inline_repeat = self._try_plus_one_inline_quoted_command(probe_inline)
            if inline_repeat:
                break
        if inline_repeat:
            logger.info(
                "引用 +1 复用指令(单行前缀): group=%s command=%s",
                event.group,
                inline_repeat,
            )
            sub = MessageEvent(
                group=event.group,
                content=inline_repeat,
                timestamp=event.timestamp,
                sender=event.sender,
                group_nickname=event.group_nickname,
                is_at_me=event.is_at_me,
                raw=event.raw,
            )
            return self._handle_one_message(sub)

        repeat_cmd = self._try_plus_one_repeat_command(lines)
        if repeat_cmd:
            logger.info(
                "引用 +1 复用指令: group=%s command=%s",
                event.group,
                repeat_cmd,
            )
            sub = MessageEvent(
                group=event.group,
                content=repeat_cmd,
                timestamp=event.timestamp,
                sender=event.sender,
                group_nickname=event.group_nickname,
                is_at_me=event.is_at_me,
                raw=event.raw,
            )
            return self._handle_one_message(sub)

        # 同一条消息内多行连发时，每行按一条独立指令处理，合并为一次回复
        if len(lines) >= 2:
            parts: List[str] = []
            for line in lines[: self._MAX_MULTILINE_COMMANDS]:
                sub = MessageEvent(
                    group=event.group,
                    content=line,
                    timestamp=event.timestamp,
                    sender=event.sender,
                    group_nickname=event.group_nickname,
                    is_at_me=event.is_at_me,
                    raw=event.raw,
                )
                one = self._handle_one_message(sub)
                if one:
                    parts.append(one)
            return "\n\n".join(parts) if parts else ""

        return self._handle_one_message(event)

    def _handle_one_message(self, event: MessageEvent) -> str:
        text = (event.content or "").strip()
        if not text:
            return ""

        envelope = self._extract_command(text, fallback_sender=event.sender)
        if not envelope:
            return ""

        logger.info(
            "解析指令: group=%s event_sender=%s sender=%s command=%s",
            event.group,
            event.sender or "<none>",
            envelope.sender or "<none>",
            envelope.command_text,
        )

        if envelope.command_text in set(self._command_aliases.get("help", [])):
            return self._help_text()
        if self._is_rollback_command(envelope.command_text):
            return self._rollback(event.group)
        if self._is_query_command(envelope.command_text):
            return self._query_all_operations(event.group)
        if self._is_clear_command(envelope.command_text):
            return self._clear_group(event.group)

        period = self.extract_bill_period(envelope.command_text)
        if period:
            return self._summary(event.group, period)

        delimited = self._parse_delimited_math_record(envelope.command_text)
        if delimited is not None:
            expr, suffix, delim, is_echo_line = delimited
            if is_echo_line:
                return (
                    "这是记账结果的展示格式（含「=金额」），不会重复入账。\n"
                    f"请发原始指令：算式{delim}后缀，例如 +1200*5.8{delim}备注"
                    "（勿复制机器人回复里带「=金额」的那一行）。"
                )
            calc = self._calculate_if_expression(expr)
            if calc is None:
                return ""
            if calc == "错误:除数为0":
                return "计算失败: 除数不能为 0"
            delta = float(calc)
            balance = self._apply_group_delta(event.group, delta)
            self._append_group_operation(
                event.group,
                {
                    "kind": "math_hash",
                    "timestamp": float(event.timestamp or time.time()),
                    "command": envelope.command_text,
                    "expression": expr,
                    "suffix": suffix,
                    "delimiter": delim,
                    "delta": delta,
                    "balance_after": balance,
                },
            )
            detail = f"{expr}={calc}{delim}{suffix}" if suffix else f"{expr}={calc}{delim}"
            total_txt = self._format_number(balance)
            return (
                f"当前账单金额: {calc}\n"
                f"当前总金额: {total_txt}\n"
                f"{detail}"
            )

        math_expr = self._extract_record_math_expression(envelope.command_text)
        if math_expr is not None:
            calc = self._calculate_if_expression(math_expr)
            if calc is None:
                return ""
            if calc == "错误:除数为0":
                return "计算失败: 除数不能为 0"
            delta = float(calc)
            previous_balance = self._read_group_balance(event.group)
            balance = self._apply_group_delta(event.group, delta)
            self._append_group_operation(
                event.group,
                {
                    "kind": "math",
                    "timestamp": float(event.timestamp or time.time()),
                    "command": envelope.command_text,
                    "expression": math_expr,
                    "delta": delta,
                    "balance_after": balance,
                },
            )
            return (
                "记账成功\n"
                f"当前余额：{self._format_number(balance)}\n"
                f"原值：{self._format_number(previous_balance)}\n"
                f"算式：{self._format_number(previous_balance)} + "
                f"({math_expr} = {self._format_number(delta)}) = {self._format_number(balance)}\n"
                f"变化：{self._format_signed_number(delta)}"
            )

        parsed = self._parse_add(envelope.command_text, event.timestamp)
        if not parsed:
            return ""

        delta = parsed.amount if parsed.direction == "收入" else -parsed.amount
        previous_balance = self._read_group_balance(event.group)
        balance = self._apply_group_delta(event.group, delta)
        self._store.add(event.group, parsed)
        self._append_group_operation(
            event.group,
            {
                "kind": "ledger",
                "timestamp": float(event.timestamp or time.time()),
                "command": envelope.command_text,
                "delta": float(delta),
                "balance_after": balance,
                "direction": parsed.direction,
                "amount": parsed.amount,
                "category": parsed.category,
                "note": parsed.note,
            },
        )
        expression_text = (
            f"{parsed.direction} {parsed.amount:.2f} {parsed.category}"
            f"{(' ' + parsed.note) if parsed.note else ''}"
        )
        return (
            "记账成功\n"
            f"当前余额：{self._format_number(balance)}\n"
            f"原值：{self._format_number(previous_balance)}\n"
            f"算式：{self._format_number(previous_balance)} + "
            f"({expression_text} = {self._format_signed_number(delta)}) = {self._format_number(balance)}\n"
            f"变化：{self._format_signed_number(delta)}"
        )

    def _help_text(self) -> str:
        prefixes = self._command_aliases.get("record_prefix", ["$"])
        prefix = prefixes[0] if prefixes else "$"
        delim_hint = "、".join(self._sorted_record_prefixes()) or prefix
        return (
            "群聊记账指令说明:\n"
            f"1) 算式{{分隔符}}后缀 — 「分隔符」只能是配置里的记账前缀之一（当前：{delim_hint}）；"
            "机器人回复「当前账单金额」「当前总金额（记后余额）」及拼写结果行\n"
            f"2) 算式可写在 {prefix} 前或后（如 {prefix}100+20-5 或 100+20-5{prefix}）\n"
            f"3) {prefix} 收入/支出 金额 分类 [备注]（如 {prefix}支出 18 餐饮 午饭；也可 支出 18 餐饮{prefix}）\n"
            "4) \\撤回 — 撤回最近一次记账并恢复余额\n"
            "5) \\查账 — 列出本群全部记账操作记录\n"
            "6) \\清账 — 清空本群操作记录并将余额归零\n"
            "7) 引用一条可执行记账指令后回复「+1」：多行时末行 +1；单行时常为「+1#」+ 被引指令全文，会再记一笔\n"
            "8) 微信 inline 引用：形如「算式引用 昵称 的消息 : #后缀」会合并为「算式#后缀」再解析"
        )

    def _merge_wechat_inline_quote(self, raw: str) -> str:
        """微信 PC 常把引用合成一行：算式 + 「引用 xxx 的消息 : 」+ 被引片段（多为 #备注）。

        若不合并，首个 # 会落在被引内容里，左侧会带上中文，导致算式无法通过 MATH_RE。
        """
        s = (raw or "").strip()
        if "引用" not in s or "消息" not in s:
            return s
        m = re.match(
            r"^(.+?)\s*引用\s*.+?\s*的消息\s*[:：︰]\s*(.*)$",
            s,
            re.DOTALL,
        )
        if not m:
            return s
        head, tail = m.group(1).strip(), m.group(2).strip()
        if not head:
            return s
        if not tail:
            return head
        prefs = self._sorted_record_prefixes()
        if not prefs:
            return s
        for p in prefs:
            if tail.startswith(p):
                return f"{head}{tail}"
        return f"{head}{prefs[0]}{tail}"

    def _extract_command(self, text: str, fallback_sender: Optional[str] = None) -> Optional[CommandEnvelope]:
        raw = (text or "").replace("\u2005", " ").replace("\xa0", " ").strip()
        raw = self._merge_wechat_inline_quote(raw)
        sender: Optional[str] = (fallback_sender or "").strip() or None
        command_text = raw

        match = SENDER_PREFIX_RE.match(raw)
        if match:
            sender = match.group(1).strip()
            command_text = match.group(2).strip()
        else:
            for sep in (":", "："):
                if sep not in raw:
                    continue
                left, right = raw.split(sep, 1)
                left = left.strip()
                right = right.strip()
                if left and right and len(left) <= 64:
                    sender = left
                    command_text = right
                    break

        command_text = self._normalize_command_text(command_text)
        command_text = self._normalize_record_delimiter_confusables(command_text)
        if not command_text:
            return None
        if not self._looks_like_command(command_text):
            return None
        return CommandEnvelope(sender=sender, command_text=command_text, raw_text=text)

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        s = (text or "").replace("\u2005", " ").replace("\xa0", " ").strip()
        if not s:
            return ""
        s = re.sub(r"^@\S+\s+", "", s)
        trans = str.maketrans({
            "：": ":",
            "＋": "+",
            "－": "-",
            "×": "*",
            "✖": "*",
            "÷": "/",
            "（": "(",
            "）": ")",
            "，": " ",
            "；": " ",
            "。": " ",
        })
        s = s.translate(trans)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _normalize_record_delimiter_confusables(self, text: str) -> str:
        """按已配置的记账前缀，把常见全角/变体符号还原成 ASCII，便于 split 命中。"""
        s = text or ""
        prefs = set(self._sorted_record_prefixes())
        if not prefs:
            return s
        pairs: List[Tuple[str, str]] = []
        if "$" in prefs:
            pairs.append(("＄", "$"))
            pairs.append(("\ufe69", "$"))
        if "#" in prefs:
            pairs.append(("＃", "#"))
        for old, new in pairs:
            if old in s:
                s = s.replace(old, new)
        return s

    def _looks_like_command(self, text: str) -> bool:
        if self._is_operator_only_command_text(text):
            return False
        if text in set(self._command_aliases.get("help", [])):
            return True
        if self._is_rollback_command(text):
            return True
        if self._is_query_command(text):
            return True
        if self._is_clear_command(text):
            return True
        if self._match_bill_command(text):
            return True
        if self._parse_delimited_math_record(text) is not None:
            return True
        if self._extract_record_math_expression(text) is not None:
            return True
        if self._match_prefixed_add(text):
            return True
        return False

    def _sorted_record_prefixes(self) -> List[str]:
        prefs: List[str] = []
        for raw in self._command_aliases.get("record_prefix", []):
            p = (raw or "").strip()
            if not p or self._is_forbidden_operator_only_record_prefix(p):
                continue
            prefs.append(p)
        prefs.sort(key=len, reverse=True)
        return prefs

    def _extract_prefixed_math_expression(self, text: str) -> Optional[str]:
        payload = (text or "").strip()
        if not payload:
            return None
        for prefix in self._sorted_record_prefixes():
            if not payload.startswith(prefix):
                continue
            expr = payload[len(prefix):].strip()
            if expr and self._is_math_expression(expr):
                return expr
        return None

    def _extract_suffixed_math_expression(self, text: str) -> Optional[str]:
        payload = (text or "").strip()
        if not payload:
            return None
        for prefix in self._sorted_record_prefixes():
            if not payload.endswith(prefix):
                continue
            expr = payload[: -len(prefix)].strip()
            if expr and self._is_math_expression(expr):
                return expr
        return None

    def _strip_echo_equals_amount(self, expr_side: str) -> Tuple[str, bool]:
        """若形如「纯算式=数字」且数字与算式结果一致，视为复制了机器人回复，返回左侧算式及 True。"""
        s = (expr_side or "").strip()
        m = BookkeepingBot._ECHO_EQUALS_SUFFIX_RE.match(s)
        if not m:
            return s, False
        left, right_txt = m.group(1).strip(), m.group(2)
        calc = self._calculate_if_expression(left)
        if calc is None or calc == "错误:除数为0":
            return s, False
        try:
            if abs(float(calc) - float(right_txt)) > 1e-6:
                return s, False
        except ValueError:
            return s, False
        return left, True

    def _parse_delimited_math_record(self, text: str) -> Optional[Tuple[str, str, str, bool]]:
        """`算式{记账前缀}后缀`：分隔符必须与配置中的 record_prefix 某项一致，算式结果计入本群余额。

        第四项为 True 表示文本里含「=金额」结果行（复制机器人回复），只提示、不入账。
        """
        s = (text or "").strip()

        def try_sep(sep: str) -> Optional[Tuple[str, str, str, bool]]:
            if sep not in s:
                return None
            expr, suffix = s.split(sep, 1)
            expr = expr.strip()
            suffix = suffix.strip()
            if not expr:
                return None
            expr, is_echo = self._strip_echo_equals_amount(expr)
            if not self.MATH_RE.match(expr):
                return None
            if not any(ch in expr for ch in "+-*/"):
                return None
            if self._calculate_if_expression(expr) is None:
                return None
            return (expr, suffix, sep, is_echo)

        for p in self._sorted_record_prefixes():
            if not p:
                continue
            hit = try_sep(p)
            if hit:
                return hit
        return None

    def _extract_record_math_expression(self, text: str) -> Optional[str]:
        expr = self._extract_prefixed_math_expression(text)
        if expr is not None:
            return expr
        return self._extract_suffixed_math_expression(text)

    def _is_math_expression(self, text: str) -> bool:
        if not text:
            return False
        if not self.MATH_RE.match(text):
            return False
        if not any(ch in text for ch in "+-*/"):
            return False
        return self._calculate_if_expression(text) is not None

    def _calculate_if_expression(self, text: str) -> Optional[str]:
        expr = (text or "").strip()
        if not expr:
            return None
        if not self.MATH_RE.match(expr):
            return None
        try:
            node = ast.parse(expr, mode="eval")
            value = self._eval_math_ast(node.body)
            if isinstance(value, float):
                if abs(value - round(value)) < 1e-12:
                    return str(int(round(value)))
                return f"{value:.10f}".rstrip("0").rstrip(".")
            return str(value)
        except ZeroDivisionError:
            return "错误:除数为0"
        except Exception:
            return None

    def _eval_math_ast(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._ALLOWED_UNARY_OPS:
            operand = self._eval_math_ast(node.operand)
            return self._ALLOWED_UNARY_OPS[type(node.op)](operand)
        if isinstance(node, ast.BinOp) and type(node.op) in self._ALLOWED_BIN_OPS:
            left = self._eval_math_ast(node.left)
            right = self._eval_math_ast(node.right)
            return self._ALLOWED_BIN_OPS[type(node.op)](left, right)
        raise ValueError("unsupported expression")

    def _match_bill_command(self, text: str) -> bool:
        bill_aliases = self._command_aliases.get("bill", [])
        for keyword in bill_aliases:
            if text == keyword:
                return True
            if text.startswith(f"{keyword} "):
                tail = text[len(keyword):].strip()
                if tail in {"今日", "本月", "全部"}:
                    return True
        return False

    def _match_prefixed_add(self, text: str) -> bool:
        if self.ADD_RE_ALT.match(text) or self.ADD_RE_ALT_RELAXED.match(text):
            return True
        for prefix in self._sorted_record_prefixes():
            if not text.startswith(prefix):
                continue
            body = text[len(prefix):].strip()
            if self.ADD_RE.match(body) or self.ADD_RE_RELAXED.match(body):
                return True
            if re.match(r"^([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$", body):
                return True
        for prefix in self._sorted_record_prefixes():
            if not text.endswith(prefix):
                continue
            body = text[: -len(prefix)].strip()
            if self.ADD_RE.match(body) or self.ADD_RE_RELAXED.match(body):
                return True
            if re.match(r"^([0-9]+(?:\.[0-9]{1,2})?)(?:\s+(\S+))?(?:\s+(.+))?$", body):
                return True
        return False

    def _parse_add(self, text: str, timestamp: float) -> Optional[LedgerEntry]:
        match = self.ADD_RE.match(text)
        if not match:
            match = self.ADD_RE_RELAXED.match(text)
        if match:
            direction = match.group(1)
            amount = float(match.group(2))
            category = (match.group(3) or "未分类").strip()
            note = (match.group(4) or "").strip()
            return LedgerEntry(timestamp, direction, amount, category, note)

        alt = self.ADD_RE_ALT.match(text)
        if not alt:
            alt = self.ADD_RE_ALT_RELAXED.match(text)
        if alt:
            amount = float(alt.group(1))
            category = (alt.group(2) or "未分类").strip()
            note = (alt.group(3) or "").strip()
            return LedgerEntry(timestamp, "支出", amount, category, note)

        for prefix in self._sorted_record_prefixes():
            if not text.startswith(prefix):
                continue
            body = text[len(prefix):].strip()
            if not body:
                continue
            prefixed = self._parse_add(body, timestamp)
            if prefixed:
                return prefixed
        for prefix in self._sorted_record_prefixes():
            if not text.endswith(prefix):
                continue
            body = text[: -len(prefix)].strip()
            if not body:
                continue
            suffixed = self._parse_add(body, timestamp)
            if suffixed:
                return suffixed
        return None

    def _rollback(self, group: str) -> str:
        op = self._pop_last_group_operation(group)
        if op is not None:
            previous_balance = self._read_group_balance(group)
            try:
                delta = float(op.get("delta", 0.0) or 0.0)
            except Exception:
                delta = 0.0
            change = -delta
            balance = self._apply_group_delta(group, change)
            is_ledger_op = op.get("kind") == "ledger" or (
                bool(op.get("direction")) and not op.get("expression")
            )
            rollback_expr = str(op.get("command", "") or op.get("expression", "") or "最近一次记账")
            if is_ledger_op:
                self._store.pop_last(group)
                return (
                    "撤回成功\n"
                    f"当前余额：{self._format_number(balance)}\n"
                    f"原值：{self._format_number(previous_balance)}\n"
                    f"算式：{self._format_number(previous_balance)} + "
                    f"({self._format_signed_number(change)}) = {self._format_number(balance)} "
                    f"（撤回 {rollback_expr}）\n"
                    f"变化：{self._format_signed_number(change)}"
                )
            return (
                "撤回成功\n"
                f"当前余额：{self._format_number(balance)}\n"
                f"原值：{self._format_number(previous_balance)}\n"
                f"算式：{self._format_number(previous_balance)} + "
                f"({self._format_signed_number(change)}) = {self._format_number(balance)} "
                f"（撤回 {rollback_expr}）\n"
                f"变化：{self._format_signed_number(change)}"
            )

        removed = self._store.pop_last(group)
        if not removed:
            return "暂无可撤回的记账记录"
        previous_balance = self._read_group_balance(group)
        if removed.direction == "收入":
            change = -removed.amount
        else:
            change = removed.amount
        balance = self._apply_group_delta(group, change)
        return (
            "撤回成功\n"
            f"当前余额：{self._format_number(balance)}\n"
            f"原值：{self._format_number(previous_balance)}\n"
            f"算式：{self._format_number(previous_balance)} + "
            f"({self._format_signed_number(change)}) = {self._format_number(balance)} "
            f"（撤回 {removed.direction} {removed.amount:.2f} {removed.category}"
            f"{(' ' + removed.note) if removed.note else ''}）\n"
            f"变化：{self._format_signed_number(change)}"
        )

    def _query_all_operations(self, group: str) -> str:
        operations = self._read_group_operations(group)
        balance = self._read_group_balance(group)
        if not operations:
            return (
                "查账成功\n"
                f"当前余额：{self._format_number(balance)}\n"
                f"原值：{self._format_number(balance)}\n"
                f"算式：{self._format_number(balance)} + (0.00) = {self._format_number(balance)}"
                "（\\查账汇总）\n"
                "变化：0\n"
                "本群暂无记账操作记录。"
            )

        total_change = 0.0
        lines: List[str] = []
        for idx, op in enumerate(operations, start=1):
            ts = float(op.get("timestamp", 0.0) or 0.0)
            when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts > 0 else "-"
            try:
                delta_value = float(op.get("delta", 0.0) or 0.0)
            except Exception:
                delta_value = 0.0
            total_change += delta_value
            delta = self._format_signed_number(delta_value)
            try:
                bal = self._format_number(float(op.get("balance_after", 0.0) or 0.0))
            except Exception:
                bal = "0"
            kind = op.get("kind")
            is_ledger = kind == "ledger" or (
                bool(op.get("direction")) and not op.get("expression")
            )
            if is_ledger:
                direction = str(op.get("direction", "") or "")
                try:
                    amt = self._format_number(float(op.get("amount", 0.0) or 0.0))
                except Exception:
                    amt = "0"
                category = str(op.get("category", "") or "未分类")
                note = str(op.get("note", "") or "")
                cmd = str(op.get("command", "") or "")
                line = (
                    f"{idx}. {when} {cmd} → {direction} {amt} 元 | 分类: {category}"
                    + (f" | 备注: {note}" if note else "")
                    + f" | 变动: {delta} | 记后余额: {bal}"
                )
            else:
                if kind == "math_hash":
                    expr_h = str(op.get("expression", "") or "")
                    suf_h = str(op.get("suffix", "") or "")
                    dlm = str(op.get("delimiter", "") or "#")
                    shown = f"{expr_h}{dlm}{suf_h}" if suf_h else f"{expr_h}{dlm}"
                    line = f"{idx}. {when} {shown} = {delta} | 记后余额: {bal}"
                else:
                    expr = str(op.get("expression", "") or op.get("command", ""))
                    line = f"{idx}. {when} {expr} = {delta} | 记后余额: {bal}"
            lines.append(line)
        original_balance = balance - total_change
        return (
            "查账成功\n"
            f"当前余额：{self._format_number(balance)}\n"
            f"原值：{self._format_number(original_balance)}\n"
            f"算式：{self._format_number(original_balance)} + "
            f"({self._format_signed_number(total_change)}) = {self._format_number(balance)}"
            "（\\查账汇总）\n"
            f"变化：{self._format_signed_number(total_change)}\n"
            f"本群记账记录（共 {len(operations)} 条）:\n"
            + "\n".join(lines)
        )

    def _clear_group(self, group: str) -> str:
        logger.warning("执行本群清账: group=%s", group)
        self._clear_group_operations(group)
        self._set_group_balance(group, 0.0)
        self._store.clear(group)
        return "已清账：本群操作记录与明细账本已清空，余额已归零。"

    def _summary(self, group: str, period: str) -> str:
        entries = self._store.list_by_period(group, period)
        balance = self._read_group_balance(group)
        if not entries:
            return f"{period}暂无账本明细。当前余额: {self._format_number(balance)}"

        total_in = sum(entry.amount for entry in entries if entry.direction == "收入")
        total_out = sum(entry.amount for entry in entries if entry.direction == "支出")
        ledger_balance = total_in - total_out

        category_cost = {}
        for entry in entries:
            if entry.direction != "支出":
                continue
            category_cost[entry.category] = category_cost.get(entry.category, 0.0) + entry.amount

        top_text = "-"
        if category_cost:
            top = sorted(category_cost.items(), key=lambda x: x[1], reverse=True)[:3]
            top_text = "，".join([f"{name}:{value:.2f} 元" for name, value in top])

        return (
            f"{period}账单汇总\n"
            f"笔数: {len(entries)}\n"
            f"收入: {total_in:.2f} 元\n"
            f"支出: {total_out:.2f} 元\n"
            f"收支差: {ledger_balance:.2f} 元\n"
            f"支出分类 Top: {top_text}\n"
            f"当前余额: {self._format_number(balance)}"
        )

    def extract_bill_period(self, text: str) -> Optional[str]:
        if self.BILL_RE.match(text):
            match = self.BILL_RE.match(text)
            if match:
                return match.group(1) or "今日"
        for keyword in self._command_aliases.get("bill", []):
            if text == keyword:
                return "今日"
            if text.startswith(f"{keyword} "):
                tail = text[len(keyword):].strip()
                if tail in {"今日", "本月", "全部"}:
                    return tail
        return None

    def _read_group_balance(self, group: str) -> float:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        balances = dict(runtime.get("group_balances") or {})
        try:
            return float(balances.get(group, 0.0) or 0.0)
        except Exception:
            return 0.0

    def _set_group_balance(self, group: str, value: float) -> None:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        balances = dict(runtime.get("group_balances") or {})
        balances[group] = round(float(value), 10)
        runtime["group_balances"] = balances
        config["runtime"] = runtime
        save_config(config)

    def _apply_group_delta(self, group: str, delta: float) -> float:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        balances = dict(runtime.get("group_balances") or {})
        try:
            current = float(balances.get(group, 0.0) or 0.0)
        except Exception:
            current = 0.0
        updated = current + float(delta)
        balances[group] = round(updated, 10)
        runtime["group_balances"] = balances
        config["runtime"] = runtime
        save_config(config)
        return updated

    def _read_group_operations(self, group: str) -> List[dict]:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        operations = dict(runtime.get("group_operations") or {})
        group_ops = operations.get(group) or []
        if not isinstance(group_ops, list):
            return []
        return [item for item in group_ops if isinstance(item, dict)]

    def _append_group_operation(self, group: str, op: dict) -> None:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        operations = dict(runtime.get("group_operations") or {})
        group_ops = operations.get(group) or []
        if not isinstance(group_ops, list):
            group_ops = []
        group_ops.append(dict(op))
        operations[group] = group_ops
        runtime["group_operations"] = operations
        config["runtime"] = runtime
        save_config(config)

    def _pop_last_group_operation(self, group: str) -> Optional[dict]:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        operations = dict(runtime.get("group_operations") or {})
        group_ops = operations.get(group) or []
        if not isinstance(group_ops, list) or not group_ops:
            return None
        op = group_ops.pop()
        operations[group] = group_ops
        runtime["group_operations"] = operations
        config["runtime"] = runtime
        save_config(config)
        return op if isinstance(op, dict) else None

    def _clear_group_operations(self, group: str) -> None:
        config = load_config()
        runtime = dict(config.get("runtime") or {})
        operations = dict(runtime.get("group_operations") or {})
        operations[group] = []
        runtime["group_operations"] = operations
        config["runtime"] = runtime
        save_config(config)

    def _is_rollback_command(self, text: str) -> bool:
        return text in set(self._command_aliases.get("rollback", [])) or text in self._ROLLBACK_COMMANDS

    def _is_query_command(self, text: str) -> bool:
        return text in self._QUERY_COMMANDS or text in set(
            self._command_aliases.get("query", [])
        )

    def _is_clear_command(self, text: str) -> bool:
        return text in self._CLEAR_COMMANDS or text in set(
            self._command_aliases.get("clear", [])
        )

    @staticmethod
    def _format_number(value: float) -> str:
        return f"{float(value):.2f}"

    @staticmethod
    def _format_signed_number(value: float) -> str:
        text = BookkeepingBot._format_number(value)
        if value > 0:
            return f"+{text}"
        if value < 0:
            return text
        return "0"

def _find_session_list(root):
    try:
        session_list = root.ListControl(AutomationId="session_list")
        if session_list.Exists(maxSearchSeconds=1):
            return session_list
    except Exception:
        pass

    try:
        for control, _depth in uia.WalkControl(root, includeTop=True, maxDepth=6):
            if str(getattr(control, "ControlTypeName", "")) != "ListControl":
                continue
            auto_id = str(getattr(control, "AutomationId", "") or "")
            name = str(getattr(control, "Name", "") or "")
            if auto_id == "session_list" or name == "会话":
                return control
    except Exception:
        return None
    return None


def _collect_session_names(root) -> List[str]:
    session_list = _find_session_list(root)
    if not session_list:
        return []

    names: List[str] = []
    seen: Set[str] = set()
    try:
        for control, _depth in uia.WalkControl(session_list, includeTop=False, maxDepth=3):
            if str(getattr(control, "ControlTypeName", "")) != "ListItemControl":
                continue
            raw_name = str(getattr(control, "Name", "") or "").strip()
            if not raw_name:
                continue
            name = re.sub(r"\s*\(\d+\)\s*$", "", raw_name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
    except Exception:
        return names
    return names


def _is_group_candidate(wx: WeChatClient, name: str) -> bool:
    try:
        results = wx.chat_window.search(name)
    except Exception:
        return False

    for bucket, items in (results or {}).items():
        if "群" not in str(bucket):
            continue
        for item in items:
            item_name = str(getattr(item, "name", "") or "")
            if name == item_name or name in item_name or item_name in name:
                return True
    return False


def discover_groups(wx: WeChatClient) -> List[str]:
    config = load_config()
    exclude_groups = set(config.get("exclude_groups") or [])
    root = wx.window.uia.root
    candidates = _collect_session_names(root)
    groups: List[str] = []
    for name in candidates:
        if name in exclude_groups:
            continue
        if _is_group_candidate(wx, name):
            groups.append(name)
    return list(dict.fromkeys(groups))


def resolve_listen_groups(wx: WeChatClient, discovered_groups: Optional[List[str]] = None) -> List[str]:
    config = load_config()
    auto_discover_groups = bool(config.get("auto_discover_groups", True))
    manual_groups = list(config.get("manual_groups") or [])
    exclude_groups = set(config.get("exclude_groups") or [])

    groups: List[str] = []
    if auto_discover_groups:
        groups.extend(discovered_groups if discovered_groups is not None else discover_groups(wx))
    groups.extend(manual_groups)
    groups = [group for group in dict.fromkeys(groups) if group and group not in exclude_groups]
    return groups


def main() -> None:
    store = GroupLedgerStore(DATA_DIR)
    initial_config = load_config()
    command_aliases = dict(initial_config.get("command_aliases") or {})
    bot = BookkeepingBot(store, command_aliases)

    with WeChatClient(auto_connect=True) as wx:
        poll_interval_seconds = 8
        notified_waiting = False
        active_processor = None
        active_groups: List[str] = []
        last_discovered_groups: List[str] = []
        last_discover_at = 0.0
        temporary_unavailable_groups: Dict[str, float] = {}
        unavailable_cooldown_seconds = 60
        last_acl_signature = json.dumps(
            {
                "command_aliases": initial_config.get("command_aliases") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        print("记账机器人已启动，将持续监听配置变化。")
        print(f"账本目录: {DATA_DIR}")
        print("权限模式: 无白名单，符合指令格式即可触发。")
        print("提示：可在控制面板填写 manual_groups，机器人会自动重载监听。")
        print(f"自动发现群聊: {'开启' if bool(load_config().get('auto_discover_groups', False)) else '关闭'}")

        try:
            while True:
                runtime_config = load_config()
                reload_handler_required = False
                current_acl_signature = json.dumps(
                    {
                        "command_aliases": runtime_config.get("command_aliases") or {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if current_acl_signature != last_acl_signature:
                    command_aliases = dict(runtime_config.get("command_aliases") or {})
                    bot = BookkeepingBot(store, command_aliases)
                    last_acl_signature = current_acl_signature
                    print("检测到指令配置变更，已热更新。")
                    reload_handler_required = True

                auto_discover = bool(runtime_config.get("auto_discover_groups", True))
                discover_interval_seconds = int(runtime_config.get("discover_interval_seconds", 60) or 60)
                discover_interval_seconds = max(10, discover_interval_seconds)

                now = time.time()
                need_discover = (
                    auto_discover
                    and (
                        not last_discovered_groups
                        or not active_groups
                        or (now - last_discover_at) >= discover_interval_seconds
                    )
                )

                if need_discover:
                    last_discovered_groups = discover_groups(wx)
                    last_discover_at = now

                discovered_groups = list(last_discovered_groups) if auto_discover else []
                target_groups = resolve_listen_groups(wx, discovered_groups=discovered_groups)

                # 清理已过冷却时间的临时不可用群
                for group, retry_after in list(temporary_unavailable_groups.items()):
                    if now >= retry_after:
                        temporary_unavailable_groups.pop(group, None)

                target_groups = [
                    group for group in target_groups
                    if group not in temporary_unavailable_groups
                ]

                update_runtime(
                    current_listening_groups=active_groups,
                    desired_listening_groups=target_groups,
                    last_discovered_groups=discovered_groups,
                )

                if not target_groups:
                    if active_processor:
                        active_processor.stop()
                        try:
                            wx._services.remove(active_processor)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        active_processor = None
                        active_groups = []
                        update_runtime(current_listening_groups=[], desired_listening_groups=target_groups)

                    if not notified_waiting:
                        print("暂未发现可监听群聊，机器人将持续重试。")
                        print("你可以在面板填写群聊名称到 manual_groups，或先把群聊显示在微信左侧会话。")
                        notified_waiting = True
                    time.sleep(poll_interval_seconds)
                    continue

                if target_groups != active_groups or (reload_handler_required and active_processor is not None):
                    if active_processor:
                        if target_groups != active_groups:
                            print("检测到监听群配置变化，正在重载监听...")
                        else:
                            print("检测到指令配置变化，正在重载监听回调...")
                        active_processor.stop()
                        try:
                            wx._services.remove(active_processor)  # type: ignore[attr-defined]
                        except Exception:
                            pass

                    try:
                        active_processor = wx.process_groups(
                            target_groups,
                            [
                                CallbackHandler(
                                    bot.handle,
                                    auto_reply=True,
                                )
                            ],
                            block=False,
                            tick=0.03,
                            batch_size=24,
                            # 含时间/系统/机器人回复时项数会膨胀；须足够大，避免 tail 截断漏掉新气泡
                            tail_size=120,
                            # 必须为 1：并发 handle 会并发读写 runtime 与余额，导致原值/余额重复
                            processing_workers=1,
                            incoming_queue_maxsize=0,
                            # 勿启 bring_subwindow_to_front：会反复抢前台，机器几乎无法操作；多群用 listener 的短轮询
                            bring_subwindow_to_front=False,
                        )
                        active_groups = list(target_groups)
                        notified_waiting = False
                        update_runtime(current_listening_groups=active_groups)
                        print(f"当前监听群({len(active_groups)}): {active_groups}")
                    except Exception as exc:
                        # 兜底：单个群不可用时不要让整个机器人退出
                        msg = str(exc)
                        failed_group = None
                        marker = "打开群聊失败:"
                        if marker in msg:
                            failed_group = msg.split(marker, 1)[1].strip()
                        if failed_group:
                            temporary_unavailable_groups[failed_group] = (
                                time.time() + unavailable_cooldown_seconds
                            )
                            print(
                                f"群聊暂不可用，已临时跳过 {failed_group!r} "
                                f"({unavailable_cooldown_seconds}s 后重试)"
                            )
                        else:
                            print(f"启动监听失败，将稍后重试: {exc}")
                        active_processor = None
                        active_groups = []
                        update_runtime(current_listening_groups=[])

                time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            if active_processor:
                active_processor.stop()
                try:
                    wx._services.remove(active_processor)  # type: ignore[attr-defined]
                except Exception:
                    pass
            update_runtime(current_listening_groups=[], desired_listening_groups=[])


if __name__ == "__main__":
    main()
