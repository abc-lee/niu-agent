# Phase 2: 飞书 Adapter 独立进程 — 简化方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 创建飞书 Adapter 独立进程，通过 TCP 连接 Gateway，纯透传，不分析内容归属。

**核心原则**：Gateway 是中间层，收啥传啥。不区分 IM/前端，不分析 source，不搞 Markdown push。

**Tech Stack:** Python 3.11+, asyncio, lark-oapi SDK, subprocess, env vars

---

## 设计决策

### D1: Gateway 只做透传，不分析内容归属

**Why**：已经分层了。Gateway 的职责就是把 Adapter 发来的东西传给 Agent，把 Agent 的东西传给 Adapter。分析"这是给前端的还是给飞书的"不是 Gateway 该干的事。

**How**：
- MSG（Adapter→Gateway）：直接透传给 ChatQueue
- SEND（Gateway→Adapter）：直接透传，Adapter 自己决定怎么发到飞书
- STREAM（Gateway→Adapter）：直接透传增量内容，Adapter 自己决定怎么更新卡片
- PUSH（Gateway→Adapter）：仅用于定时任务等**没有流式上下文**的主动推送

### D2: 只有一条通道 — 流式卡片，不搞独立消息

**Why**：之前有两条路往飞书发消息（流式卡片 + 独立 Markdown/图片消息），导致重复发送且无法去重。根本原因：流式卡片终结时图片已嵌入卡片 body，再发一条独立消息就是重复。

**关键事实**：
- 飞书支持 Markdown 消息格式（`{"markdown": content}`），卡片内也有 markdown 元素
- 飞书 Markdown 消息中的 `![alt](local_path)` 无法渲染本地路径，但上传后替换为飞书远端路径是可以的
- 流式卡片中的 `{"tag": "img", "img_key": "xxx"}` 元素是更可靠的图片展示方式（不依赖 URL 有效性）
- 两条通道并存导致重复，**必须只有一条**

**How**：
- Agent 回复用户消息 → 走流式推送（STREAM 逐 chunk 更新卡片 → SEND 终结卡片）
  - 流式过程中：遇到 `![alt](local_path)` → 上传图片拿 image_key → 存入 pending_images → 标记替换为 `[PHOTO_SEP]`
  - 终结卡片时：按 `[PHOTO_SEP]` 拆分文本，交替插入 `{"tag": "markdown"}` + `{"tag": "img", "img_key": "xxx"}` 元素
- 定时任务/HA 主动推送 → 走 PUSH（Markdown 文本消息 `{"markdown": content}`，不含图片标记）
- 没有流式卡片时的异常回退 → 发一条纯 Markdown 文本消息（图片标记被替换为 `↑ xxx的照片` 文字提示）

**删除**：`_on_send` 不再发独立的文本/图片消息，只终结流式卡片。`parse_and_send` 函数删除，改为 `strip_media_markers`（仅剥离图片标记，用于异常回退）。

### D3: 配置名 = 进程名，自动推导启动命令

**Why**：`im.adapter = "feishu"` → 自动启动 `python -m niu_feishu_adapter`，无需硬编码映射。未来加微信只需配置 `im.adapter = "wechat"` + 创建对应包。

**How**：`adapter_type` → `adapter_module = f"niu_{adapter_type}_adapter"`

### D4: 凭证和端口统一通过环境变量传递

**Why**：命令行参数在 ps 中可见，app_secret 泄露。端口号也统一走环境变量，保持一致性。

**How**：Gateway 读 preferences.json，提取对应 IM 的凭证和端口号，全部设入子进程的 env。Adapter 从环境变量读取所有配置，不使用 argparse。

### D5: Adapter 目录在 im-adapters/feishu/

**Why**：Adapter 是独立进程，不属于 API Server 包。与 mcp-servers/ 平级。

### D6: STREAM 通知包含增量内容 + channel_id + is_final

**Why**：Adapter 是独立进程，无法访问 MessageStore 或 DB。必须在通知中携带完整信息（内容、目标会话、是否结束）。

**How**：从 runner.py 的流式生成器中发送 STREAM 通知，携带增量 chunk 内容和 channel_id。生成器结束时发送 is_final=true。

### D7: 消息缓冲防丢失 — 重放不重复缓冲

**Why**：Adapter 崩溃时，Gateway 的 SEND 指令被丢弃（writer=None）。Agent 已经回复了但用户看不到。

**How**：Gateway 缓冲最近 10 条 SEND/PUSH，Adapter 重连后重放。**关键**：重放时绕过缓冲逻辑（`_async_send` 增加 `_skip_buffer` 参数），避免被重放的消息再次进入缓冲区导致无限循环。

### D8: Adapter 区分永久错误 vs 瞬时错误

**Why**：配置错误时 Adapter 立即退出，Gateway 重启 3 次都是浪费。

**How**：exit code 2 = 永久错误（不重启），exit code 1 = 瞬时错误（重启）。

### D9: Adapter 并发写入保护

**Why**：飞书 SDK 回调从 WebSocket 线程通过 `run_coroutine_threadsafe` 注入 asyncio 事件循环，与 `_read_loop` 的 `_dispatch` 在同一个事件循环中交替执行。两个协程可能同时调用 `_send`，交错 `write+drain` 破坏 TCP 帧格式。

**How**：Adapter 添加 `asyncio.Lock`（`_write_lock`）保护 `_send` 中的 `write+drain`。

### D10: 流式卡片状态按 session 隔离

**Why**：如果多个会话同时触发流式推送（如私聊 + 群聊），单一 `_stream_card_id` 会互相覆盖。

**How**：用 `dict` 按 `channel_id` 存储流式卡片状态（card_id, seq, receive_id）。SEND 终结卡片时只终结对应 channel 的卡片。

### D12: channel_id 从 ChatQueue 透传到 runner 流式生成器

**Why**：STREAM 通知必须携带 `channel_id`，否则多会话场景下流式卡片路由错误。但当前 `channel_id` 在 `ChatRequest` 层面可用，在 `_process_single → runner.chat()` 调用链中被丢弃。

**How**：在 3 个函数签名上追加 `channel_id` 参数并透传，runner 内部用 `self._current_channel_id` 实例变量传递到流式生成器。不修改 `agent_loop.py` 或 `StreamEvent`，避免侵入生成器管道。

传递链：`ChatRequest.channel_id` → `_process_with_merge` → `_process_single(channel_id=...)` → `runner.chat(channel_id=...)` → `self._current_channel_id` → 流式生成器中 `notify_stream(content, channel_id=self._current_channel_id)`

### D11: Adapter 消息大小限制

**Why**：Gateway 有 10MB 限制，Adapter 也需要同样的保护，防止异常数据导致内存溢出。

**How**：`_read_loop` 添加 `MAX_MESSAGE_SIZE = 10 * 1024 * 1024` 检查。

---

## 配置结构

```json
{
  "im": {
    "enabled": true,
    "gateway_port": 19877,
    "adapter": "feishu"
  },
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "user_p2p_chat_id": "oc_xxx",
    "user_open_id": "ou_xxx"
  }
}
```

`im.adapter` 决定启动哪个进程。`feishu` 段保持不变（向后兼容）。

---

## TCP 指令协议

### Adapter → Gateway（入方向）

**MSG — 用户消息**
```json
{
  "type": "MSG",
  "content": "你好\n![照片](/local/path.jpg)",
  "channel_id": "oc_xxx",
  "sender_id": "ou_xxx",
  "session_id": "feishu:ou_xxx",
  "is_group": false
}
```
content 是 Markdown，图片/文件已经是本地路径。Adapter 下载完再发 MSG。

**READY — Adapter 就绪**
```json
{"type": "READY", "adapter": "feishu", "push_target": "oc_xxx"}
```

**PING — 心跳**
```json
{"type": "PING"}
```

### Gateway → Adapter（出方向）

**SEND — 回复消息（Agent 回复完成后的最终内容）**
```json
{
  "type": "SEND",
  "channel_id": "oc_xxx",
  "content": "完整回复内容，支持 Markdown"
}
```

**STREAM — 流式推送（Agent 回复过程中的增量内容）**
```json
{
  "type": "STREAM",
  "channel_id": "oc_xxx",
  "content": "增量文本",
  "is_final": false
}
```
最终一条（is_final=true）表示流式结束，Adapter 应终结卡片。

**PUSH — 主动推送（定时任务等，无流式上下文）**
```json
{
  "type": "PUSH",
  "channel_id": "oc_xxx",
  "content": "提醒内容，Markdown 格式"
}
```

**PONG — 心跳回复**
```json
{"type": "PONG"}
```

---

## 修改范围

### 新建文件

1. `im-adapters/feishu/pyproject.toml`
2. `im-adapters/feishu/src/niu_feishu_adapter/__init__.py`
3. `im-adapters/feishu/src/niu_feishu_adapter/__main__.py`
4. `im-adapters/feishu/src/niu_feishu_adapter/adapter.py`
5. `im-adapters/feishu/src/niu_feishu_adapter/stream_card.py`
6. `im-adapters/feishu/src/niu_feishu_adapter/media.py`

### 修改文件

7. `niu_api/channel/gateway.py` — 启动逻辑（自动推导命令 + PYTHONPATH + 环境变量 + 消息缓冲 + 区分永久/瞬时退出码）
8. `niu_api/chat_queue.py` — channel_id 透传到 runner.chat()
9. `agent/runner.py` — channel_id 透传 + STREAM 通知携带增量内容 + channel_id + is_final

**无需修改的文件**：
- `niu_api/chat.py` — `/chat` 和 `/chat/sync` 端点调用 `runner.chat()` 时不传 `channel_id`，默认值 `""` 正确（前端请求无飞书 channel_id）
- `niu_api/compat.py` — `/api/chat/session` 端点同理，默认值 `""` 正确

---

## Task 1: 修改 Gateway — 启动逻辑 + 消息缓冲 + STREAM 协议

**Files:**
- Modify: `niu_api/channel/gateway.py`

- [ ] **Step 1: 添加导入**

在文件顶部添加 `from collections import deque` 和 `import os`。

- [ ] **Step 2: __init__ 添加消息缓冲区**

在 `self._connected_since = 0.0` 后添加：
```python
self._send_buffer: deque = deque(maxlen=10)
```

- [ ] **Step 3: 重写 _launch_adapter — 全部走环境变量，不用命令行参数**

当前代码（行 108-131）替换为：
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
                logger.info("[IMGateway] No adapter type configured, skipping adapter launch")
                return

            # 自动推导启动命令：adapter_type = "feishu" → python -m niu_feishu_adapter
            adapter_module = f"niu_{adapter_type}_adapter"
            adapter_workdir = Path(__file__).resolve().parent.parent.parent / "im-adapters" / adapter_type / "src"
            if not adapter_workdir.exists():
                logger.error(f"[IMGateway] Adapter directory not found: {adapter_workdir}")
                return

            # 构造环境变量（所有配置统一走 env，不用命令行参数）
            env = dict(os.environ)
            env["NIU_IM_ADAPTER"] = adapter_type
            env["NIU_GATEWAY_PORT"] = str(self._port)
            # PYTHONPATH 加入 adapter workdir
            python_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{adapter_workdir}:{python_path}" if python_path else str(adapter_workdir)

            # 传递 IM 特定的凭证
            adapter_config = prefs.get(adapter_type, {})
            app_id = adapter_config.get("app_id", "")
            app_secret = adapter_config.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(f"[IMGateway] {adapter_type} credentials missing (app_id/app_secret), skipping adapter launch")
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
            logger.info(f"[IMGateway] Adapter process launched: type={adapter_type}, PID={self._adapter_proc.pid}")
        except Exception as e:
            logger.error(f"[IMGateway] Failed to launch adapter: {e}")
```

注意：`argv` 不再包含 `--gateway-port`，端口号通过 `NIU_GATEWAY_PORT` 环境变量传递。

- [ ] **Step 4: _adapter_watchdog 区分永久/瞬时退出码**

在 watchdog 中修改退出码检查：
```python
                retcode = self._adapter_proc.poll()
                if retcode is not None:
                    if retcode == 2:
                        logger.error(f"[IMGateway] Adapter exited with permanent error (code=2), not restarting")
                        self._adapter_proc = None
                        break
                    self._restart_count += 1
                    if self._restart_count > self._MAX_RESTARTS:
```

- [ ] **Step 5: _async_send 添加缓冲逻辑 + _skip_buffer 参数**

在 `_async_send` 签名中增加 `_skip_buffer=False` 参数，在 `async with self._write_lock:` 块开头添加：
```python
    async def _async_send(self, cmd: dict, _skip_buffer=False):
        """发送指令给 Adapter（async，带 drain）

        _lock 保护 writer 引用的读取（跨线程安全）。
        _write_lock 序列化 write+drain（协程级安全，防止两个协程交错写入破坏协议）。
        _skip_buffer=True 时跳过缓冲（用于重放，避免无限循环）。
        """
        with self._lock:
            writer = self._writer
        if writer is None:
            return
        if not _skip_buffer and cmd.get("type") in ("SEND", "PUSH"):
            self._send_buffer.append(cmd)
        async with self._write_lock:
            try:
                payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
                header = len(payload).to_bytes(4, "big")
                writer.write(header + payload)
                await writer.drain()
            except Exception as e:
                logger.error(f"[IMGateway] Send command failed: {e}")
                with self._lock:
                    if self._writer is writer:
                        self._writer = None
                        self._connected.clear()
                try:
                    writer.close()
                except Exception:
                    pass
```

- [ ] **Step 6: _on_ready 改为 async + 重放缓冲区（用 _skip_buffer=True 避免重复缓冲）**

当前 `_on_ready`（行 212-217）替换为：
```python
    async def _on_ready(self, msg: dict):
        """处理 READY 指令 — 记录适配器信息 + 重放缓冲消息"""
        with self._lock:
            self._adapter_name = msg.get("adapter", "im")
            self._push_target = msg.get("push_target")
            logger.info(f"[IMGateway] Adapter ready: {self._adapter_name}, push_target={self._push_target}")
        if self._send_buffer:
            logger.info(f"[IMGateway] Replaying {len(self._send_buffer)} buffered messages")
            replayed = list(self._send_buffer)
            for buffered_cmd in replayed:
                try:
                    await self._async_send(buffered_cmd, _skip_buffer=True)
                except Exception as e:
                    logger.error(f"[IMGateway] Replay failed: {e}")
            # 重放完成后清空缓冲区（已全部重放，无需保留）
            self._send_buffer.clear()
```

同时修改 `_dispatch` 中 READY 的调用：
```python
        elif t == "READY":
            await self._on_ready(msg)
```

- [ ] **Step 7: 修改 notify_stream 签名和内容**

当前代码替换为：
```python
    def notify_stream(self, content: str, channel_id: str = "", is_final: bool = False):
        """通知 Adapter 有新内容，携带增量文本供 Adapter 更新流式卡片。"""
        self._send_command({
            "type": "STREAM",
            "channel_id": channel_id,
            "content": content,
            "is_final": is_final,
        })
```

注意：Gateway 不分析这个内容是给谁的，直接透传。Adapter 收到后自己决定怎么处理。

- [ ] **Step 8: 语法验证**

```bash
python -c "from niu_api.channel.gateway import IMGateway; print('OK')"
```

---

## Task 2: 修改 chat_queue.py + runner.py — channel_id 透传 + STREAM 通知

**Files:**
- Modify: `niu_api/chat_queue.py`
- Modify: `agent/runner.py`

- [ ] **Step 1: chat_queue.py — _process_single 签名增加 channel_id**

当前 `_process_single` 签名（约行 278）：
```python
async def _process_single(self, content: str, session_id: str = "default",
                          user_contents: list[str] | None = None, channel: str = "electron") -> str:
```

改为：
```python
async def _process_single(self, content: str, session_id: str = "default",
                          user_contents: list[str] | None = None, channel: str = "electron",
                          channel_id: str = "") -> str:
```

- [ ] **Step 2: chat_queue.py — _process_with_merge 调用 _process_single 时传入 channel_id**

当前调用（约行 246）：
```python
reply = await self._process_single(merged_content, first_req.session_id, all_contents,
                                   channel=first_req.channel)
```

改为：
```python
reply = await self._process_single(merged_content, first_req.session_id, all_contents,
                                   channel=first_req.channel, channel_id=first_req.channel_id)
```

- [ ] **Step 3: chat_queue.py — _process_single 内部调用 runner.chat() 时传入 channel_id**

当前 `_process_single` 内部调用 runner.chat() 的位置（约行 311-313）：
```python
for chunk in self._runner.chat(session_id, content, stream=False, history=history_for_runner):
```

改为：
```python
for chunk in self._runner.chat(session_id, content, stream=False, history=history_for_runner, channel_id=channel_id):
```

注意：需要搜索 `_process_single` 中所有调用 `self._runner.chat()` 的位置，确保都传入 `channel_id`。

- [ ] **Step 4: runner.py — chat() 签名增加 channel_id**

当前签名（约行 1730）：
```python
def chat(self, session_id: str, user_input: str, stream: bool = True,
         max_turns: int = 40, history: list = None, resources: list | None = None):
```

改为：
```python
def chat(self, session_id: str, user_input: str, stream: bool = True,
         max_turns: int = 40, history: list = None, resources: list | None = None,
         channel_id: str = ""):
```

- [ ] **Step 5: runner.py — chat() 方法体内存储 channel_id**

在 `chat()` 方法体开头（`gen = agent_runner_loop(...)` 之前），添加：
```python
        # 安全性：_chat_lock 保证 runner.chat() 严格串行执行，
        # 因此 _current_channel_id 不会被并发覆盖。
        self._current_channel_id = channel_id
```

在 `chat()` 方法的 `finally` 块中清理残留值：
```python
        self._current_channel_id = ""
```

- [ ] **Step 6: runner.py — 在流式生成器中发送 STREAM 通知（携带 content + channel_id）**

在 runner.py 消费 StreamEvent 的循环中，`chunk.type == "reply"` 且 `chunk.content` 非空时，发送 STREAM 通知：

```python
                                # 流式 chunk 通知 Gateway
                                try:
                                    from niu_api.channel.gateway import get_im_gateway
                                    _gw = get_im_gateway()
                                    if _gw and _gw.is_connected:
                                        _gw.notify_stream(chunk.content, channel_id=self._current_channel_id)
                                except Exception:
                                    pass
```

- [ ] **Step 7: runner.py — 在流式生成器结束时发送 is_final=true 的 STREAM 通知**

在 `chat()` 方法的流式循环结束后（`while True` 循环的 `finally` 块或循环正常退出后），发送终结通知：

```python
                                # 流式结束通知
                                try:
                                    from niu_api.channel.gateway import get_im_gateway
                                    _gw = get_im_gateway()
                                    if _gw and _gw.is_connected:
                                        _gw.notify_stream("", channel_id=self._current_channel_id, is_final=True)
                                except Exception:
                                    pass
```

- [ ] **Step 8: runner.py — 删除 _persist_one_msg 中的旧 notify_stream("") 调用**

删除 `_persist_one_msg` 中的旧 `notify_stream("")` 调用。旧方案是"通知有新内容"的信号式推送（不含内容，Adapter 需自行拉取），新方案是"直接传内容"的增量式推送（每个 chunk 都携带内容），两者机制不同，不能并存。

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
- Create: `im-adapters/feishu/src/niu_feishu_adapter/stream_card.py`
- Create: `im-adapters/feishu/src/niu_feishu_adapter/media.py`

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
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"
```

- [ ] **Step 3: __init__.py**

```python
"""飞书 IM Adapter — 通过 TCP 连接 IM Gateway"""
```

- [ ] **Step 4: __main__.py — 从环境变量读取所有配置，不用 argparse**

```python
"""飞书 Adapter 入口 — python -m niu_feishu_adapter

退出码约定：
  0 — 正常退出（不应被重启）
  1 — 瞬时错误（Gateway 可重启）
  2 — 永久错误（配置缺失，不应重启）
"""
import asyncio
import os
import sys

from loguru import logger

EXIT_TRANSIENT = 1
EXIT_PERMANENT = 2


def main():
    # 从环境变量读取所有配置（不使用命令行参数，避免 ps 泄露）
    adapter_type = os.environ.get("NIU_IM_ADAPTER", "")
    if adapter_type != "feishu":
        logger.error(f"NIU_IM_ADAPTER={adapter_type}, expected 'feishu'")
        sys.exit(EXIT_PERMANENT)

    gateway_port_str = os.environ.get("NIU_GATEWAY_PORT", "")
    if not gateway_port_str:
        logger.error("Missing NIU_GATEWAY_PORT environment variable")
        sys.exit(EXIT_PERMANENT)
    try:
        gateway_port = int(gateway_port_str)
    except ValueError:
        logger.error(f"Invalid NIU_GATEWAY_PORT: {gateway_port_str}")
        sys.exit(EXIT_PERMANENT)

    app_id = os.environ.get("NIU_FEISHU_APP_ID", "")
    app_secret = os.environ.get("NIU_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.error("Missing NIU_FEISHU_APP_ID or NIU_FEISHU_APP_SECRET")
        sys.exit(EXIT_PERMANENT)

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
        sys.exit(EXIT_TRANSIENT)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: adapter.py — 修正 SDK 回调签名 + 并发写入保护 + 流式卡片按 channel 隔离 + 消息大小限制**

```python
"""飞书 Adapter 主类 — TCP Client + 飞书 SDK

职责：
1. 连接 Gateway（TCP Client）
2. 收到飞书消息 → 下载附件 → 构造 MSG 发给 Gateway
3. 收到 Gateway 的 SEND → 解析 Markdown → 上传图片/文件 → 发飞书消息
4. 收到 Gateway 的 STREAM → 更新/创建飞书流式卡片
5. 收到 Gateway 的 PUSH → 发飞书消息（Markdown 格式）
"""
import asyncio
import json
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB，与 Gateway 一致


class StreamState:
    """单个 channel 的流式卡片状态"""
    __slots__ = ("card_id", "seq", "receive_id", "pending_images", "accumulated_text")

    def __init__(self, card_id: str, seq: int, receive_id: str):
        self.card_id = card_id
        self.seq = seq
        self.receive_id = receive_id
        self.pending_images: list[dict] = []  # [{"img_key": "xxx", "alt": "照片", "local_path": "/path"}]
        self.accumulated_text: str = ""       # 含 [PHOTO_SEP] 的累积文本


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
        self._client = None  # lark.Client
        # 流式卡片状态：按 channel_id 隔离
        self._stream_states: dict[str, StreamState] = {}

    async def run(self):
        """主循环"""
        self._loop = asyncio.get_running_loop()
        self._init_feishu_sdk()
        await self._connect_gateway()
        await self._send_ready()
        self._start_feishu_listener()
        await self._read_loop()

    # ── 飞书 SDK ──

    def _init_feishu_sdk(self):
        """初始化飞书 SDK Client"""
        import lark_oapi as lark
        self._client = lark.Client.builder() \
            .app_id(self._app_id) \
            .app_secret(self._app_secret) \
            .log_level(lark.LogLevel.DEBUG) \
            .build()
        logger.info("[FeishuAdapter] SDK initialized")

    def _start_feishu_listener(self):
        """启动飞书 WebSocket 事件监听（独立线程）"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        def on_message(data: P2ImMessageReceiveV1) -> None:
            """飞书消息回调 — SDK 传入 P2ImMessageReceiveV1 对象"""
            try:
                asyncio.run_coroutine_threadsafe(
                    self._on_feishu_message(data), self._loop
                )
            except Exception as e:
                logger.error(f"[FeishuAdapter] Event handler error: {e}")

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(on_message) \
            .build()

        ws_client = lark.ws.Client(
            self._app_id, self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.DEBUG,
        )
        thread = threading.Thread(target=ws_client.start, daemon=True)
        thread.start()
        logger.info("[FeishuAdapter] Event listener started")

    async def _on_feishu_message(self, data):
        """飞书消息回调 → 下载附件 → 构造 MSG → 发给 Gateway

        data 是 P2ImMessageReceiveV1 对象，属性访问而非 dict。
        """
        try:
            message = data.event.message
            sender = data.event.sender
        except AttributeError:
            logger.error("[FeishuAdapter] Malformed event data")
            return

        content_str = message.content or "{}"
        chat_id = message.chat_id or ""
        sender_id = sender.sender_id.open_id if sender.sender_id else ""
        msg_type = message.message_type or "text"
        is_group = message.chat_type == "group"

        if msg_type == "text":
            try:
                text = json.loads(content_str).get("text", "")
            except Exception:
                text = content_str
        elif msg_type == "image":
            text = await self._download_image_and_markdown(content_str)
        elif msg_type == "file":
            text = await self._download_file_and_markdown(content_str)
        else:
            text = f"[不支持的消息类型: {msg_type}]"

        await self._send({
            "type": "MSG",
            "content": text,
            "channel_id": chat_id,
            "sender_id": sender_id,
            "session_id": f"feishu:{sender_id}",
            "is_group": is_group,
        })

    async def _download_image_and_markdown(self, content_str: str) -> str:
        from niu_feishu_adapter.media import download_image
        try:
            image_key = json.loads(content_str).get("image_key", "")
            if not image_key:
                return "[图片: 无 image_key]"
            local_path = await download_image(self._client, image_key)
            return f"![图片]({local_path})"
        except Exception as e:
            logger.error(f"[FeishuAdapter] Download image failed: {e}")
            return f"[图片下载失败: {e}]"

    async def _download_file_and_markdown(self, content_str: str) -> str:
        from niu_feishu_adapter.media import download_file
        try:
            content_json = json.loads(content_str)
            file_key = content_json.get("file_key", "")
            file_name = content_json.get("file_name", "unknown")
            if not file_key:
                return f"[文件: 无 file_key]"
            local_path = await download_file(self._client, file_key, file_name)
            return f"[{file_name}]({local_path})"
        except Exception as e:
            logger.error(f"[FeishuAdapter] Download file failed: {e}")
            return f"[文件下载失败: {e}]"

    # ── Gateway TCP 连接 ──

    async def _connect_gateway(self):
        self._reader, self._writer = await asyncio.open_connection(
            "127.0.0.1", self._gateway_port
        )
        logger.info(f"[FeishuAdapter] Connected to Gateway 127.0.0.1:{self._gateway_port}")

    async def _send_ready(self):
        await self._send({
            "type": "READY",
            "adapter": "feishu",
            "push_target": self._push_chat_id,
        })

    async def _read_loop(self):
        try:
            while True:
                header = await self._reader.readexactly(4)
                length = int.from_bytes(header, "big")
                if length > MAX_MESSAGE_SIZE:
                    logger.error(f"[FeishuAdapter] Message too large: {length} bytes, dropping connection")
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
        if t == "SEND":
            await self._on_send(cmd)
        elif t == "PUSH":
            await self._on_push(cmd)
        elif t == "STREAM":
            await self._on_stream(cmd)
        elif t == "PONG":
            pass

    async def _on_send(self, cmd: dict):
        """SEND = Agent 最终回复 → 终结流式卡片（图片嵌入卡片 body，不发独立消息）

        飞书 Markdown 不支持 ![alt](url) 内嵌图片，图片只能通过卡片的
        {"tag": "img", "img_key": "xxx"} 元素展示。终结卡片时把完整内容
        （含图片 img_key）写入卡片 body elements。
        如果没有流式卡片（异常情况），发一条纯 Markdown 文本消息（图片标记被剥离）。
        """
        receive_id = cmd.get("channel_id", "")
        content = cmd.get("content", "")
        state = self._stream_states.pop(receive_id, None)
        if state:
            # 正常路径：终结流式卡片，图片嵌入卡片 body
            from niu_feishu_adapter.stream_card import finalize_stream_card
            state.seq += 1
            await finalize_stream_card(
                self._client, state.card_id, content, state.seq,
                pending_images=state.pending_images,
                accumulated_text=state.accumulated_text,
            )
        else:
            # 异常路径：没有流式卡片，发纯 Markdown 文本（剥离图片标记）
            from niu_feishu_adapter.media import strip_media_markers, send_text_message
            text = strip_media_markers(content)
            if text:
                await send_text_message(self._client, receive_id, text)

    async def _on_push(self, cmd: dict):
        """PUSH = 定时任务主动推送 → 纯 Markdown 文本消息（不含图片）"""
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        from niu_feishu_adapter.media import send_text_message
        await send_text_message(self._client, receive_id, content)

    async def _on_stream(self, cmd: dict):
        """STREAM = 流式增量内容 → 创建/更新飞书卡片（按 channel_id 隔离）

        流式推送过程中：
        - 遇到 Markdown 图片标记 → 上传图片到飞书拿 image_key → 存入 pending_images
        - 图片标记替换为 [PHOTO_SEP] 分隔符 → 终结时用于拆分文本+插入 img 元素
        - 文本内容更新到卡片的 markdown 元素
        """
        receive_id = cmd.get("channel_id", "") or self._push_chat_id
        content = cmd.get("content", "")
        is_final = cmd.get("is_final", False)
        from niu_feishu_adapter.stream_card import create_stream_card, update_stream_card, finalize_stream_card
        from niu_feishu_adapter.media import filter_media_in_stream

        state = self._stream_states.get(receive_id)
        if not state:
            # 首次：过滤图片 + 创建卡片
            filtered_content, pending_images = await filter_media_in_stream(self._client, content, [])
            card_id = await create_stream_card(self._client, receive_id, filtered_content)
            self._stream_states[receive_id] = StreamState(card_id, 0, receive_id)
            self._stream_states[receive_id].pending_images = pending_images
            self._stream_states[receive_id].accumulated_text = filtered_content
        else:
            # 后续：过滤图片 + 累积文本 + 更新卡片
            filtered_content, new_images = await filter_media_in_stream(
                self._client, content, state.pending_images
            )
            state.pending_images = new_images
            state.accumulated_text += filtered_content
            state.seq += 1
            # 更新卡片显示（不含 [PHOTO_SEP]）
            display_content = state.accumulated_text.replace("[PHOTO_SEP]", "")
            await update_stream_card(self._client, state.card_id, display_content, state.seq)

        if is_final:
            state = self._stream_states.pop(receive_id, None)
            if state:
                state.seq += 1
                await finalize_stream_card(
                    self._client, state.card_id, content, state.seq,
                    pending_images=state.pending_images,
                    accumulated_text=state.accumulated_text,
                )

    # ── 通用发送（带并发写入保护）──

    async def _send(self, cmd: dict):
        """发送指令给 Gateway，带 _write_lock 防止并发写入破坏 TCP 帧格式"""
        if not self._writer:
            return
        async with self._write_lock:
            payload = json.dumps(cmd, ensure_ascii=False).encode("utf-8")
            header = len(payload).to_bytes(4, "big")
            self._writer.write(header + payload)
            await self._writer.drain()
```

- [ ] **Step 6: stream_card.py — 流式卡片创建/更新/终结，终结时图片嵌入卡片 body**

```python
"""飞书流式卡片管理

终结卡片时的图片嵌入逻辑（参考原 _build_final_card_body）：
1. accumulated_text 中含 [PHOTO_SEP] 分隔符（由 filter_media_in_stream 替换而来）
2. 按 [PHOTO_SEP] 拆分文本，交替插入 markdown + img 元素
3. img 元素使用 image_key 引用，不依赖 URL
"""
import json
from loguru import logger


async def create_stream_card(client, receive_id: str, initial_content: str) -> str:
    """创建飞书流式卡片，返回 card_id"""
    card = {
        "schema": "2.0",
        "header": {
            "title": {"content": "Niu助手", "tag": "plain_text"},
            "subtitle": {"content": "思考中...", "tag": "plain_text"},
        },
        "config": {
            "streaming_mode": True,
            "update_multi": True,
        },
        "body": {
            "elements": [{"tag": "markdown", "content": initial_content, "element_id": "md1"}],
        },
    }
    card_json = json.dumps(card, ensure_ascii=False)
    # TODO: 实现 — 使用 CardKit API 创建卡片实体，发送卡片消息
    logger.debug(f"[StreamCard] Creating card for {receive_id}")
    return ""


async def update_stream_card(client, card_id: str, content: str, seq: int):
    """更新流式卡片内容（Element 级更新）"""
    # TODO: 实现 — 使用 CardKit UpdateElement API
    logger.debug(f"[StreamCard] Update card={card_id} seq={seq}")


async def finalize_stream_card(client, card_id: str, final_content: str, seq: int,
                                pending_images: list | None = None,
                                accumulated_text: str = ""):
    """终结流式卡片：Settings API 关闭 streaming_mode → UpdateCard 写入完整内容

    final_content: 完整的 Markdown 内容（可能含图片标记）
    pending_images: [{"img_key": "xxx", "alt": "照片", "local_path": "/path"}]
    accumulated_text: 流式过程中累积的文本（含 [PHOTO_SEP] 分隔符）
    """
    # 1. Settings API 关闭 streaming_mode
    # TODO: 实现

    # 2. 构建终结卡片的 body elements
    if pending_images and "[PHOTO_SEP]" in accumulated_text:
        elements = _build_final_card_body(accumulated_text, pending_images)
    else:
        # 没有图片，直接用 markdown 元素
        display_content = final_content
        if len(display_content) > 18000:
            display_content = display_content[:17900] + "\n\n...[内容已截断]"
        elements = [{"tag": "markdown", "content": display_content, "element_id": "md1"}]

    final_card = {
        "schema": "2.0",
        "header": {
            "title": {"content": "Niu助手", "tag": "plain_text"},
            "subtitle": {"content": "", "tag": "plain_text"},
        },
        "config": {"streaming_mode": False, "update_multi": True},
        "body": {"elements": elements},
    }
    final_json = json.dumps(final_card, ensure_ascii=False)
    # TODO: 实现 — 使用 CardKit UpdateCard API 更新卡片
    logger.debug(f"[StreamCard] Finalize card={card_id} seq={seq}")


def _build_final_card_body(final_text: str, pending_images: list) -> list:
    """构建终结卡片的 body elements：markdown + img 交替排列

    按 [PHOTO_SEP] 拆分文本，在文本片段之间插入对应的 img 元素。
    这是原来 _build_final_card_body 的逻辑，保证图片嵌入卡片而非独立消息。
    """
    elements = []
    parts = final_text.split("[PHOTO_SEP]")
    md_idx = 1
    img_idx = 0
    for i, part in enumerate(parts):
        part = part.strip()
        if part:
            if len(part) > 18000:
                part = part[:17900] + "\n\n...[内容已截断]"
            elements.append({"tag": "markdown", "content": part, "element_id": f"md{md_idx}"})
            md_idx += 1
        if i < len(pending_images):
            img_info = pending_images[i]
            elements.append({
                "tag": "img",
                "img_key": img_info["img_key"],
                "alt": {"tag": "plain_text", "content": img_info.get("alt", "照片")},
                "element_id": f"img_{img_idx}",
            })
            img_idx += 1
    if not elements:
        elements.append({"tag": "markdown", "content": final_text.replace("[PHOTO_SEP]", ""), "element_id": "md1"})
    return elements
```

- [ ] **Step 7: media.py — 图片上传 + 流式图片过滤 + 文本消息发送**

```python
"""飞书媒体下载与上传

核心原则：只有流式卡片一条通道，不搞独立图片消息。
- 流式推送中遇到图片 → 上传拿 image_key → 存入 pending_images → 替换为 [PHOTO_SEP]
- 终结卡片时 → 按 [PHOTO_SEP] 拆分 → 交替插入 markdown + img 元素
- 异常回退（无流式卡片）→ 剥离图片标记，发纯 Markdown 文本
"""
import json
import re
from pathlib import Path
from loguru import logger

TEMP_DIR = Path.home() / ".niu" / "tmp"
_LOCAL_PATH_PREFIX = str(TEMP_DIR)


# ── 入方向：飞书 → 本地 ──

async def download_image(client, image_key: str) -> str:
    """下载飞书图片到本地"""
    local_path = TEMP_DIR / f"feishu_img_{image_key}.jpg"
    if local_path.exists():
        return str(local_path)
    # TODO: 实现 — 使用飞书 API 下载图片
    return str(local_path)


async def download_file(client, file_key: str, filename: str) -> str:
    """下载飞书文件到本地"""
    local_path = TEMP_DIR / f"feishu_file_{file_key}_{filename}"
    if local_path.exists():
        return str(local_path)
    # TODO: 实现 — 使用飞书 API 下载文件
    return str(local_path)


# ── 出方向：本地 → 飞书 ──

async def upload_image_to_feishu(client, local_path: str) -> str | None:
    """上传本地图片到飞书，返回 image_key（只上传不发送消息）"""
    # TODO: 实现 — POST https://open.feishu.cn/open-apis/im/v1/images
    return None


async def upload_file_to_feishu(client, local_path: str, filename: str) -> str | None:
    """上传本地文件到飞书，返回 file_key（只上传不发送消息）"""
    # TODO: 实现
    return None


async def filter_media_in_stream(client, content: str, existing_images: list) -> tuple[str, list[dict]]:
    """流式推送中处理图片标记：上传 → 替换为 [PHOTO_SEP]

    返回 (filtered_content, updated_pending_images)。
    filtered_content 中的 ![alt](local_path) 被替换为 [PHOTO_SEP]。
    updated_pending_images 追加了新上传的图片信息。
    """
    pending_images = list(existing_images)
    replacements: list[tuple[int, int, str]] = []

    for m in re.finditer(r'!\[([^\]]*)\]\(([^)]+)\)', content):
        alt = m.group(1)
        path = m.group(2)
        if not path.startswith(_LOCAL_PATH_PREFIX):
            replacements.append((m.start(), m.end(), ""))
            continue
        if not Path(path).exists():
            replacements.append((m.start(), m.end(), ""))
            continue
        img_key = await upload_image_to_feishu(client, path)
        if img_key:
            pending_images.append({"img_key": img_key, "alt": alt or "照片", "local_path": path})
            replacements.append((m.start(), m.end(), "[PHOTO_SEP]"))
        else:
            replacements.append((m.start(), m.end(), ""))

    # 从后向前替换，避免偏移
    for start, end, repl in sorted(replacements, key=lambda x: x[0], reverse=True):
        content = content[:start] + repl + content[end:]

    return content, pending_images


def strip_media_markers(content: str) -> str:
    """剥离 Markdown 图片标记，替换为文字提示（用于异常回退：发纯 Markdown 文本）"""
    # 替换 ![alt](path) 为 ↑ alt的照片
    def _replace_image(m):
        alt = m.group(1) or "照片"
        return f"↑ {alt}"
    content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', _replace_image, content)
    # 替换 [name](local_path) 为 ↑ name（仅本地路径）
    def _replace_file(m):
        path = m.group(2)
        if path.startswith(_LOCAL_PATH_PREFIX):
            return f"↑ {m.group(1)}"
        return m.group(0)
    content = re.sub(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)', _replace_file, content)
    return content


async def send_text_message(client, receive_id: str, text: str):
    """发送飞书 Markdown 文本消息（用于 PUSH 和异常回退）"""
    # TODO: 实现 — 使用飞书 SDK 的 channel.send(target, {"markdown": content})
    logger.debug(f"[Media] Send text to {receive_id}")
```

- [ ] **Step 8: 语法验证**

```bash
cd im-adapters/feishu/src && PYTHONPATH=. python -c "from niu_feishu_adapter.adapter import FeishuAdapter; print('OK')"
```

---

## 验证清单

每个 Task 完成后：
1. 语法验证通过

所有 Task 完成后：
2. 配置 preferences.json 添加 `im.adapter: "feishu"`
3. 启动 `./niu`，检查日志：
   - Gateway 启动成功
   - 识别 adapter 类型 "feishu"
   - 设置 PYTHONPATH 成功
   - 读取飞书凭证成功
   - 启动 Adapter 子进程
4. 检查 Adapter 连接：READY 指令发送成功
5. 杀掉所有 niu 进程

---

## 审查检查项

- [ ] C1: 无 ADAPTER_COMMANDS 硬编码，启动命令从 im.adapter 自动推导
- [ ] C2: _launch_adapter 设置 PYTHONPATH 包含 adapter workdir
- [ ] C3: 凭证和端口统一通过环境变量传递（NIU_FEISHU_APP_ID、NIU_GATEWAY_PORT 等），不用命令行参数
- [ ] C4: 缺少 app_id/app_secret 时记录错误跳过（不崩溃）
- [ ] C5: Adapter exit code 2 = 永久错误（不重启），exit code 1 = 瞬时错误（重启）
- [ ] C6: 消息缓冲区 deque(maxlen=10) 仅缓冲 SEND/PUSH
- [ ] C7: _on_ready 重放时用 _skip_buffer=True 避免重复缓冲，重放后 clear()
- [ ] C8: _on_ready 改为 async
- [ ] C9: STREAM 通知包含 content、is_final、channel_id
- [ ] C10: notify_stream 新签名 (content, channel_id="", is_final=False)
- [ ] C11: Gateway 不分析内容归属，直接透传
- [ ] C12: Adapter 目录在 im-adapters/feishu/
- [ ] C13: Adapter 只做透传：Gateway 给啥就传给飞书，飞书给啥就传给 Gateway
- [ ] C14: 流式推送一条通道，无独立图片/文件消息（不发 msg_type="image"/"file"）
- [ ] C14a: _on_send 只终结流式卡片，不发独立消息；异常回退发纯 Markdown 文本（图片标记替换为文字提示）
- [ ] C14b: 流式卡片终结时图片嵌入卡片 body（_build_final_card_body：markdown + img 交替排列）
- [ ] C15: self._loop 在 _start_feishu_listener 之前设置
- [ ] C16: 无端口冲突
- [ ] C17: Adapter _send 有 _write_lock 并发写入保护
- [ ] C18: Adapter _read_loop 有 MAX_MESSAGE_SIZE 检查
- [ ] C19: 流式卡片状态按 channel_id 隔离（dict），SEND 终结时只终结匹配的 channel
- [ ] C20: 飞书 SDK 回调签名正确（P2ImMessageReceiveV1 对象，属性访问）
- [ ] C21: runner.py 流式生成器结束时发送 is_final=true 的 STREAM 通知
- [ ] C22: runner.py 中 STREAM 通知携带 channel_id（通过 self._current_channel_id）
- [ ] C22a: chat_queue.py _process_single 签名增加 channel_id 参数
- [ ] C22b: chat_queue.py _process_with_merge 调用 _process_single 时传入 first_req.channel_id
- [ ] C22c: chat_queue.py _process_single 内部调用 runner.chat() 时传入 channel_id
- [ ] C22d: runner.py chat() 签名增加 channel_id 参数，存入 self._current_channel_id
- [ ] C23: media.py 无 parse_and_send 函数，改为 filter_media_in_stream（流式图片处理）+ strip_media_markers（异常回退）
- [ ] C24: media.py filter_media_in_stream 只匹配 ~/.niu/tmp/ 下的本地路径，替换为 [PHOTO_SEP]
- [ ] C25: stream_card.py finalize_stream_card 接收 pending_images + accumulated_text，构建 markdown+img 交替 body
