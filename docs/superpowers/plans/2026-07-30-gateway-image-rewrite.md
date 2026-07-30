# Gateway 图片格式改写实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Gateway 端把不支持图片格式的 `![](path)` 改写为 `[文件名](path)`，让 IM adapter 自然走文件上传路径。

**Architecture:** 在 `IMGateway.send()`、`push()`、`notify_stream()` 三个方法中，发送 content 前调用 `_rewrite_unsupported_images(content)`，把扩展名不在支持列表中的 `![alt](path)` 改写为 `[文件名](path)`。adapter 端零改动。

**Tech Stack:** Python 3.11, 括号平衡解析

---

## 已验证的关键事实

1. **Gateway 三个出口**：`send()`（L353）、`push()`（L363）、`notify_stream()`（L372）都把 content 原样发给 adapter
2. **飞书 adapter 用 Markdown 语法区分图片/文件**：`![](path)` → 图片上传，`[](path)` → 文件上传
3. **飞书图片 API 支持**：png、jpeg、gif、webp、bmp。不支持 SVG 等
4. **`_on_stream` 也需要处理**：流式阶段 `create_card` 把 Markdown 原样放入卡片，`![](local_path)` 会被飞书尝试渲染为图片但因 local_path 非有效 image_key/URL 导致破损占位图
5. **改动只在 `niu_api/channel/gateway.py` 一个文件**，adapter 端零改动

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/channel/gateway.py` | 新增 `_rewrite_unsupported_images()` + 在 send/push/notify_stream 中调用 | 修改 |

---

### Task 1: 实现 _rewrite_unsupported_images 并在三个出口调用

**Files:**
- Modify: `niu_api/channel/gateway.py`

- [ ] **Step 1: 实现 _rewrite_unsupported_images 方法**

在 `IMGateway` 类中，`send_media` 方法之后（约 L388），新增：

```python
    # 飞书等 IM 图片 API 支持的格式
    _SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    @classmethod
    def _rewrite_unsupported_images(cls, content: str) -> str:
        """把不支持图片格式的 ![alt](path) 改写为 [文件名](path)。

        IM adapter 用 ![] 语法判断图片上传、[] 语法判断文件上传。
        SVG 等不支持格式如果走图片路径，飞书 API 会拒绝。
        改写为文件链接语法后，adapter 自然走文件上传。

        使用括号平衡解析（与飞书 adapter 的 extract_md_refs 一致），
        正确处理路径中含括号的情况。仅改写本地文件路径，不影响 URL 和 data URI。
        """
        import os
        result = []
        last_end = 0
        i = 0
        while i < len(content):
            # 检测 ![
            if content[i] == '!' and i + 1 < len(content) and content[i + 1] == '[':
                start = i
                i += 2
            else:
                i += 1
                continue

            # 找 alt_text（到第一个 ]）
            alt_start = i
            while i < len(content) and content[i] != ']':
                i += 1
            if i >= len(content):
                continue
            alt_text = content[alt_start:i]
            i += 1  # 跳过 ]

            # 必须紧跟 (
            if i >= len(content) or content[i] != '(':
                continue
            i += 1  # 跳过 (

            # 括号平衡找 path
            path_start = i
            depth = 1
            while i < len(content) and depth > 0:
                if content[i] == '(':
                    depth += 1
                elif content[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            path = content[path_start:i - 1]

            # URL / data URI 不改写
            if path.startswith(("http://", "https://", "ftp://", "data:", "mailto:")):
                continue

            # 检查扩展名
            ext = os.path.splitext(path)[1].lower()
            if ext in cls._SUPPORTED_IMAGE_EXTS:
                continue  # 支持的格式，不改写

            # 不支持的格式，改写为文件链接
            filename = os.path.basename(path) or alt_text or "文件"
            # 追加改写前的内容
            result.append(content[last_end:start])
            result.append(f"[{filename}]({path})")
            last_end = i

        result.append(content[last_end:])
        return "".join(result)
```

- [ ] **Step 2: 在 send() 中调用**

修改 `send()` 方法（L353-361），在 `await self._async_send(...)` 前加一行：

当前：
```python
    async def send(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot send")
            return
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        await self._async_send({"type": "SEND", "channel_id": channel_id, "content": content, "reply_to_id": reply_to_id})
        with self._lock:
            self._reply_to_ids.pop(channel_id, None)
```

改为：
```python
    async def send(self, channel_id: str, content: str) -> None:
        if not self._connected.is_set():
            logger.debug("[IMGateway] Adapter not connected, cannot send")
            return
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        content = self._rewrite_unsupported_images(content)
        await self._async_send({"type": "SEND", "channel_id": channel_id, "content": content, "reply_to_id": reply_to_id})
        with self._lock:
            self._reply_to_ids.pop(channel_id, None)
```

- [ ] **Step 3: 在 push() 中调用**

修改 `push()` 方法（L363-370），在 `await self._async_send(...)` 前加一行：

当前：
```python
    async def push(self, channel_id: str, content: str) -> None:
        with self._lock:
            target = channel_id or self._push_target or ""
            connected = self._connected.is_set()
        if not connected:
            logger.debug("[IMGateway] Adapter not connected, cannot push")
            return
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})
```

改为：
```python
    async def push(self, channel_id: str, content: str) -> None:
        with self._lock:
            target = channel_id or self._push_target or ""
            connected = self._connected.is_set()
        if not connected:
            logger.debug("[IMGateway] Adapter not connected, cannot push")
            return
        content = self._rewrite_unsupported_images(content)
        await self._async_send({"type": "PUSH", "channel_id": target, "content": content})
```

- [ ] **Step 4: 在 notify_stream() 中调用**

修改 `notify_stream()` 方法（L372-382），在 `self._send_command(...)` 前加一行：

当前：
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
```

改为：
```python
    def notify_stream(self, content: str, channel_id: str = "", is_final: bool = False):
        """通知 Adapter 有新增量内容"""
        with self._lock:
            reply_to_id = self._reply_to_ids.get(channel_id, "")
        content = self._rewrite_unsupported_images(content)
        self._send_command({
            "type": "STREAM",
            "channel_id": channel_id,
            "content": content,
            "is_final": is_final,
            "reply_to_id": reply_to_id,
        })
```

- [ ] **Step 5: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check niu_api/channel/gateway.py`
Expected: OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/channel/gateway.py
git commit -m "feat(gateway): 不支持图片格式自动改写为文件链接语法"
```

---

### Task 2: 运行环境验证

**Files:** 无（手动验证）

- [ ] **Step 1: 重启应用**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

- [ ] **Step 2: 通过飞书测试**

向飞书 Agent 发消息："帮我看看扫地机的地图"

预期：飞书中以文件形式收到扫地机地图 SVG（可下载），不是空白卡片

- [ ] **Step 3: 确认 DevTools 无错误**

飞书 adapter 日志（`~/.niu/logs/im_adapter_stderr.log`）不应有 `card contains invalid image keys` 错误

---

## Self-Review

### 1. Spec coverage
- ✅ Gateway 端区分格式 → `_rewrite_unsupported_images()` 在 send/push/notify_stream 三个出口调用
- ✅ 不支持的走文件 → `![](path.svg)` 改写为 `[文件名](path.svg)`
- ✅ 支持的保持不变 → PNG/JPG/GIF/WEBP/BMP 不改写
- ✅ adapter 端零改动
- ✅ stream 路径也覆盖 → notify_stream 也调用
- ✅ 括号平衡解析 → 与飞书 adapter 的 extract_md_refs 一致，正确处理路径含括号
- ⚠️ 已知限制：push 路径不走 _filter_media，文件链接显示为不可下载的超链接（预存限制，非本次引入）

### 2. Placeholder scan
- 无 TBD/TODO
- 所有代码块完整

### 3. Type consistency
- `_rewrite_unsupported_images(content: str) -> str` 纯函数，classmethod
- `_SUPPORTED_IMAGE_EXTS` 类变量
- 三个出口都在 content 发出前调用
