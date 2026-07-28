# Memory 提示词每轮重读 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 memory.json 派生的提示词段（identity / workspace / user / permanent / firstRun）从静态缓存改为每轮 LLM 调用前重新读取 memory.json 重建，删除 `_memory_dirty` + `_refresh_user_memories` 这套局部刷新机制，解决"Agent 写入 memory.json 后下一轮 system prompt 不更新"的 bug。

**Architecture:**
- 现状：`static_system_prompt = niu.md + memory_section`（`__init__` 时构建一次，缓存）。`_refresh_user_memories` 只在 `_memory_dirty` 触发时局部刷新 permanent 段，其他 memory 字段（identity/workspace/user/firstRun）写文件后 system prompt 不感知。
- 目标：`static_system_prompt` 只保留 niu.md（保持 Claude cache_control）。memory 派生的 5 个段（身份/工作目录/用户信息/长期记忆/首次使用）每轮在 `_on_before_llm` 重新读 memory.json 生成，作为动态段拼到 static 之后。
- 删除：`_memory_dirty` Event、`_refresh_user_memories` 方法、`handler.py` 中 2 处 dirty 设置代码、`_load_memory_for_prompt` 在 `_build_static_system_prompt` 中的调用。

**Tech Stack:** Python 3.11、FastAPI、`agent/runner.py`、`agent/handler.py`、`mcp-servers/config-manager`、`mcp-servers/memory-server`、pytest。

---

## File Structure

| 文件 | 责任 | 改动 |
|------|------|------|
| `agent/runner.py` | 系统提示词构建 + 每轮注入 | **修改**：拆分 static/dynamic；删除 dirty 机制；`_on_before_llm` 加入 memory_section 重建；`_load_memory_for_prompt` 加锁 |
| `agent/handler.py` | 工具分发 + dirty 触发点 | **修改**：删除 2 处 `_memory_dirty.set()` 代码块 + 1 处注释 |
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | 配置管理 - memory 写入 | **修改**：`save_memory()` 改原子写（tmp + os.replace） |
| `mcp-servers/memory-server/src/niu_memory_server/__init__.py` | 用户记忆 - permanent 写入 | **修改**：`_write_permanent_only()` 改原子写（tmp + os.replace） |
| `tests/test_runner_memory_refresh.py` | 每轮重读行为测试 | **新建**：覆盖 5 个 memory 段的每轮重读 + firstRun 关闭后提示词消失 + 用户字段从占位符到实填的切换 |
| `tests/test_prompt_cache.py` | prompt cache 既有测试 | **修改**：4 处 `_assemble_system_message` 旧 3 参调用改 4 参；1 处 docstring 更新；删除 `test_refresh_user_memories_updates_static_and_recomputes_base` |
| `tests/test_on_before_llm_method.py` | `_on_before_llm` 既有测试 | **修改**：3 处断言索引 `args[0][1]` 改 `args[0][2]`；fixture 加 hermetic patch；删除 `_refresh_user_memories` 断言 |
| `tests/test_lightrag_retrieval_migration.py` | LightRAG 迁移测试 | **修改**：patch `_load_memory_for_prompt` 返回固定值，保持 hermetic；删除 `_memory_dirty` mock |

---

## Task 0: `_load_memory_for_prompt` 加锁（防并发读到半写文件）

每轮重读之后，memory.json 的读频率从启动一次变为每轮一次。memory-server 的写入与 runner 的读取并发时，如果读到半写文件，`json.loads` 抛异常 → 返回 `""` → 该轮 5 个 memory 段全部消失（用户视角：信息突然丢失一轮）。加 `_memory_file_lock` 保护读取侧。

**防御层次说明**：Task 0 的锁是**纵深防御**（防御未来可能出现的未走原子写的新写入路径，以及跨模块协作时锁语义漂移的场景）；Task 0.5 的原子写是**主保证**（`tmp + os.replace` 让 reader 永远只看到完整文件，无需依赖任何锁）。两者并存构成多层防护：原子写是常态保护，锁是防御未来回归的安全网。即使未来有新代码绕过原子写直接 write_text，runner 端持锁读仍能避免 torn write。

**Files:**
- Modify: `agent/runner.py:208-285`（`_load_memory_for_prompt` 函数）

- [ ] **Step 1: 确认 `_memory_file_lock` 位置**

锁定义在 `mcp-servers/memory-server/src/niu_memory_server/__init__.py:127`：

```python
_memory_file_lock = threading.Lock()
```

memory-server 的写入侧已经在 L226 用 `with _memory_file_lock:` 保护，本次只需让 runner 的读取侧也用同一把锁。

- [ ] **Step 2: 修改 `_load_memory_for_prompt` 加锁读**

`agent/runner.py:208-217` 当前实现：

```python
def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    # ...（后续渲染逻辑）
```

改为：

```python
def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    memory_path = Path.home() / ".niu" / "memory.json"
    if not memory_path.exists():
        return ""

    try:
        # 加锁读，避免与 memory-server 写并发时拿到半写文件
        # memory-server 可能未加载（如单元测试环境），失败则降级为 nullcontext
        try:
            from niu_memory_server import _memory_file_lock
            lock_ctx = _memory_file_lock
        except ImportError:
            import contextlib
            lock_ctx = contextlib.nullcontext()

        with lock_ctx:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    # ...（后续渲染逻辑保持不变）
```

**注意**：
- `niu_memory_server` 是 MCP 服务器模块，由 `mcp-servers/memory-server/src/niu_memory_server/__init__.py` 导出。同进程架构下应已在 sys.path（preload 时通过 workdir 加入）。
- 如果 import 失败（如单元测试未 preload memory-server），降级为 `nullcontext()` 不加锁，保持向后兼容。
- 锁的获取路径：`niu_memory_server/__init__.py:127` 的模块级 `threading.Lock()`。

- [ ] **Step 3: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax ok')"
```

预期输出：`syntax ok`

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py
git commit -m "fix(runner): _load_memory_for_prompt 加锁读，防并发读到半写文件

每轮重读之后，读频率从启动一次变为每轮一次。
与 memory-server 写入并发时可能拿到半写 JSON → 该轮 5 个 memory 段全消失。
加 _memory_file_lock 保护读取侧（memory-server 写侧已用同一把锁）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 0.5: config-manager + memory-server 原子写（防 torn write）

锁只能保护同进程内的并发，跨模块/跨进程仍可能出现"runner 读到半写文件"的场景：
- `config-manager` 的 `save_memory()` 当前 L446-448 直接 `MEMORY_PATH.write_text(...)`，**不使用** `_memory_file_lock`
- `memory-server` 的 `_write_permanent_only()` 虽然用 `_memory_file_lock` 保护读改写，但最终 `path.write_text(...)` 仍是**非原子**写

runner 端持锁读时，config-manager 依然可以并发写入 → torn write。原子写（tmp + os.replace）可以让 reader 永远只看到完整文件，无需依赖锁。

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:442-448`（`save_memory` 改原子写）
- Modify: `mcp-servers/memory-server/src/niu_memory_server/__init__.py:222-241`（`_write_permanent_only` 改原子写）

- [ ] **Step 1: config-manager `save_memory()` 改原子写**

`mcp-servers/config-manager/src/niu_config_manager/__init__.py:442-448` 当前实现：

```python
def save_memory(memory: dict[str, Any]) -> None:
    """Save memory to ~/.niu/memory.json."""
    NIU_DIR.mkdir(parents=True, exist_ok=True)
    memory["lastActiveAt"] = datetime.now().isoformat()
    MEMORY_PATH.write_text(
        json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8"
    )
```

改为（**注意**：config-manager 模块顶部 L9 已 `import os`，函数内**不需要**重复 import；`tempfile` 提升到模块顶部新加一行）：

模块顶部 import 区改为（L7-12 当前状态）：

```python
import asyncio
import json
import os
import tempfile  # 新增：save_memory 原子写需要
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
```

`save_memory` 函数体改为：

```python
def save_memory(memory: dict[str, Any]) -> None:
    """Save memory to ~/.niu/memory.json (atomic write via tmp + os.replace).

    runner 端每轮 _load_memory_for_prompt() 读 memory.json。原子写保证
    reader 永远看到完整文件，不依赖跨模块文件锁（config-manager 不使用
    _memory_file_lock，锁方案在此无效）。
    """
    NIU_DIR.mkdir(parents=True, exist_ok=True)
    memory["lastActiveAt"] = datetime.now().isoformat()

    fd, tmp_path = tempfile.mkstemp(
        dir=str(MEMORY_PATH.parent),
        prefix=MEMORY_PATH.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, MEMORY_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

- [ ] **Step 2: memory-server `_write_permanent_only()` 改原子写**

`mcp-servers/memory-server/src/niu_memory_server/__init__.py:222-241` 当前实现：

```python
def _write_permanent_only(permanent: list):
    """Read-modify-write: update only the permanent field, preserve all others.
    Thread-safe via module-level lock.
    """
    with _memory_file_lock:
        path = _get_memory_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing file to preserve other fields (identity, workspace, user, etc.)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing["permanent"] = permanent
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
```

改为（保留 `_memory_file_lock` 保护 read-modify-write 的"读"阶段，最后 `write_text` 改原子写）：

```python
def _write_permanent_only(permanent: list):
    """Read-modify-write: update only the permanent field, preserve all others.
    Thread-safe via module-level lock + atomic write (tmp + os.replace).

    锁保护 read-modify-write 的 race；原子写保证 runner 端读到完整文件。
    """
    import os
    import tempfile

    with _memory_file_lock:
        path = _get_memory_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing file to preserve other fields (identity, workspace, user, etc.)
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing["permanent"] = permanent

        # Atomic write: tmp + os.replace（reader 永远看到完整文件）
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
```

- [ ] **Step 3: 语法检查**

```bash
python3 -c "
import ast
ast.parse(open('mcp-servers/config-manager/src/niu_config_manager/__init__.py').read())
ast.parse(open('mcp-servers/memory-server/src/niu_memory_server/__init__.py').read())
print('syntax ok')
"
```

预期输出：`syntax ok`

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py mcp-servers/memory-server/src/niu_memory_server/__init__.py
git commit -m "fix(mcp): save_memory/_write_permanent_only 改原子写，防 torn write

runner 每轮 _load_memory_for_prompt() 读 memory.json。
config-manager 的 save_memory 不用 _memory_file_lock（跨模块锁无效），
memory-server 的最终 write_text 也非原子。
tmp + os.replace 保证 reader 永远看到完整文件，无需依赖锁。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1: `_build_static_system_prompt` 只保留 niu.md

`_load_memory_for_prompt()` 当前在 `_build_static_system_prompt` 末尾被调用并拼接，需要把这两行删掉，让 static 段只剩 niu.md。**关键：yaml 解析逻辑必须原样保留**，包括 `try/except: pass` 和 `sys_prompt += parts[2].strip()`——否则会丢 niu.md 306 行正文（工具规则、子 Agent 委托等核心提示词）。

**Files:**
- Modify: `agent/runner.py:523-561`（`_build_static_system_prompt` 删除对 `_load_memory_for_prompt` 的调用）

- [ ] **Step 1: 查看当前 `_build_static_system_prompt` 完整实现**

```bash
sed -n '523,561p' agent/runner.py
```

- [ ] **Step 2: 修改 `_build_static_system_prompt` 删除 memory_section 调用**

`agent/runner.py:523-561` 当前实现（**真实代码**，用 Read 工具读过）：

```python
    @staticmethod
    def _build_static_system_prompt() -> str:
        """构建静态系统提示词段（cache 友好）。

        只包含 niu.md 正文 + memory_section（身份/工作目录/用户长期记忆）。
        不包含 Current Time、disk_desc、injection——这些是动态段。
        静态段字节稳定，是 prompt cache 的前缀。
        memory.json 变化时由 _refresh_user_memories 同步更新。
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # 1. 读取 niu.md
        sys_prompt = ""
        niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")
        if os.path.exists(niu_md_path):
            with open(niu_md_path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml as _yaml
                            config = _yaml.safe_load(parts[1])
                            if config and config.get("description"):
                                sys_prompt = config["description"].strip() + "\n\n"
                        except Exception:
                            pass
                        sys_prompt += parts[2].strip()
                else:
                    sys_prompt = content

        if not sys_prompt:
            sys_prompt = "# Role: Niu Agent\nYou are a helpful assistant with file and code access."

        # 2. 注入 memory.json 中的身份设定和用户偏好
        memory_section = _load_memory_for_prompt()
        if memory_section:
            sys_prompt += "\n\n" + memory_section

        return sys_prompt
```

改为（**yaml 解析逻辑保持原样**，只删 docstring 中过时行 + 删末尾 memory 拼接两行 + 改 docstring）：

```python
    @staticmethod
    def _build_static_system_prompt() -> str:
        """构建静态系统提示词段（cache 友好）。

        只包含 niu.md 正文，是 prompt cache 的前缀，字节稳定。
        memory 派生段（identity/workspace/user/permanent/firstRun）由
        _on_before_llm 每轮从 memory.json 重读生成，作为动态段拼接。
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # 1. 读取 niu.md
        sys_prompt = ""
        niu_md_path = os.path.join(script_dir, "..", "config", "agents", "niu.md")
        if os.path.exists(niu_md_path):
            with open(niu_md_path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml as _yaml
                            config = _yaml.safe_load(parts[1])
                            if config and config.get("description"):
                                sys_prompt = config["description"].strip() + "\n\n"
                        except Exception:
                            pass
                        sys_prompt += parts[2].strip()
                else:
                    sys_prompt = content

        if not sys_prompt:
            sys_prompt = "# Role: Niu Agent\nYou are a helpful assistant with file and code access."

        return sys_prompt
```

**变化**（共 3 处，其余代码原样保留）：
1. docstring 删除 "只包含 niu.md 正文 + memory_section（身份/工作目录/用户长期记忆）。" 和 "memory.json 变化时由 _refresh_user_memories 同步更新。" 两行；改为说明 "memory 派生段由 _on_before_llm 每轮重读"
2. 删除第 556-559 行（`# 2. 注入 memory.json 中的身份设定和用户偏好` 注释 + `memory_section = _load_memory_for_prompt()` + `if memory_section: sys_prompt += "\n\n" + memory_section`）
3. 其他所有代码（yaml 解析、`parts[2].strip()` 拼接、fallback 默认值）**一字不改**

- [ ] **Step 3: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax ok')"
```

预期输出：`syntax ok`

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py
git commit -m "refactor(runner): _build_static_system_prompt 只保留 niu.md，移除 memory 拼接

memory 派生段将由每轮 _on_before_llm 重读 memory.json 生成，作为动态段。
本 Task 先拆分静态段，下一 Task 处理动态段拼装。

注意：本 commit 是中间状态，memory 段暂缺，Task 2 完成后恢复。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 修改 `_assemble_system_message` 签名 + `_on_before_llm` 每轮重读 memory.json（原子 Task）

当前 `_assemble_system_message` 把 `static_system_prompt`（含 memory）+ `dynamic_text` 拼到 messages[0]。需要改为：`static_system_prompt`（仅 niu.md）+ `memory_section`（每轮重读）+ `dynamic_text`。

**关键风险**：`_assemble_system_message` 全库共 3 个调用点，签名变更后**必须在同一原子 commit 内全部修改**，否则中间状态的 `_on_before_llm`（每轮 LLM 调用前必触发）会以 3 参调 4 参函数 → 必 TypeError。三个调用点：
- `agent/runner.py:837`（`_on_before_llm`）— 本 Task Step 3 处理（真实 memory_section）
- `agent/runner.py:2350`（`chat()` 入口组装初始 system message）— 本 Task Step 4 处理（传 `""` 占位）
- `agent/runner.py:1687`（proactive compress 后 force-reload 重建 system message）— 本 Task Step 5 处理（传 `""` 占位）

**Files:**
- Modify: `agent/runner.py:768-813`（`_assemble_system_message` 方法签名 + 实现）
- Modify: `agent/runner.py:815-837`（`_on_before_llm` 方法，加入 `_load_memory_for_prompt` 调用）
- Modify: `agent/runner.py:2346-2350`（`chat()` 入口调用点）
- Modify: `agent/runner.py:1683-1688`（proactive compress force-reload 调用点）

- [ ] **Step 1: 查看当前 `_assemble_system_message` 和 `_on_before_llm` 完整实现**

```bash
sed -n '768,837p' agent/runner.py
```

- [ ] **Step 2: 修改 `_assemble_system_message` 签名和实现（保留 early-return guard）**

`agent/runner.py:768-813` 当前实现（**真实代码**，用 Read 工具读过）：

```python
    def _assemble_system_message(
        self,
        messages: list,
        injection: str,
        model: str,
    ) -> None:
        """组装 system message，根据 model 决定是否用 cache_control。

        原地修改 messages[0]["content"]。

        - Claude 模型：content 改为 list 格式，静态段末尾打 cache_control breakpoint。
          静态段（niu.md + memory）被 cache，命中后 input token 计费降至 10%。
          动态段（Current Time + disk_desc + injection）每轮重新发送。
        - 其他模型（火山方舟/DeepSeek/Qwen 等）：content 保持字符串格式。
          静态段在开头且字节稳定，靠服务端自动 prefix cache 命中。

        Args:
            messages: 消息列表，messages[0] 必须是 role=system
            injection: 动态注入内容（skills/knowledge/brain region）
            model: 当前模型名，用于判断是否 Claude
        """
        if not messages or messages[0].get("role") != "system":
            return

        # 动态段 = Current Time + disk_desc + injection
        dynamic_text = self.dynamic_system_prefix
        if injection:
            dynamic_text += injection

        model_lower = (model or "").lower()
        if "claude" in model_lower:
            # Claude：list 格式 + cache_control breakpoint
            messages[0]["content"] = [
                {
                    "type": "text",
                    "text": self.static_system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ]
        else:
            # 其他模型：字符串格式，静态段在开头
            messages[0]["content"] = self.static_system_prompt + dynamic_text
```

改为（**保留 L789-790 的 early-return guard 一字不改**；签名插入 `memory_section`；docstring 更新；`dynamic_text` 拼装把 `memory_section` 放最前）：

```python
    def _assemble_system_message(
        self,
        messages: list,
        memory_section: str,
        injection: str,
        model: str,
    ) -> None:
        """组装 system message，根据 model 决定是否用 cache_control。

        原地修改 messages[0]["content"]。

        - Claude 模型：content 改为 list 格式，静态段末尾打 cache_control breakpoint。
          静态段（仅 niu.md）被 cache，命中后 input token 计费降至 10%。
          动态段（memory + Current Time + disk_desc + injection）每轮重新发送。
        - 其他模型（火山方舟/DeepSeek/Qwen 等）：content 保持字符串格式。
          静态段在开头且字节稳定，靠服务端自动 prefix cache 命中。

        Args:
            messages: 消息列表，messages[0] 必须是 role=system
            memory_section: 本轮从 memory.json 重读的 memory 段（identity/workspace/user/permanent/firstRun）
            injection: 动态注入内容（skills/knowledge/brain region）
            model: 当前模型名，用于判断是否 Claude
        """
        if not messages or messages[0].get("role") != "system":
            return

        # 动态段 = memory_section + Current Time + disk_desc + injection
        dynamic_text = ""
        if memory_section:
            dynamic_text += "\n\n" + memory_section
        dynamic_text += self.dynamic_system_prefix
        if injection:
            dynamic_text += injection

        model_lower = (model or "").lower()
        if "claude" in model_lower:
            # Claude：list 格式 + cache_control breakpoint
            messages[0]["content"] = [
                {
                    "type": "text",
                    "text": self.static_system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ]
        else:
            # 其他模型：字符串格式，静态段在开头
            messages[0]["content"] = self.static_system_prompt + dynamic_text
```

**变化**（共 4 处，其余代码原样保留）：
1. 签名插入 `memory_section: str` 作为第 2 个参数
2. docstring 把 "静态段（niu.md + memory）被 cache" 改为 "静态段（仅 niu.md）被 cache"；把 "动态段（Current Time + disk_desc + injection）" 改为 "动态段（memory + Current Time + disk_desc + injection）"；新增 `memory_section` Args 说明
3. `dynamic_text` 拼装从 `dynamic_text = self.dynamic_system_prefix` 改为：先 `dynamic_text = ""`，若有 memory_section 则 `dynamic_text += "\n\n" + memory_section`，然后 `dynamic_text += self.dynamic_system_prefix`（保持 dynamic_prefix 和 injection 的原有相对顺序）
4. early-return guard（L789-790）**原样保留**

- [ ] **Step 3: 修改 `_on_before_llm` 加入 `_load_memory_for_prompt` 调用**

`agent/runner.py:815-837` 当前实现（**真实代码**，用 Read 工具读过）：

```python
    def _on_before_llm(self, messages: list, turn: int) -> None:
        """每轮 LLM 调用前刷新动态注入（skill/knowledge/脑区/habits）。

        关键：在 client.chat 之前调，让本轮 LLM 立即读到新 system message。
        原地修改 messages[0]，无返回值。

        Args:
            messages: agent_runner_loop 的消息列表引用
            turn: 当前轮次（从 1 开始）
        """
        # 提取最近 3 条消息作为 context（保持原样，按用户原始设计）
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # C4 修复：首轮合并拖入文件的 resources 模式要求（chat() 存入实例属性）
        # chat() 把 resources 模式文本存入 self._first_turn_extra_injection，
        # 这里 turn==1 时合并进 injection，让首轮 LLM 能读到 mode=reference/move 指令
        if turn == 1 and getattr(self, "_first_turn_extra_injection", ""):
            injection += self._first_turn_extra_injection
            self._first_turn_extra_injection = ""  # 清空，防跨对话泄漏

        # 原地修改 messages[0]，本轮 LLM 立即读到
        self._assemble_system_message(messages, injection, self.default_model)
```

改为（**docstring 首行更新；首行加 `_load_memory_for_prompt()` 调用；末尾 `_assemble_system_message` 调用改 4 参**）：

```python
    def _on_before_llm(self, messages: list, turn: int) -> None:
        """每轮 LLM 调用前重读 memory.json + 刷新动态注入。

        每轮从 memory.json 重新构建 memory_section（identity/workspace/user/permanent/firstRun），
        保证 Agent 写入 memory.json 后下一轮 system prompt 立即感知。
        关键：在 client.chat 之前调，让本轮 LLM 立即读到新 system message。
        原地修改 messages[0]，无返回值。

        Args:
            messages: agent_runner_loop 的消息列表引用
            turn: 当前轮次（从 1 开始）
        """
        # 1. 每轮重读 memory.json（关键：解决 Agent 写入后下轮 system prompt 不更新的 bug）
        memory_section = _load_memory_for_prompt()

        # 2. 提取最近 3 条消息作为 context（保持原样，按用户原始设计）
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # C4 修复：首轮合并拖入文件的 resources 模式要求（chat() 存入实例属性）
        # chat() 把 resources 模式文本存入 self._first_turn_extra_injection，
        # 这里 turn==1 时合并进 injection，让首轮 LLM 能读到 mode=reference/move 指令
        if turn == 1 and getattr(self, "_first_turn_extra_injection", ""):
            injection += self._first_turn_extra_injection
            self._first_turn_extra_injection = ""  # 清空，防跨对话泄漏

        # 3. 原地修改 messages[0]，本轮 LLM 立即读到
        self._assemble_system_message(messages, memory_section, injection, self.default_model)
```

- [ ] **Step 4: 修复 `chat()` 入口调用点（L2350），传 `""` 占位**

`agent/runner.py:2346-2350` 当前代码（**真实代码**，用 Read 工具读过）：

```python
        # 组装 system message（首轮就按 model 决定格式，Claude 走 cache_control）
        # injection="" 因为动态注入移到 _on_before_llm 首轮
        # resources 文本在实例属性里，_on_before_llm 首轮会合并进 injection
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], "", self.default_model)
```

改为（`memory_section` 传 `""` 占位，入口组装完立刻被第一轮 `_on_before_llm` 覆盖，**不需要**在这里读 memory.json）：

```python
        # 组装 system message（首轮就按 model 决定格式，Claude 走 cache_control）
        # injection="" 因为动态注入移到 _on_before_llm 首轮
        # memory_section="" 因为 _on_before_llm 首轮会重读 memory.json 覆盖（入口先放空骨架）
        # resources 文本在实例属性里，_on_before_llm 首轮会合并进 injection
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], "", "", self.default_model)
```

**注意**：传 `""` 而非真实 `_load_memory_for_prompt()`，因为入口组装完立刻被第一轮 `_on_before_llm` 覆盖，传 `""` 等价但更简洁，避免入口多一次 IO。

- [ ] **Step 5: 修复 proactive compress force-reload 调用点（L1687），传 `""` 占位**

`agent/runner.py:1683-1688` 当前代码（**真实代码**，用 Read 工具读过）：

```python
                # 保留 system prompt（messages[0]），替换其余消息
                system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
                if system_msg:
                    # 压缩后重建 system message（确保 Claude cache_control 不丢失）
                    # injection 为空，本轮 _on_before_llm 会重新注入（动态注入已从 _on_turn_end 移到 LLM 调用前）
                    self._assemble_system_message([system_msg], "", self.default_model)
                    messages[:] = [system_msg] + fresh_msgs
```

改为（`memory_section` 传 `""` 占位，compress force-reload 的语境是"先重建一个干净骨架，等 `_on_before_llm` 再填血肉"）：

```python
                # 保留 system prompt（messages[0]），替换其余消息
                system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
                if system_msg:
                    # 压缩后重建 system message（确保 Claude cache_control 不丢失）
                    # injection="" 和 memory_section=""：本轮 _on_before_llm 会重新读 memory + 重新注入
                    # （动态注入已从 _on_turn_end 移到 LLM 调用前，memory 也已每轮重读）
                    self._assemble_system_message([system_msg], "", "", self.default_model)
                    messages[:] = [system_msg] + fresh_msgs
```

- [ ] **Step 6: 验证 3 个调用点都已修复**

```bash
grep -n "_assemble_system_message(" agent/runner.py
```

预期输出应包含 4 行：
- 1 行定义（`def _assemble_system_message(`，签名 4 参）
- 1 行 `_on_before_llm` 调用（L837 附近，4 参：messages, memory_section, injection, model）
- 1 行 force-reload 调用（L1687 附近，4 参：[system_msg], "", "", model）
- 1 行 chat() 入口调用（L2350 附近，4 参：[system_message], "", "", model）

确认全库**没有任何 3 参调用残留**。

- [ ] **Step 7: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax ok')"
```

预期输出：`syntax ok`

- [ ] **Step 8: Commit**

```bash
git add agent/runner.py
git commit -m "refactor(runner): _assemble_system_message 4 参签名 + _on_before_llm 每轮重读 memory.json

签名变更：(messages, injection, model) → (messages, memory_section, injection, model)。
原子修复 3 个调用点 + _on_before_llm 每轮重读：
- _on_before_llm（L837）：每轮 _load_memory_for_prompt() 后传入（本 Task 核心）
- chat() 入口（L2350）：传 \"\" 占位（_on_before_llm 首轮会覆盖）
- proactive compress force-reload（L1687）：传 \"\" 占位（_on_before_llm 会重建）

签名变更与全部调用点适配在同一原子 commit 完成，避免中间状态 TypeError。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 修复签名变更影响的既有测试（仅签名适配，不动 dirty 机制）

Task 2 改了 `_assemble_system_message` 签名（3 参 → 4 参），并且 `_on_before_llm` 现在每轮真实调用 `_load_memory_for_prompt()`。这些变更会破坏既有测试，必须先修复，再进入 Task 4 删 dirty 机制。

**Files:**
- Modify: `tests/test_prompt_cache.py:47-48`（docstring 更新：静态段不再含 memory）
- Modify: `tests/test_prompt_cache.py:77, 98, 127, 143`（4 处旧 3 参调用改 4 参）
- Modify: `tests/test_on_before_llm_method.py:22-33`（fixture 加 hermetic patch）
- Modify: `tests/test_on_before_llm_method.py:50, 116-119, 137-138`（断言索引 `args[0][1]` 改 `args[0][2]`）
- Modify: `tests/test_lightrag_retrieval_migration.py:466-486`（patch `_load_memory_for_prompt` 保持 hermetic）

**注意**：本 Task **不删除** `test_refresh_user_memories_updates_static_and_recomputes_base`，也**不删除** `_refresh_user_memories` 断言——这些和 Task 4 的方法删除是原子的，放到 Task 4 一起做。

- [ ] **Step 1: 更新 `tests/test_prompt_cache.py:47-48` docstring**

当前：

```python
def test_build_static_system_prompt_excludes_current_time():
    """静态段不含 Current Time/disk_desc，含 niu.md 正文 + memory 段。"""
```

改为：

```python
def test_build_static_system_prompt_excludes_current_time():
    """静态段不含 Current Time/disk_desc/memory 段，仅含 niu.md 正文。

    memory 段由 _on_before_llm 每轮从 memory.json 重读，不在 static_system_prompt 里。
    """
```

- [ ] **Step 2: 修复 `tests/test_prompt_cache.py` 4 处旧 3 参调用**

当前 4 处调用都是 `runner._assemble_system_message(messages, injection, model=...)`，新签名是 `(messages, memory_section, injection, model)`。需要在 `injection` 前插入 `memory_section` 实参。

**L77**（`test_assemble_system_message_non_claude`）：

```python
# 旧
runner._assemble_system_message(messages, injection, model="ark-code-latest")
# 新
runner._assemble_system_message(messages, "", injection, model="ark-code-latest")
```

**L98**（`test_assemble_system_message_claude`）：同上模式：

```python
runner._assemble_system_message(messages, "", injection, model="claude-sonnet-4-6")
```

**L127**（`test_assemble_system_message_empty_injection`）：

```python
# 旧
runner._assemble_system_message(messages, "", model="ark-code-latest")
# 新
runner._assemble_system_message(messages, "", "", model="ark-code-latest")
```

**L143**（`test_assemble_system_message_non_system_first_msg`）：

```python
# 旧
runner._assemble_system_message(messages, "inj", model="ark-code-latest")
# 新
runner._assemble_system_message(messages, "", "inj", model="ark-code-latest")
```

这些测试的场景都是"memory_section 为空"，所以第一个参数传 `""` 即可。

- [ ] **Step 3: 修复 `tests/test_on_before_llm_method.py` 断言索引**

新签名 `(messages, memory_section, injection, model)` 下，`args[0][1]` 是 `memory_section`，`args[0][2]` 才是 `injection`。原断言都是 `args[0][1] == "..."`，需要改为 `args[0][2]`。

**L50**（`test_on_before_llm_calls_inject_and_assemble`）：

```python
# 旧
assert args[0][1] == "INJECTION TEXT" or args.kwargs.get("injection") == "INJECTION TEXT"
# 新
assert args[0][2] == "INJECTION TEXT" or args.kwargs.get("injection") == "INJECTION TEXT"
```

**L116-119**（`test_on_before_llm_first_turn_merges_resources`）：

```python
# 旧
injection_arg = args[0][1]
# 新
injection_arg = args[0][2]
```

后续 `assert "DYNAMIC_INJECTION" in injection_arg` 等断言行不变。

**L137-138**（`test_on_before_llm_second_turn_no_resources_merge`）：

```python
# 旧
injection_arg = args[0][1]
# 新
injection_arg = args[0][2]
```

- [ ] **Step 4: fixture 加 hermetic patch（防 `_on_before_llm` 读真实 ~/.niu/memory.json）**

`_on_before_llm` 现在每轮调 `_load_memory_for_prompt()`，会读开发机真实 `~/.niu/memory.json`，违反 hermetic。在 fixture（L22-33）末尾加一行 patch：

```python
@pytest.fixture
def runner(monkeypatch):
    """构造一个最小化 NiuRunner 实例（C2 + M1 修复：补齐 _inject_dynamic_resources 访问的所有属性）

    故意跳过 __init__，已预填 _inject_dynamic_resources 当前实际访问的所有实例属性；
    若未来 _inject_dynamic_resources 新增实例属性访问，需同步更新此 fixture。
    """
    runner = NiuRunner.__new__(NiuRunner)
    # skill 计数器相关（_inject_dynamic_resources L2154-2167 访问）
    runner._skill_score_counter = {}
    runner._skill_entity_cache = {}
    # _assemble_system_message 访问（C2 修复：缺 dynamic_system_prefix 必跑 AttributeError）
    runner.default_model = "test-model"
    runner.static_system_prompt = "STATIC SYSTEM PROMPT"
    runner.dynamic_system_prefix = ""  # C2 修复：_assemble_system_message L782 访问
    # _format_lightrag_entities_for_prompt 访问的两个黑名单（类属性，L1859-1860 定义）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    # 每轮重读 memory.json 后，patch 掉避免读真实 ~/.niu/memory.json（hermetic）
    monkeypatch.setattr("agent.runner._load_memory_for_prompt", lambda: "")
    return runner
```

注意 fixture 签名从 `def runner():` 改为 `def runner(monkeypatch):`。

- [ ] **Step 5: 修复 `tests/test_lightrag_retrieval_migration.py:466-486` hermetic**

`test_on_before_llm_uses_lightrag` 走真实 `_on_before_llm`，Task 2 之后会真实调 `_load_memory_for_prompt()`，读开发机真实 `~/.niu/memory.json`，违反 hermetic 原则（CLAUDE.md 铁律 5：测试必须用真实数据但不得污染真实数据；同时单元测试不应依赖机器状态）。

当前代码（L466-486）：

```python
def test_on_before_llm_uses_lightrag(self, runner):
    """..."""
    runner.base_system_prompt = "system prompt"
    runner.static_system_prompt = "system prompt"
    runner.dynamic_system_prefix = ""
    runner.default_model = "test-model"
    runner._first_turn_extra_injection = ""
    runner._memory_dirty = MagicMock()
    runner._memory_dirty.is_set.return_value = False

    with patch.object(runner, "_inject_dynamic_resources", return_value=("injected text", {})) as mock_inject, \
         patch.object(runner, "_extract_context_from_messages", return_value="context"):
        messages = [{"role": "system", "content": "system prompt"}]
        runner._on_before_llm(messages, turn=1)

        mock_inject.assert_called_once_with("context")
        assert "injected text" in messages[0]["content"]
```

改为（在 with 块第一行加 `patch("agent.runner._load_memory_for_prompt", return_value="")`；`_memory_dirty` mock 暂时保留，Task 4 删除方法时再删）：

```python
def test_on_before_llm_uses_lightrag(self, runner):
    """..."""
    runner.base_system_prompt = "system prompt"
    runner.static_system_prompt = "system prompt"
    runner.dynamic_system_prefix = ""
    runner.default_model = "test-model"
    runner._first_turn_extra_injection = ""
    runner._memory_dirty = MagicMock()
    runner._memory_dirty.is_set.return_value = False

    # patch _load_memory_for_prompt 返回空，避免读真实 ~/.niu/memory.json
    with patch("agent.runner._load_memory_for_prompt", return_value=""), \
         patch.object(runner, "_inject_dynamic_resources", return_value=("injected text", {})) as mock_inject, \
         patch.object(runner, "_extract_context_from_messages", return_value="context"):
        messages = [{"role": "system", "content": "system prompt"}]
        runner._on_before_llm(messages, turn=1)

        mock_inject.assert_called_once_with("context")
        assert "injected text" in messages[0]["content"]
```

注意：本 Task 只加 `patch("agent.runner._load_memory_for_prompt", ...)`，**不删** `_memory_dirty = MagicMock()` 两行——后者等 Task 4 删除方法时同步删。

- [ ] **Step 6: 跑修复后的既有测试**

```bash
pytest tests/test_prompt_cache.py tests/test_on_before_llm_method.py tests/test_lightrag_retrieval_migration.py -v
```

预期：全部 PASS（除 `_refresh_user_memories` 相关测试仍正常 PASS，因为 Task 4 才会删方法）。

- [ ] **Step 7: Commit**

```bash
git add tests/test_prompt_cache.py tests/test_on_before_llm_method.py tests/test_lightrag_retrieval_migration.py
git commit -m "test: 适配 _assemble_system_message 新 4 参签名 + hermetic patch

变更：
- test_prompt_cache.py: 4 处旧 3 参调用改 4 参（memory_section 传 \"\"）
- test_prompt_cache.py: test_build_static_system_prompt_excludes_current_time docstring 更新
- test_on_before_llm_method.py: 3 处断言 args[0][1] → args[0][2]
- test_on_before_llm_method.py: fixture patch _load_memory_for_prompt 保持 hermetic
- test_lightrag_retrieval_migration.py: patch _load_memory_for_prompt 保持 hermetic

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 删除 dirty 机制 + 同步删除失效测试（原子 Task）

每轮重读之后，dirty 标志 + 局部刷新机制变成死代码。删除。同时**在同一原子 commit 内**删除/修改指向已删方法的既有测试，避免中间状态测试失败。

**Files:**
- Modify: `agent/runner.py:617-618`（删除 `_memory_dirty` 初始化）
- Modify: `agent/runner.py:1811-1848`（删除 `_refresh_user_memories` 方法）
- Modify: `agent/runner.py:839-855`（`_on_turn_end` 删除 `_refresh_user_memories` 调用）
- Modify: `agent/runner.py:577-578`（更新 static_system_prompt 注释）
- Modify: `agent/runner.py:614-615`（更新 base_system_prompt 注释）
- Modify: `agent/runner.py:155-157`（更新 `_render_permanent_section` docstring）
- Modify: `agent/handler.py:1191-1207`（删除 dirty 设置块 1，保留 status 分支）
- Modify: `agent/handler.py:1267-1284`（删除 dirty 设置块 2，保留 status 分支）
- Modify: `agent/handler.py:492`（删除指向旧机制的注释）
- Modify: `agent/handler.py:1165`（更新 `memory dirty flag` 自然语言注释为 `status 检查`）
- Modify: `tests/test_prompt_cache.py:199-242`（删除 `test_refresh_user_memories_updates_static_and_recomputes_base`）
- Modify: `tests/test_on_before_llm_method.py:79-96`（删除 `_refresh_user_memories` 断言 + docstring 微调）
- Modify: `tests/test_lightrag_retrieval_migration.py:477-478`（删除 `_memory_dirty` mock 两行）

- [ ] **Step 1: 删除 `_memory_dirty` 初始化**

`agent/runner.py:617-618`：

```python
# 用户记忆脏标记（remember/forget 工具调用后 set）
self._memory_dirty = threading.Event()
```

删除这两行。

- [ ] **Step 2: 删除 `_refresh_user_memories` 方法**

`agent/runner.py:1811-1848` 整个方法删除：

```python
def _refresh_user_memories(self, messages: list):
    """Refresh the ### [用户长期记忆] section in system prompt if dirty"""
    if not self._memory_dirty.is_set():
        return
    # ...（整个方法体删除）
```

- [ ] **Step 3: 删除 `_on_turn_end` 中的 `_refresh_user_memories` 调用**

`agent/runner.py:843` 附近的注释：

```python
"""每轮循环结束后的清理工作（动态注入已移到 _on_before_llm）。

保留：
- _refresh_user_memories：刷新用户长期记忆（dirty 检测）
- 脑区衰减 decay_all：每轮降低脑区激活级别
```

以及 L851 附近的调用：

```python
self._refresh_user_memories(messages)
```

同时删除注释里的 `_refresh_user_memories` 一行 + 调用行。

- [ ] **Step 4: 删除 handler.py 两处 dirty 设置**

`agent/handler.py:1191-1207` 当前完整代码（**真实代码**，用 Read 工具读过）：

```python
                if is_success:
                    # Set memory dirty flag for user memory tools on success
                    if real_tool_name in ("memory-server/user_memory_remember", "memory-server/user_memory_forget"):
                        try:
                            from agent.runner import get_runner
                            runner = get_runner()
                            if runner and hasattr(runner, '_memory_dirty'):
                                runner._memory_dirty.set()
                        except Exception as e:
                            logger.debug(f"Memory dirty flag set failed: {e}")
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    return StepOutcome(result, next_prompt="")
```

改为（**仅删除 L1192-1200 共 9 行 dirty flag 代码块**，从 `# Set memory dirty flag...` 注释到 `logger.debug(...)` 结束；is_success 之后的 status 判断 + StepOutcome 返回分支**全部原样保留**）：

```python
                if is_success:
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    return StepOutcome(result, next_prompt="")
```

`agent/handler.py:1267-1284` 同样处理。当前完整代码（**真实代码**，用 Read 工具读过）：

```python
                if is_success:
                    # Set memory dirty flag for user memory tools on success
                    if tool_name in ("memory-server/user_memory_remember", "memory-server/user_memory_forget"):
                        try:
                            from agent.runner import get_runner
                            runner = get_runner()
                            if runner and hasattr(runner, '_memory_dirty'):
                                runner._memory_dirty.set()
                        except Exception as e:
                            logger.debug(f"Memory dirty flag set failed: {e}")
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    # 需要进一步处理，返回anchor prompt
                    return StepOutcome(result, next_prompt="")
```

改为（**仅删除 L1268-1276 共 9 行 dirty flag 代码块**；is_success 之后的 status 判断 + StepOutcome 返回分支 + else 分支**全部原样保留**）：

```python
                if is_success:
                    # status 为 ok/success 表示任务完成，提示汇报；其他非 error 状态（need_category 等）让 LLM 自行判断
                    if isinstance(result, dict) and result.get("status") in ("ok", "success"):
                        return StepOutcome(result, next_prompt="")
                    else:
                        return StepOutcome(result, next_prompt="")
                else:
                    # 需要进一步处理，返回anchor prompt
                    return StepOutcome(result, next_prompt="")
```

**关键**：两处删除的都是 `if real_tool_name/tool_name in (...)` 开头的 9 行（含 `try/except`），**不要**误删 is_success 之后的 `if isinstance(result, dict) and result.get("status") in ("ok", "success"):` 分支。

- [ ] **Step 5: 清理指向旧机制的注释（5 处）**

每轮重读之后，以下注释指向已删除的旧机制，必须清理避免误导后续维护者。

**Step 5.1：`agent/runner.py:577-578` static_system_prompt 注释**

当前：

```python
# 静态段：niu.md + memory（cache 友好，字节稳定）
# memory 变化时由 _refresh_user_memories 同步更新此属性
self.static_system_prompt = self._build_static_system_prompt()
```

改为：

```python
# 静态段：仅 niu.md（cache 友好，字节稳定）
# memory 段由 _on_before_llm 每轮从 memory.json 重读，不在此缓存
self.static_system_prompt = self._build_static_system_prompt()
```

**Step 5.2：`agent/runner.py:614-615` base_system_prompt 注释**

当前：

```python
# 向后兼容：base_system_prompt = 静态段 + 动态前缀段（不含 injection）
self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix
```

改为：

```python
# 向后兼容：base_system_prompt = 静态段(仅 niu.md) + 动态前缀段
# 不含 memory 段（每轮 _on_before_llm 重读）和 injection（每轮动态生成）
self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix
```

**Step 5.3：`agent/runner.py:155-157` `_render_permanent_section` docstring**

当前：

```python
def _render_permanent_section(permanent: list) -> str:
    """Render permanent memory items into a system prompt section.
    Shared by _load_memory_for_prompt and _refresh_user_memories."""
```

改为：

```python
def _render_permanent_section(permanent: list) -> str:
    """Render permanent memory items into a system prompt section.
    Used by _load_memory_for_prompt."""
```

**Step 5.4：`agent/handler.py:492` 注释**

当前：

```python
# Note: memory dirty flag is set in MCP dispatch path (see dispatch() method)
```

直接删除（指向的 dirty flag 机制已不存在）。

**Step 5.5：`agent/handler.py:1165` 注释**

当前（**真实代码**，用 Read 工具读过）：

```python
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查和 memory dirty flag
                result = disk_result.raw_result
```

改为（去掉 `memory dirty flag` 残留，只保留 status 检查说明）：

```python
            if disk_result.action == "EXECUTE":
                # 返回原始 MCP 结果，保留 status 检查
                result = disk_result.raw_result
```

**注意**：本处是自然语言注释（`memory dirty flag`），不含代码标识符 `_memory_dirty` 或 `_refresh_user_memories`，因此前面的 grep 抓不到，必须在 Step 9 的 grep 命令里加 `memory dirty` 自然语言匹配才能验证干净。

- [ ] **Step 6: 删除 `tests/test_prompt_cache.py:199-242` 的 `_refresh_user_memories` 测试**

`test_refresh_user_memories_updates_static_and_recomputes_base` 调 `runner._refresh_user_memories([])`，本 Task 已删除该方法，测试失去目标。整个测试函数删除（包括 `import sys` / `mem_src` / `sys.path.insert` 这些仅为该测试服务的代码块，从 L199 到 L242 共 44 行）。

**注意**：不要删文件开头的其他测试。只删 `def test_refresh_user_memories_updates_static_and_recomputes_base():` 这一个函数及其函数体。

- [ ] **Step 7: 删除 `tests/test_on_before_llm_method.py:79-96` 的 `_refresh_user_memories` 断言**

`test_on_turn_end_no_longer_calls_inject` 在 L83 设置了 `runner._refresh_user_memories = MagicMock()`，并在 L96 断言 `runner._refresh_user_memories.assert_called_once()`。本 Task 之后 `_on_turn_end` 不再调 `_refresh_user_memories`（方法本身已删除），断言必失败。

修改方式：
1. 删除 L83 `runner._refresh_user_memories = MagicMock()`
2. 删除 L95-96 两行（注释 `# 但 _refresh_user_memories 应被调用（保留）` + 断言）
3. docstring 也需要微调（L79-80）：把 "3. _on_turn_end 不再调 _inject_dynamic_resources" 改为 "3. _on_turn_end 不再调 _inject_dynamic_resources（也不再调已删除的 _refresh_user_memories）"

- [ ] **Step 8: 删除 `tests/test_lightrag_retrieval_migration.py:477-478` 的 `_memory_dirty` mock**

`test_on_before_llm_uses_lightrag` 在 Task 3 Step 5 之后仍包含：

```python
runner._memory_dirty = MagicMock()
runner._memory_dirty.is_set.return_value = False
```

本 Task 已删除 `_memory_dirty` 属性，这两行 mock 失去目标，删除。

- [ ] **Step 9: 语法检查 + 残留搜索**

```bash
python3 -c "import ast; ast.parse(open('agent/runner.py').read()); ast.parse(open('agent/handler.py').read()); print('syntax ok')"
grep -rn "_memory_dirty\|_refresh_user_memories\|memory dirty" agent/ tests/ --include="*.py"
```

预期：
- 语法检查 `syntax ok`
- grep 输出为空（agent/ 和 tests/ 都无残留，包括代码标识符和自然语言注释）

**注意**：第三个 pattern `memory dirty` 是自然语言匹配，用来抓 Step 5.5 这类不含代码标识符的注释残留。如果只用前两个代码标识符，handler.py:1165 这种注释会漏检。

- [ ] **Step 10: 跑受影响的既有测试**

```bash
pytest tests/test_prompt_cache.py tests/test_on_before_llm_method.py tests/test_lightrag_retrieval_migration.py -v
```

预期：全部 PASS。

- [ ] **Step 11: Commit**

```bash
git add agent/runner.py agent/handler.py tests/test_prompt_cache.py tests/test_on_before_llm_method.py tests/test_lightrag_retrieval_migration.py
git commit -m "refactor(runner): 删除 _memory_dirty + _refresh_user_memories 机制

每轮重读 memory.json 后，dirty 标志 + 局部刷新机制成为死代码。
handler.py 中 2 处 dirty 设置点一并删除。
同步删除/修改指向已删方法的 3 处既有测试，保证原子 commit 不引入失败：
- test_prompt_cache.py: 删 test_refresh_user_memories_updates_static_and_recomputes_base
- test_on_before_llm_method.py: 删 _refresh_user_memories 断言
- test_lightrag_retrieval_migration.py: 删 _memory_dirty mock

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 编写测试覆盖每轮重读行为

新建测试文件验证 5 个 memory 段的每轮重读。

**Files:**
- Create: `tests/test_runner_memory_refresh.py`

- [ ] **Step 1: 写测试文件**

**关键设计决策**：fixture 必须每个测试独立的 `tmp_path`（pytest 自带，天然隔离）。**不要**用 `tmp_path.parent` 或共享目录——同一 pytest 会话内多个测试共享 basetemp，会导致 `test_memory_file_missing_returns_empty` 这类"验证文件不存在"的测试必失败（前面测试已写入同一文件），并且测试间相互污染。

```python
"""验证 memory 派生段每轮从 memory.json 重读。

覆盖场景：
1. firstRun=true → false 后，"## 首次使用"段从 system prompt 消失
2. user.name 从占位符变为实值后，"## 用户信息"段出现在 system prompt
3. workspace.path 从占位符变为实值后，"## 工作目录"段出现在 system prompt
4. permanent 新增条目后，"### [用户长期记忆]"段立即反映
5. memory.json 不存在时，memory_section 为空字符串
"""

import json
import pytest
from pathlib import Path

from agent.runner import _load_memory_for_prompt


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    """Mock ~/.niu/memory.json 到独立临时目录（每个测试独立 home）

    关键：用 pytest 自带的 tmp_path 作为 fake_home，每个测试独立目录天然隔离，
    避免 basetemp 共享导致的交叉污染（如 test_memory_file_missing_returns_empty
    会被前面测试写入的 memory.json 干扰）。
    """
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    niu_dir = fake_home / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    return niu_dir / "memory.json"


def test_first_run_true_includes_prompt(memory_file):
    """firstRun=true 时，memory_section 包含 '## 首次使用'"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": True,
    }))
    section = _load_memory_for_prompt()
    assert "## 首次使用" in section
    assert "工作目录想放在哪里" in section


def test_first_run_false_excludes_prompt(memory_file):
    """firstRun=false 时，memory_section 不包含 '## 首次使用'"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": False,
    }))
    section = _load_memory_for_prompt()
    assert "## 首次使用" not in section


def test_first_run_removed_after_write(memory_file):
    """模拟 Agent 写入 memory.json 把 firstRun 改为 false，下轮调用读到新状态"""
    # 初始：firstRun=true
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": True,
    }))
    section1 = _load_memory_for_prompt()
    assert "## 首次使用" in section1

    # Agent 写入：firstRun=false + workspace.path
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "/Users/li/work"},
        "firstRun": False,
    }))
    section2 = _load_memory_for_prompt()
    assert "## 首次使用" not in section2
    assert "/Users/li/work" in section2  # 工作目录段出现


def test_user_fields_placeholder_to_real(memory_file):
    """user 字段从占位符变为实值后，## 用户信息 段出现"""
    # 初始：占位符
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "user": {
            "name": "请询问用户真实姓名",
            "nickname": "请询问用户称呼",
            "occupation": "请询问用户职业",
            "organization": "请询问用户工作单位",
        },
    }))
    section1 = _load_memory_for_prompt()
    assert "## 用户信息" not in section1  # 占位符不出现

    # Agent 写入实值
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "user": {
            "name": "李雷",
            "nickname": "雷子",
            "occupation": "软件工程师",
            "organization": "ACME",
        },
    }))
    section2 = _load_memory_for_prompt()
    assert "## 用户信息" in section2
    assert "李雷" in section2
    assert "软件工程师" in section2


def test_permanent_updates_reflect_immediately(memory_file):
    """permanent 新增条目后，### [用户长期记忆] 段立即反映"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "permanent": [],
    }))
    section1 = _load_memory_for_prompt()
    assert "用户长期记忆" not in section1 or "共0" in section1

    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "permanent": [
            {"type": "memory", "content": "座右铭：先做后说"}
        ],
    }))
    section2 = _load_memory_for_prompt()
    assert "先做后说" in section2


def test_memory_file_missing_returns_empty(memory_file):
    """memory.json 不存在时，返回空字符串

    依赖 fixture 提供独立 tmp_path（每个测试独立 home），
    本测试不写 memory.json，验证 _load_memory_for_prompt 在文件缺失时返回 ""。
    """
    assert not memory_file.exists()
    section = _load_memory_for_prompt()
    assert section == ""


def test_workspace_placeholder_not_shown(memory_file):
    """workspace.path 是占位符时，## 工作目录 段不出现"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "请询问用户指定工作目录"},
    }))
    section = _load_memory_for_prompt()
    assert "## 工作目录" not in section


def test_workspace_real_path_shown(memory_file):
    """workspace.path 是实值时，## 工作目录 段出现"""
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "/Users/li/knowledge"},
    }))
    section = _load_memory_for_prompt()
    assert "## 工作目录" in section
    assert "/Users/li/knowledge" in section
```

- [ ] **Step 2: 跑测试**

```bash
pytest tests/test_runner_memory_refresh.py -v
```

预期：8 个测试全部 PASS。

如果失败：
- 检查 `_load_memory_for_prompt` 的 placeholder 过滤逻辑（`str.startswith("请询问")`）是否按预期工作
- 检查 `_render_permanent_section` 对空 permanent 列表的输出格式
- 检查 fixture 的 `Path.home` patch 是否生效（在 `_load_memory_for_prompt` 内部 `Path.home()` 应返回 tmp_path）

- [ ] **Step 3: Commit**

```bash
git add tests/test_runner_memory_refresh.py
git commit -m "test(runner): 新增 memory 每轮重读测试

8 个测试覆盖：firstRun 开/关、user 字段占位符→实值、permanent 更新、
workspace 占位符/实值、memory.json 缺失。
fixture 用 tmp_path 独立 home，避免测试间污染。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 集成测试 — 完整 chat 链路验证每轮重读

跑一个集成测试：模拟 Agent 在第一轮写入 memory.json，验证第二轮 `_on_before_llm` 生成的 system prompt 已经反映新状态。

**Files:**
- Modify: `tests/test_runner_memory_refresh.py`（追加集成测试）

- [ ] **Step 1: 追加集成测试**

**关键设计决策**：用 `NiuRunner.__new__(NiuRunner)` 绕过 `__init__`，避免触发真实副作用（参考 `tests/test_on_before_llm_method.py:22` 的既有模式）。直接 `NiuRunner(llm_config=...)` 会触发：
- 扫真实 `~/.niu/agents/`（os.path，不受 Path.home patch 影响）
- `get_tools_schema()` 读真实 tools_schema.json
- `get_skill_sync(auto_start=True)` 启动 watchdog + daemon 线程，可能真实初始化 LightRAG
- DiskEngine 读真实 config
- MCPClientManager

违反项目铁律"测试不得操作真实 ~/.niu 数据"。

在 `tests/test_runner_memory_refresh.py` 末尾追加：

```python
def test_integration_second_turn_reflects_write(memory_file, monkeypatch):
    """集成：第一轮写入 memory.json，第二轮 _on_before_llm 生成的 system prompt 已反映"""
    from agent.runner import NiuRunner

    # 初始：firstRun=true
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "firstRun": True,
    }))

    # 用 __new__ 绕过 __init__（避免真实 ~/.niu 副作用 + LightRAG 初始化 + MCP 加载）
    # 参考 tests/test_on_before_llm_method.py:22 的既有模式
    runner = NiuRunner.__new__(NiuRunner)
    runner.default_model = "test-model"
    runner.static_system_prompt = "# niu.md content"
    runner.dynamic_system_prefix = ""
    runner._first_turn_extra_injection = ""

    # Mock _inject_dynamic_resources 和 _extract_context_from_messages（避免真实 LightRAG）
    monkeypatch.setattr(runner, "_inject_dynamic_resources", lambda ctx: ("", []))
    monkeypatch.setattr(runner, "_extract_context_from_messages", lambda msgs: "")

    # 第 1 轮：firstRun=true，memory_section 应包含"## 首次使用"
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=1)
    system_content_1 = messages[0]["content"]
    if isinstance(system_content_1, list):
        text_1 = system_content_1[0]["text"] + system_content_1[1]["text"]
    else:
        text_1 = system_content_1
    assert "## 首次使用" in text_1

    # 模拟 Agent 在第 1 轮写入 memory.json：firstRun=false + workspace
    memory_file.write_text(json.dumps({
        "identity": {"name": "妞妞"},
        "workspace": {"path": "/Users/li/work"},
        "firstRun": False,
    }))

    # 第 2 轮：firstRun=false，memory_section 不应包含"## 首次使用"
    messages2 = [{"role": "system", "content": ""}, {"role": "user", "content": "next"}]
    runner._on_before_llm(messages2, turn=2)
    system_content_2 = messages2[0]["content"]
    if isinstance(system_content_2, list):
        text_2 = system_content_2[0]["text"] + system_content_2[1]["text"]
    else:
        text_2 = system_content_2
    assert "## 首次使用" not in text_2
    assert "/Users/li/work" in text_2  # 工作目录段出现
```

**关键点**：
- 用 `NiuRunner.__new__(NiuRunner)` 绕过 `__init__`，不触发真实 `~/.niu` 扫描、不初始化 LightRAG、不启动 watchdog
- 手动设置 4 个必要实例属性（`default_model` / `static_system_prompt` / `dynamic_system_prefix` / `_first_turn_extra_injection`），与 `test_on_before_llm_method.py` 的 fixture 模式一致
- `_load_memory_for_prompt` 由 fixture `memory_file` 提供的 `Path.home` patch 自动隔离（无需额外 patch）
- 只 mock 两个动态注入方法，保留真实 `_on_before_llm` 调用链

- [ ] **Step 2: 跑集成测试**

```bash
pytest tests/test_runner_memory_refresh.py::test_integration_second_turn_reflects_write -v
```

预期：PASS。

如果失败：
- 检查 `_assemble_system_message` 的 Claude list 格式 / 字符串格式分支
- 检查 `_load_memory_for_prompt` 的 Path.home patch 是否在 runner 方法内正确生效
- 检查 `_first_turn_extra_injection` 在 turn=1 后是否正确清空

- [ ] **Step 3: 跑全部新增测试**

```bash
pytest tests/test_runner_memory_refresh.py -v
```

预期：9 个测试全部 PASS（8 个单元 + 1 个集成）。

- [ ] **Step 4: 跑相关既有测试，确保无回归**

```bash
pytest tests/ -v -k "runner or memory or prompt or on_before_llm"
```

预期：所有相关测试 PASS。

如果有测试失败：
- 检查是否还有测试依赖 `_memory_dirty` 或 `_refresh_user_memories`（Task 4 应已清理）
- 检查是否还有测试断言 static_system_prompt 包含 memory 段
- 检查是否还有测试漏 patch `_load_memory_for_prompt`

- [ ] **Step 5: Commit**

```bash
git add tests/test_runner_memory_refresh.py
git commit -m "test(runner): 集成测试 — 第2轮 _on_before_llm 反映 memory.json 写入

用 NiuRunner.__new__ 模式绕过 __init__，避免真实 ~/.niu 副作用。
模拟 Agent 第1轮写入 firstRun=false，验证第2轮 system prompt 不再含'## 首次使用'。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 验证 Claude cache_control 仍然只缓存 niu.md

确认改动后 Claude 模型的 cache_control 仍然只作用于 niu.md 静态段，memory 段不被缓存。

**Files:**
- 不需要改代码，仅验证

- [ ] **Step 1: 写一个小验证脚本**

**关键设计决策**：与 Task 6 集成测试相同，用 `NiuRunner.__new__(NiuRunner)` 绕过 `__init__`，避免真实 `~/.niu` 副作用。

```bash
cat > /tmp/verify_cache_split.py <<'EOF'
"""验证 Claude 格式下 cache_control 只作用于 niu.md 段"""
from unittest.mock import patch
from agent.runner import NiuRunner

# 用 __new__ 绕过 __init__（避免真实 ~/.niu 副作用 + LightRAG 初始化）
runner = NiuRunner.__new__(NiuRunner)
runner.default_model = "claude-sonnet-4-6"
runner.static_system_prompt = "# niu.md content"
runner.dynamic_system_prefix = ""
runner._first_turn_extra_injection = ""

# Mock 动态注入 + memory 加载（避免真实依赖）
runner._inject_dynamic_resources = lambda ctx: ("[injection]", [])
runner._extract_context_from_messages = lambda msgs: ""

# patch _load_memory_for_prompt 返回固定 memory 段
with patch("agent.runner._load_memory_for_prompt", return_value="## 用户信息\n- 姓名：测试"):
    messages = [{"role": "system", "content": ""}]
    runner._on_before_llm(messages, turn=1)

content = messages[0]["content"]
assert isinstance(content, list), "Claude 模型应使用 list 格式"
assert len(content) == 2, f"应有 2 个 block，实际 {len(content)}"
assert content[0].get("cache_control") == {"type": "ephemeral"}, "第1个 block 应有 cache_control"
assert "cache_control" not in content[1], "第2个 block 不应有 cache_control"
assert content[0]["text"] == "# niu.md content", "第1个 block 应仅含 niu.md"
assert "## 用户信息" in content[1]["text"], "第2个 block 应含 memory 段"
assert "[injection]" in content[1]["text"], "第2个 block 应含 injection"
print("✓ cache_control 只作用于 niu.md 静态段（第1个 block）")
print(f"  第1段长度：{len(content[0]['text'])}")
print(f"  第2段长度：{len(content[1]['text'])}")
EOF
python3 /tmp/verify_cache_split.py
```

预期输出：`✓ cache_control 只作用于 niu.md 静态段（第1个 block）`

如果失败：
- 检查 `_assemble_system_message` 的 Claude 分支是否正确把 memory_section 放在第 2 个 block
- 检查 static_system_prompt 是否仍然只是 niu.md（不含 memory）

- [ ] **Step 2: 清理临时脚本**

```bash
rm /tmp/verify_cache_split.py
```

- [ ] **Step 3: Commit（如有改动）**

如果验证发现需要改动，commit 修复。如果验证通过无改动，跳过 commit。

---

## Task 8: 文档更新 + 最终回归

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`（如有首次启动说明）
- Modify: `docs/manual-user-guide.md`（如有首次启动说明）

- [ ] **Step 1: 检查文档是否提及首次启动机制**

```bash
grep -l "首次使用\|firstRun\|first.run\|首次启动" docs/*.md
```

- [ ] **Step 2: 如果有提及，更新为"每轮重读"机制说明**

主要看 `docs/manual-user-guide.md` 的"首次启动流程"段。如果原文说"memory.json 写入后下次启动生效"，改为"memory.json 写入后下一轮对话立即生效"。

如果原文没有相关说明，跳过此步。

- [ ] **Step 3: 全量测试回归**

```bash
pytest tests/ -v
```

预期：全部 PASS（包括既有测试 + 新增 9 个测试）。

**注意**：`tests/` 是项目实际测试目录（`tests/agent/` 不存在）。所有既有测试 + Task 5 新增的 8 个单元测试 + Task 6 新增的 1 个集成测试都应 PASS。

- [ ] **Step 4: 真实数据验证（用户配合）**

提示用户：

> 改动完成，建议你做一次真实验证：
> 1. 备份当前 `~/.niu/memory.json`
> 2. 把 `firstRun` 改回 `true`，把 `workspace.path` 改回 `"请询问用户指定工作目录"`
> 3. 启动程序 `./niu`，观察首轮是否提示"工作目录想放在哪里"
> 4. 回答路径后，观察**下一轮**对话是否不再出现"## 首次使用"提示（这是本次修复的核心）
> 5. 同时观察"## 工作目录"段是否立即出现你回答的路径
> 6. 验证完成后恢复 backup 的 memory.json

- [ ] **Step 5: Commit（如有文档改动）**

```bash
git add docs/
git commit -m "docs: 首次启动机制说明对齐'每轮重读'实现

memory.json 写入后下一轮对话立即生效（不再依赖重启或 dirty 标志）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**:
- ✅ `_load_memory_for_prompt` 加锁防并发：Task 0
- ✅ config-manager + memory-server 原子写防 torn write：Task 0.5
- ✅ niu.md 保持缓存，memory 拆出：Task 1 + Task 2
- ✅ `_assemble_system_message` 3 个调用点全部修复 + `_on_before_llm` 每轮重读：Task 2（原子 commit）
- ✅ 签名变更影响的既有测试适配：Task 3
- ✅ 删除 dirty 机制 + 同步删除失效测试：Task 4（原子 commit）
- ✅ 注释清理（5 处指向旧机制，含 1 处自然语言注释）：Task 4 Step 5
- ✅ 5 个 memory 段（identity/workspace/user/permanent/firstRun）每轮重读：Task 5 测试全覆盖
- ✅ firstRun 写入后下一轮不再注入"## 首次使用"：Task 6 集成测试验证（用 `__new__` 模式避免真实副作用）
- ✅ Claude cache_control 仍然只作用于 niu.md：Task 7 验证

**2. Placeholder scan**:
- ✅ 无 TBD/TODO/implement later
- ✅ 所有步骤含具体代码 / 命令 / 预期输出
- ✅ Task 6 的集成测试有完整 `__new__` 模式代码，绕开 NiuRunner 构造副作用

**3. Type consistency**:
- ✅ `_load_memory_for_prompt()` 签名保持 `-> str`（Task 0/1/2/5 一致）
- ✅ `_assemble_system_message` 参数顺序 `(messages, memory_section, injection, model)` 在 Task 2 定义、Task 3/4/6 调用一致
- ✅ `_on_before_llm(messages, turn)` 签名不变（Task 2 实现、Task 3/4/6 调用一致）

**4. 风险点**:
- Task 0 的 `_memory_file_lock` 依赖 `niu_memory_server` 模块，如果单元测试环境未 preload，会 fallback 到 `nullcontext()` 不加锁——这是有意的降级，不影响功能
- Task 0 与 Task 0.5 防御层次：Task 0 锁是**纵深防御**（防御未来绕过原子写的新写入路径），Task 0.5 原子写是**主保证**（reader 永远看到完整文件）。两者并存构成多层防护：原子写覆盖常态场景，锁覆盖未来回归。即使将来新增写入路径忘记走原子写，锁仍能避免 runner 读到半写文件
- Task 0.5 的原子写是跨模块/跨进程的强保证，不依赖锁，能覆盖 config-manager 不用 `_memory_file_lock` 的场景
- Task 1 单独 commit 后 memory 段暂缺（中间状态），Task 2 完成后恢复。Task 1 的 commit message 已标注"中间状态"提醒，避免被误认为是 regression
- Task 2 签名变更必须与所有调用点适配在同一原子 commit 完成，否则 `_on_before_llm` 每轮必 TypeError（本计划已合并 Task 2 + 旧 Task 3 为原子 Task）
- Task 4 删除 `_refresh_user_memories` 必须与同步删除/修改失效测试在同一原子 commit 完成，否则中间状态 `test_refresh_user_memories_updates_static_and_recomputes_base` AttributeError、`test_on_turn_end_no_longer_calls_inject` 断言失败（本计划已合并 Task 4 + 旧 Task 6 失效测试清理为原子 Task）
- Task 4 Step 5 注释清理覆盖代码标识符（`_memory_dirty` / `_refresh_user_memories`）和自然语言（`memory dirty flag`）两类，Step 9 grep 必须带 3 个 pattern 才能验证干净
- Task 5 的 `memory_file` fixture 用 `monkeypatch.setattr(Path, "home", ...)` 在 pytest 中稳定工作（pytest 自带 tmp_path 隔离）
- Task 6 集成测试用 `__new__` 模式，需要确认 4 个必要实例属性全部设置（`default_model` / `static_system_prompt` / `dynamic_system_prefix` / `_first_turn_extra_injection`）

**5. Round 1 审查问题修复对照**:

| 审查问题 | 修复位置 |
|---------|---------|
| Critical 1: 漏改 `_assemble_system_message` 两个调用点（L2350/L1687） | 原 Task 2 Step 3/4/5 → 现合并入新 Task 2 |
| Critical 2: fixture 的 `Path.home` patch 共享 basetemp | Task 5 Step 1（改为 `tmp_path` 独立 home） |
| Critical 3: 漏改 8 处既有测试 + `pytest tests/agent/` 路径错误 | 拆为 Task 3 + Task 4 + 全文 `pytest tests/` |
| Critical 4: Task 6 集成测试 NiuRunner 构造副作用 | Task 6（改用 `__new__` 模式） |
| Major 5: `_load_memory_for_prompt` 加锁 | Task 0 |
| Major 6/7: 注释更新（4 处） | Task 4 Step 5 |
| Major 8: 残留 grep 范围扩到 tests/ | Task 4 Step 9 |
| Major 9: 走真实 `_on_before_llm` 的测试需 patch `_load_memory_for_prompt` | Task 3 Step 4/5 |

**6. Round 2 审查问题修复对照**:

| 审查问题 | 修复位置 |
|---------|---------|
| Critical 1: Task 1 Step 2 替换代码丢失 niu.md 306 行正文 | Task 1 Step 2（精准复制真实 L523-561，只删 memory 拼接两行 + docstring；yaml 解析逻辑、`sys_prompt += parts[2].strip()`、`except Exception: pass` 一字不改） |
| Critical 2: Task 2 commit 后 L837 还是 3 参调用，_on_before_llm 必 TypeError | 合并旧 Task 2 + 旧 Task 3 为新 Task 2（原子 commit 内同时改签名 + 3 个调用点 + _on_before_llm 每轮重读） |
| Major 1: 锁覆盖不全 — config-manager 的 `save_memory()` 不用 `_memory_file_lock` | 新增 Task 0.5（config-manager + memory-server 都改 tmp + os.replace 原子写，不依赖锁） |
| Major 2: Task 2 Step 2 新签名丢失 early-return guard | 新 Task 2 Step 2（"改为"代码片段开头显式保留 `if not messages or messages[0].get("role") != "system": return`） |
| Major 3: Task 4 → Task 6 中间状态既有测试失败 | 新 Task 3 仅做签名适配（不动 dirty 相关测试）；新 Task 4 把"删除方法 + 同步删除失效测试"合并为原子 commit；Task 顺序调整为 Task 3 → Task 4 → Task 5 |
| Minor 1: `test_prompt_cache.py:47-48` docstring 过时 | Task 3 Step 1 |
| Minor 2: chat() 入口读 memory 冗余 | 新 Task 2 Step 4（chat() 入口传 `""` 占位，注释说明 _on_before_llm 首轮会覆盖） |
| Minor 3: Self-Review 风险点漏 Task 2/3 中间崩溃 | Self-Review "4. 风险点" 新增一条 |

**7. Round 3 审查问题修复对照**:

| 审查问题 | 修复位置 |
|---------|---------|
| Minor 1: handler.py:1165 `memory dirty flag` 自然语言注释遗漏 | Task 4 Step 5 新增 Step 5.5 + Files 清单 + Step 9 grep 加 `memory dirty` pattern |
| Minor 2: Task 0.5 Step 1 config-manager 函数内 `import os` 冗余 | Task 0.5 Step 1（删除函数内 `import os`，`import tempfile` 提升到模块顶部；memory-server 保持函数内 import 因为顶部没有） |
| Minor 3: Task 1 单独 commit 后 memory 段暂缺未标注 | Task 1 commit message 末尾加"中间状态"说明 + Self-Review "4. 风险点" 新增一条 |
| Minor 4: Task 4 Step 4 改法代码片段不完整，可能误删 is_success 之后分支 | Task 4 Step 4（两处都改为完整展示 is_success 之后的 status 判断 + StepOutcome 返回分支，显式标注"仅删除 9 行 dirty flag，不要误删后续分支"） |
| Minor 5: Task 0 锁 vs Task 0.5 原子写防御重叠未解释 | Task 0 顶部加"防御层次说明"段 + Self-Review "4. 风险点" 新增一条 |

---

## 执行交付条件

1. 所有 10 个 Task（Task 0、Task 0.5、Task 1-8）完成，每个 Task 单独 commit
2. `pytest tests/` 全量 PASS（包括 Task 3 修复的既有测试 + Task 4 同步删除的失效测试 + Task 5/6 新增测试）
3. Task 7 验证脚本输出 `✓ cache_control 只作用于 niu.md 静态段`
4. 用户在真实环境验证：firstRun=true → 写入 → 下一轮不再出现"## 首次使用"
