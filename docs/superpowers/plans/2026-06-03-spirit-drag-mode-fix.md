# Spirit 拖入文件操作模式传递修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Spirit 小女孩拖入文件时 mode（copy/move/reference）信息丢失的问题，确保 LLM 能正确识别并传递 mode 参数到 ingest 工具。

**Architecture:** 双通道修复——前端 spirit.html 增强模式提示文本（LLM 可读），main.js 传递结构化 resources 到后端，后端 compat.py 将 resources 注入 system_prompt 作为明确指令。两个通道互补：文本提示让 LLM 自然推断 mode，结构化数据作为可靠 fallback。

**Tech Stack:** JavaScript (Electron IPC), Python (FastAPI/Pydantic), LLM prompt engineering

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `ui/assistant/spirit.html:571-586` | 前端拖入处理，生成消息文本 | 修改 |
| `ui/assistant/main.js:703-748` | Electron IPC handler，转发到后端 | 修改 |
| `niu_api/compat.py:458-545` | `/api/chat/session` 端点，接收 resources 并注入 | 修改 |
| `agent/runner.py:857-858` | runner.chat() 签名增加 resources 参数 | 修改 |
| `tests/test_spirit_drag_mode.py` | 新建测试文件 | 新建 |

---

### Task 1: spirit.html — 增强模式提示文本

**Files:**
- Modify: `ui/assistant/spirit.html:571-586`

- [ ] **Step 1: 修改 handleDroppedFiles 中的模式提示文本**

将 `spirit.html` 第 583 行的模式提示从编程术语改为 LLM 易懂的语义描述。

找到这段代码（line 582-583）：

```javascript
      // 构建入库指令（简洁，避免 Agent 当对话回复而不调用工具）
      const message = `入库文件（${mode}模式）：${fileList.map(f => f.path.replace(/\\/g, '/')).join('、')}`;
```

替换为：

```javascript
      // 构建入库指令（简洁，避免 Agent 当对话回复而不调用工具）
      let modeHint = '';
      if (mode === 'reference') {
        modeHint = '（引用模式，使用原路径引用文件，不要拷贝）';
      } else if (mode === 'move') {
        modeHint = '（移动模式，将文件移动到存储目录）';
      }
      const message = `入库文件${modeHint}：${fileList.map(f => f.path.replace(/\\/g, '/')).join('、')}`;
```

效果对比：

| 拖入方式 | 修改前 | 修改后 |
|---------|--------|--------|
| 普通拖入 | `入库文件（copy模式）：/path` | `入库文件：/path` |
| Shift+拖入 | `入库文件（move模式）：/path` | `入库文件（移动模式，将文件移动到存储目录）：/path` |
| Ctrl+拖入 | `入库文件（reference模式）：/path` | `入库文件（引用模式，使用原路径引用文件，不要拷贝）：/path` |

- [ ] **Step 2: 手动验证**

启动应用 `go run main.go`，在 Spirit 小女孩窗口：
1. Ctrl+拖入一个文件 → 确认消息包含"引用模式，使用原路径引用文件，不要拷贝"
2. Shift+拖入一个文件 → 确认消息包含"移动模式，将文件移动到存储目录"
3. 普通拖入一个文件 → 确认消息为"入库文件：/path"（无模式后缀）

- [ ] **Step 3: 提交**

```bash
git add ui/assistant/spirit.html
git commit -m "fix: enhance spirit drag mode hint text — use semantic descriptions instead of programming terms"
```

---

### Task 2: main.js — 传递结构化 resources 到后端

**Files:**
- Modify: `ui/assistant/main.js:703-748`

- [ ] **Step 1: 修改 send-to-agent handler，将 context 以 resources 字段传递**

找到 `main.js` 第 708 行：

```javascript
      const data = JSON.stringify({ message: message });
```

替换为：

```javascript
      // 传递 resources：将拖入文件和模式信息以结构化方式传递给后端
      const resources = (context && context.files)
        ? context.files.map(f => ({
            path: (f.path || f).replace(/\\/g, '/'),
            mode: context.mode || 'copy'
          }))
        : undefined;
      const payload = { message: message };
      if (resources) {
        payload.resources = resources;
      }
      const data = JSON.stringify(payload);
```

注意：`context.files` 中每个元素可能是对象 `{name, size, type, path}` 也可能是字符串路径，所以用 `f.path || f` 兼容两种情况。

- [ ] **Step 2: 验证请求体格式**

在 `main.js` 的 `ipcMain.handle('send-to-agent', ...)` 回调中，第 704 行已有 `console.log`，确认日志中能看到 resources 字段：

```bash
# 启动应用后拖入文件，查看 Electron 控制台日志
# 应看到类似输出：
# 发送消息给 Agent: 入库文件（引用模式，使用原路径引用文件，不要拷贝）：/Users/test/doc.pdf
```

也可以临时在 `req.write(data)` 前加一行 `console.log('Request payload:', data.substring(0, 500));` 来确认 payload 中包含 resources。

- [ ] **Step 3: 提交**

```bash
git add ui/assistant/main.js
git commit -m "fix: pass structured resources with mode to backend in send-to-agent handler"
```

---

### Task 3: 后端 compat.py — 接收 resources 并注入到 system_prompt

**Files:**
- Modify: `niu_api/compat.py:458-545`
- Modify: `agent/runner.py:857-858`

- [ ] **Step 1: 修改 runner.chat() 签名，增加 resources 参数**

找到 `agent/runner.py` 第 857-858 行：

```python
    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None
    ) -> Generator[str, None, None]:
```

替换为：

```python
    def chat(
        self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None, resources: list = None
    ) -> Generator[str, None, None]:
```

然后在 `chat()` 方法体内，找到组装 system_prompt 的位置（line 876-878，紧接在 `if injection: system_prompt += injection` 之后、line 880 `# 组装 tools_schema` 之前）：

```python
        # 组装 system_prompt
        system_prompt = self.base_system_prompt
        if injection:
            system_prompt += injection
```

在 `if injection:` 块**之后**、`# 组装 tools_schema` 行**之前**，插入 resources 注入逻辑：

```python
        # 注入 resources（拖入文件的模式信息）
        if resources:
            # 防御性过滤：只处理格式正确的资源条目
            valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
            if valid_resources:
                resource_lines = []
                for r in valid_resources:
                    path = r.get("path", "")
                    mode = r.get("mode", "copy")
                    if mode == "reference":
                        resource_lines.append(f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用")
                    elif mode == "move":
                        resource_lines.append(f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录")
                    # mode="copy" 不需要额外提示，这是默认行为
                if resource_lines:
                    system_prompt += "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n" + "\n".join(resource_lines)
```

- [ ] **Step 2: 修改 compat.py，将 request.resources 传递给 runner.chat()**

找到 `niu_api/compat.py` 第 509-511 行：

```python
        def sync_chat():
            chunks = []
            for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner):
                chunks.append(chunk)
            return "".join(chunks)
```

替换为：

```python
        def sync_chat():
            chunks = []
            for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner, resources=request.resources or None):
                chunks.append(chunk)
            return "".join(chunks)
```

- [ ] **Step 3: 确认其他端点不需要修改**

以下端点也调用 `runner.chat()`，但**暂不修改**，因为它们当前不接收 resources：

| 端点 | 文件 | 调用方式 | 原因 |
|------|------|---------|------|
| `POST /chat` | `niu_api/chat.py:351` | `runner.chat(session_id, request.message, stream=True)` | SSE 流式前端使用，非 Spirit 路径 |
| `POST /chat/sync` | `niu_api/chat.py:479` | `runner.chat(session_id, request.message, stream=True, history=...)` | 同步前端使用，非 Spirit 路径 |
| ChatQueue worker | `niu_api/chat_queue.py:294` | `self._runner.chat(session_id, content, stream=False, history=...)` | 飞书消息入口，飞书不区分拖入模式 |

`runner.chat()` 新增的 `resources` 参数默认值为 `None`，这些端点不传递 resources 时行为不变。

- [ ] **Step 4: 提交**

```bash
git add agent/runner.py niu_api/compat.py
git commit -m "feat: inject resources with mode info into system_prompt for drag-drop file operations"
```

---

### Task 4: 编写测试

**Files:**
- Create: `tests/test_spirit_drag_mode.py`

- [ ] **Step 1: 编写测试 — 验证 resources 注入到 system_prompt**

```python
"""Test: spirit drag mode fix — resources injection into system_prompt"""
import pytest


def _build_resource_lines(resources):
    """Simulate the injection logic from runner.py — with defensive filtering."""
    if not resources:
        return []
    valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
    if not valid_resources:
        return []
    resource_lines = []
    for r in valid_resources:
        path = r.get("path", "")
        mode = r.get("mode", "copy")
        if mode == "reference":
            resource_lines.append(
                f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用"
            )
        elif mode == "move":
            resource_lines.append(
                f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录"
            )
    return resource_lines


class TestResourcesInjection:
    """Test that resources with mode info are correctly injected into system_prompt."""

    def test_reference_mode_injection(self):
        lines = _build_resource_lines([{"path": "/Users/test/doc.pdf", "mode": "reference"}])
        assert len(lines) == 1
        assert "mode=reference" in lines[0]
        assert "不要拷贝" in lines[0]
        assert "/Users/test/doc.pdf" in lines[0]

    def test_move_mode_injection(self):
        lines = _build_resource_lines([{"path": "/Users/test/file.txt", "mode": "move"}])
        assert len(lines) == 1
        assert "mode=move" in lines[0]
        assert "移动到存储目录" in lines[0]

    def test_copy_mode_no_injection(self):
        """Copy mode is default behavior — no extra instruction generated."""
        lines = _build_resource_lines([{"path": "/Users/test/file.txt", "mode": "copy"}])
        assert len(lines) == 0

    def test_mixed_modes_injection(self):
        """Only non-copy modes generate instructions."""
        lines = _build_resource_lines([
            {"path": "/Users/test/ref.pdf", "mode": "reference"},
            {"path": "/Users/test/move.txt", "mode": "move"},
            {"path": "/Users/test/copy.doc", "mode": "copy"},
        ])
        assert len(lines) == 2
        assert "mode=reference" in lines[0]
        assert "mode=move" in lines[1]

    def test_empty_resources_no_injection(self):
        lines = _build_resource_lines([])
        assert len(lines) == 0

    def test_none_resources_no_injection(self):
        lines = _build_resource_lines(None)
        assert len(lines) == 0

    def test_malformed_resources_filtered(self):
        """Malformed entries (missing path/mode, not dict) are silently filtered."""
        lines = _build_resource_lines([
            {"path": "/Users/test/ref.pdf", "mode": "reference"},
            {"path": "/missing-mode"},          # missing 'mode' key
            {"mode": "move"},                    # missing 'path' key
            "not_a_dict",                        # not a dict
            None,                                # None entry
        ])
        assert len(lines) == 1
        assert "mode=reference" in lines[0]


class TestFullSystemPromptInjection:
    """Test the complete system_prompt assembly with resources."""

    def test_resources_appended_after_skills(self):
        base_prompt = "你是助手。"
        injection = "\n\n【技能】\n某些技能内容"
        resources = [{"path": "/Users/test/doc.pdf", "mode": "reference"}]

        system_prompt = base_prompt
        if injection:
            system_prompt += injection

        lines = _build_resource_lines(resources)
        if lines:
            system_prompt += (
                "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n"
                + "\n".join(lines)
            )

        assert "【技能】" in system_prompt
        assert "【文件操作模式要求】" in system_prompt
        assert "mode=reference" in system_prompt

    def test_no_resources_no_extra_section(self):
        base_prompt = "你是助手。"
        resources = None

        system_prompt = base_prompt
        lines = _build_resource_lines(resources)
        if lines:
            system_prompt += (
                "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n"
                + "\n".join(lines)
            )

        assert "【文件操作模式要求】" not in system_prompt
        assert system_prompt == base_prompt
```

- [ ] **Step 2: 运行测试验证通过**

Run: `cd <repo_root> && python -m pytest tests/test_spirit_drag_mode.py -v`
Expected: 9 tests PASS (7 in TestResourcesInjection + 2 in TestFullSystemPromptInjection)

- [ ] **Step 3: 提交**

```bash
git add tests/test_spirit_drag_mode.py
git commit -m "test: add spirit drag mode fix tests — resources injection into system_prompt"
```

---

### Task 5: 回归测试 — 确认不影响现有单聊和 process-image 路径

**Files:**
- None (verification only)

- [ ] **Step 1: 验证 process-image 路径不受影响**

`process-image` handler（main.js line 619-662）的请求体只有 `session_id` 和 `message`，没有 `resources` 字段。后端 `request.resources` 将为默认空列表 `[]`，传入 `runner.chat()` 的 `resources=None`（因为 `request.resources or None` 对空列表返回 `None`），不会触发注入。确认无误。

- [ ] **Step 2: 验证普通文本消息不受影响**

通过 chat.html 发送普通文本消息，请求体只有 `message` 字段，`resources` 为默认空列表，不触发注入。确认无误。

- [ ] **Step 3: 运行现有测试套件确认无回归**

Run: `cd <repo_root> && python -m pytest tests/ -v --timeout=60 -x -q 2>&1 | tail -30`
Expected: 所有现有测试通过

- [ ] **Step 4: 最终端到端验证**

启动应用 `go run main.go`：
1. Ctrl+拖入一个文件到小女孩 → 消息应包含"引用模式" → 后端 system_prompt 应有 mode=reference 指令 → ingest 应以 mode="reference" 调用
2. Shift+拖入一个文件到小女孩 → 消息应包含"移动模式" → 后端 system_prompt 应有 mode=move 指令
3. 普通拖入 → 消息无模式后缀 → 无额外注入 → 行为不变
4. 在对话窗口拖入文件 → 无模式区分 → 行为不变
5. 普通文本对话 → 无 resources 注入 → 行为不变
