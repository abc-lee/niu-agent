# Tilde Path Expansion Fix — 工具循环入口统一展开 `~/`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在工具循环入口统一展开 `~/` 路径，确保 LLM 传入的任何 `~/xxx` 路径参数在到达文件系统操作前都被转换为绝对路径，避免 LLM 不理解 `~` 导致找不到文件后乱猜乱操作。

**根因分析：** LLM 看到提示词中的 `~/.niu/xxx` 路径后，原样传给工具调用。当前 `do_write`/`do_read`/`do_edit` 通过 `_get_abs_path()` 做了 `expanduser()`，`do_bash` 通过 `bash -c` 由 shell 展开。但 **MCP 工具路径完全没有展开**（`func(**args)` 直接传参），且底层函数 `write_file()`/`edit_file()` 中的 `Path().resolve()` 不会展开 `~`。此外 `disk_executor` 绕过 `dispatch()` 直接调用 MCP 工具，也不展开 `~`。在 `dispatch()` 入口统一展开是最优雅的方案——像 Shell 处理 `~` 一样，不管路径从哪来，只要经过工具调用就自动展开。

**Architecture:** 将展开逻辑提取为可复用函数 `expand_path_args(args: dict)`，在 `handler.py` 的 `dispatch()` 和 `disk_executor.py` 的 `_execute_tool()` 中复用。同时修复 MCP 服务器内部缺失的 `expanduser()`；修复底层函数 `Path().resolve()` 不展开 `~` 的问题。

**Tech Stack:** Python, os.path

---

## File Structure

| 文件 | 职责 |
|------|------|
| `agent/handler.py` | **修改** — 添加 `expand_path_args()` 函数 + `dispatch()` 入口调用 |
| `niu_api/internal/disk_executor.py` | **修改** — `_execute_tool()` 中调用 `expand_path_args()` |
| `mcp-servers/file-parser/src/niu_file_parser/__init__.py` | **修改** — `parse_file` 的 `file_path` 加 `expanduser()` |
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | **修改** — 多个工具的路径参数加 `expanduser()` |
| `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` | **修改** — `lightrag_insert_file` 的 `file_path` 加 `expanduser()` |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | **修改** — `ingest()`/`ingest_document()` 的路径参数加 `expanduser()` |

---

### Task 1: `agent/handler.py` — `expand_path_args()` 可复用函数 + `dispatch()` 入口调用

**Files:**
- Modify: `agent/handler.py`
- Test: `tests/test_tilde_expansion.py`

**原理：** 将路径展开逻辑提取为模块级函数 `expand_path_args(args)`，在 `dispatch()` 和 `disk_executor` 中复用。在 `dispatch()` 方法开头调用此函数，对所有已知路径参数名做 `os.path.expanduser()` 展开。

**已知路径参数名（从 tools_schema.json 和 MCP 工具 schema 中提取）：**
- `file_path` — read/write/edit/grep、file-parser 的 parse_file、lightrag 的 insert_file、photo-server 的 ingest_document
- `path` — grep 的搜索路径、config-manager 的 set_workspace/mkdir/copy_to_path/move_to_path、photo-server 的 ingest
- `cwd` — bash/code_run 的工作目录
- `output_path` — 通用输出路径
- `source_path`、`dest_path` — config-manager 的 copy_to_path/move_to_path
- `workspace_path` — config-manager 的 complete_setup
- `document_root`、`database_path` — config-manager 的 set_storage_config

注意：`command`（bash）和 `code`/`script`（code_run）不在列表中——bash 命令由 shell 自己展开 `~`，code_run 的代码中路径由 Python 运行时处理。

- [ ] **Step 1: 写测试**

```python
# tests/test_tilde_expansion.py
"""测试 expand_path_args() 对 ~/ 路径参数的自动展开"""
import os


def test_expand_tilde_in_path_args():
    from agent.handler import expand_path_args
    args = {"file_path": "~/test.json", "content": "hello"}
    expand_path_args(args)
    assert args["file_path"] == os.path.expanduser("~/test.json")
    assert "~" not in args["file_path"]
    assert args["content"] == "hello"


def test_no_expand_non_tilde_path():
    from agent.handler import expand_path_args
    args = {"file_path": "/Users/xxx/test.json", "content": "hello"}
    expand_path_args(args)
    assert args["file_path"] == "/Users/xxx/test.json"


def test_no_expand_non_path_args():
    from agent.handler import expand_path_args
    args = {"content": "~/some/text", "command": "ls ~/Documents"}
    expand_path_args(args)
    assert args["content"] == "~/some/text"
    assert args["command"] == "ls ~/Documents"


def test_expand_multiple_path_args():
    from agent.handler import expand_path_args
    args = {"source_path": "~/src/file.txt", "dest_path": "~/dst/file.txt"}
    expand_path_args(args)
    assert "~" not in args["source_path"]
    assert "~" not in args["dest_path"]


def test_expand_none_value_skipped():
    from agent.handler import expand_path_args
    args = {"file_path": None, "path": "~/test"}
    expand_path_args(args)
    assert args["file_path"] is None
    assert "~" not in args["path"]


def test_expand_path_args_called_in_dispatch():
    """验证 expand_path_args 被调用后路径参数已展开"""
    from agent.handler import expand_path_args
    args = {"file_path": "~/test_dispatch.json", "content": "hello"}
    expand_path_args(args)
    assert "~" not in args["file_path"]
    assert args["file_path"] == os.path.expanduser("~/test_dispatch.json")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_tilde_expansion.py -v`
Expected: FAIL — `ImportError: cannot import name 'expand_path_args'`

- [ ] **Step 3: 在 `agent/handler.py` 中实现 `expand_path_args()` 函数**

在 `NiuHandler` 类定义之前（模块级），添加：

```python
# 统一路径参数展开 — 像 Shell 处理 ~ 一样
_PATH_ARG_NAMES = frozenset({
    "file_path", "path", "cwd", "output_path",
    "source_path", "dest_path", "workspace_path",
    "document_root", "database_path",
})


def expand_path_args(args: dict) -> None:
    """原地展开 args 中已知路径参数名的 ~/ 前缀。

    像 Shell 处理 ~ 一样，在工具调用入口统一展开。
    只展开以 ~/ 开头的值，不影响其他路径格式。
    """
    for key in _PATH_ARG_NAMES:
        val = args.get(key)
        if isinstance(val, str) and val.startswith("~/"):
            args[key] = os.path.expanduser(val)
```

- [ ] **Step 4: 在 `dispatch()` 方法开头调用 `expand_path_args(args)`**

在 `agent/handler.py` 的 `dispatch()` 方法开头（约第909行，`def dispatch` 之后、路由逻辑之前）添加一行：

```python
expand_path_args(args)
```

- [ ] **Step 5: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`

- [ ] **Step 6: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tilde_expansion.py -v`
Expected: 6 passed

- [ ] **Step 7: 提交**

```bash
git add agent/handler.py tests/test_tilde_expansion.py
git commit -m "fix: add expand_path_args() — shell-like ~/ expansion at dispatch() entry"
```

---

### Task 2: `niu_api/internal/disk_executor.py` — 复用 `expand_path_args()`

**Files:**
- Modify: `niu_api/internal/disk_executor.py`

**原理：** `disk_executor` 的 `execute()` 方法直接通过 `registry.get()` 调用 MCP 工具函数，绕过 `dispatch()`。需要在调用 `func(**kwargs)` 之前对 `kwargs` 做同样的路径展开。

- [ ] **Step 1: 在 `execute()` 方法中调用 `expand_path_args()`**

在 `niu_api/internal/disk_executor.py` 的 `execute()` 方法中，找到 `func(**kwargs)` 调用之前，添加：

```python
from agent.handler import expand_path_args
expand_path_args(kwargs)
```

- [ ] **Step 2: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('niu_api/internal/disk_executor.py').read()); print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add niu_api/internal/disk_executor.py
git commit -m "fix: expand ~/ in path args at disk_executor entry — same as dispatch()"
```

---

### Task 3: 修复底层函数 `Path().resolve()` 不展开 `~` 的问题

**Files:**
- Modify: `agent/handler.py`

**原理：** `write_file()`、`edit_file()` 等底层函数使用 `Path(file_path).resolve()` 处理路径，但 `Path().resolve()` 不展开 `~`——它把 `~` 当作当前目录下的字面子目录名。虽然在 `dispatch()` 入口已经展开了路径参数，但底层函数作为独立入口点（可能被测试代码或其他代码直接调用）也应正确处理。

- [ ] **Step 1: 搜索所有使用 `Path(.*).resolve()` 的底层函数**

Run: `grep -n 'resolve()' agent/handler.py`
确认哪些函数使用了 `Path().resolve()` 处理路径。

- [ ] **Step 2: 在每个 `Path(path).resolve()` 前加 `expanduser`**

将：
```python
file_path = str(Path(file_path).resolve())
```
改为：
```python
file_path = str(Path(os.path.expanduser(file_path)).resolve())
```

对所有匹配位置做此替换。

- [ ] **Step 3: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add agent/handler.py
git commit -m "fix: add expanduser before Path().resolve() — prevent ~ treated as literal dir"
```

---

### Task 4: MCP 服务器 — file-parser 的 `parse_file`

**Files:**
- Modify: `mcp-servers/file-parser/src/niu_file_parser/__init__.py`

- [ ] **Step 1: 在 `parse_file` 函数中加 `expanduser`**

当前（约第175行）：
```python
path = Path(file_path)
```
改为：
```python
path = Path(os.path.expanduser(file_path))
```
确保文件顶部有 `import os`（如没有则添加）。

- [ ] **Step 2: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('mcp-servers/file-parser/src/niu_file_parser/__init__.py').read()); print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/file-parser/src/niu_file_parser/__init__.py
git commit -m "fix: expanduser in file-parser parse_file — support ~/ paths"
```

---

### Task 5: MCP 服务器 — config-manager 的多个工具

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py`

需要修改的工具和参数：

| 工具 | 参数 | 约行号 |
|------|------|--------|
| `set_workspace` | `path` | 716 |
| `complete_setup` | `workspace_path` | 795 |
| `mkdir` | `path` | 870 |
| `copy_to_path` | `source_path`, `dest_path` | 894 |
| `move_to_path` | `source_path`, `dest_path` | 929 |
| `set_storage_config` | `document_root`, `database_path` | 存入配置 |

- [ ] **Step 1: 在每个工具函数中，对路径参数做 `expanduser` 后再使用**

模式：在函数开头，对每个路径参数做：
```python
path = os.path.expanduser(path) if path else path
```

对于 `copy_to_path` 和 `move_to_path`，两个参数都要展开：
```python
source_path = os.path.expanduser(source_path) if source_path else source_path
dest_path = os.path.expanduser(dest_path) if dest_path else dest_path
```

对于 `set_storage_config`，`document_root` 和 `database_path` 在存入配置前展开。

- [ ] **Step 2: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('mcp-servers/config-manager/src/niu_config_manager/__init__.py').read()); print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "fix: expanduser in config-manager path args — support ~/ paths"
```

---

### Task 6: MCP 服务器 — lightrag-server 的 `lightrag_insert_file`

**Files:**
- Modify: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`

- [ ] **Step 1: 在 `lightrag_insert_file` 函数中加 `expanduser`**

当前（约第835-836行）：
```python
original_path = str(_Path(file_path).resolve())
file = _Path(file_path)
```
改为：
```python
original_path = str(_Path(os.path.expanduser(file_path)).resolve())
file = _Path(os.path.expanduser(file_path))
```
注意：两行都需要修复。第835行的 `_Path(file_path).resolve()` 同样不会展开 `~`，会导致 `original_path` 指向错误的字面 `~/` 路径，使知识图谱源引用损坏。
确保文件顶部有 `import os`（如没有则添加）。

- [ ] **Step 2: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py').read()); print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py
git commit -m "fix: expanduser in lightrag insert_file — support ~/ paths"
```

---

### Task 7: MCP 服务器 — photo-server 的 `ingest` 和 `ingest_document`

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

- [ ] **Step 1: 在 `ingest()` 函数中对 `path` 参数加 `expanduser`**

当前（约第3154行）：
```python
Path(path)
```
改为：
```python
Path(os.path.expanduser(path))
```

- [ ] **Step 2: 在 `ingest_document()` 函数中对 `file_path` 参数加 `expanduser`**

当前（约第3310行）：
```python
Path(file_path)
```
改为：
```python
Path(os.path.expanduser(file_path))
```

- [ ] **Step 3: 语法检查**

Run: `cd <repo_root> && python -c "import ast; ast.parse(open('mcp-servers/photo-server/src/niu_photo_server/__init__.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "fix: expanduser in photo-server ingest/ingest_document — support ~/ paths"
```

---

## 验证清单

1. `python -m pytest tests/test_tilde_expansion.py -v` — 6 个测试通过
2. 启动程序，在对话中让 LLM 用 `write` 工具写入 `~/test_tilde.json`，确认文件出现在 `~/.niu/` 下而非工作目录
3. 让 LLM 用 `bash` 执行 `ls ~/.niu/`，确认输出正确（shell 展开）
4. 让 LLM 用 MCP 工具（如 `parse_file`）传入 `~/xxx` 路径，确认不报错
5. 触发一次压缩，确认 `compress_plan.json` 写入 `~/.niu/` 而非工作目录
6. `grep -rn 'Path(.*).resolve()' agent/handler.py` — 确认所有 `resolve()` 前都有 `expanduser()`
7. 验证 `disk_executor` 路径：通过 disk 命令调用 MCP 工具传入 `~/` 路径，确认不报错
