# -*- coding: utf-8 -*-
"""一键启动群聊记账机器人（面板 + 监听）。"""

from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PANEL_SCRIPT = ROOT / "group_bookkeeping_panel.py"
BOT_SCRIPT = ROOT / "group_bookkeeping_bot.py"
PANEL_URL = "http://127.0.0.1:8966"


def _spawn(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=str(ROOT.parent.parent))


def main() -> None:
    python = sys.executable

    print("启动控制面板...")
    panel_proc = _spawn([python, str(PANEL_SCRIPT)])
    time.sleep(1.2)

    try:
        webbrowser.open(PANEL_URL)
    except Exception:
        pass

    print(f"控制面板地址: {PANEL_URL}")
    print("启动群聊监听机器人...")
    bot_proc = _spawn([python, str(BOT_SCRIPT)])

    try:
        bot_code = bot_proc.wait()
        if bot_code != 0:
            print(f"机器人已退出，退出码: {bot_code}")
    except KeyboardInterrupt:
        print("收到停止信号，正在关闭...")
    finally:
        for proc in (bot_proc, panel_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (bot_proc, panel_proc):
            try:
                proc.wait(timeout=5)
            except Exception:
                if proc.poll() is None:
                    proc.kill()


if __name__ == "__main__":
    main()
