# -*- coding: utf-8 -*-
"""微信群聊监听与自动回复。

该模块实现的是已在调试验证过的方案：
1. 每个群聊打开一个独立聊天窗口。
2. 每个窗口固定缓存 ``chat_message_list``。
3. 使用单调度器按时间分片轮询多个窗口。
4. 自动回复时记录本库发送的消息，监听回流时只忽略一次。

注意：
    微信 4.x 的 Qt UIA 对消息方向/发送者暴露不足，无法稳定识别用户手动
    发送的“自己消息”。因此这里默认只忽略“本库发送并记录过”的消息。
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

import win32api
import win32con
import win32gui
import win32process

from ...core import uiautomation as uia
from ..chat import ChatWindow
from ...utils.logger import get_logger

logger = get_logger(__name__)

# 列表虚拟化时，同一条气泡可能被 UIA 短时间反复上报。识别规则（会话内）：
# 1) 启动采样：滚底后把当前 UIA 可见列表里的消息气泡 ui_key 记入「历史」；
#    仅当 ui_key 与启动快照一致时才视为历史（不按正文拦截，避免新发的同文案消息被误挡）。
# 2) ui_key = (RuntimeId, ClassName, Name)，只增不减，避免同一控件反复入队；
# 3) 仅处理「从当前快照列表底部向上、连续且尚未见过 ui_key」的消息气泡（视为当前最新一段）；
# 4) 须 ScrollPattern 判定在列表底部，且当前可见尾部与 bottom_anchor_sigs 有交集（补滚动条不准）；
#    用户上翻时只补 seen、不入队；未在底部时不强行滚底，避免误判为底部。
# 5) 用户曾进入历史视图后，回到底部的首帧只做 resync（整屏记入 seen/historic + 更新锚点），不入队，
#    避免虚拟化后旧气泡新 RuntimeId 被当成新消息；可能漏掉浏览历史期间群内新来的消息。
# 6) 成功入队分配 dispatch_event_id = time.time_ns()（须 >= 会话 boot_monotonic_ns），记入 dispatch_event_ids。
# 代价：ScrollPattern 恒失败时会退化为「假定在底部」；锚点未建立前不判 history_view；虚拟化边界仍有漏网风险。
# 在 DUPLICATE_CONTENT_SUPPRESS_SECONDS 窗口内，同一归一化正文最多放行
# MAX_IDENTICAL_CONTENT_EVENTS_PER_WINDOW 条（含），超过则丢弃——这样同一秒内两人发相同文案仍会各处理一次，
# 而 UIA 对同一条的多次重复上报多数会在第 3 次起被挡住。
DUPLICATE_CONTENT_SUPPRESS_SECONDS = 10.0
CONTENT_RECENT_TTL_SECONDS = 120.0
MAX_IDENTICAL_CONTENT_EVENTS_PER_WINDOW = 5

WECHAT_EXE_NAMES = {"wechat.exe", "weixin.exe"}
MESSAGE_CLASSES = {
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleItemView",
}
TIME_CLASS = "mmui::ChatItemView"


@dataclass(frozen=True)
class MessageEvent:
    """监听到的新消息。"""

    group: str
    content: str
    timestamp: float
    sender: Optional[str] = None
    group_nickname: Optional[str] = None
    is_at_me: bool = False
    raw: object = None


@dataclass(frozen=True)
class _VisibleItem:
    kind: str
    name: str
    class_name: str
    runtime_id: Tuple[int, ...]
    control: object = None

    @property
    def key(self) -> Tuple[Tuple[int, ...], str, str]:
        return self.runtime_id, self.class_name, self.name


@dataclass
class _ListenSession:
    group: str
    hwnd: int
    root: object
    msg_list: object
    seen: Set[Tuple[Tuple[int, ...], str, str]]
    """已处理过的 UIA 气泡 key (RuntimeId, ClassName, Name)，会话内只增不减。"""
    boot_wall_time: float
    """打开本群监听窗口、开始 bootstrap 采样时的 ``time.time()``。"""
    boot_monotonic_ns: int
    """与 boot 同一时刻的 ``time.time_ns()``，用于与 dispatch_event_id 对比。"""
    bootstrap_completed_wall_time: float
    """bootstrap 两轮采样结束时的 ``time.time()``；仅在此之后才按「非历史」逻辑入队。"""
    historic_ui_keys: Set[Tuple[Tuple[int, ...], str, str]]
    """启动可见快照中的消息气泡 ui_key，视为历史；仅 key 相同才拦截，不按正文去重。"""
    bottom_anchor_sigs: Deque[Tuple[str, str]]
    """最近确认在列表底部时见到的消息稳定签名 (class_name, 归一化正文)，用于识别上翻历史视图。"""
    needs_resync_after_history: bool = field(default=False)
    """用户曾处于历史视图，回到底部后需先跑一帧「仅同步基线、不入队」。"""
    history_view_since: float = field(default=0.0)
    """最近一次进入历史视图的时间；0 表示未在「待重同步」链中。"""
    last_resync_at: float = field(default=0.0)
    """最近一次 return-to-bottom resync 的 ``time.time()``。"""
    dispatch_event_ids: Set[int] = field(default_factory=set)
    """已成功入队的系统时间型事件 id（time.time_ns()），用于排查与扩展。"""
    content_recent: Deque[Tuple[float, str]] = field(default_factory=lambda: deque(maxlen=500))
    new_count: int = 0
    scan_count: int = 0
    fail_count: int = 0
    last_message_at: float = field(default_factory=time.time)
    next_scan_at: float = field(default_factory=time.time)
    interval: float = 0.3


@dataclass
class _OutgoingRecord:
    group: str
    content: str
    expires_at: float
    remaining_hits: int


@dataclass(frozen=True)
class _ReplyTask:
    group: str
    content: str


@dataclass(frozen=True)
class _IncomingTask:
    group: str
    content: str
    timestamp: float
    group_nickname: Optional[str]
    is_at_me: bool


class OutgoingMessageRegistry:
    """记录本库发送的消息，用于监听回流时忽略一次。"""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = ttl_seconds
        self._records: Deque[_OutgoingRecord] = deque()

    def record(self, group: str, content: str, max_hits: int = 8) -> None:
        """记录整段正文及各行：微信 UIA 常把长回复拆成多条气泡，仅记整段会匹配不上回流。"""
        raw = str(content or "")
        chunks: List[str] = []
        full = _normalize_message_text(raw)
        if full:
            chunks.append(full)
        for line in raw.splitlines():
            ln = _normalize_message_text(line)
            if len(ln) >= 6:
                chunks.append(ln)
        seen_body: Set[str] = set()
        now = time.time()
        exp = now + self.ttl_seconds
        for body in chunks:
            if not body or body in seen_body:
                continue
            seen_body.add(body)
            self._records.append(
                _OutgoingRecord(
                    group=group,
                    content=body,
                    expires_at=exp,
                    remaining_hits=max_hits,
                )
            )

    def should_ignore(self, group: str, content: str) -> bool:
        now = time.time()
        content = _normalize_message_text(content)
        while self._records and self._records[0].expires_at < now:
            self._records.popleft()

        for index, record in enumerate(self._records):
            if record.group != group:
                continue
            if _is_same_outgoing_message(record.content, content):
                record.remaining_hits -= 1
                if record.remaining_hits <= 0:
                    del self._records[index]
                return True
        return False


def _normalize_message_text(content: str) -> str:
    """归一化消息文本，提升本库发送回流的识别稳定性。"""
    text = str(content or "")
    text = text.replace("\u2005", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_same_outgoing_message(expected: str, actual: str) -> bool:
    """判断回流消息是否可视为本库刚发送的同一则消息。"""
    if not expected or not actual:
        return False
    if expected == actual:
        return True

    # 微信 UIA 在部分版本上会对长文本、多行文本做轻微归一化或截断。
    # 这里允许“包含关系”命中，避免机器人自己的回复再次触发监听链路。
    shorter, longer = sorted((expected, actual), key=len)
    if len(shorter) < 8:
        return False
    return shorter in longer


def _safe_text(control, attr: str) -> str:
    try:
        return str(getattr(control, attr, "") or "")
    except Exception:
        return ""


def _safe_children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _safe_runtime_id(control) -> Tuple[int, ...]:
    try:
        return tuple(control.GetRuntimeId() or ())
    except Exception:
        return ()


def _looks_like_time_or_system(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    if re.match(r"^\d{1,2}:\d{2}$", value):
        return True
    if value.startswith(("昨天", "今天", "星期")):
        return True
    if value.startswith("@"):
        return True
    return False


def _normalize_sender(sender: str) -> str:
    value = str(sender or "").strip()
    value = value.replace("\u2005", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value)


def _extract_sender_from_text(raw_text: str, message_text: str) -> Optional[str]:
    text = (raw_text or "").strip()
    msg = (message_text or "").strip()
    if not text or not msg:
        return None

    # 典型格式：昵称: 内容（含全角冒号等）
    match = re.match(r"^([^:\n:：\uFF1A﹕]{1,64})[:：\uFF1A﹕]\s*(.+)$", text)
    if match:
        sender = _normalize_sender(match.group(1))
        content = match.group(2).strip()
        if sender and (content == msg or msg.endswith(content) or content.endswith(msg)):
            return sender

    # 兜底：同一段文本中包含消息内容，前半部分可能是昵称
    if msg in text and text != msg:
        prefix = text.split(msg, 1)[0].strip(" \t\r\n:：\uFF1A﹕-[]()")
        prefix = _normalize_sender(prefix)
        if prefix and not _looks_like_time_or_system(prefix) and len(prefix) <= 64:
            return prefix

    return None


def _collect_control_texts(control, max_depth: int = 3, limit: int = 80) -> List[str]:
    texts: List[str] = []
    stack = [(control, 0)]
    while stack and len(texts) < limit:
        node, depth = stack.pop()
        name = _safe_text(node, "Name").strip()
        if name:
            texts.append(name)
        if depth >= max_depth:
            continue
        children = _safe_children(node)
        for child in reversed(children):
            stack.append((child, depth + 1))
    return texts


def _safe_parent(control):
    try:
        return control.GetParentControl()
    except Exception:
        return None


def _safe_rect(control):
    try:
        rect = control.BoundingRectangle
        if not rect:
            return None
        return (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )
    except Exception:
        return None


def _find_message_row_container(control, max_up: int = 6):
    """向上查找当前消息所在的「行容器」，避免跨消息串行。"""
    current = control
    depth = 0
    while current and depth <= max_up:
        ctrl_type = _safe_text(current, "ControlTypeName")
        cls = _safe_text(current, "ClassName")
        auto_id = _safe_text(current, "AutomationId")
        if (
            ctrl_type in {"ListItemControl", "PaneControl"}
            and ("Chat" in cls or "Item" in cls or "msg" in auto_id.lower())
        ):
            return current
        current = _safe_parent(current)
        depth += 1
    return _safe_parent(control) or control


def _is_likely_sender_name(value: str, message_text: str) -> bool:
    text = _normalize_sender(value)
    msg = _normalize_sender(message_text)
    if not text:
        return False
    if text == msg:
        return False
    if _looks_like_time_or_system(text):
        return False
    if text.startswith("计算结果"):
        return False
    if text in {"消息", "系统消息", "聊天记录", "微信", "通知"}:
        return False
    if len(text) > 32:
        return False
    if any(ch in text for ch in "+-*/=<>$"):
        return False
    if any(ch.isdigit() for ch in text):
        return False
    return True


def _is_invalid_sender_result(value: Optional[str]) -> bool:
    text = _normalize_sender(value or "")
    if not text:
        return True
    if text in {"消息", "系统消息", "聊天记录", "微信", "通知"}:
        return True
    if text.startswith("计算结果"):
        return True
    if len(text) <= 1:
        return True
    return False


def _ocr_read_text_candidates(image) -> List[str]:
    if not pytesseract:
        return []

    variants = []
    try:
        gray = ImageOps.grayscale(image)
        variants.append(gray)
        variants.append(ImageEnhance.Contrast(gray).enhance(2.0))
        variants.append(ImageOps.autocontrast(gray))
        bw = gray.point(lambda p: 255 if p > 170 else 0)
        variants.append(bw)
        # 对昵称小字做 2x 放大，提高 OCR 识别率
        w, h = gray.size
        if w > 0 and h > 0:
            enlarged = gray.resize((w * 2, h * 2))
            variants.append(enlarged)
            variants.append(ImageEnhance.Contrast(enlarged).enhance(2.2))
            variants.append(ImageOps.autocontrast(enlarged))
            variants.append(ImageOps.invert(enlarged))
    except Exception:
        variants = [image]

    results: List[str] = []
    languages = ("eng", "chi_sim+eng", "chi_sim")
    for img in variants:
        for lang in languages:
            for psm in ("7", "6", "11"):
                try:
                    text = pytesseract.image_to_string(
                        img,
                        lang=lang,
                        config=f"--oem 3 --psm {psm}",
                    )
                except Exception as exc:
                    logger.warning("OCR read failed (lang=%s psm=%s): %s", lang, psm, exc)
                    continue
                if text and text.strip():
                    results.extend([line.strip() for line in text.splitlines() if line.strip()])
    return results


def _extract_sender_via_ocr(control, message_text: str) -> Optional[str]:
    global _OCR_IMPORT_ERROR_LOGGED

    logger.info("OCR fallback: start for message=%r", (message_text or "").strip())

    if not control:
        logger.info("OCR fallback: skip, control is None")
        return None
    if not pytesseract:
        if not _OCR_IMPORT_ERROR_LOGGED:
            logger.warning("OCR sender fallback disabled: pytesseract is not installed.")
            _OCR_IMPORT_ERROR_LOGGED = True
        logger.info("OCR fallback: unavailable, pytesseract import failed")
        return None

    rect = _safe_rect(control)
    if not rect:
        logger.info("OCR fallback: skip, message rect is None")
        return None
    left, top, right, bottom = rect
    width = max(1, right - left)

    # 截图聚焦“气泡上方昵称区域”，避免把正文一并截进去。
    # 1) 左侧昵称带：适合对方消息（头像在左）
    # 2) 右侧昵称带：适合自己消息或特殊布局
    # 3) 居中宽带：兜底
    candidates = [
        (
            max(0, left - 42),
            max(0, top - 130),
            max(1, left + min(340, width + 220)),
            max(1, top - 24),
        ),
        (
            max(0, right - min(340, width + 220)),
            max(0, top - 130),
            max(1, right + 42),
            max(1, top - 24),
        ),
        (
            max(0, left - 120),
            max(0, top - 150),
            max(1, right + 120),
            max(1, top - 20),
        ),
        # 兜底：覆盖消息行上方更高区域（少量包含正文）
        (
            max(0, left - 220),
            max(0, top - 180),
            max(1, right + 220),
            max(1, min(bottom, top + 18)),
        ),
    ]

    best = None
    for idx, bbox in enumerate(candidates):
        l, t, r, b = bbox
        if r <= l or b <= t:
            logger.info("OCR fallback: skip invalid bbox[%d]=%s", idx, bbox)
            continue
        try:
            shot = ImageGrab.grab(bbox=(l, t, r, b))
        except Exception as exc:
            logger.warning("OCR fallback: screenshot failed bbox[%d]=%s err=%s", idx, bbox, exc)
            continue

        logger.info("OCR fallback: screenshot bbox[%d]=%s", idx, bbox)
        if _OCR_DUMP_ENABLED:
            try:
                os.makedirs(_OCR_DUMP_DIR, exist_ok=True)
                dump_path = os.path.join(
                    _OCR_DUMP_DIR,
                    f"sender_ocr_{int(time.time() * 1000)}_{idx}.png",
                )
                shot.save(dump_path)
                logger.info("OCR fallback: dump bbox[%d] -> %s", idx, dump_path)
            except Exception as exc:
                logger.warning("OCR fallback: dump failed bbox[%d] err=%s", idx, exc)
        texts = _ocr_read_text_candidates(shot)
        logger.info("OCR fallback: raw_texts[%d]=%s", idx, texts[:12])
        for raw in texts:
            sender = _normalize_sender(raw.strip("[](){}<>|"))
            if _is_likely_sender_name(sender, message_text):
                if not best or len(sender) < len(best):
                    best = sender
    if best:
        logger.info("OCR sender detected: %s", best)
    else:
        logger.info("OCR fallback: no valid sender detected")
    return best


def _collect_context_controls(control, max_up: int = 3) -> List[object]:
    """收集消息控件的上下文中控件：自己、父链、父链的兄弟等。"""
    if not control:
        return []

    results: List[object] = []
    seen: Set[Tuple[int, ...]] = set()

    def add(ctrl) -> None:
        if not ctrl:
            return
        rid = _safe_runtime_id(ctrl)
        if rid and rid in seen:
            return
        if rid:
            seen.add(rid)
        results.append(ctrl)

    current = control
    depth = 0
    while current and depth <= max_up:
        add(current)
        parent = _safe_parent(current)
        if parent:
            add(parent)
            for sibling in _safe_children(parent):
                add(sibling)
        current = parent
        depth += 1
    return results


def _collect_text_candidates_from_controls(
    controls: List[object],
    max_depth: int = 3,
    per_control_limit: int = 80,
) -> List[Tuple[str, Optional[Tuple[int, int, int, int]]]]:
    candidates: List[Tuple[str, Optional[Tuple[int, int, int, int]]]] = []
    seen: Set[str] = set()
    for ctrl in controls:
        rect = _safe_rect(ctrl)
        for text in _collect_control_texts(ctrl, max_depth=max_depth, limit=per_control_limit):
            key = f"{text}#{rect}"
            if key in seen:
                continue
            seen.add(key)
            candidates.append((text, rect))
    return candidates


def _describe_control_for_debug(control) -> str:
    if not control:
        return "<none>"
    ctrl_type = _safe_text(control, "ControlTypeName")
    name = _safe_text(control, "Name")
    auto_id = _safe_text(control, "AutomationId")
    class_name = _safe_text(control, "ClassName")
    texts = _collect_control_texts(control, max_depth=1, limit=12)
    texts_str = " | ".join([t.strip() for t in texts if t and t.strip()][:8])
    return (
        f"ControlType={ctrl_type!r}, Name={name!r}, AutomationId={auto_id!r}, "
        f"ClassName={class_name!r}, ChildTexts={texts_str!r}"
    )


def _dump_sender_debug_context(control, message_text: str) -> None:
    """打印发送者识别失败时的控件树上下文，便于定位昵称与挂载位置。"""
    try:
        parent = _safe_parent(control) if control else None
        grandparent = _safe_parent(parent) if parent else None

        logger.info("==== Sender Debug Begin ====")
        logger.info("MessageText=%r", (message_text or "").strip())
        logger.info("Current: %s", _describe_control_for_debug(control))
        logger.info("Parent : %s", _describe_control_for_debug(parent))

        if parent:
            siblings = _safe_children(parent)
            logger.info("ParentChildren count=%d", len(siblings))
            for index, child in enumerate(siblings[:20]):
                logger.info(
                    "ParentChild[%d]: %s",
                    index,
                    _describe_control_for_debug(child),
                )

        logger.info("Grandparent: %s", _describe_control_for_debug(grandparent))
        logger.info("==== Sender Debug End ====")
    except Exception as exc:
        logger.debug("Sender debug dump failed: %s", exc)


def _extract_sender_from_message_control(control, message_text: str) -> Optional[str]:
    # 已按需求禁用了发送者识别（不再使用 UIA/OCR 推断 sender）。
    _ = control, message_text
    return None


def _get_process_image_name(pid: int) -> str:
    """通过 pid 获取进程路径。"""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint32(1024)
            buf = ctypes.create_unicode_buffer(1024)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _find_wechat_windows() -> List[Tuple[int, str, str]]:
    windows: List[Tuple[int, str, str]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe_name = os.path.basename(_get_process_image_name(pid)).lower()
            title = win32gui.GetWindowText(hwnd) or ""
            class_name = win32gui.GetClassName(hwnd) or ""
        except Exception:
            return True

        if exe_name in WECHAT_EXE_NAMES and win32gui.IsWindowVisible(hwnd):
            windows.append((hwnd, title, class_name))
        return True

    win32gui.EnumWindows(callback, 0)
    return windows


def _find_window_by_title(title_keyword: str, exclude_hwnd: Optional[int] = None) -> Optional[int]:
    for hwnd, title, _class_name in _find_wechat_windows():
        if hwnd == exclude_hwnd:
            continue
        if title_keyword in title:
            return hwnd
    return None


def _find_message_list(root):
    """查找聊天消息列表。"""
    try:
        msg_list = root.ListControl(AutomationId="chat_message_list")
        if msg_list.Exists(maxSearchSeconds=1):
            return msg_list
    except Exception:
        pass

    candidates = []
    try:
        for control, depth in uia.WalkControl(root, includeTop=True, maxDepth=8):
            if _safe_text(control, "ControlTypeName") != "ListControl":
                continue
            score = 0
            for child in _safe_children(control)[-12:]:
                cls = _safe_text(child, "ClassName")
                if cls in MESSAGE_CLASSES:
                    score += 10
                elif cls == TIME_CLASS:
                    score += 2
            if score:
                candidates.append((score, depth, control))
    except Exception:
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def _try_scroll_message_list_to_end(msg_list) -> None:
    """将消息列表尽量滚到末尾，使虚拟化列表实例化出最新气泡的 UIA 子节点。

    连发时若视口未在底部，GetChildren 可能只包含部分子项，导致漏单。
    """
    try:
        sc = msg_list.GetScrollPattern()
    except Exception:
        sc = None
    if sc is not None:
        try:
            if sc.VerticallyScrollable:
                sc.SetScrollPercent(
                    uia.ScrollPattern.NoScrollValue,
                    100.0,
                    waitTime=0.01,
                )
        except Exception:
            pass
    # 对末尾若干子项 ScrollIntoView，补懒加载（部分端仅有 ScrollItemPattern）
    try:
        children = _safe_children(msg_list)
    except Exception:
        return
    for child in reversed(children[-10:]):
        try:
            sip = child.GetScrollItemPattern()
        except Exception:
            continue
        if sip:
            try:
                sip.ScrollIntoView(waitTime=0.01)
            except Exception:
                pass
            break
    time.sleep(0.01)


def _read_visible_items(msg_list) -> List[_VisibleItem]:
    items: List[_VisibleItem] = []
    for child in _safe_children(msg_list):
        cls = _safe_text(child, "ClassName")
        name = _safe_text(child, "Name").strip()
        if not name:
            continue
        if cls == TIME_CLASS:
            kind = "time/system"
        elif cls in MESSAGE_CLASSES:
            kind = "message"
        else:
            continue
        items.append(
            _VisibleItem(
                kind=kind,
                name=name,
                class_name=cls,
                runtime_id=_safe_runtime_id(child),
                control=child,
            )
        )
    return items


def _stable_message_signature(item: _VisibleItem) -> Tuple[str, str]:
    """不依赖 RuntimeId 的气泡签名，用于底部锚点与历史视图推断。"""
    return item.class_name, _normalize_message_text(item.name)


def _is_message_list_at_bottom(msg_list, tolerance: float = 1.0) -> bool:
    """根据 ScrollPattern 判断是否已滚到列表底部（垂直滚动条接近 100%）。"""
    try:
        sc = msg_list.GetScrollPattern()
    except Exception as exc:
        logger.debug("GetScrollPattern failed, assume at bottom: %s", exc)
        return True
    if not sc:
        logger.debug("no ScrollPattern on message list, assume at bottom")
        return True
    try:
        if not sc.VerticallyScrollable:
            return True
        percent = float(sc.VerticalScrollPercent)
        return percent >= 100.0 - tolerance
    except Exception as exc:
        logger.debug("VerticalScrollPercent read failed, assume at bottom: %s", exc)
        return True


def _update_bottom_anchors(session: _ListenSession, items_full: List[_VisibleItem]) -> None:
    """在确认处于底部视图时，把当前快照最后几条消息签名记入锚点队列（去重追加）。"""
    msgs = [it for it in items_full if it.kind == "message"]
    for it in msgs[-5:]:
        sig = _stable_message_signature(it)
        if sig not in session.bottom_anchor_sigs:
            session.bottom_anchor_sigs.append(sig)


def _looks_like_history_view(session: _ListenSession, items_full: List[_VisibleItem]) -> bool:
    """当前可见尾部与已知底部锚点完全无交集时，推断用户正在看历史区域（补 ScrollPercent 不可靠）。"""
    msgs = [it for it in items_full if it.kind == "message"]
    if not msgs:
        return False
    if not session.bottom_anchor_sigs:
        return False
    current_tail = [_stable_message_signature(it) for it in msgs[-5:]]
    return not any(sig in session.bottom_anchor_sigs for sig in current_tail)


def _mark_visible_items_seen(
    session: _ListenSession,
    items_full: List[_VisibleItem],
) -> int:
    """将 ``items_full`` 中当前可见项记入 ``seen``；消息气泡同时记入 ``historic_ui_keys``。不入队。

    返回本次新加入 ``seen`` 的 message 气泡 key 数量。
    """
    new_message_keys = 0
    for it in items_full:
        if it.kind == "message":
            if it.key not in session.seen:
                new_message_keys += 1
            session.seen.add(it.key)
            session.historic_ui_keys.add(it.key)
        else:
            session.seen.add(it.key)
    return new_message_keys


def _contiguous_new_message_suffix_from_bottom(
    items_full: List[_VisibleItem],
    seen_ui: Set[Tuple[Tuple[int, ...], str, str]],
) -> List[_VisibleItem]:
    """从当前快照中「最后一条消息」往上数，连续未在 seen_ui 中出现过的消息（时间顺序：旧→新）。"""
    msgs = [it for it in items_full if it.kind == "message"]
    suffix: List[_VisibleItem] = []
    for it in reversed(msgs):
        if it.key in seen_ui:
            break
        suffix.append(it)
    suffix.reverse()
    return suffix


def _find_session_list(root):
    """查找微信左侧会话列表。"""
    try:
        session_list = root.ListControl(AutomationId="session_list")
        if session_list.Exists(maxSearchSeconds=1):
            return session_list
    except Exception:
        pass

    try:
        for control, _depth in uia.WalkControl(root, includeTop=True, maxDepth=6):
            if _safe_text(control, "ControlTypeName") != "ListControl":
                continue
            if _safe_text(control, "AutomationId") == "session_list" or _safe_text(control, "Name") == "会话":
                return control
    except Exception:
        return None
    return None


def _find_session_item(root, group_name: str):
    session_list = _find_session_list(root)
    if not session_list:
        return None

    candidates = []
    try:
        for control, depth in uia.WalkControl(session_list, includeTop=False, maxDepth=3):
            if _safe_text(control, "ControlTypeName") != "ListItemControl":
                continue
            name = _safe_text(control, "Name")
            cls = _safe_text(control, "ClassName")
            score = 0
            if group_name in name:
                score += 100
            if "Session" in cls or "Conversation" in cls or "Cell" in cls:
                score += 30
            try:
                if control.IsSelected:
                    score += 80
            except Exception:
                pass
            if score:
                candidates.append((score, depth, control))
    except Exception:
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def _double_click_control(control) -> bool:
    try:
        control.DoubleClick(simulateMove=False)
        return True
    except Exception:
        pass

    try:
        rect = control.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        win32api.SetCursorPos((x, y))
        for _ in range(2):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.08)
        return True
    except Exception:
        return False
class WeChatGroupListener:
    """微信群聊监听器。"""

    def __init__(
        self,
        client,
        groups: Iterable[str],
        on_message: Callable[[MessageEvent], Optional[str]],
        *,
        auto_reply: bool = True,
        ignore_client_sent: bool = True,
        reply_on_at: bool = False,
        group_nicknames: Optional[Dict[str, str]] = None,
        outgoing_ttl: float = 60.0,
        tick: float = 0.1,
        batch_size: int = 8,
        tail_size: int = 8,
        processing_workers: int = 2,
        incoming_queue_maxsize: int = 0,
        reply_send_interval: float = 0.0,
        bring_subwindow_to_front: bool = False,
    ):
        self.client = client
        self.groups = list(dict.fromkeys(groups))
        self.on_message = on_message
        self.auto_reply = auto_reply
        self.ignore_client_sent = ignore_client_sent
        self.reply_on_at = reply_on_at
        self.group_nicknames = dict(group_nicknames or {})
        self.tick = tick
        self.batch_size = batch_size
        self.tail_size = tail_size
        self.processing_workers = max(1, int(processing_workers))
        self.incoming_queue_maxsize = max(0, int(incoming_queue_maxsize))
        self.reply_send_interval = max(0.0, float(reply_send_interval))
        shared_registry = getattr(self.client, "outgoing_registry", None)
        self.outgoing_registry = shared_registry or OutgoingMessageRegistry(outgoing_ttl)
        self.sessions: Dict[str, _ListenSession] = {}
        if self.incoming_queue_maxsize > 0:
            self._incoming_queue: "queue.Queue[_IncomingTask]" = queue.Queue(maxsize=self.incoming_queue_maxsize)
        else:
            self._incoming_queue = queue.Queue()
        self._reply_queue: "queue.Queue[_ReplyTask]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._processor_threads: List[threading.Thread] = []
        # 为 True 时每次拉取前抢前台，可能利于非激活子窗口的 UIA；会严重干扰本机操作，仅建议调试/无人值守
        self.bring_subwindow_to_front = bool(bring_subwindow_to_front)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, block: bool = False) -> "WeChatGroupListener":
        """启动监听。"""
        self._open_sessions()
        self._stop_event.clear()
        self._start_processors()
        self._start_sender()
        if block:
            try:
                self._run_loop()
            finally:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """停止监听。"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=5)
        for thread in self._processor_threads:
            if thread.is_alive():
                thread.join(timeout=5)
        self._processor_threads.clear()

    def run_forever(self) -> None:
        """阻塞当前线程持续监听，直到 Ctrl+C。"""
        try:
            if not self.is_running:
                self.start(block=True)
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def _open_sessions(self) -> None:
        for group in self.groups:
            if group in self.sessions:
                continue

            chat_already_open = False
            if self.reply_on_at and not self.group_nicknames.get(group):
                chat_already_open = self._read_group_nickname(group)

            hwnd = self._ensure_subwindow(group, chat_already_open=chat_already_open)
            root = uia.ControlFromHandle(hwnd)
            msg_list = _find_message_list(root)
            if not msg_list:
                raise RuntimeError(f"未找到群聊消息列表: {group}")
            boot_wall = time.time()
            boot_ns = time.time_ns()
            initial_seen, historic_ui, anchor_seed = self._bootstrap_initial_seen(
                msg_list
            )
            bootstrap_done = time.time()
            bottom_anchors: Deque[Tuple[str, str]] = deque(maxlen=30)
            for sig in anchor_seed:
                if sig not in bottom_anchors:
                    bottom_anchors.append(sig)
            self.sessions[group] = _ListenSession(
                group=group,
                hwnd=hwnd,
                root=root,
                msg_list=msg_list,
                seen=initial_seen,
                boot_wall_time=boot_wall,
                boot_monotonic_ns=boot_ns,
                bootstrap_completed_wall_time=bootstrap_done,
                historic_ui_keys=historic_ui,
                bottom_anchor_sigs=bottom_anchors,
            )
            logger.info(
                "会话启动采样: tail 子项=%s，历史消息气泡 ui_key=%s，底部锚点=%s（group=%s）",
                len(initial_seen),
                len(historic_ui),
                len(bottom_anchors),
                group,
            )

    def _read_group_nickname(self, group: str) -> bool:
        """读取群昵称。

        ``GroupManager.get_group_nickname`` 本身会打开目标群聊并进入详情侧栏。
        返回 True 表示当前主窗口大概率已停在该群聊，可直接双击左侧会话项
        打开独立窗口，避免再次搜索同一群。
        """
        try:
            nickname = self.client.group_manager.get_group_nickname(group)
        except Exception as exc:
            logger.warning(f"读取群昵称失败: {group}: {exc}")
            return False

        if nickname:
            self.group_nicknames[group] = nickname
        else:
            logger.warning(f"未读取到群昵称，无法精确判断是否 @ 我: {group}")
        return True

    def _ensure_subwindow(self, group: str, chat_already_open: bool = False) -> int:
        main_hwnd = self.client.window.hwnd
        hwnd = _find_window_by_title(group, exclude_hwnd=main_hwnd)
        if hwnd:
            return hwnd

        if not chat_already_open:
            if not self.client.chat_window.open_chat(group, target_type="group"):
                raise RuntimeError(f"打开群聊失败: {group}")
            time.sleep(0.8)

        item = _find_session_item(self.client.window.uia.root, group)
        if not item and chat_already_open:
            logger.debug(f"当前会话项未找到，重新搜索并打开群聊: {group}")
            if not self.client.chat_window.open_chat(group, target_type="group"):
                raise RuntimeError(f"打开群聊失败: {group}")
            time.sleep(0.8)
            item = _find_session_item(self.client.window.uia.root, group)

        if not item or not _double_click_control(item):
            raise RuntimeError(f"打开独立聊天窗口失败: {group}")

        deadline = time.time() + 5
        while time.time() < deadline:
            hwnd = _find_window_by_title(group, exclude_hwnd=main_hwnd)
            if hwnd:
                return hwnd
            time.sleep(0.2)
        raise RuntimeError(f"等待独立聊天窗口超时: {group}")

    def _bootstrap_initial_seen(
        self, msg_list
    ) -> Tuple[
        Set[Tuple[Tuple[int, ...], str, str]],
        Set[Tuple[Tuple[int, ...], str, str]],
        List[Tuple[str, str]],
    ]:
        """打开独立窗口后先滚到底再采样与 ``_poll_session`` 一致的 tail。

        若不做滚底，首帧 ``seen`` 往往只是列表上半截；下一轮 poll 滚底后尾部历史全会变成「新 key」，
        导致重启后把旧聊天记录（含机器人自己的长回复）整条当新消息再跑一遍回调。

        同时对当前 UIA **可见**范围内的消息气泡打上历史标记（非完整聊天记录，虚拟列表仅实例化一截）。
        """
        seen_ui: Set[Tuple[Tuple[int, ...], str, str]] = set()
        historic_ui: Set[Tuple[Tuple[int, ...], str, str]] = set()
        anchor_seed: List[Tuple[str, str]] = []
        for round_idx in (0, 1):
            if round_idx == 1:
                time.sleep(0.04)
            _try_scroll_message_list_to_end(msg_list)
            time.sleep(0.06)
            items_full = _read_visible_items(msg_list)
            window = (
                items_full[-self.tail_size :]
                if self.tail_size > 0
                else list(items_full)
            )
            seen_ui |= {it.key for it in window}
            for it in items_full:
                if it.kind != "message":
                    continue
                historic_ui.add(it.key)
            msgs = [it for it in items_full if it.kind == "message"]
            anchor_seed = [_stable_message_signature(it) for it in msgs[-5:]]
        return seen_ui, historic_ui, anchor_seed

    def _run_loop(self) -> None:
        logger.info(f"开始监听群聊: {', '.join(self.groups)}")
        while not self._stop_event.is_set():
            now = time.time()
            for session in self._due_sessions(now):
                self._poll_session(session)
            time.sleep(self.tick)
        logger.info("group listener stopped")

    def _due_sessions(self, now: float) -> List[_ListenSession]:
        sessions = [
            session for session in self.sessions.values()
            if session.next_scan_at <= now
        ]
        sessions.sort(key=lambda session: session.next_scan_at)
        return sessions[:self.batch_size]

    def _purge_stale_content_recent(self, session: _ListenSession, now: float) -> None:
        dq = session.content_recent
        while dq and dq[0][0] < now - CONTENT_RECENT_TTL_SECONDS:
            dq.popleft()

    def _is_recent_duplicate_incoming(self, session: _ListenSession, norm: str, now: float) -> bool:
        if not norm:
            return False
        self._purge_stale_content_recent(session, now)
        cutoff = now - DUPLICATE_CONTENT_SUPPRESS_SECONDS
        same_count = sum(
            1 for ts, prev in session.content_recent if prev == norm and ts >= cutoff
        )
        return same_count >= MAX_IDENTICAL_CONTENT_EVENTS_PER_WINDOW

    def _note_recent_incoming(self, session: _ListenSession, norm: str, now: float) -> None:
        if not norm:
            return
        self._purge_stale_content_recent(session, now)
        session.content_recent.append((now, norm))

    @staticmethod
    def _allocate_dispatch_event_id(session: _ListenSession) -> int:
        if len(session.dispatch_event_ids) > 100_000:
            session.dispatch_event_ids.clear()
        eid = max(time.time_ns(), session.boot_monotonic_ns)
        while eid in session.dispatch_event_ids:
            eid += 1
        session.dispatch_event_ids.add(eid)
        return eid

    @staticmethod
    def _is_bootstrap_historic(session: _ListenSession, item: _VisibleItem) -> bool:
        """气泡 ui_key 在启动快照或历史同步基线中则视为历史，不入队处理。"""
        return item.key in session.historic_ui_keys

    def _resync_after_history_view(
        self,
        session: _ListenSession,
        items_full: List[_VisibleItem],
        reason: str,
    ) -> None:
        """从历史上翻回底部后的首帧：只重建 seen/锚点，不触发 on_message。"""
        new_msg_keys = _mark_visible_items_seen(session, items_full)
        _update_bottom_anchors(session, items_full)
        session.needs_resync_after_history = False
        session.last_resync_at = time.time()
        msg_count = sum(1 for it in items_full if it.kind == "message")
        logger.info(
            "resync after history view, suppress first bottom frame: group=%s reason=%s "
            "visible_messages=%s new_message_keys_marked=%s seen=%s anchors=%s",
            session.group,
            reason,
            msg_count,
            new_msg_keys,
            len(session.seen),
            len(session.bottom_anchor_sigs),
        )

    def _ingest_visible_items(
        self,
        session: _ListenSession,
        items_full: List[_VisibleItem],
        window: List[_VisibleItem],
    ) -> int:
        """将本帧中「当前最新一段」新消息入队。

        自列表底部向上连续、且尚未在 ``seen`` 中的消息气泡视为最新一批，整段先记入 ``seen``，
        再逐条尝试入队；tail 窗口内、但不属于该段的新控件（多为上翻历史）只补 ``seen`` 不入队。
        命中启动快照「历史」标记（仅 ui_key 与启动时一致）的永不指令入队。
        每次成功入队分配唯一 ``dispatch_event_id``（``>= boot_monotonic_ns`` 的 ``time.time_ns()``）。
        """
        if session.needs_resync_after_history:
            logger.debug(
                "ingest suppressed: needs_resync_after_history group=%s",
                session.group,
            )
            return 0
        added = 0
        now = time.time()
        for it in window:
            if it.kind != "message":
                if it.key not in session.seen:
                    session.seen.add(it.key)
                continue

        new_suffix = _contiguous_new_message_suffix_from_bottom(items_full, session.seen)
        suffix_triples = {
            (it.runtime_id, it.class_name, it.name) for it in new_suffix
        }
        for it in new_suffix:
            session.seen.add(it.key)

        for item in new_suffix:
            norm = _normalize_message_text(item.name)
            if self._is_bootstrap_historic(session, item):
                logger.debug(
                    "skip historic-tagged message group=%s preview=%r",
                    session.group,
                    norm[:120],
                )
                continue
            if self.ignore_client_sent and self.outgoing_registry.should_ignore(
                session.group, item.name
            ):
                continue
            if norm and self._is_recent_duplicate_incoming(session, norm, now):
                logger.debug(
                    "skip duplicate incoming (stable content): group=%s preview=%r",
                    session.group,
                    norm[:120],
                )
                continue

            eid = self._allocate_dispatch_event_id(session)
            logger.debug(
                "enqueue dispatch_event_id=%s group=%s preview=%r",
                eid,
                session.group,
                norm[:120],
            )
            added += 1
            session.new_count += 1
            self._enqueue_incoming(session, item)
            self._note_recent_incoming(session, norm, now)

        for it in window:
            if it.kind != "message":
                continue
            triple = (it.runtime_id, it.class_name, it.name)
            if it.key in session.seen:
                continue
            if triple not in suffix_triples:
                session.seen.add(it.key)

        return added

    def _try_focus_subwindow_for_read(self, session: _ListenSession) -> None:
        if not self.bring_subwindow_to_front or not session.hwnd:
            return
        try:
            if win32gui.IsIconic(session.hwnd):
                win32gui.ShowWindow(session.hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(session.hwnd)
        except Exception:
            pass
        time.sleep(0.03)

    def _poll_session(self, session: _ListenSession) -> None:
        session.scan_count += 1
        self._try_focus_subwindow_for_read(session)
        added = 0
        try:
            skip_ingest_for_remaining_rounds = False
            # 两轮采样：首轮滚底+读；短休后无滚动再读，部分情况下 UIA 子树晚一帧才补全
            for round_idx in (0, 1):
                if skip_ingest_for_remaining_rounds:
                    continue
                if round_idx == 1:
                    time.sleep(0.04)
                if round_idx == 0:
                    # 用户已上翻时不要强行滚底，否则 ScrollPercent 恒为底部、误判为可入队
                    if _is_message_list_at_bottom(session.msg_list):
                        _try_scroll_message_list_to_end(session.msg_list)
                at_bottom = _is_message_list_at_bottom(session.msg_list)
                items_full = _read_visible_items(session.msg_list)
                window = (
                    items_full[-self.tail_size :]
                    if self.tail_size > 0
                    else list(items_full)
                )
                history_view = _looks_like_history_view(session, items_full)

                if not at_bottom or history_view:
                    session.needs_resync_after_history = True
                    if session.history_view_since <= 0:
                        session.history_view_since = time.time()
                    visible_msg = sum(1 for it in items_full if it.kind == "message")
                    _mark_visible_items_seen(session, items_full)
                    logger.info(
                        "history view detected, suppress ingest and mark visible seen: "
                        "group=%s at_bottom=%s history_view=%s visible_messages=%s",
                        session.group,
                        at_bottom,
                        history_view,
                        visible_msg,
                    )
                    continue

                if session.needs_resync_after_history:
                    self._resync_after_history_view(
                        session,
                        items_full,
                        "return_to_bottom_after_history",
                    )
                    session.history_view_since = 0.0
                    skip_ingest_for_remaining_rounds = True
                    continue

                logger.debug("normal bottom ingest: group=%s", session.group)
                added += self._ingest_visible_items(session, items_full, window)
                _update_bottom_anchors(session, items_full)
        except Exception as exc:
            session.fail_count += 1
            logger.debug(f"读取群聊消息失败: {session.group}: {exc}")
            return

        self._update_next_scan(session, added)

    def _enqueue_incoming(self, session: _ListenSession, item: _VisibleItem) -> None:
        task = _IncomingTask(
            group=session.group,
            content=item.name,
            timestamp=time.time(),
            group_nickname=self.group_nicknames.get(session.group),
            is_at_me=self._is_at_me(session.group, item.name),
        )
        try:
            self._incoming_queue.put_nowait(task)
        except queue.Full:
            logger.warning("incoming queue is full, drop message: group=%s content=%r", task.group, task.content)

    def _handle_message(self, task: _IncomingTask) -> None:
        event = MessageEvent(
            group=task.group,
            content=task.content,
            timestamp=task.timestamp,
            sender=None,
            group_nickname=task.group_nickname,
            is_at_me=task.is_at_me,
            raw=None,
        )
        try:
            reply = self.on_message(event)
        except Exception as exc:
            logger.exception("message callback failed: %s: %s", task.group, exc)
            return

        if self.auto_reply and reply and self._should_send_reply(event):
            self.enqueue_reply(task.group, str(reply))

    def _is_at_me(self, group: str, content: str) -> bool:
        nickname = self.group_nicknames.get(group)
        if not nickname:
            return False
        return f"@{nickname}" in content or f"@{nickname}\u2005" in content

    def _should_send_reply(self, event: MessageEvent) -> bool:
        if not self.reply_on_at:
            return True
        return event.is_at_me

    def _update_next_scan(self, session: _ListenSession, added: int) -> None:
        now = time.time()
        # 多群时若仍用「长期无新消息则拉长 interval」，某些群在 UIA 上迟迟不出现 added，
        # 会一直被当成空闲 → 0.5s/1.2s，形成「收不到消息 → 更难得扫到」的饥饿。多群统一短间隔单群可省电。
        multi_group = len(self.sessions) > 1
        if added:
            session.last_message_at = now
            session.interval = 0.04
        else:
            if multi_group:
                session.interval = 0.1
            else:
                idle_for = now - session.last_message_at
                if idle_for >= 120:
                    session.interval = 1.2
                elif idle_for >= 30:
                    session.interval = 0.5
                else:
                    session.interval = 0.04
        session.next_scan_at = now + session.interval

    def reply(self, group: str, content: str) -> bool:
        """立即使用对应独立窗口回复该群聊。

        注意：该方法会直接操作窗口、剪贴板与焦点。自动回复默认不直接调用它，
        而是进入发送队列，由单条 sender 线程串行发送，避免多个群同时回复时
        抢占窗口。
        """
        session = self.sessions.get(group)
        if not session:
            raise ValueError(f"未监听该群: {group}")

        if self.ignore_client_sent:
            # 先登记，再发送，避免微信回流速度大于登记速度导致漏判。
            self.outgoing_registry.record(group, content)

        sent = self._send_in_subwindow(session, content)
        return sent

    def enqueue_reply(self, group: str, content: str) -> None:
        """将回复加入串行发送队列。"""
        content = (content or "").strip()
        if not content:
            return
        self._reply_queue.put(_ReplyTask(group=group, content=content))

    def _start_processors(self) -> None:
        alive = [t for t in self._processor_threads if t.is_alive()]
        self._processor_threads = alive
        while len(self._processor_threads) < self.processing_workers:
            idx = len(self._processor_threads)
            thread = threading.Thread(
                target=self._process_loop,
                daemon=True,
                name=f"wx-listener-worker-{idx}",
            )
            thread.start()
            self._processor_threads.append(thread)

    def _process_loop(self) -> None:
        while not self._stop_event.is_set() or not self._incoming_queue.empty():
            try:
                task = self._incoming_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self._handle_message(task)
            except Exception as exc:
                logger.exception("process incoming task failed: %s", exc)
            finally:
                self._incoming_queue.task_done()

    def _start_sender(self) -> None:
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._sender_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._sender_thread.start()

    def _send_loop(self) -> None:
        """串行发送回复，避免多窗口同时抢夺焦点/剪贴板。"""
        while not self._stop_event.is_set() or not self._reply_queue.empty():
            try:
                task = self._reply_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self.reply(task.group, task.content)
                if self.reply_send_interval > 0:
                    time.sleep(self.reply_send_interval)
            except Exception as exc:
                logger.exception(f"发送队列回复失败: {task.group}: {exc}")
            finally:
                self._reply_queue.task_done()

    def _send_in_subwindow(self, session: _ListenSession, content: str) -> bool:
        root = session.root
        edit = self._find_chat_input(root)
        if not edit:
            logger.error(f"未找到聊天输入框: {session.group}")
            return False

        return ChatWindow.send_text_via_input(
            edit,
            content,
            clipboard_error="写入回复到剪贴板失败",
            send_error=f"发送群聊回复失败: {session.group}",
            logger_override=logger,
        )

    @staticmethod
    def _find_chat_input(root):
        possible_ids = ["chat_input_field", "input_field", "msg_input", "edit_input"]
        for auto_id in possible_ids:
            try:
                edit = root.EditControl(AutomationId=auto_id)
                if edit.Exists(maxSearchSeconds=0.3):
                    return edit
            except Exception:
                continue

        candidates = []
        try:
            root_rect = root.BoundingRectangle
            for control, _depth in uia.WalkControl(root, includeTop=True, maxDepth=8):
                if _safe_text(control, "ControlTypeName") != "EditControl":
                    continue
                rect = control.BoundingRectangle
                if rect.top < root_rect.top + root_rect.height() * 0.55:
                    continue
                width = rect.right - rect.left
                if width <= 100:
                    continue
                candidates.append((width, control))
        except Exception:
            return None

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]


