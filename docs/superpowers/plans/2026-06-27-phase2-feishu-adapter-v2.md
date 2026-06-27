# Phase 2: 飞书 Adapter — 简洁中转方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 创建飞书 Adapter 独立进程，纯中转——飞书给啥传 Gateway，Gateway 给啥传飞书。

**Architecture:** Adapter 是个简单的 TCP Client + 飞书 SDK 桥。不管理流式状态，不读 DB，不做内容分析。Gateway 推来增量内容就更新卡片，推来最终内容就终结卡片，推来 PUSH 就发消息。

**Tech Stack:** Python 3.11+, asyncio, lark-oapi SDK, subprocess, env vars

---

## 核心原则

1. **纯中转**：Gateway 给啥就传给飞书，飞书给啥就传给 Gateway，不分析内容归属
2. **一条通道**：只有流式卡片，不发独立图片/文件消息（避免重复）
3. **配置名 = 进程名**：`im.adapter="feishu"` → 自动启动 `python -m niu_feishu_adapter`
4. **全部走环境变量**：凭证、端口都从 env 读取，不用命令行参数

---

## TCP 协议（Gateway ↔ Adapter 已有的协议，不做修改）

Adapter → Gateway：MSG / READY / PING
Gateway → Adapter：SEND / STREAM / PUSH / PONG

STREAM 指令携带 `content`（增量文本）、`channel_id`、`is_final`。

---

## Adapter 处理逻辑（极简）

### 入方向（飞书 → Gateway）

收到飞书消息 → 下载附件到本地 → 构造 MSG（Markdown 格式，附件用本地路径引用）→ 发给 Gateway

群聊特殊处理：
- 仅 @bot 消息触发 Agent
- 注入发送者前缀 `[群聊] 发送者: 内容`
- 清理 @bot 文本
- reply_to_id 传给 Gateway（供群聊回复用）

### 出方向（Gateway → 飞书）

- **STREAM**：有卡片就更新，没卡片就创建 → 累积文本 + 上传图片拿 img_key → 更新卡片 markdown 元素
- **SEND**：终结流式卡片（图片嵌入卡片 body）→ 无卡片时发 Markdown 文本
- **PUSH**：发 Markdown 文本消息

---

## 修改范围

### 新建文件

1. `im-adapters/feishu/pyproject.toml`
2. `im-adapters/feishu/src/niu_feishu_adapter/__init__.py`
3. `im-adapters/feishu/src/niu_feishu_adapter/__main__.py`
4. `im-adapters/feishu/src/niu_feishu_adapter/adapter.py`
5. `im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py`

### 修改文件

6. `niu_api/channel/gateway.py` — 启动逻辑 + 消息缓冲 + STREAM 协议 + notify_stream 签名
7. `niu_api/chat_queue.py` — channel_id 透传
8. `agent/runner.py` — STREAM 通知携带增量内容 + channel_id + is_final

---

## Task 1: 修改 Gateway — 启动逻辑 + 消息缓冲 + STREAM 协议

**Files:**
- Modify: `niu_api/channel/gateway.py`

- [ ] **Step 1: 添加 import + 新字段**

文件顶部添加 `from collections import deque` 和 `import os`。
`__init__` 中 `self._connected_since = 0.0` 后添加：
```python
        self._send_buffer: deque = deque(maxlen=10)
        self._reply_to_ids: dict[str, str] = {}   # channel_id → reply_to_id 映射（群聊回复目标）
        self._finalized_channels: set[str] = set() # 已通过 STREAM(is_final) 终结的 channel_id，避免 SEND 重复终结
```

- [ ] **Step 2: 重写 _launch_adapter — 自动推导 + 全部走 env**

当前 `_launch_adapter`（行 108-131）替换为：

```python
    def _launch_adapter(self):
        """根据配置拉起 IM Adapter 子进程"""
        try:
            prefs_path = Path.home() / ".niu" / "preferences.json"
            if not prefs_path.exists():
                return
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            im_config = prefs.get("im", {})
            adapter_type = im_config.get("adapter", "").strip()
            if not adapter_type:
                logger.info("[IMGateway] No adapter configured, skipping launch")
                return

            adapter_module = f"niu_{adapter_type}_adapter"
            adapter_workdir = Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
            if not adapter_workdir.exists():
                logger.error(f"[IMGateway] Adapter not found: {adapter_workdir}")
                return

            env = dict(os.environ)
            env["NIU_IM_ADAPTER"] = adapter_type
            env["NIU_GATEWAY_PORT"] = str(self._port)
            python_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{adapter_workdir}:{python_path}" if python_path else str(adapter_workdir)

            adapter_config = prefs.get(adapter_type, {})
            app_id = adapter_config.get("app_id", "")
            app_secret = adapter_config.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(f"[IMGateway] {adapter_type} credentials missing, skipping")
                return
            env[f"NIU_{adapter_type.upper()}_APP_ID"] = app_id
            env[f"NIU_{adapter_type.upper()}_APP_SECRET"] = app_secret
            for key in ("user_p2p_chat_id", "user_open_id"):
                val = adapter_config.get(key, "")
                if val:
                    env[f"NIU_{adapter_type.upper()}_{key.upper()}"] = val

            argv = [sys.executable, "-m", adapter_module]
            log_dir = Path.home() / ".niu" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            adapter_stderr = open(log_dir / "im_adapter_stderr.log", "a")
            self._adapter_proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=adapter_stderr, env=env,
            )
            adapter_stderr.close()
            logger.info(f"[IMGateway] Adapter launched: {adapter_type}, PID={self._adapter_proc.pid}")
        except Exception as e:
            logger.error(f"[IMGateway] Launch failed: {e}")
```

- [ ] **Step 3: watchdog 区分永久/瞬时退出码**

在 `retcode = self._adapter_proc.poll()` 之后、`self._restart_count += 1` 之前添加：

```python
                    if retcode == 2:
                        logger.error(f"[IMGateway] Adapter permanent error (code=2), not restarting")
                        self._adapter_proc = None
                        break
```

- [ ] **Step 4: _async_send 添加缓冲 + _skip_buffer 参数**

签名改为 `async def _async_send(self, cmd: dict, _skip_buffer=False):`。
在 `if writer is None: return` 之后、`async with self._write_lock:` 之前添加：

```python
        if not _skip_buffer and cmd.get("type") in ("SEND", "PUSH"):
            # 缓冲：同一 channel_id+type 只保留最后一条，避免重放重复
            cid = cmd.get("channel_id", "__global__")
            existing = next((i for i, c in enumerate(self._send_buffer)
                             if c.get("channel_id") == cid and c.get("type") == cmd.get("type")), None)
            if existing is not None:
                self._send_buffer[existing] = cmd
            else:
                self._send_buffer.append(cmd)
```

- [ ] **Step 5: _on_ready 改为 async + 重放缓冲**

替换当前 `_on_ready`：

```python
    async def _on_ready(self, msg: dict):
        """处理 READY — 记录信息 + 重放缓冲"""
        with self._lock:
            self._adapter_name = msg.get("adapter", "im")
            self._push_target = msg.get("push_target")
            logger.info(f"[IMGateway] Adapter ready: {self._adapter_name}, push_target={self._push_target}")
        if self._send_buffer:
            logger.info(f"[IMGateway] Replaying {len(self._send_buffer)} buffered messages")
            for cmd in list(self._send_buffer):
                try:
                    await self._async_send(cmd, _skip_buffer=True)
                except Exception as e:
                    logger.error(f"[IMGateway] Replay failed: {e}")
            self._send_buffer.clear()
```

`_dispatch` 中 READY 分支**必须**改为 `await self._on_ready(msg)`（原来是同步调用 `self._on_ready(msg)`，漏掉 await 会导致协程不执行，READY 处理静默失败）。

- [ ] **Step 6: notify_stream 新签名**

替换为：

```python
    def notify_stream(self, content: str, channel_id: str = "", is_final: bool = False):
        """通知 Adapter 有新增量内容"""
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        self._send_command({
            "type": "STREAM",
            "channel_id": channel_id,
            "content": content,
            "is_final": is_final,
            "reply_to_id": reply_to_id,
        })
        if is_final:
            with self._lock:
                self._finalized_channels.add(channel_id)
```

- [ ] **Step 7: _on_msg 存储 reply_to_id**

当前 `_on_msg` 中构造 `UnifiedMessage` 后、调用 `route_in_sync` 之前，添加：

```python
        reply_to_id = msg.get("reply_to_id")
        if reply_to_id:
            with self._lock:
                self._reply_to_ids[msg.get("channel_id", "")] = reply_to_id
```

- [ ] **Step 8: send() 跳过已终结的 channel + 附带 reply_to_id**

替换当前 `send` 方法：

```python
    async def send(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot send")
            return
        with self._lock:
            if channel_id in self._finalized_channels:
                self._finalized_channels.discard(channel_id)
                self._reply_to_ids.pop(channel_id, None)
                logger.debug(f"[IMGateway] Channel {channel_id} already finalized via STREAM, skipping SEND")
                return
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        await self._async_send({"type": "SEND", "channel_id": channel_id, "content": content, "reply_to_id": reply_to_id})
        with self._lock:
            self._reply_to_ids.pop(channel_id, None)
```

- [ ] **Step 9: _handle_adapter 断连时清理新字段**

在 `_handle_adapter` 的 `finally` 块中，现有代码清除 `_writer`、`_connected`、`_adapter_name`、`_push_target`。
添加清除新字段：

```python
                self._reply_to_ids.clear()
                self._finalized_channels.clear()
```

- [ ] **Step 10: 语法验证**

```bash
python -c "from niu_api.channel.gateway import IMGateway; print('OK')"
```

---

## Task 2: channel_id 透传 — chat_queue.py + runner.py

**Files:**
- Modify: `niu_api/chat_queue.py`
- Modify: `agent/runner.py`

- [ ] **Step 1: chat_queue.py _process_single 签名加 channel_id**

签名末尾加 `channel_id: str = ""`。

- [ ] **Step 2: chat_queue.py _process_with_merge 传入 channel_id**

调用 `_process_single` 时加 `channel_id=first_req.channel_id`。

- [ ] **Step 3: chat_queue.py _process_single 内部调用 runner.chat() 传入 channel_id**

所有 `self._runner.chat(...)` 调用加 `channel_id=channel_id`。

- [ ] **Step 4: runner.py chat() 签名加 channel_id**

签名末尾加 `channel_id: str = ""`。

- [ ] **Step 5: runner.py chat() 存储和清理 channel_id**

方法体开头：`self._current_channel_id = channel_id`
finally 块中：`self._current_channel_id = ""`

**重要**：在 `NiuRunner.__init__` 中添加 `self._current_channel_id = ""` 初始化，否则 chat() 未运行前访问会 AttributeError。

- [ ] **Step 6: runner.py 流式 chunk 中发送 STREAM 通知**

在 `chunk.type == "reply"` 且 `chunk.content` 非空时：

```python
try:
    from niu_api.channel.gateway import get_im_gateway
    _gw = get_im_gateway()
    if _gw and _gw.is_connected:
        _gw.notify_stream(chunk.content, channel_id=self._current_channel_id)
except Exception:
    pass
```

注意：当前生产环境 `verbose=False`，每次 reply 事件的 content 是该轮 LLM 的**完整输出**（非增量）。
Adapter 的 `_on_stream` 收到后会直接用这个内容替换卡片显示（不累积），所以多次 reply 不会导致文本重复。

- [ ] **Step 7: runner.py 流式结束后发送 is_final**

在流式循环结束处（chat_idle 之前）：

```python
try:
    from niu_api.channel.gateway import get_im_gateway
    _gw = get_im_gateway()
    if _gw and _gw.is_connected:
        _gw.notify_stream("", channel_id=self._current_channel_id, is_final=True)
except Exception:
    pass
```

- [ ] **Step 8: runner.py 修改 _persist_one_msg 中的旧 notify_stream**

将 `_persist_one_msg` 中的 `notify_stream("")` 改为 `notify_stream("", channel_id=self._current_channel_id)`。
只传空内容（信号通知"有更新"），不传完整内容（避免与 Step 6 的完整内容重复）。
Tool 调用期间的 persist 事件会触发这个空内容 STREAM，Adapter 收到后只更新卡片（用空内容替换不会改变显示，但 seq 会递增，保持卡片活跃）。

- [ ] **Step 9: 语法验证**

```bash
python -c "from agent.runner import NiuRunner; print('OK')"
python -c "from niu_api.chat_queue import ChatQueue; print('OK')"
```

---

## Task 3: 创建飞书 Adapter 包

**Files:**
- Create: `im-adapters/feishu/pyproject.toml`
- Create: `im-adapters/feishu/src/niu_feishu_adapter/__init__.py`
- Create: `im-adapters/feishu/src/niu_feishu_adapter/__main__.py`
- Create: `im-adapters/feishu/src/niu_feishu_adapter/adapter.py`
- Create: `im-adapters/feishu/src/niu_feishu_adapter/feishu_api.py`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p im-adapters/feishu/src/niu_feishu_adapter
```

- [ ] **Step 2: pyproject.toml**

```toml
[project]
name = "niu-feishu-adapter"
version = "0.1.0"
description = "Feishu IM Adapter for Niu AI Bot"
requires-python = ">=3.11"
dependencies = [
    "lark-oapi>=1.4.0",
    "loguru>=0.7.0",
    "requests>=2.28.0",
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: __init__.py**

```python
"""飞书 IM Adapter — TCP Client，连接 IM Gateway，纯中转"""
```

- [ ] **Step 4: __main__.py**

```python
"""飞书 Adapter 入口 — python -m niu_feishu_adapter

退出码：0=正常, 1=瞬时错误(可重启), 2=永久错误(不重启)
"""
import asyncio
import os
import sys

from loguru import logger


def main():
    adapter_type = os.environ.get("NIU_IM_ADAPTER", "")
    if adapter_type != "feishu":
        logger.error(f"NIU_IM_ADAPTER={adapter_type}, expected 'feishu'")
        sys.exit(2)

    port_str = os.environ.get("NIU_GATEWAY_PORT", "")
    if not port_str:
        logger.error("Missing NIU_GATEWAY_PORT")
        sys.exit(2)
    try:
        gateway_port = int(port_str)
    except ValueError:
        logger.error(f"Invalid NIU_GATEWAY_PORT: {port_str}")
        sys.exit(2)

    app_id = os.environ.get("NIU_FEISHU_APP_ID", "")
    app_secret = os.environ.get("NIU_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.error("Missing NIU_FEISHU_APP_ID or NIU_FEISHU_APP_SECRET")
        sys.exit(2)

    push_chat_id = os.environ.get("NIU_FEISHU_USER_P2P_CHAT_ID", "")
    push_open_id = os.environ.get("NIU_FEISHU_USER_OPEN_ID", "")

    from niu_feishu_adapter.adapter import FeishuAdapter
    adapter = FeishuAdapter(
        gateway_port=gateway_port,
        app_id=app_id,
        app_secret=app_secret,
        push_chat_id=push_chat_id,
        push_open_id=push_open_id,
    )
    try:
        asyncio.run(adapter.run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"[FeishuAdapter] Fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: feishu_api.py — 飞书 API 封装（上传、下载、发送消息、卡片操作）**

```python
"""飞书 API 封装 — 上传/下载/发消息/卡片操作

所有飞书 REST API 调用集中在此文件，adapter.py 不直接调飞书 API。
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import requests as _requests
from loguru import logger

TEMP_DIR = Path.home() / ".niu" / "tmp"


# ── tenant token ──

_token_cache: dict = {"token": "", "expires_at": 0.0, "app_id": "", "app_secret": ""}


def _get_tenant_token(app_id: str, app_secret: str) -> str | None:
    """获取 tenant_access_token（带缓存，提前5分钟刷新）"""
    now = time.monotonic()
    if (_token_cache["token"] and _token_cache["app_id"] == app_id
            and _token_cache["expires_at"] > now + 300):
        return _token_cache["token"]
    try:
        resp = _requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Token failed: {result.get('msg', '')}")
            return None
        token = result["tenant_access_token"]
        expire = result.get("expire", 7200)
        _token_cache.update(token=token, expires_at=now + expire, app_id=app_id, app_secret=app_secret)
        return token
    except Exception as e:
        logger.error(f"[FeishuAPI] Token error: {e}")
        return None


# ── receive_id_type 推断 ──

def infer_receive_id_type(receive_id: str) -> str:
    """根据 ID 前缀推断 receive_id_type"""
    if not receive_id:
        return "chat_id"
    if "@" in receive_id:
        return "email"
    if receive_id.startswith("oc_"):
        return "chat_id"
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "user_id"


# ── 上传 ──

def upload_image(app_id: str, app_secret: str, local_path: str) -> str | None:
    """上传图片到飞书，返回 image_key"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    p = Path(local_path)
    if not p.exists():
        return None
    try:
        with open(str(p), "rb") as f:
            resp = _requests.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (p.name, f)},
                timeout=30,
            )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Upload image failed: {result.get('msg', '')}")
            return None
        return result.get("data", {}).get("image_key", "") or None
    except Exception as e:
        logger.error(f"[FeishuAPI] Upload image error: {e}")
        return None


def upload_file(app_id: str, app_secret: str, local_path: str, filename: str) -> str | None:
    """上传文件到飞书，返回 file_key"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    p = Path(local_path)
    if not p.exists():
        return None
    clean_name = re.sub(r'[\x00-\x1f\x7f"\\]', '_', filename or p.name)[:200]
    try:
        with open(str(p), "rb") as f:
            resp = _requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": "stream", "file_name": clean_name},
                files={"file": (clean_name, f, "application/octet-stream")},
                timeout=60,
            )
        result = resp.json()
        if result.get("code", -1) != 0:
            logger.error(f"[FeishuAPI] Upload file failed: {result.get('msg', '')}")
            return None
        return result.get("data", {}).get("file_key", "") or None
    except Exception as e:
        logger.error(f"[FeishuAPI] Upload file error: {e}")
        return None


# ── 下载 ──

def download_resource(app_id: str, app_secret: str, file_key: str,
                      rtype: str, file_name: str = "",
                      message_id: str = "") -> str | None:
    """下载飞书资源到本地，返回本地路径"""
    token = _get_tenant_token(app_id, app_secret)
    if not token:
        return None
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    name = file_name or f"{rtype}_{file_key}"
    if rtype == "image":
        name = name if '.' in name else f"{name}.jpg"
    local_path = TEMP_DIR / f"feishu_in_{file_key[:20]}_{name}"
    if local_path.exists():
        return str(local_path)
    try:
        # 主路径：message_resource（需要 message_id）
        if message_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            params = {"type": rtype}
        elif rtype == "image":
            url = f"https://open.feishu.cn/open-apis/im/v1/images/{file_key}"
            params = {}
        else:
            url = f"https://open.feishu.cn/open-apis/im/v1/files/{file_key}"
            params = {}
        resp = _requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=30,
        )
        if resp.status_code == 200:
            tmp = local_path.with_suffix(local_path.suffix + ".dl")
            tmp.write_bytes(resp.content)
            tmp.replace(local_path)
            return str(local_path)
        logger.error(f"[FeishuAPI] Download failed: {resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"[FeishuAPI] Download error: {e}")
        return None


# ── 发送消息 ──

async def send_markdown(client, target: str, content: str):
    """发送 Markdown 消息（包装为流式卡片格式，确保正确渲染）"""
    receive_id_type = infer_receive_id_type(target)
    card = json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }, ensure_ascii=False)
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(target)
                .msg_type("interactive")
                .content(card)
                .build()) \
            .build()
        resp = await asyncio.to_thread(client.im.v1.message.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Send markdown failed: {resp.code} {resp.msg}")
    except Exception as e:
        logger.error(f"[FeishuAPI] Send markdown error: {e}")


# ── 流式卡片操作 ──

async def create_card(client, receive_id: str, content: str,
                       reply_to_id: str | None = None) -> tuple[str, str | None]:
    """创建流式卡片，返回 (card_id, message_id)"""
    card_json = json.dumps({
        "schema": "2.0",
        "header": {"title": {"content": "Niu助手", "tag": "plain_text"},
                   "subtitle": {"content": "思考中...", "tag": "plain_text"}},
        "config": {"streaming_mode": True, "update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": content, "element_id": "md1"}]},
    }, ensure_ascii=False)
    try:
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest, CreateCardRequestBody,
        )
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
            ReplyMessageRequest, ReplyMessageRequestBody,
        )

        # 创建卡片实体
        body = CreateCardRequestBody.builder().type("card_json").data(card_json).build()
        req = CreateCardRequest.builder().request_body(body).build()
        resp = await asyncio.to_thread(client.cardkit.v1.card.create, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Create card entity failed: {resp.code} {resp.msg}")
            return "", None
        card_id = resp.data.card.card_id

        # 发送卡片消息
        card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
        msg_id = None
        if reply_to_id:
            # 群聊：回复消息
            send_req = ReplyMessageRequest.builder() \
                .message_id(reply_to_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .msg_type("interactive").content(card_ref).build()) \
                .build()
            send_resp = await asyncio.to_thread(client.im.v1.message.reply, send_req)
            if send_resp.success():
                msg_id = send_resp.data.message_id
        else:
            # 单聊：新建消息
            receive_id_type = infer_receive_id_type(receive_id)
            send_req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive").content(card_ref).build()) \
                .build()
            send_resp = await asyncio.to_thread(client.im.v1.message.create, send_req)
            if send_resp.success():
                msg_id = send_resp.data.message_id

        if not (send_resp.success()):
            # 发送失败，无法清理孤立卡片（CardKit 没有 delete card API）
            logger.error(f"[FeishuAPI] Send card message failed: {send_resp.code}")
            return "", None

        return card_id, msg_id
    except Exception as e:
        logger.error(f"[FeishuAPI] Create card error: {e}")
        return "", None


async def update_card_element(client, card_id: str, content: str, seq: int):
    """更新卡片的 markdown 元素"""
    try:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )
        req = ContentCardElementRequest.builder() \
            .card_id(card_id).element_id("md1") \
            .request_body(ContentCardElementRequestBody.builder()
                .content(content).sequence(seq).uuid(f"niu-stream-{seq}").build()) \
            .build()
        resp = await asyncio.to_thread(client.cardkit.v1.card_element.content, req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Update element failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Update element error: {e}")
        return False


async def finalize_card(client, card_id: str, final_json: str, seq: int):
    """终结卡片：Settings API 关闭 streaming_mode + UpdateCard 写完整内容"""
    try:
        from lark_oapi.api.cardkit.v1 import (
            SettingsCardRequest, SettingsCardRequestBody,
            UpdateCardRequest, UpdateCardRequestBody, Card,
        )
        # 1. 关闭 streaming_mode
        settings_json = json.dumps({"config": {"streaming_mode": False}})
        settings_req = SettingsCardRequest.builder() \
            .card_id(card_id) \
            .request_body(SettingsCardRequestBody.builder()
                .settings(settings_json).sequence(seq)
                .uuid("niu-finalize-settings").build()) \
            .build()
        await asyncio.to_thread(client.cardkit.v1.card.settings, settings_req)

        # 2. 更新完整内容
        new_seq = seq + 1
        update_req = UpdateCardRequest.builder() \
            .card_id(card_id) \
            .request_body(UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(final_json).build())
                .sequence(new_seq).uuid("niu-finalize-update").build()) \
            .build()
        resp = await asyncio.to_thread(client.cardkit.v1.card.update, update_req)
        if not resp.success():
            logger.error(f"[FeishuAPI] Finalize card failed: {resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:
        logger.error(f"[FeishuAPI] Finalize card error: {e}")
        return False
```

- [ ] **Step 6: adapter.py — 主类**

```python
"""飞书 Adapter — TCP Client + 飞书 SDK，纯中转

入方向：飞书消息 → 下载附件 → 构造 MSG → 发给 Gateway
出方向：Gateway 指令 → 调飞书 API → 结果留在飞书侧
"""
import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

MAX_MSG_SIZE = 10 * 1024 * 1024  # 10MB
_LOCAL_PATH_PREFIX = str(Path.home() / ".niu" / "tmp")


class CardState:
    """单个 channel 的流式卡片状态"""
    __slots__ = ("card_id", "seq", "message_id", "receive_id", "reply_to_id",
                 "pending_images", "last_content")

    def __init__(self, card_id: str, receive_id: str, reply_to_id: str | None = None):
        self.card_id = card_id
        self.seq = 0
        self.message_id: str | None = None
        self.receive_id = receive_id
        self.reply_to_id = reply_to_id
        self.pending_images: list[dict] = []
        self.last_content = ""


class FeishuAdapter:
    """飞书 IM Adapter — TCP Client"""

    def __init__(self, gateway_port: int, app_id: str, app_secret: str,
                 push_chat_id: str = "", push_open_id: str = ""):
        self._gateway_port = gateway_port
        self._app_id = app_id
        self._app_secret = app_secret
        self._push_chat_id = push_chat_id
        self._push_open_id = push_open_id
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._card_states: dict[str, CardState] = {}

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._init_sdk()
        await self._connect_gateway()
        await self._send_ready()
        self._start_listener()
        await self._read_loop()

    # ── 飞书 SDK ──

    def _init_sdk(self):
        import lark_oapi as lark
        self._client = lark.Client.builder() \
            .app_id(self._app_id).app_secret(self._app_secret) \
            .log_level(lark.LogLevel.DEBUG).build()
        logger.info("[FeishuAdapter] SDK initialized")

    def _start_listener(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        def on_message(data: P2ImMessageReceiveV1) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._on_feishu_msg(data), self._loop)
            except Exception as e:
                logger.error(f"[FeishuAdapter] Event error: {e}")

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(on_message).build()
        ws = lark.ws.Client(self._app_id, self._app_secret,
                            event_handler=handler, log_level=lark.LogLevel.DEBUG)
        threading.Thread(target=ws.start, daemon=True).start()
        logger.info("[FeishuAdapter] Listener started")

    async def _on_feishu_msg(self, data):
        """飞书消息回调"""
        try:
            msg = data.event.message
            sender = data.event.sender
        except AttributeError:
            return

        content_str = msg.content or "{}"
        chat_id = msg.chat_id or ""
        sender_id = sender.sender_id.open_id if sender.sender_id else ""
        msg_type = msg.message_type or "text"
        is_group = msg.chat_type == "group"
        message_id = getattr(msg, 'message_id', '') or ''

        # 群聊 @bot 过滤
        if is_group:
            mentions = getattr(msg, 'mentions', None) or []
            bot_mentioned = any(
                getattr(getattr(m, 'id', None), 'open_id', '') == self._app_id
                for m in mentions if m
            )
            if not bot_mentioned:
                return

        # 文本内容
        if msg_type == "text":
            try:
                text = json.loads(content_str).get("text", "")
            except Exception:
                text = content_str
        else:
            text = content_str

        # 资源下载
        from niu_feishu_adapter.feishu_api import download_resource
        try:
            if msg_type == "image":
                image_key = json.loads(content_str).get("image_key", "")
                if image_key:
                    local = await asyncio.to_thread(
                        download_resource, self._app_id, self._app_secret,
                        image_key, "image", message_id=message_id)
                    if local:
                        text = f"![图片]({local})"
                    else:
                        text = f"[图片下载失败: {image_key}]"
            elif msg_type == "file":
                cj = json.loads(content_str)
                file_key = cj.get("file_key", "")
                file_name = cj.get("file_name", "unknown")
                if file_key:
                    local = await asyncio.to_thread(
                        download_resource, self._app_id, self._app_secret,
                        file_key, "file", file_name, message_id=message_id)
                    if local:
                        text = f"[{file_name}]({local})"
                    else:
                        text = f"[文件下载失败: {file_name}]"
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"[FeishuAdapter] Resource parse error: {e}, raw: {content_str[:200]}")

        # 群聊处理
        reply_to_id = None
        if is_group:
            # 清理 @bot 文本
            text = re.sub(r'@_user_\d+\s*', '', text).strip()
            # 发送者前缀（EventSender 没有 name，用 sender_id 前6位作标识）
            sender_name = f"用户{sender_id[:6]}" if sender_id else "未知"
            text = f"[群聊] {sender_name}: {text}"
            reply_to_id = message_id

        await self._send({
            "type": "MSG",
            "content": text,
            "channel_id": chat_id,
            "sender_id": sender_id,
            "session_id": f"feishu:{sender_id}" if not is_group else f"feishu:group:{chat_id}",
            "is_group": is_group,
            "reply_to_id": reply_to_id,
        })

    # ── Gateway TCP ──

    async def _connect_gateway(self):
        """连接 Gateway，最多重试 30 次（等待 Gateway 启动）"""
        for attempt in range(30):
            try:
                self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self._gateway_port)
                self._card_states.clear()  # 重连后清空旧卡片状态，避免用过时状态更新
                logger.info(f"[FeishuAdapter] Connected to Gateway :{self._gateway_port}")
                return
            except ConnectionRefusedError:
                if attempt == 0:
                    logger.info("[FeishuAdapter] Waiting for Gateway...")
                await asyncio.sleep(1)
        raise RuntimeError(f"Gateway not available after 30s on port {self._gateway_port}")

    async def _send_ready(self):
        await self._send({"type": "READY", "adapter": "feishu", "push_target": self._push_chat_id})

    async def _read_loop(self):
        try:
            while True:
                header = await self._reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length > MAX_MSG_SIZE:
                    logger.error(f"[FeishuAdapter] Message too large: {length}")
                    break
                data = await self._reader.readexactly(length)
                try:
                    cmd = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                await self._dispatch(cmd)
        except Exception as e:
            logger.error(f"[FeishuAdapter] Connection error: {e}")
        finally:
            if self._writer:
                self._writer.close()

    async def _dispatch(self, cmd: dict):
        t = cmd.get("type")
        if t == "STREAM":
            await self._on_stream(cmd)
        elif t == "SEND":
            await self._on_send(cmd)
        elif t == "PUSH":
            await self._on_push(cmd)

    async def _on_stream(self, cmd: dict):
        """STREAM = 完整内容 → 创建/更新卡片

        注意：当前生产环境 verbose=False，每次 STREAM 的 content 是该轮 LLM 的完整输出，
        不是增量文本。所以不累积，直接用 content 替换卡片显示。

        如果 content 为空（仅信号通知），只递增 seq 保持卡片活跃，不更新显示内容。
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        is_final = cmd.get("is_final", False)
        reply_to_id = cmd.get("reply_to_id")

        from niu_feishu_adapter.feishu_api import (
            create_card, update_card_element, finalize_card,
        )

        state = self._card_states.get(receive_id)

        # 空内容 = 信号通知（保持卡片活跃），只递增 seq
        if not content and state:
            state.seq += 1
            # 不更新卡片显示，但递增 seq 以保持流式连接活跃
            if is_final:
                self._card_states.pop(receive_id, None)
                await self._do_finalize(state, state.last_content)
            return

        # 有内容 = 更新卡片显示
        filtered, images = await asyncio.to_thread(self._filter_media, content)
        display = filtered.replace("[PHOTO_SEP]", "")
        if len(display) > 18000:
            display = display[:17900] + "\n\n...[内容已截断]"

        if not state:
            card_id, msg_id = await create_card(self._client, receive_id, display, reply_to_id)
            if not card_id:
                logger.error(f"[FeishuAdapter] Card creation failed for {receive_id}")
                return
            state = CardState(card_id, receive_id, reply_to_id)
            state.message_id = msg_id
            state.seq = 1
            self._card_states[receive_id] = state
        else:
            state.seq += 1
            await update_card_element(self._client, state.card_id, display, state.seq)

        state.pending_images = images
        state.last_content = content

        if is_final:
            self._card_states.pop(receive_id, None)
            await self._do_finalize(state, content)

    async def _on_send(self, cmd: dict):
        """SEND = 最终回复 → 终结卡片（无卡片时发 Markdown 文本）"""
        receive_id = cmd.get("channel_id", "")
        content = cmd.get("content", "")
        state = self._card_states.pop(receive_id, None)
        if state:
            await self._do_finalize(state, content)
        else:
            # 异常回退：无卡片，发纯 Markdown（图片标记替换为文字提示）
            from niu_feishu_adapter.feishu_api import send_markdown
            text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', lambda m: f'↑ {m.group(1) or "照片"}', content)
            await send_markdown(self._client, receive_id, text)

    async def _on_push(self, cmd: dict):
        """PUSH = 主动推送 → Markdown 消息"""
        receive_id = cmd.get("channel_id", "")
        if not receive_id:
            logger.warning("[FeishuAdapter] PUSH without channel_id, dropping")
            return
        content = cmd.get("content", "")
        from niu_feishu_adapter.feishu_api import send_markdown
        await send_markdown(self._client, receive_id, content)

    async def _do_finalize(self, state: CardState, final_content: str):
        """终结卡片：图片嵌入卡片 body"""
        from niu_feishu_adapter.feishu_api import finalize_card

        # 终结时重新过滤图片（确保所有图片都已上传）
        filtered, images = await asyncio.to_thread(self._filter_media, final_content)
        state.pending_images = images

        # 构建终结卡片 body
        if state.pending_images and "[PHOTO_SEP]" in filtered:
            elements = self._build_final_body(filtered, state.pending_images)
        else:
            display = filtered.replace("[PHOTO_SEP]", "")
            if len(display) > 18000:
                display = display[:17900] + "\n\n...[内容已截断]"
            elements = [{"tag": "markdown", "content": display, "element_id": "md1"}]

        final_card = {
            "schema": "2.0",
            "header": {"title": {"content": "Niu助手", "tag": "plain_text"},
                       "subtitle": {"content": "", "tag": "plain_text"}},
            "config": {"streaming_mode": False, "update_multi": True},
            "body": {"elements": elements},
        }
        final_json = json.dumps(final_card, ensure_ascii=False)
        state.seq += 1
        ok = await finalize_card(self._client, state.card_id, final_json, state.seq)
        if not ok:
            logger.error(f"[FeishuAdapter] Finalize failed for card {state.card_id}")

    # ── 图片处理 ──

    def _filter_media(self, content: str) -> tuple[str, list[dict]]:
        """过滤 Markdown 图片：上传 → 替换为 [PHOTO_SEP]，返回 (filtered, images)

        每次调用独立处理完整 content（非增量），返回本次所有图片。
        """
        from niu_feishu_adapter.feishu_api import upload_image
        images: list[dict] = []
        replacements: list[tuple[int, int, str]] = []
        for m in re.finditer(r'!\[([^\]]*)\]\(([^)]+)\)', content):
            path = m.group(2)
            if not path.startswith(_LOCAL_PATH_PREFIX) or not Path(path).exists():
                replacements.append((m.start(), m.end(), ""))
                continue
            img_key = upload_image(self._app_id, self._app_secret, path)
            if img_key:
                images.append({"img_key": img_key, "alt": m.group(1) or "照片"})
                replacements.append((m.start(), m.end(), "[PHOTO_SEP]"))
            else:
                replacements.append((m.start(), m.end(), ""))
        for start, end, repl in sorted(replacements, key=lambda x: x[0], reverse=True):
            content = content[:start] + repl + content[end:]
        return content, images

    @staticmethod
    def _build_final_body(filtered_content: str, pending_images: list) -> list:
        """构建终结卡片 body：markdown + img 交替"""
        elements = []
        parts = filtered_content.split("[PHOTO_SEP]")
        md_idx, img_idx = 1, 0
        for i, part in enumerate(parts):
            part = part.strip()
            if part:
                if len(part) > 18000:
                    part = part[:17900] + "\n\n...[内容已截断]"
                elements.append({"tag": "markdown", "content": part, "element_id": f"md{md_idx}"})
                md_idx += 1
            if i < len(pending_images):
                info = pending_images[i]
                elements.append({
                    "tag": "img",
                    "img_key": info["img_key"],
                    "alt": {"tag": "plain_text", "content": info.get("alt", "照片")},
                    "element_id": f"img_{img_idx}",
                })
                img_idx += 1
        if not elements:
            elements.append({"tag": "markdown", "content": filtered_content.replace("[PHOTO_SEP]", ""),
                             "element_id": "md1"})
        return elements

    # ── 通用 TCP 发送 ──

    async def _send(self, cmd: dict):
        if not self._writer:
            return
        async with self._write_lock:
            payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
            self._writer.write(len(payload).to_bytes(4, "big") + payload)
            await self._writer.drain()
```

- [ ] **Step 7: 语法验证**

```bash
cd im-adapters/feishu/src && PYTHONPATH=. python -c "from niu_feishu_adapter.adapter import FeishuAdapter; print('OK')"
```

---

## 验证清单

每个 Task 完成后语法验证通过。

所有 Task 完成后：
1. 配置 preferences.json 添加 `"im": {"enabled": true, "gateway_port": 19877, "adapter": "feishu"}`
2. 启动 `./niu`，检查日志确认 Gateway 启动 + Adapter 子进程启动 + READY 收到
3. 杀掉所有 niu 进程

---

## 审查检查项

- [ ] C1: 启动命令从 im.adapter 自动推导，无硬编码映射
- [ ] C2: PYTHONPATH 包含 adapter workdir
- [ ] C3: 凭证和端口全部走环境变量
- [ ] C4: 缺少凭证时记录错误跳过
- [ ] C5: exit code 2=永久, 1=瞬时
- [ ] C6: 消息缓冲 deque(maxlen=10) + _skip_buffer 避免重放循环
- [ ] C7: _on_ready async + 重放后 clear()
- [ ] C8: notify_stream(content, channel_id="", is_final=False)
- [ ] C9: Gateway 不分析内容归属，直接透传
- [ ] C10: 只有一条通道（流式卡片），不发独立图片/文件消息
- [ ] C11: channel_id 从 ChatRequest 透传到 runner.chat() → notify_stream
- [ ] C12: 群聊 @bot 过滤（忽略非 @bot 消息）
- [ ] C13: 群聊发送者前缀注入
- [ ] C14: 群聊 @bot 文本清理
- [ ] C15: 群聊 reply_to_id（卡片回复目标消息）
- [ ] C16: session_id 格式：P2P=feishu:{sender_id}, 群聊=feishu:group:{chat_id}
- [ ] C17: 资源下载双路径（message_resource 主路径 + standalone 回退）
- [ ] C18: receive_id_type 推断
- [ ] C19: 卡片创建区分群聊(reply API)和单聊(create API)
- [ ] C20: 终结卡片时图片嵌入 body（markdown + img 交替）
- [ ] C21: Adapter _send 有 _write_lock
- [ ] C22: Adapter _read_loop 有 MAX_MSG_SIZE
- [ ] C23: STREAM(is_final=True) 后 SEND 不重复终结（Gateway._finalized_channels）
- [ ] C24: reply_to_id 在出方向透传（Gateway 维护 _reply_to_ids 映射，STREAM/SEND 附带 reply_to_id）
- [ ] C25: _persist_one_msg 保留流式通知但改用新签名 notify_stream(content, channel_id=...)
- [ ] C26: _on_push 不做 fallback（纯中转原则）
- [ ] C27: TCP 重连后清空 _card_states
- [ ] C28: _dispatch 中 READY 必须 await（不能遗漏）
