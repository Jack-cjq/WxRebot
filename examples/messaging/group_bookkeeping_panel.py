# -*- coding: utf-8 -*-
"""群聊记账机器人控制面板（本地 Web UI）。"""

from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from examples.messaging.bookkeeping_config import load_config, save_config, update_runtime


HOST = "127.0.0.1"
PORT = 8966


def _split_lines(text: str) -> List[str]:
    return [item.strip() for item in (text or "").splitlines() if item.strip()]


def _split_csv(text: str) -> List[str]:
    raw = str(text or "")
    parts = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    return [item.strip() for item in parts if item.strip()]


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = load_config()

    config["auto_discover_groups"] = bool(payload.get("auto_discover_groups", True))
    config["manual_groups"] = _split_lines(str(payload.get("manual_groups_text", "")))
    config["exclude_groups"] = _split_lines(str(payload.get("exclude_groups_text", "")))

    default_admins = _split_csv(str(payload.get("default_admins_csv", "")))
    per_group_raw = str(payload.get("per_group_admins_json", "")).strip()
    per_group = {}
    if per_group_raw:
        try:
            parsed = json.loads(per_group_raw)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            for group, users in parsed.items():
                if isinstance(users, list):
                    per_group[str(group)] = [str(u).strip() for u in users if str(u).strip()]

    merged_admins = {"*": default_admins}
    merged_admins.update(per_group)
    config["group_admins"] = merged_admins

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


def _discover_groups_now() -> List[str]:
    # 懒加载：避免面板启动阶段就强依赖微信自动化环境。
    from examples.messaging.group_bookkeeping_bot import discover_groups
    from src import WeChatClient

    with WeChatClient(auto_connect=True) as wx:
        return discover_groups(wx)


def _to_form_data(config: Dict[str, Any]) -> Dict[str, Any]:
    group_admins = dict(config.get("group_admins") or {})
    default_admins = group_admins.pop("*", [])
    command_aliases = dict(config.get("command_aliases") or {})
    runtime = dict(config.get("runtime") or {})
    return {
        "auto_discover_groups": bool(config.get("auto_discover_groups", True)),
        "manual_groups_text": "\n".join(config.get("manual_groups") or []),
        "exclude_groups_text": "\n".join(config.get("exclude_groups") or []),
        "default_admins_csv": ", ".join(default_admins),
        "per_group_admins_json": json.dumps(group_admins, ensure_ascii=False, indent=2),
        "help_aliases_csv": ", ".join(command_aliases.get("help") or []),
        "rollback_aliases_csv": ", ".join(command_aliases.get("rollback") or []),
        "bill_aliases_csv": ", ".join(command_aliases.get("bill") or []),
        "record_prefix_csv": ", ".join(command_aliases.get("record_prefix") or []),
        "runtime": runtime,
    }


class PanelHandler(BaseHTTPRequestHandler):
    def _json_response(self, data: Dict[str, Any], code: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/":
            page = _render_html()
            data = page.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if route == "/api/config":
            config = load_config()
            self._json_response({"ok": True, "data": _to_form_data(config)})
            return

        self._json_response({"ok": False, "error": "Not Found"}, code=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/config":
            try:
                payload = self._read_json()
                config = _normalize_payload(payload)
                save_config(config)
                self._json_response({"ok": True, "data": _to_form_data(config)})
            except Exception as exc:
                self._json_response({"ok": False, "error": str(exc)}, code=HTTPStatus.BAD_REQUEST)
            return

        if route == "/api/discover-groups":
            try:
                groups = _discover_groups_now()
                update_runtime(last_discovered_groups=groups)
                config = load_config()
                self._json_response({"ok": True, "groups": groups, "data": _to_form_data(config)})
            except Exception as exc:
                self._json_response({"ok": False, "error": str(exc)}, code=HTTPStatus.BAD_REQUEST)
            return

        if route == "/api/manual-groups/add":
            try:
                payload = self._read_json()
                config = _append_manual_group(str(payload.get("group_name", "")))
                self._json_response({"ok": True, "data": _to_form_data(config)})
            except Exception as exc:
                self._json_response({"ok": False, "error": str(exc)}, code=HTTPStatus.BAD_REQUEST)
            return

        self._json_response({"ok": False, "error": "Not Found"}, code=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _render_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>微信群记账机器人控制面板</title>
  <style>
    :root { --bg:#f4f6fb; --card:#ffffff; --text:#1f2937; --muted:#6b7280; --line:#dbe1ea; --accent:#0f766e; --btn:#0ea5a4; --btn2:#2563eb; }
    * { box-sizing:border-box; font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }
    body { margin:0; background:linear-gradient(120deg,#f7fafc,#edf2f7 60%,#e6fffa); color:var(--text); }
    .wrap { max-width:1100px; margin:24px auto; padding:0 16px; }
    .title { font-size:24px; font-weight:700; margin-bottom:12px; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; box-shadow:0 8px 24px rgba(15,23,42,.05); }
    .card h3 { margin:0 0 10px; font-size:16px; }
    .muted { color:var(--muted); font-size:13px; }
    .row { margin-bottom:10px; }
    label { display:block; font-size:13px; margin-bottom:6px; color:#334155; }
    textarea,input[type=text] { width:100%; border:1px solid #cbd5e1; border-radius:8px; padding:8px; font-size:13px; background:#fff; }
    textarea { min-height:90px; resize:vertical; }
    .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    button { border:none; border-radius:8px; padding:9px 14px; color:#fff; cursor:pointer; font-size:13px; }
    .primary { background:var(--btn); }
    .secondary { background:var(--btn2); }
    .status { margin-top:10px; font-size:13px; color:#0f5132; }
    ul { margin:8px 0 0 18px; padding:0; }
    @media (max-width: 860px) { .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="title">微信群记账机器人控制面板</div>
    <div class="muted">所有配置写入同一个 JSON 文件：<code>examples/messaging/group_bookkeeping_config.json</code></div>
    <div class="grid">
      <section class="card">
        <h3>监听状态</h3>
        <div class="muted">当前已监听群聊（由机器人进程写入）</div>
        <ul id="listeningGroups"></ul>
        <div class="muted" style="margin-top:8px;">计划监听群聊（含手动添加）</div>
        <ul id="desiredGroups"></ul>
        <div class="muted" style="margin-top:8px;">最近发现的群</div>
        <ul id="discoveredGroups"></ul>
        <div class="row" style="margin-top:10px;">
          <label><input id="autoDiscover" type="checkbox" /> 自动发现群聊</label>
        </div>
        <div class="row">
          <label>手动补充监听群（每行一个）</label>
          <textarea id="manualGroups"></textarea>
        </div>
        <div class="row">
          <label>快速添加单个群聊</label>
          <div class="btns">
            <input id="quickGroupName" type="text" placeholder="输入群聊名称，例如：项目群A" />
            <button class="secondary" onclick="addManualGroup()">加入手动监听</button>
          </div>
        </div>
        <div class="row">
          <label>排除群（每行一个）</label>
          <textarea id="excludeGroups"></textarea>
        </div>
      </section>

      <section class="card">
        <h3>白名单配置</h3>
        <div class="row">
          <label>全局白名单（逗号分隔）</label>
          <input id="defaultAdmins" type="text" />
        </div>
        <div class="row">
          <label>按群白名单（JSON，键=群名，值=用户名数组）</label>
          <textarea id="perGroupAdmins"></textarea>
        </div>
        <div class="muted">
          示例：
          <pre style="margin:6px 0 0; padding:8px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; overflow:auto;">{
  "项目群A": ["王五", "赵六"],
  "财务群": ["小李"]
}</pre>
        </div>
        <div class="muted">消息需满足“发送者: 指令”并命中白名单。</div>
      </section>

      <section class="card">
        <h3>指令触发词</h3>
        <div class="row">
          <label>帮助指令（逗号分隔）</label>
          <input id="helpAliases" type="text" />
        </div>
        <div class="row">
          <label>撤销指令（逗号分隔）</label>
          <input id="rollbackAliases" type="text" />
        </div>
        <div class="row">
          <label>账单指令前缀（逗号分隔）</label>
          <input id="billAliases" type="text" />
        </div>
        <div class="row">
          <label>记账前缀（逗号分隔）</label>
          <input id="recordPrefix" type="text" />
          <div class="muted" style="margin-top:6px;">不能纯为四则运算符 +、-、*、/；保存时若不符合将拒绝写入。</div>
        </div>
      </section>

      <section class="card">
        <h3>操作</h3>
        <div class="btns">
          <button class="primary" onclick="saveConfig()">保存配置</button>
          <button class="secondary" onclick="discoverGroups()">刷新发现群聊</button>
          <button class="secondary" onclick="loadConfig()">重新加载</button>
        </div>
        <div id="status" class="status"></div>
      </section>
    </div>
  </div>
  <script>
    const ids = {
      autoDiscover: document.getElementById('autoDiscover'),
      manualGroups: document.getElementById('manualGroups'),
      quickGroupName: document.getElementById('quickGroupName'),
      excludeGroups: document.getElementById('excludeGroups'),
      defaultAdmins: document.getElementById('defaultAdmins'),
      perGroupAdmins: document.getElementById('perGroupAdmins'),
      helpAliases: document.getElementById('helpAliases'),
      rollbackAliases: document.getElementById('rollbackAliases'),
      billAliases: document.getElementById('billAliases'),
      recordPrefix: document.getElementById('recordPrefix'),
      listeningGroups: document.getElementById('listeningGroups'),
      desiredGroups: document.getElementById('desiredGroups'),
      discoveredGroups: document.getElementById('discoveredGroups'),
      status: document.getElementById('status'),
    };

    function setStatus(text, isError=false) {
      ids.status.textContent = text;
      ids.status.style.color = isError ? '#b91c1c' : '#0f5132';
    }

    function renderList(ul, arr) {
      ul.innerHTML = '';
      (arr || []).forEach(item => {
        const li = document.createElement('li');
        li.textContent = item;
        ul.appendChild(li);
      });
      if (!arr || !arr.length) {
        const li = document.createElement('li');
        li.textContent = '暂无';
        ul.appendChild(li);
      }
    }

    async function loadConfig() {
      const res = await fetch('/api/config');
      const json = await res.json();
      if (!json.ok) {
        setStatus(json.error || '读取配置失败', true);
        return;
      }
      const d = json.data;
      ids.autoDiscover.checked = !!d.auto_discover_groups;
      ids.manualGroups.value = d.manual_groups_text || '';
      ids.excludeGroups.value = d.exclude_groups_text || '';
      ids.defaultAdmins.value = d.default_admins_csv || '';
      ids.perGroupAdmins.value = d.per_group_admins_json || '{}';
      ids.helpAliases.value = d.help_aliases_csv || '';
      ids.rollbackAliases.value = d.rollback_aliases_csv || '';
      ids.billAliases.value = d.bill_aliases_csv || '';
      ids.recordPrefix.value = d.record_prefix_csv || '';
      renderList(ids.listeningGroups, d.runtime?.current_listening_groups || []);
      renderList(ids.desiredGroups, d.runtime?.desired_listening_groups || []);
      renderList(ids.discoveredGroups, d.runtime?.last_discovered_groups || []);
      setStatus('配置已加载');
    }

    async function saveConfig() {
      const payload = {
        auto_discover_groups: ids.autoDiscover.checked,
        manual_groups_text: ids.manualGroups.value,
        exclude_groups_text: ids.excludeGroups.value,
        default_admins_csv: ids.defaultAdmins.value,
        per_group_admins_json: ids.perGroupAdmins.value,
        help_aliases_csv: ids.helpAliases.value,
        rollback_aliases_csv: ids.rollbackAliases.value,
        bill_aliases_csv: ids.billAliases.value,
        record_prefix_csv: ids.recordPrefix.value,
      };
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await res.json();
      if (!json.ok) {
        setStatus(json.error || '保存失败', true);
        return;
      }
      setStatus('配置已保存');
      await loadConfig();
    }

    async function discoverGroups() {
      setStatus('正在发现群聊，请稍候...');
      const res = await fetch('/api/discover-groups', { method: 'POST' });
      const json = await res.json();
      if (!json.ok) {
        setStatus(json.error || '发现群聊失败', true);
        return;
      }
      setStatus('已刷新发现群聊');
      await loadConfig();
    }

    async function addManualGroup() {
      const name = (ids.quickGroupName.value || '').trim();
      if (!name) {
        setStatus('请先输入群聊名称', true);
        return;
      }
      const res = await fetch('/api/manual-groups/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_name: name }),
      });
      const json = await res.json();
      if (!json.ok) {
        setStatus(json.error || '添加失败', true);
        return;
      }
      ids.quickGroupName.value = '';
      setStatus('已加入手动监听，机器人将自动重载');
      await loadConfig();
    }

    loadConfig();
  </script>
</body>
</html>
"""


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), PanelHandler)
    print(f"控制面板已启动: http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
