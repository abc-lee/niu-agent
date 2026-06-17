# 本地 Tokenizer 替换 o200k_base 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 token 计数从 litellm.token_counter(model="gpt-4o")（o200k_base）替换为本地 HuggingFace tokenizer，消除中文 token 高估 ~1.3x 的问题。

**Architecture:** 引入一个统一的 `TokenCalculator` 类，启动时固定加载 DeepSeek-V3 本地 tokenizer.json，所有 token 计数调用点统一走这个类。tokenizer.json 文件存放在 `models/tokenizers/deepseek-v3/` 目录，随项目分发。MCP 服务器通过确保项目根目录在 sys.path 中来共享同一个 TokenCalculator。

**Tech Stack:** tokenizers (HuggingFace，已安装)、litellm (保留回退)、json (配置读取)

---

## 核心风险与决策

### 风险1：新 tokenizer 产出更少 token，导致下游阈值判断偏移

o200k_base 对中文高估 ~1.3x，换成真实 tokenizer 后 token 数减少 ~30%。
这意味着 `usage_percent` 会降低，可能导致：
- 压缩模式从"半破坏性"降级为"非破坏性"（需 usage_percent >= 50 才选半破坏性）
- journal-agent 调用条件从满足变为不满足（需 usage_percent >= 50）

**应对**：这是**期望行为**。当前 o200k_base 高估导致过早触发压缩、过度删除消息。换成真实 tokenizer 后，压缩时机更准确，这正是我们要修复的。

### 风险2：纯文本 tokenizer 无法处理 messages 结构化开销

litellm.token_counter 的 messages 模式会自动加上 role 标记、ChatML 格式开销（每条消息约 +3-7 token）、tool_calls 序列化。纯 HuggingFace tokenizer 只能处理纯文本。

**应对**：TokenCalculator 对 messages 模式采用"逐条文本编码 + 结构开销加成"策略：
- 每条消息的 content 用 HuggingFace tokenizer 编码
- 加上固定开销：role 标记 +5 token（`<|im_start|>role\n` 约 3 token + `<|im_end|>` 约 2 token）
- tool_calls 每条 +6 token（函数名 + id 序列化）
- tool 角色消息有 tool_call_id 时 +3 token
- 开销常量将在 Task 2 中用实测数据校准

### 风险3：子 Agent FIFO 裁剪延迟

**应对**：同风险1，这是期望行为。当前 FIFO 裁剪过早删除消息。

### 风险4：MCP 服务器无法 import agent.token_calculator

MCP 服务器从 `mcp-servers/<name>/src/` 加载，该目录不含 `agent/` 包。

**应对**：在 `mcp_loader.py` 中确保项目根目录在 `sys.path` 中。同进程架构下这是安全的。

### 风险5：回退到 litellm o200k_base 时行为不一致

当 tokenizer.json 文件缺失时，TokenCalculator 回退到 litellm o200k_base，产出又变回高估值。

**应对**：回退时输出 WARNING 日志，并在 stats API 中暴露当前 tokenizer 状态。字符回退使用 CJK 感知估算（中文字符 * 1.5 + 英文字符 * 0.25），比 `len(text)//2` 更准确。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `agent/token_calculator.py` | 新建：TokenCalculator 类，统一 token 计数入口 |
| `models/tokenizers/deepseek-v3/tokenizer.json` | 新建：DeepSeek-V3 tokenizer 文件（7.5MB） |
| `agent/mcp_loader.py` | 修改：确保项目根目录在 sys.path 中（MCP 服务器 import 兼容） |
| `agent/context_manager.py:83-104` | 修改：count_tokens_simple 改用 TokenCalculator |
| `agent/generic/agent_loop.py:40-50` | 修改：count_messages_tokens 改用 TokenCalculator |
| `agent/subagent.py:19-42` | 修改：count_tokens_for_text 改用 TokenCalculator |
| `agent/session.py:316-319` | 修改：delete_messages_by_ids 改用 TokenCalculator |
| `agent/runner.py:641-651` | 修改：_recalc_msg_stats 改用 TokenCalculator |
| `niu_api/compat.py` L236, L267, L905, L1093, L1181, L1272, L1473, L1561 | 修改：所有内联 litellm.token_counter 改用 TokenCalculator |
| `niu_api/internal/region_manager.py:1172,1189,1403,1420` | 修改：prompt 截断改用 TokenCalculator |
| `mcp-servers/memory-server/src/niu_memory_server/__init__.py:135` | 修改：_count_tokens 改用 TokenCalculator |
| `mcp-servers/session-manager/src/niu_session_manager/__init__.py:171,372` | 修改：token 计数改用 TokenCalculator |

---

## Task 1: 创建 TokenCalculator 类

**Files:**
- Create: `agent/token_calculator.py`

- [ ] **Step 1: 写 TokenCalculator 类**

```python
"""统一 token 计数模块 — 使用本地 HuggingFace tokenizer 替代 o200k_base。

中文场景下 o200k_base 对中文高估约 1.3x，导致压缩过早触发。
本模块根据当前模型加载对应的本地 tokenizer.json，产出与实际 API 一致的 token 计数。
"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent / "models" / "tokenizers"

# 固定使用 DeepSeek-V3 tokenizer（对中文场景最准确，无需根据模型名动态选择）
_DEFAULT_TOKENIZER = "deepseek-v3"

# messages 结构开销（ChatML 格式：每条消息的 role 标记 + 格式标记）
# <|im_start|>role\n 约 3 token + <|im_end|>\n 约 2 token = 5
_MSG_OVERHEAD = 5
# tool_calls 序列化额外开销（函数名 + 参数括号 + id）
_TOOL_CALL_OVERHEAD = 6
# tool 角色消息中 tool_call_id 序列化开销
_TOOL_CALL_ID_OVERHEAD = 3


class TokenCalculator:
    """统一 token 计数入口。启动时加载本地 tokenizer，所有计数调用走此类。"""

    _instance: Optional["TokenCalculator"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tokenizer = self._load_tokenizer()
        self._using_fallback = self._tokenizer is None

    @classmethod
    def get(cls) -> "TokenCalculator":
        """获取全局单例。首次调用时自动初始化。线程安全。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
                    if cls._instance._using_fallback:
                        logger.warning("[TokenCalculator] Using litellm fallback — token counts may be overestimated for CJK text")
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例，用于模型切换后重新初始化。"""
        with cls._instance_lock:
            cls._instance = None

    @property
    def using_fallback(self) -> bool:
        """当前是否在使用回退模式（litellm o200k_base）。"""
        return self._using_fallback

    @property
    def tokenizer_name(self) -> str:
        """当前使用的 tokenizer 名称。"""
        if self._tokenizer is not None:
            return _DEFAULT_TOKENIZER
        return "litellm-o200k-fallback"

    def _load_tokenizer(self):
        """从本地文件加载 HuggingFace tokenizer。"""
        tokenizer_path = _BASE_DIR / _DEFAULT_TOKENIZER / "tokenizer.json"

        if not tokenizer_path.exists():
            logger.warning(f"[TokenCalculator] tokenizer not found: {tokenizer_path}, falling back to litellm")
            return None

        try:
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(str(tokenizer_path))
            logger.info(f"[TokenCalculator] loaded tokenizer: {tokenizer_path}")
            return tok
        except Exception as e:
            logger.warning(f"[TokenCalculator] failed to load tokenizer: {e}, falling back to litellm")
            return None

    def count_text(self, text: str) -> int:
        """计算纯文本的 token 数。"""
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text).ids)
        # 回退到 litellm o200k_base
        try:
            from litellm import token_counter
            return token_counter(model="gpt-4o", text=text)
        except Exception:
            return _cjk_aware_estimate(text)

    def count_messages(self, messages: List[Dict]) -> int:
        """计算消息列表的 token 数，逐条计算并包含结构开销。

        采用逐条计算而非一次性计算，与 compat.py 和 runner.py 中
        逐条 msg_tokens 求和的方式保持一致，避免两种路径产出不同结果。
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            total += self.count_text(content)
            total += _MSG_OVERHEAD
            # tool_calls 额外开销（assistant 消息）
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += len(tool_calls) * _TOOL_CALL_OVERHEAD
            # tool 角色消息的 tool_call_id 开销
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                total += _TOOL_CALL_ID_OVERHEAD
        return total

    def count_message_single(self, role: str, content: str) -> int:
        """计算单条消息的 token 数（含结构开销）。"""
        overhead = _MSG_OVERHEAD
        if role == "tool":
            overhead += _TOOL_CALL_ID_OVERHEAD
        return self.count_text(content) + overhead


def _cjk_aware_estimate(text: str) -> int:
    """CJK 感知的字符级 token 估算。

    中文字符约 1.5 字符/token（偏保守避免低估），
    英文/其他字符约 4 字符/token。
    """
    cjk_count = sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')
    other_count = len(text) - cjk_count
    return max(1, int(cjk_count * 1.5 + other_count * 0.25))
```

- [ ] **Step 2: 验证模块可导入**

Run: `python -c "from agent.token_calculator import TokenCalculator; print('OK')"`
Expected: 输出 OK（tokenizer 文件尚未放入，会 fallback 到 litellm）

- [ ] **Step 3: 提交**

```bash
git add agent/token_calculator.py
git commit -m "feat: add TokenCalculator class for local tokenizer-based token counting"
```

---

## Task 2: 放入 tokenizer.json 文件 + 校准开销常量

**Files:**
- Create: `models/tokenizers/deepseek-v3/tokenizer.json`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p models/tokenizers/deepseek-v3
```

- [ ] **Step 2: 复制 tokenizer.json 文件**

从 `/tmp/deepseek_tokenizer/tokenizer.json` 复制：

```bash
cp /tmp/deepseek_tokenizer/tokenizer.json models/tokenizers/deepseek-v3/tokenizer.json
```

如果 `/tmp` 下的文件不存在，需要从 HuggingFace 手动下载：
- DeepSeek-V3: https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/main/tokenizer.json

- [ ] **Step 3: 验证文件可加载**

```bash
python -c "
from tokenizers import Tokenizer
ds = Tokenizer.from_file('models/tokenizers/deepseek-v3/tokenizer.json')
text = '今天天气很好'
print(f'DeepSeek: {len(ds.encode(text).ids)} tokens')
print('OK')
"
```

Expected: 输出 token 数和 OK

- [ ] **Step 4: 校准 _MSG_OVERHEAD 常量**

用实测数据验证开销常量是否准确：

```bash
python -c "
import tiktoken
from tokenizers import Tokenizer

o200k = tiktoken.get_encoding('o200k_base')
ds = Tokenizer.from_file('models/tokenizers/deepseek-v3/tokenizer.json')

# 用 litellm 的 messages 模式计算（含结构开销）
from litellm import token_counter
msgs = [{'role': 'user', 'content': '你好'}]
litellm_count = token_counter(model='gpt-4o', messages=msgs)

# 用纯文本 + 开销计算
text_count = len(ds.encode('你好').ids)
overhead = litellm_count - text_count

print(f'litellm messages mode: {litellm_count}')
print(f'DeepSeek text only:   {text_count}')
print(f'Implied overhead:     {overhead}')
print(f'Current _MSG_OVERHEAD: 5')
print()
print('If implied overhead differs significantly from 5, adjust _MSG_OVERHEAD')
"
```

Expected: implied overhead 在 4-7 范围内，当前值 5 是合理的近似

- [ ] **Step 5: 验证 TokenCalculator 能加载本地文件**

```bash
python -c "
from agent.token_calculator import TokenCalculator
calc = TokenCalculator()
print(f'DeepSeek text count: {calc.count_text(\"今天天气很好\")}')
print(f'Using fallback: {calc.using_fallback}')
print(f'Tokenizer name: {calc.tokenizer_name}')
print('OK')
"
```

Expected: 输出 token 数、using_fallback=False、tokenizer_name=deepseek-v3、OK

- [ ] **Step 6: 提交**

```bash
git add models/tokenizers/
git commit -m "feat: add local DeepSeek-V3 tokenizer.json for accurate CJK token counting"
```

---

## Task 3: 确保 MCP 服务器能 import agent.token_calculator

**Files:**
- Modify: `agent/mcp_loader.py`

- [ ] **Step 1: 在 mcp_loader.py 中确保项目根目录在 sys.path**

找到 `_add_server_workdirs_to_sys_path` 函数（或等效位置），在添加 workdir 之前，先确保项目根目录在 sys.path 中：

```python
# 在添加 workdir 之前，确保项目根目录在 sys.path（MCP 服务器需要 import agent.*）
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
```

注意：需要先读取 mcp_loader.py 确认精确的插入位置。

- [ ] **Step 2: 验证 MCP 服务器能 import TokenCalculator**

```bash
python -c "
import sys
sys.path.insert(0, '.')
from agent.token_calculator import TokenCalculator
print(f'OK: {TokenCalculator.get().tokenizer_name}')
"
```

Expected: OK: deepseek

- [ ] **Step 3: 提交**

```bash
git add agent/mcp_loader.py
git commit -m "fix: ensure project root in sys.path for MCP server imports"
```

---

## Task 4: 替换 agent/context_manager.py

**Files:**
- Modify: `agent/context_manager.py:83-104`

- [ ] **Step 1: 修改 count_tokens_simple**

将 `count_tokens_simple` 改为使用 TokenCalculator，采用逐条计算方式（与 compat.py/runner.py 一致）：

```python
def count_tokens_simple(self, messages: List[Dict[str, Any]]) -> int:
    """计算消息列表的 token 数量。

    使用本地 HuggingFace tokenizer（比 o200k_base 对中文更准确），
    回退到 CJK 感知字符估算（偏保守避免低估）。
    """
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_messages(messages)
    except Exception:
        total_tokens = 0
        for msg in messages:
            content = msg.get("content", "")
            total_tokens += max(1, len(content) // 2) + 4
        return total_tokens
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from agent.context_manager import ContextManager; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/context_manager.py
git commit -m "refactor: context_manager uses TokenCalculator for token counting"
```

---

## Task 5: 替换 agent/generic/agent_loop.py

**Files:**
- Modify: `agent/generic/agent_loop.py:40-50`

- [ ] **Step 1: 修改 count_messages_tokens**

```python
def count_messages_tokens(messages: list) -> int:
    """计算消息列表的 token 数量。

    使用本地 HuggingFace tokenizer，回退到 CJK 感知字符估算。
    """
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_messages(messages)
    except Exception:
        total = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            total += max(1, len(content) // 2) + 4
        return total
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from agent.generic.agent_loop import count_messages_tokens; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/generic/agent_loop.py
git commit -m "refactor: agent_loop uses TokenCalculator for token counting"
```

---

## Task 6: 替换 agent/subagent.py

**Files:**
- Modify: `agent/subagent.py:19-42`

- [ ] **Step 1: 修改 count_tokens_for_text**

```python
def count_tokens_for_text(text: str) -> int:
    """计算纯文本的 token 数量。

    使用本地 HuggingFace tokenizer，回退到 CJK 感知字符估算。
    """
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_text(text)
    except Exception:
        return max(1, len(text) // 2)
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from agent.subagent import count_tokens_for_text; print(count_tokens_for_text('你好'))"`
Expected: 输出 token 数

- [ ] **Step 3: 提交**

```bash
git add agent/subagent.py
git commit -m "refactor: subagent uses TokenCalculator for token counting"
```

---

## Task 7: 替换 agent/runner.py

**Files:**
- Modify: `agent/runner.py:641-651`

- [ ] **Step 1: 修改 _recalc_msg_stats 中的 litellm.token_counter 调用**

找到 `_recalc_msg_stats` 方法中逐条调用 `litellm.token_counter(model="gpt-4o", messages=[...])` 的代码，替换为：

```python
from agent.token_calculator import TokenCalculator
calc = TokenCalculator.get()
...
# 原代码: t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
# 新代码:
t = calc.count_message_single(msg.role, msg.content or "")
```

注意：需要先读取 runner.py:636-660 的完整代码确认精确的替换位置和上下文。

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from agent.runner import NiuRunner; print('OK')"`（可能需要完整环境）
Expected: 无 ImportError

- [ ] **Step 3: 提交**

```bash
git add agent/runner.py
git commit -m "refactor: runner uses TokenCalculator for msg token recalculation"
```

---

## Task 8: 替换 agent/session.py

**Files:**
- Modify: `agent/session.py:316-319`

- [ ] **Step 1: 修改 delete_messages_by_ids 中的 freed_tokens 计算**

```python
# 原代码:
# from litellm import token_counter
# t = token_counter(model="gpt-4o", messages=[{"role": row["role"], "content": row["content"] or ""}])

# 新代码:
from agent.token_calculator import TokenCalculator
calc = TokenCalculator.get()
t = calc.count_message_single(row["role"], row["content"] or "")
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from agent.session import MessageStore; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/session.py
git commit -m "refactor: session uses TokenCalculator for freed_tokens calculation"
```

---

## Task 9: 替换 niu_api/compat.py 内联调用（最复杂）

**Files:**
- Modify: `niu_api/compat.py` 多处内联调用

这是改动最多的文件，有 8 处内联 `litellm.token_counter(model="gpt-4o")` 调用。每处的替换方式不同：

| 行号 | 函数 | 当前调用方式 | 替换为 |
|------|------|-------------|--------|
| L236 | `_truncate_task_for_subagent` | `token_counter(model="gpt-4o", messages=[{"role":"user","content":task}])` | `calc.count_text(task)` — 纯文本截断，无需消息开销 |
| L267 | `_estimate_total_tokens` | 逐条 `token_counter(model="gpt-4o", messages=[{role,content}])` | `calc.count_message_single(role, content)` |
| L905 | `_tidy_context_impl` 初始计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |
| L1093 | sleep dream-evolver 重新计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |
| L1181 | sleep journal-agent 重新计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |
| L1272 | sleep context-manager 重新计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |
| L1473 | force dream-evolver 重新计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |
| L1561 | force journal-agent 重新计数 | 逐条 `token_counter(...)` | `calc.count_message_single(msg.role, msg.content or "")` |

- [ ] **Step 1: 逐处替换为 TokenCalculator**

子Agent需要先完整阅读 compat.py 中所有 litellm.token_counter 调用的上下文（每处前后 10 行），然后按上表逐处替换。替换模式：

```python
from agent.token_calculator import TokenCalculator
calc = TokenCalculator.get()

# 纯文本截断（L236）:
t = calc.count_text(task)

# 逐条消息计数（L267, L905, L1079, L1167, L1258, L1459, L1547）:
t = calc.count_message_single(role, content)
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "import niu_api.compat; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: compat.py uses TokenCalculator for all token counting"
```

---

## Task 10: 替换 region_manager.py

**Files:**
- Modify: `niu_api/internal/region_manager.py:1172,1189,1403,1420`

- [ ] **Step 1: 替换 prompt 截断中的 litellm.token_counter 调用**

```python
# 原代码:
# from litellm import token_counter
# count = token_counter(model="gpt-4o", text=prompt)

# 新代码:
from agent.token_calculator import TokenCalculator
count = TokenCalculator.get().count_text(prompt)
```

- [ ] **Step 2: 验证修改无语法错误**

Run: `python -c "from niu_api.internal.region_manager import RegionManager; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add niu_api/internal/region_manager.py
git commit -m "refactor: region_manager uses TokenCalculator for prompt truncation"
```

---

## Task 11: 替换 memory-server 和 session-server

**Files:**
- Modify: `mcp-servers/memory-server/src/niu_memory_server/__init__.py:135`
- Modify: `mcp-servers/session-manager/src/niu_session_manager/__init__.py:171,372`

- [ ] **Step 1: 替换 memory-server _count_tokens**

```python
# 原代码:
# from litellm import token_counter
# return token_counter(model="gpt-4o", text=text)

# 新代码:
try:
    from agent.token_calculator import TokenCalculator
    return TokenCalculator.get().count_text(text)
except Exception:
    # CJK 感知回退
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    return max(1, int(cjk * 1.5 + (len(text) - cjk) * 0.25))
```

注意：Task 3 已确保项目根目录在 sys.path 中，所以 `from agent.token_calculator` 可行。

- [ ] **Step 2: 替换 session-manager 两处 token_counter 调用**

L171 使用 `getattr(msg, "role", "user")`，L372 使用 `msg.get("role", "user")`，替换时需保留这些表达式：

```python
# L171 原代码:
# tokens = token_counter(model="gpt-4o", messages=[{"role": getattr(msg, "role", "user"), "content": content}])
# L171 新代码:
try:
    from agent.token_calculator import TokenCalculator
    tokens = TokenCalculator.get().count_message_single(getattr(msg, "role", "user"), content)
except Exception:
    tokens = max(1, len(content) // 2) + 4

# L372 原代码:
# tokens = token_counter(model="gpt-4o", messages=[{"role": msg.get("role", "user"), "content": content}])
# L372 新代码:
try:
    from agent.token_calculator import TokenCalculator
    tokens = TokenCalculator.get().count_message_single(msg.get("role", "user"), content)
except Exception:
    tokens = max(1, len(content) // 2) + 4
```

- [ ] **Step 3: 验证修改无语法错误**

Run: `python -c "from niu_memory_server import *; print('memory OK')"` 和 `python -c "from niu_session_manager import *; print('session OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/memory-server/ mcp-servers/session-manager/
git commit -m "refactor: memory-server and session-server use TokenCalculator for token counting"
```

---

## Task 12: 集成验证 — 真实对比测试

**Files:**
- Create: `tests/test_token_calculator.py`

- [ ] **Step 1: 写对比测试**

```python
"""验证 TokenCalculator 产出与 litellm.token_counter(o200k_base) 的差异在预期范围内。"""
import pytest
from agent.token_calculator import TokenCalculator, _cjk_aware_estimate


@pytest.fixture
def calc():
    return TokenCalculator()


class TestTokenCalculatorAccuracy:
    """对比 o200k_base 和本地 tokenizer 的差异。"""

    def test_chinese_text_ratio(self, calc):
        """中文文本：本地 tokenizer 应产出更少 token（比 o200k 少约 20-30%）。"""
        text = "今天我们一起讨论了知识图谱的设计方案。首先分析了现有的架构问题。"
        local_count = calc.count_text(text)

        import tiktoken
        o200k_count = len(tiktoken.get_encoding("o200k_base").encode(text))

        ratio = local_count / o200k_count
        assert 0.6 <= ratio <= 0.95, f"Ratio {ratio:.2f} out of expected range"

    def test_english_text_parity(self, calc):
        """英文文本：本地和 o200k 差异应很小。"""
        text = "Hello, the weather is nice today."
        local_count = calc.count_text(text)

        import tiktoken
        o200k_count = len(tiktoken.get_encoding("o200k_base").encode(text))

        ratio = local_count / o200k_count
        assert 0.85 <= ratio <= 1.15, f"Ratio {ratio:.2f} out of expected range"

    def test_messages_overhead(self, calc):
        """消息结构开销：每条消息应包含固定 overhead。"""
        messages = [{"role": "user", "content": "你好"}]
        count = calc.count_messages(messages)
        text_only = calc.count_text("你好")
        assert count > text_only, "Messages count should include overhead"
        assert count <= text_only + 10, "Overhead should be reasonable"

    def test_tool_message_overhead(self, calc):
        """tool 角色消息应包含 tool_call_id 额外开销。"""
        user_msg = [{"role": "user", "content": "test"}]
        tool_msg = [{"role": "tool", "content": "result", "tool_call_id": "call_123"}]
        user_count = calc.count_messages(user_msg)
        tool_count = calc.count_messages(tool_msg)
        # tool 消息应比 user 消息多 _TOOL_CALL_ID_OVERHEAD=3
        assert tool_count > user_count, "tool message should have more overhead than user"

    def test_fallback_when_no_tokenizer(self):
        """tokenizer 文件不存在时应 fallback 到 litellm。"""
        # 暂时重置单例，用不存在的路径测试
        TokenCalculator.reset()
        original = TokenCalculator._load_tokenizer
        def _fake_load(self):
            return None
        TokenCalculator._load_tokenizer = _fake_load
        try:
            calc = TokenCalculator()
            assert calc.using_fallback is True
            count = calc.count_text("你好世界")
            assert count > 0, "Fallback should still produce a count"
        finally:
            TokenCalculator._load_tokenizer = original
            TokenCalculator.reset()

    def test_cjk_aware_estimate(self):
        """CJK 感知估算应对中文保守（高估），避免低估。"""
        text = "今天天气很好"
        estimate = _cjk_aware_estimate(text)
        # 6 个中文字符 * 1.5 = 9
        assert estimate >= 6, "CJK estimate should be conservative"

    def test_count_messages_matches_sum_of_singles(self, calc):
        """count_messages 逐条求和应与逐条 count_message_single 求和一致。"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
            {"role": "user", "content": "今天天气如何？"},
        ]
        batch = calc.count_messages(messages)
        summed = sum(calc.count_message_single(m["role"], m["content"]) for m in messages)
        assert batch == summed, f"Batch {batch} != Summed {summed}"
```

- [ ] **Step 2: 运行测试**

Run: `cd agent && pytest tests/test_token_calculator.py -v`
Expected: 所有测试 PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_token_calculator.py
git commit -m "test: add TokenCalculator accuracy comparison tests"
```

---

## Task 13: 真实运行验证

- [ ] **Step 1: 启动完整服务**

```bash
go build -o niu.exe && ./niu.exe
```

- [ ] **Step 2: 发送一段中文对话，观察 stats API 的 token 计数**

对比 LLM API 返回的真实 `prompt_tokens` 与本地 `estimated_tokens`：
- 之前差异约 1.3x（本地偏高）
- 修改后差异应 < 1.1x

- [ ] **Step 3: 验证压缩触发时机正常**

在 stats 中观察 `usage_percent`：
- 如果之前是 85%（偏高），现在应该在 60-70% 左右（更接近真实）
- 压缩应在 80% 阈值时触发（比之前更晚，但更准确）

- [ ] **Step 4: 验证 TokenCalculator 状态暴露**

确认 stats API 返回中包含 tokenizer 状态信息（using_fallback、tokenizer_name），便于诊断。

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "feat: complete TokenCalculator integration — local tokenizer replaces o200k_base for accurate CJK token counting"
```
