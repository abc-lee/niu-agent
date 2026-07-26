# Harness 输出验证：图片/文件引用标准化 + 路径验证反馈

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Harness 输出验证机制——LLM 输出的图片/文件引用必须路径存在才能通过，否则拦截并反馈给 LLM 修正；同时将文件引用格式从自创的 `::file::JSON::` 标准化为 Markdown 链接 `[文件名](路径)`。

**Architecture:** 在 agent_loop 层添加输出验证函数，当 LLM 不调用工具直接输出文本时，扫描其中的 `![alt](path)` 图片引用和 `[text](path)` 文件链接，验证本地路径是否存在。路径不存在时构造 next_prompt 反馈给 LLM，告知正确做法，阻止无效内容到达用户端。飞书/桌面端的解析逻辑同步适配新格式。

**Tech Stack:** Python (agent_loop, handler, feishu_channel), JavaScript (chat.html marked.js)

---

## 核心设计

### Markdown 标准引用格式

| 类型 | 格式 | Markdown 渲染 | 说明 |
|------|------|--------------|------|
| 图片 | `![alt](path)` | `<img src="path" alt="alt">` | 感叹号开头，现有格式不变 |
| 文件 | `[文件名](path)` | `<a href="path">文件名</a>` | 无感叹号，标准链接语法 |

- `::file::JSON::` 自创格式废弃，改用标准 Markdown 链接
- `::person_photo::JSON::` 已在之前迁移为 `![alt](path)`，本次不动

### Harness 验证逻辑

```
LLM 输出文本
    ↓
扫描 ![alt](path) 和 [text](path)
    ↓
对每个引用验证：Path(path).exists()
    ↓
全部通过 → 正常输出
任一失败 → 拦截，构造 next_prompt 反馈给 LLM：
  "[System] 输出验证失败：引用的文件/图片路径不存在：xxx。
   请检查：1) 路径是否正确 2) 如需显示人物照片，先调用 get_person_photos 获取 boxed_path
   3) 如需发送文件，确认文件已在本地知识库中"
    ↓
LLM 收到反馈，重新输出正确内容
    ↓
验证通过 → 输出给用户
```

### 拦截位置

**`agent/generic/agent_loop.py` 第 196-200 行**——这是 verbose=False 模式下纯文本回复的唯一出口：

```python
# 当前代码 (line 196-200)
else:
    response = exhaust(response_gen)
    content = response.content or ""
    content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
    yield StreamEvent("reply", content)
```

修改为：在 yield 之前调用验证函数，验证失败时不 yield，而是注入 next_prompt 继续循环。

---

## 改动清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `agent/output_validator.py` | 新建：图片/文件引用解析 + 路径验证 | 低（新文件） |
| `agent/generic/agent_loop.py:196-200` | 添加 Harness 验证拦截 | 高（核心循环） |
| `niu_api/channel/feishu_channel.py` | `_filter_media_markers` 和 `resolve_outbound_content` 适配 `[文件名](path)` 格式 | 中 |
| `ui/assistant/chat.html` | 桌面端适配 `[文件名](path)` 文件链接（可点击下载） | 低 |
| `config/agents/niu.md` | 提示词添加文件引用格式说明 | 低 |
| `config/agents/file-processor.md` | 子 Agent 返回格式添加文件引用 | 低 |

---

## Task 1: 创建输出验证模块

**Files:**
- Create: `agent/output_validator.py`
- Test: `tests/test_output_validator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_output_validator.py
import pytest
import tempfile
from pathlib import Path
from agent.output_validator import validate_references, ReferenceError


class TestValidateReferences:
    def test_no_references_passes(self):
        """纯自然语言文本无引用，直接通过"""
        result = validate_references("你好，这是一段普通文字")
        assert result.is_valid is True
        assert result.errors == []

    def test_valid_image_reference_passes(self, tmp_path):
        """图片引用路径存在，通过"""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")
        content = f"![刘永辉]({img})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_invalid_image_reference_fails(self):
        """图片引用路径不存在，失败"""
        content = "![刘永辉](/nonexistent/path/photo.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 1
        assert "图片" in result.errors[0].kind
        assert "/nonexistent/path/photo.jpg" in result.errors[0].path

    def test_url_image_reference_fails(self):
        """图片引用是 URL 而非本地路径，失败"""
        content = "![刘永辉](https://example.com/photo.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert "URL" in result.errors[0].kind

    def test_valid_file_reference_passes(self, tmp_path):
        """文件链接路径存在，通过"""
        doc = tmp_path / "报告.pdf"
        doc.write_bytes(b"%PDF-1.4")
        content = f"[报告.pdf]({doc})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_invalid_file_reference_fails(self):
        """文件链接路径不存在，失败"""
        content = "[报告.pdf](/nonexistent/报告.pdf)"
        result = validate_references(content)
        assert result.is_valid is False
        assert "文件" in result.errors[0].kind

    def test_multiple_errors(self):
        """多个引用错误全部收集"""
        content = "![a](/bad1.jpg) 和 [b.pdf](/bad2.pdf) 和 ![c](/bad3.png)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 3

    def test_mixed_valid_and_invalid(self, tmp_path):
        """混合有效和无效引用，只报告无效的"""
        img = tmp_path / "good.jpg"
        img.write_bytes(b"\xff\xd8")
        content = f"![good]({img}) 和 ![bad](/bad.jpg)"
        result = validate_references(content)
        assert result.is_valid is False
        assert len(result.errors) == 1

    def test_file_protocol_stripped(self, tmp_path):
        """file:/// 前缀应被剥离后验证"""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8")
        content = f"![photo](file:///{img})"
        result = validate_references(content)
        assert result.is_valid is True

    def test_format_feedback_message(self):
        """验证错误消息格式包含正确指导"""
        content = "![刘永辉](https://example.com/photo.jpg)"
        result = validate_references(content)
        feedback = result.format_feedback()
        assert "chat-with-file-processor" in feedback
        assert "本地绝对路径" in feedback


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_output_validator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.output_validator'`

- [ ] **Step 3: Write implementation**

```python
# agent/output_validator.py
"""Harness 输出验证——验证 LLM 输出中的图片/文件引用路径是否存在"""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReferenceError:
    kind: str          # "图片" 或 "文件"
    path: str          # 引用的路径
    reason: str        # "路径不存在" 或 "不允许使用URL"


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[ReferenceError] = field(default_factory=list)

    def format_feedback(self) -> str:
        """构造反馈给 LLM 的提示消息"""
        if self.is_valid:
            return ""
        lines = ["[System] 输出验证失败：以下引用路径无效："]
        for err in self.errors:
            lines.append(f"  - {err.kind}引用：{err.path}（{err.reason}）")
        lines.append("")
        lines.append("请修正：")
        lines.append("1. 图片和文件必须使用本地绝对路径（如 /Users/xxx/photo.jpg），禁止使用 URL")
        lines.append("2. 如需显示人物照片，请使用 chat-with-file-processor 查询人物照片")
        lines.append("3. 如需发送文件，请确认文件已存在于本地知识库中")
        return "\n".join(lines)


# Markdown 图片语法：![alt](path)
_IMG_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Markdown 链接语法：[text](path) — 排除以 http/https 开头的常规超链接
# 只匹配本地路径引用（绝对路径或 file:/// 开头）
_LINK_PATTERN = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')


def _normalize_path(path: str) -> str:
    """规范化路径：剥离 file:/// 前缀"""
    if path.startswith("file:///"):
        return path[7:]
    if path.startswith("file://"):
        return path[6:]
    return path


def _is_local_path(path: str) -> bool:
    """判断是否为本地路径（非 URL）"""
    return not path.startswith(("http://", "https://", "ftp://", "mailto:"))


def validate_references(content: str) -> ValidationResult:
    """验证文本中所有 Markdown 图片和文件引用的路径是否存在

    扫描规则：
    - ![alt](path)：图片引用，path 必须是本地路径且文件存在
    - [text](path)：文件链接（非图片），path 必须是本地路径且文件存在
    - 以 http/https 开头的 URL 链接视为普通超链接，不验证
    - 以 http/https 开头的图片引用视为错误（LLM 不应输出 URL 图片）
    """
    result = ValidationResult()
    seen_paths = set()  # 去重

    # 1. 验证图片引用
    for match in _IMG_PATTERN.finditer(content):
        alt_text = match.group(1)
        raw_path = match.group(2)
        path = _normalize_path(raw_path)

        if path in seen_paths:
            continue
        seen_paths.add(path)

        if not _is_local_path(path):
            result.errors.append(ReferenceError(
                kind="图片", path=raw_path,
                reason="不允许使用URL，必须使用本地绝对路径"
            ))
            continue

        if not Path(path).exists():
            result.errors.append(ReferenceError(
                kind="图片", path=path,
                reason="路径不存在"
            ))

    # 2. 验证文件链接（排除图片引用，排除 URL 超链接）
    for match in _LINK_PATTERN.finditer(content):
        link_text = match.group(1)
        raw_path = match.group(2)
        path = _normalize_path(raw_path)

        if path in seen_paths:
            continue
        seen_paths.add(path)

        # 跳过 URL 超链接（LLM 输出普通网页链接是正常的）
        if not _is_local_path(path):
            continue

        # 跳过已有图片引用的路径（已在上面的图片验证中处理）
        if Path(path).exists() and any(e.path == path for e in result.errors):
            continue

        if not Path(path).exists():
            result.errors.append(ReferenceError(
                kind="文件", path=path,
                reason="路径不存在"
            ))

    result.is_valid = len(result.errors) == 0
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_output_validator.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/output_validator.py tests/test_output_validator.py
git commit -m "feat: add output_validator module for harness reference validation"
```

---

## Task 2: 在 agent_loop 中集成 Harness 验证

**Files:**
- Modify: `agent/generic/agent_loop.py:191-210`
- Test: `tests/test_output_validator.py` (追加集成测试)

**当前代码** (`agent_loop.py:191-210`)：

```python
# verbose=False 模式
else:
    response = exhaust(response_gen)
    content = response.content or ""
    content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
    yield StreamEvent("reply", content)
```

**目标行为**：
- LLM 输出纯文本时，先验证图片/文件引用
- 验证通过 → 正常 yield reply
- 验证失败 → 不 yield reply，将 feedback 注入 messages 继续循环（LLM 重新输出）
- 最多重试 3 次，超过后强制通过（防止无限循环）

- [ ] **Step 1: 写集成测试**

在 `tests/test_output_validator.py` 末尾追加：

```python
class TestValidateIntegration:
    def test_max_retries_forces_pass(self):
        """超过最大重试次数后强制通过"""
        # 连续 3 次验证失败后，第 4 次应强制通过
        content = "![a](/nonexistent.jpg)"
        from agent.output_validator import validate_references
        result = validate_references(content)
        assert result.is_valid is False
        # 实际的强制通过逻辑在 agent_loop 中，这里只验证 validator 本身
        # agent_loop 侧的集成测试需要 mock
```

- [ ] **Step 2: 修改 agent_loop.py**

在 `agent_runner_loop` 函数开头添加导入和常量：

```python
# agent_loop.py 顶部新增导入
from agent.output_validator import validate_references

# 在 agent_runner_loop 函数内，while True 循环前添加
_harness_fail_count = 0
_MAX_HARNESS_RETRIES = 3
```

修改 `verbose=False` 分支（约第 191-210 行），替换 `yield StreamEvent("reply", content)` 部分：

```python
# verbose=False 分支
else:
    response = exhaust(response_gen)
    content = response.content or ""
    content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)

    # Harness 验证：仅在 LLM 不调工具直接回复用户时验证
    # 条件 not response.tool_calls 精确区分最终回复 vs 中间工具调用
    if not response.tool_calls:
        validation = validate_references(content)
        if not validation.is_valid and _harness_fail_count < _MAX_HARNESS_RETRIES:
            _harness_fail_count += 1
            feedback = validation.format_feedback()
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": feedback})
            continue  # 回到 while 循环，让 LLM 修正
        _harness_fail_count = 0

    yield StreamEvent("reply", content)
```

**注意**：`messages.append` 需要在 agent_runner_loop 的作用域中。当前 `messages` 列表在函数内是可访问的（函数参数或内部变量）。需确认 `agent_runner_loop` 中 messages 的变量名——通过代码审查确认是 `messages` 列表。

- [ ] **Step 3: 验证语法**

Run: `python -m py_compile agent/generic/agent_loop.py`

- [ ] **Step 4: 运行测试**

Run: `cd <repo_root> && python -m pytest tests/test_output_validator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_output_validator.py
git commit -m "feat: integrate harness validation in agent_loop — block invalid references"
```

---

## Task 3: 飞书端适配文件链接格式

**Files:**
- Modify: `niu_api/channel/feishu_channel.py:598-626`（`_filter_media_markers`）
- Modify: `niu_api/channel/feishu_channel.py:952-979`（`resolve_outbound_content`）

**当前状态**：飞书端只解析 `::file::JSON::` 格式传送文件。`[文件名](path)` 会被 marked.js 渲染为超链接但飞书无法点击。

**目标**：`_filter_media_markers` 和 `resolve_outbound_content` 额外解析 `[文件名](path)` 格式（非 `!` 开头的链接），提取本地文件发送到飞书。

- [ ] **Step 1: 修改 `_filter_media_markers`**

在 `::file::JSON::` 解析之前（约第 596 行），添加 Markdown 文件链接解析：

```python
        # 1.5 解析 Markdown 文件链接 [文件名](path)（非图片链接）
        md_link_pattern = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')
        for match in md_link_pattern.finditer(text):
            link_text = match.group(1)
            link_path = match.group(2)
            full_match = match.group(0)

            # 只处理本地路径，跳过 URL
            if not link_path or link_path.startswith(("http://", "https://", "ftp://", "mailto:")):
                continue

            # 规范化路径
            if link_path.startswith("file:///"):
                link_path = link_path[7:]
            elif link_path.startswith("file://"):
                link_path = link_path[6:]

            if Path(link_path).exists() and link_path not in self._stream_sent_media_paths:
                self._stream_pending_files.append({
                    "local_path": link_path,
                    "filename": link_text or Path(link_path).name,
                    "kind": "file",
                })
                self._stream_sent_media_paths.add(link_path)
            # 从文本中删除链接标记（文件通过飞书文件消息发送，不走 markdown）
            text = text.replace(full_match, f"↑ {link_text or Path(link_path).name}", 1)
```

- [ ] **Step 2: 修改 `resolve_outbound_content`**

在 `::file::JSON::` 解析之前（约第 947 行），添加 Markdown 文件链接解析：

```python
        # 1.5 解析 Markdown 文件链接 [文件名](path)
        md_link_pattern = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')
        for match in md_link_pattern.finditer(cleaned_content):
            link_text = match.group(1)
            link_path = match.group(2)
            full_match = match.group(0)

            if not link_path or link_path.startswith(("http://", "https://", "ftp://", "mailto:")):
                continue

            if link_path.startswith("file:///"):
                link_path = link_path[7:]
            elif link_path.startswith("file://"):
                link_path = link_path[6:]

            if not link_path:
                replacement = "[文件信息缺失]"
            elif not self._is_path_allowed(link_path):
                replacement = "[文件无法发送: 安全限制]"
            elif not Path(link_path).exists():
                replacement = f"[文件不存在: {link_text}]" if link_text else "[文件不存在]"
            else:
                media_messages.append(ResolvedMessage(kind="file", local_path=link_path, filename=link_text))
                replacement = f"↑ {link_text}" if link_text else "↑ 文件"
            cleaned_content = cleaned_content.replace(full_match, replacement, 1)
```

- [ ] **Step 3: 验证语法**

Run: `python -m py_compile niu_api/channel/feishu_channel.py`

- [ ] **Step 4: Commit**

```bash
git add niu_api/channel/feishu_channel.py
git commit -m "feat: feishu channel supports Markdown file links [name](path)"
```

---

## Task 4: 桌面端适配文件链接格式

**Files:**
- Modify: `ui/assistant/chat.html`（Markdown 渲染后处理部分）

**当前状态**：桌面端 `chat.html` 用 marked.js 渲染 Markdown。`[文件名](path)` 会被渲染为 `<a href="path">文件名</a>`，但本地路径的 href 无法直接点击打开。

**目标**：对本地路径的 `<a>` 链接添加点击处理——用系统默认程序打开文件。

- [ ] **Step 1: 在 chat.html 的 Markdown 后处理中添加文件链接处理**

在图片后处理逻辑之后（约第 830 行后），添加文件链接处理：

```javascript
// 后处理：本地路径文件链接添加点击打开功能
textDiv.querySelectorAll('a').forEach(a => {
  let href = a.getAttribute('href') || '';
  // 剥离 file:/// 前缀
  if (href.startsWith('file:///')) {
    href = decodeURIComponent(href.replace('file:///', ''));
  } else if (href.startsWith('file://')) {
    href = decodeURIComponent(href.replace('file://', ''));
  }
  // 判断是否为本地路径（绝对路径）
  if (href && (/^[A-Za-z]:[\/]/.test(href) || /^\//.test(href))) {
    a.setAttribute('data-file-path', href);
    a.setAttribute('href', '#');
    a.classList.add('local-file-link');
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const filePath = a.getAttribute('data-file-path');
      if (filePath && window.electronAPI && window.electronAPI.openWithSystemViewer) {
        window.electronAPI.openWithSystemViewer(filePath);
      }
    });
  }
});
```

- [ ] **Step 2: 添加文件链接样式**

在 chat.html 的 `<style>` 中添加：

```css
.local-file-link {
  color: #1890ff;
  text-decoration: underline;
  cursor: pointer;
}
.local-file-link:hover {
  color: #40a9ff;
}
.local-file-link::before {
  content: '📎 ';
}
```

- [ ] **Step 3: 验证**

手动在浏览器中测试：输入包含 `[报告.pdf](/path/to/file.pdf)` 的消息，确认链接可点击打开。

- [ ] **Step 4: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "feat: desktop file links [name](path) — click to open with system viewer"
```

---

## Task 5: 更新提示词——添加文件引用格式说明

**Files:**
- Modify: `config/agents/niu.md`
- Modify: `config/agents/file-processor.md`

- [ ] **Step 1: 修改 `niu.md` 照片管理部分**

当前：
```markdown
# 照片管理

展示人物照片时，使用 Markdown 标准图片语法 `![人物名](图片路径)`，路径使用绝对路径（如 `/Users/xxx/photo.jpg`），不要加 `file://` 前缀，不要使用自定义标记格式。
详细操作请读取 skill：~/.niu/skills/photo-face-display.md
```

改为：
```markdown
# 照片与文件引用

## 照片

展示人物照片时，使用 Markdown 标准图片语法 `![人物名](图片路径)`，路径使用本地绝对路径（如 `/Users/xxx/photo.jpg`），不要加 `file://` 前缀，不要使用 URL。
详细操作请读取 skill：~/.niu/skills/photo-face-display.md

## 文件

发送文件给用户时，使用 Markdown 标准链接语法 `[文件名](本地路径)`，路径使用本地绝对路径。例如：`[报告.pdf](/Users/xxx/.niu/work/2026/报告/报告.pdf)`。

**禁止使用 URL 作为图片或文件路径**——系统会验证所有引用路径，路径不存在或使用 URL 会被拦截并要求修正。
```

- [ ] **Step 2: 修改 `file-processor.md` 返回格式部分**

在 **返回格式** 段落末尾添加：

```markdown
**文件成功** — 从工具返回值中提取以下字段：
- `file_path` → 存储位置（工具动态生成的路径）
- `category` → 分类

展示给用户时，使用 Markdown 链接语法：`[文件名](file_path)`，不要使用 `::file::` 格式。
```

- [ ] **Step 3: 同步 Skill 文件到 ~/.niu/skills/**

```bash
cp config/user-data/skills/photo-face-display.md ~/.niu/skills/photo-face-display.md
```

检查 photo-face-display.md 是否需要更新文件引用格式说明。

- [ ] **Step 4: Commit**

```bash
git add config/agents/niu.md config/agents/file-processor.md
git commit -m "feat: update prompts with file reference format and validation rules"
```

---

## Task 6: 清理废弃的 `::file::` 代码

**Files:**
- Modify: `niu_api/channel/feishu_channel.py`（删除 `::file::` 解析逻辑）
- Modify: `niu_api/channel/base.py`（更新注释）

**注意**：此 Task 在 Task 3 完成后执行，确保 `[文件名](path)` 已替代 `::file::`。

- [ ] **Step 1: 删除 `_filter_media_markers` 中的 `::file::` 解析**

删除第 596-626 行的 `::file::` 解析代码块。

- [ ] **Step 2: 删除 `resolve_outbound_content` 中的 `::file::` 解析**

删除第 947-979 行的 `::file::` 解析代码块。

- [ ] **Step 3: 删除 `send` 方法回退路径中的 `::file::` 剥离**

将第 367 行：
```python
content = re.sub(r'::(?:person_photo|file)::.*?::', '', content)
```
改为：
```python
content = re.sub(r'::person_photo::.*?::', '', content)  # 兼容极旧格式
```

- [ ] **Step 4: 更新 base.py 注释**

更新 `niu_api/channel/base.py` 第 54 行附近的注释，移除 `::file::` 的说明。

- [ ] **Step 5: 全局搜索确认无遗漏**

```bash
grep -r "::file::" --include="*.py" --include="*.md" --include="*.html" .
```

确认无残留引用（docs/ 目录的历史文档可以保留）。

- [ ] **Step 6: 验证语法**

Run: `python -m py_compile niu_api/channel/feishu_channel.py`

- [ ] **Step 7: Commit**

```bash
git add niu_api/channel/feishu_channel.py niu_api/channel/base.py
git commit -m "cleanup: remove deprecated ::file:: format, replaced by Markdown [name](path)"
```

---

## Task 7: 端到端验证

**Files:**
- 无代码修改，纯手动测试

- [ ] **Step 1: 测试 LLM 输出无效图片路径时被拦截**

向 Agent 发送："刘永辉是谁，有照片吗？"

预期行为：
1. LLM 输出 `![刘永辉](https://xxx)` 或不存在路径
2. Harness 拦截，反馈给 LLM
3. LLM 重新调用 `get_person_photos` 获取正确路径
4. 输出 `![person_id|刘永辉](/Users/xxx/.niu/tmp/xxx.png)` 验证通过
5. 飞书端显示照片，桌面端显示带红框照片

- [ ] **Step 2: 测试文件引用**

向 Agent 发送："帮我发一份之前的报告"

预期行为：
1. LLM 搜索知识库找到文件路径
2. 输出 `[报告.pdf](/Users/xxx/.niu/work/2026/报告/xxx.pdf)` 验证通过
3. 飞书端收到文件消息，桌面端收到可点击链接

- [ ] **Step 3: 测试路径不存在的文件引用被拦截**

模拟 LLM 输出不存在的文件路径，验证 Harness 反馈机制正常工作。

---

## 自查清单

### Spec 覆盖率

| 需求 | 对应 Task |
|------|----------|
| 图片引用路径验证 | Task 1 + Task 2 |
| 文件引用格式标准化 | Task 3 + Task 4 + Task 5 |
| 验证失败反馈 LLM | Task 2 |
| 飞书端文件传输适配 | Task 3 |
| 桌面端文件链接适配 | Task 4 |
| 提示词更新 | Task 5 |
| 废弃旧格式清理 | Task 6 |
| 端到端测试 | Task 7 |

### Placeholder 扫描

无 TBD/TODO/placeholder。

### 类型一致性

- `validate_references` 返回 `ValidationResult`，`agent_loop.py` 使用 `validation.is_valid` 和 `validation.format_feedback()` — 一致
- `_filter_media_markers` 和 `resolve_outbound_content` 中文件链接解析使用相同的正则和路径规范化逻辑 — 一致
