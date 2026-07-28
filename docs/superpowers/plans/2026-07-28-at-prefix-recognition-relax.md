# @end/@niu-agent 识别范围放宽 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `@end` 和 `@niu-agent` 标记的识别从 `startswith` 改为"content 里找未转义标记"（前字符是 `\` 不识别，其他位置都识别），解决 LLM 用反引号/引号包装 `@end` 导致程序不识别的 bug。剥前缀、优先级、返回值等原有逻辑保持不变。

**Architecture:**
- 现状：`agent_loop.py:108` 和 `agent_loop.py:149` 用 `stripped.startswith(...)` 判断标记必须出现在 content 开头（仅剥左侧空白）。LLM 输出 `` `@end ...` ``（反引号包装）时识别失败 → FORMAT_ERROR 重试。
- 目标：引入 helper 函数 `_find_unescaped_marker(content, marker) -> int`，在 content 里查找未转义标记的位置（前字符是 `\` 不识别，否则识别，返回标记起始 index）。三处 `startswith` 调用点改为调 helper。
- 保持原逻辑：找到标记后剥前缀返回后续内容的逻辑不变（`agent_loop.py:110`、`agent_loop.py:704`）；`@niu-agent` 优先于 `@end`（代码顺序保证）；FORMAT_ERROR、INTERCEPTED_SYNC 路径不动。

**Tech Stack:** Python 3.11、`agent/generic/agent_loop.py`、pytest。

---

## File Structure

| 文件 | 责任 | 改动 |
|------|------|------|
| `agent/generic/agent_loop.py` | @前缀拦截核心逻辑 | **修改**：新增 `_find_unescaped_marker` helper + 3 处 startswith 改为调 helper |
| `tests/test_at_prefix_interception.py` | @前缀拦截测试 | **修改**：新增测试覆盖反引号包装、引号包装、转义、中间位置等场景 |

---

## Task 1: 新增 `_find_unescaped_marker` helper 函数

**Files:**
- Modify: `agent/generic/agent_loop.py:28` 附近（在常量定义块末尾 `NO_INTERCEPTION` 常量之后插入 helper，保持常量块完整）

- [ ] **Step 1: 备份当前代码**

```bash
cd /Users/lilei/tools/ai-bot
git status --short
# 工作区干净就跳过空 commit
```

- [ ] **Step 2: Read 确认插入位置**

```bash
sed -n '25,35p' /Users/lilei/tools/ai-bot/agent/generic/agent_loop.py
```

预期看到常量定义块（`INTERCEPTED` / `INTERCEPTED_SYNC` / `EXIT` / `FORMAT_ERROR` / `NO_INTERCEPTION`），其中 `NO_INTERCEPTION = "no_intercept"` 是常量块最后一行（约 L28），其后是 `@dataclass class StreamEvent` 定义。helper 插入到 `NO_INTERCEPTION` 常量之后、`@dataclass` 之前的空行处，这样常量块保持完整不被切断。

- [ ] **Step 3: Edit 在常量定义块末尾插入 helper 函数**

old_string（锚定常量块最后一行 `NO_INTERCEPTION` 常量）：

```python
NO_INTERCEPTION = "no_intercept"     # 不拦截（主 Agent 或有 tool_calls）
```

new_string（在 `NO_INTERCEPTION` 常量之后追加 helper 函数，常量块保持完整）：

```python
NO_INTERCEPTION = "no_intercept"     # 不拦截（主 Agent 或有 tool_calls）


def _find_unescaped_marker(content: str, marker: str) -> int:
    """在 content 里查找未转义标记的位置。

    规则（简单转义判断）：
    - 标记前一个紧邻字符是 `\\` → 不识别（转义），继续向后找
    - 其他位置（开头、中间、被反引号/引号包装等）→ 识别

    Args:
        content: 待搜索的文本（已 lstrip 或原始均可）
        marker: 要查找的标记（如 "@end" / "@niu-agent"）

    Returns:
        标记在 content 里的起始 index；未找到返回 -1。

    Examples:
        >>> _find_unescaped_marker("@end 任务完成", "@end")
        0
        >>> _find_unescaped_marker("`@end 任务完成`", "@end")
        1
        >>> _find_unescaped_marker("blah @end blah", "@end")
        5
        >>> _find_unescaped_marker(r"\\@end 任务完成", "@end")
        -1
        >>> _find_unescaped_marker("没有标记", "@end")
        -1
    """
    start = 0
    while True:
        idx = content.find(marker, start)
        if idx == -1:
            return -1
        # 前一个紧邻字符是 \\ → 转义，跳过本次匹配，从 idx+1 继续找
        if idx > 0 and content[idx - 1] == "\\":
            start = idx + 1
            continue
        return idx
```

**注意**：
- 函数返回 `int`，找到返回 index，未找到返回 -1
- 转义判断只看紧邻前一个字符（简单规则）
- `while True` 循环处理"前一个被转义但后面还有未转义"的情况（如 `\@end @end`，第一个是转义，第二个未转义）

- [ ] **Step 4: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/agent/generic/agent_loop.py').read()); print('syntax ok')"
```

预期：`syntax ok`

- [ ] **Step 5: 手工验证 helper**

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/lilei/tools/ai-bot')
from agent.generic.agent_loop import _find_unescaped_marker

# 基本位置
assert _find_unescaped_marker('@end 任务完成', '@end') == 0
assert _find_unescaped_marker('\`@end 任务完成\`', '@end') == 1
assert _find_unescaped_marker('blah @end blah', '@end') == 5

# 转义
assert _find_unescaped_marker('\\\\@end 任务完成', '@end') == -1  # 前字符是 \\，转义
assert _find_unescaped_marker('\\\\@end @end', '@end') == 6  # 第一个转义，第二个未转义

# 未找到
assert _find_unescaped_marker('没有标记', '@end') == -1

# @niu-agent
assert _find_unescaped_marker('@niu-agent 我该选哪个', '@niu-agent') == 0
assert _find_unescaped_marker('\`@niu-agent 我该选哪个\`', '@niu-agent') == 1

print('all helper assertions pass')
"
```

预期：`all helper assertions pass`

- [ ] **Step 6: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "$(cat <<'EOF'
feat(agent_loop): 新增 _find_unescaped_marker helper 函数

在 content 里查找未转义标记的位置（前字符是 \\ 不识别）。
解决 LLM 用反引号/引号包装 @end 导致程序不识别的 bug 的前置重构。
本 Task 仅新增 helper，下一 Task 替换 3 处 startswith 调用。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 替换 3 处 startswith 调用为 helper

**Files:**
- Modify: `agent/generic/agent_loop.py:108`（@niu-agent 识别）
- Modify: `agent/generic/agent_loop.py:149`（@end 识别）
- Modify: `agent/generic/agent_loop.py:703`（@end 剥前缀位置）

- [ ] **Step 1: Read 确认三处调用点**

```bash
sed -n '105,155p' /Users/lilei/tools/ai-bot/agent/generic/agent_loop.py
sed -n '698,712p' /Users/lilei/tools/ai-bot/agent/generic/agent_loop.py
```

- [ ] **Step 2: Edit 第一处 — `agent_loop.py:108` @niu-agent 识别**

old_string：

```python
    # 子 Agent 拦截（原逻辑）：@niu-agent / @end / 格式错误
    if stripped.startswith(_AT_NIU_PREFIX):
        # 剥除 "@niu-agent" 前缀 + 可选空格
        question = stripped[len(_AT_NIU_PREFIX):].lstrip()
```

new_string：

```python
    # 子 Agent 拦截（原逻辑）：@niu-agent / @end / 格式错误
    # 识别规则：content 里找未转义的 @niu-agent（前字符是 \\ 不识别，其他位置都识别）
    at_niu_idx = _find_unescaped_marker(stripped, _AT_NIU_PREFIX)
    if at_niu_idx >= 0:
        # 剥除 "@niu-agent" 前缀 + 可选空格（question 是 @niu-agent 之后的内容）
        question = stripped[at_niu_idx + len(_AT_NIU_PREFIX):].lstrip()
```

**注意**：剥前缀的切片从 `stripped[len(_AT_NIU_PREFIX):]` 改为 `stripped[at_niu_idx + len(_AT_NIU_PREFIX):]`，因为标记可能不在位置 0。

- [ ] **Step 3: Edit 第二处 — `agent_loop.py:149` @end 识别**

old_string：

```python
    # @end 允许退出（用 startswith("@end") 兼容 @end无空格）
    if stripped.startswith("@end"):
        return (EXIT, None)
```

new_string：

```python
    # @end 允许退出（content 里找未转义的 @end，前字符是 \\ 不识别，其他位置都识别）
    if _find_unescaped_marker(stripped, "@end") >= 0:
        return (EXIT, None)
```

- [ ] **Step 4: Edit 第三处 — `agent_loop.py:703` @end 剥前缀位置**

old_string：

```python
                if interception_status == EXIT:
                    # @end 允许退出，剥除 "@end" 前缀 + 可选空格后推前端
                    stripped_content = content.lstrip()
                    if stripped_content.startswith("@end"):
                        exit_content = stripped_content[4:].lstrip()
                        # 边界：@end 恰好 4 字符时剥前缀后为空，用原始 content 避免前端收到空回复
                        if not exit_content:
                            exit_content = content
                    else:
                        exit_content = content
```

new_string：

```python
                if interception_status == EXIT:
                    # @end 允许退出，剥除 "@end" 前缀 + 可选空格后推前端
                    stripped_content = content.lstrip()
                    at_end_idx = _find_unescaped_marker(stripped_content, "@end")
                    if at_end_idx >= 0:
                        exit_content = stripped_content[at_end_idx + 4:].lstrip()
                        # 边界：@end 恰好 4 字符时剥前缀后为空，用原始 content 避免前端收到空回复
                        if not exit_content:
                            exit_content = content
                    else:
                        exit_content = content
```

**注意**：剥前缀的切片从 `stripped_content[4:]` 改为 `stripped_content[at_end_idx + 4:]`。

**已知限制**：尾部包裹字符（如右反引号）会残留进 question/exit_content。例如 content = `` `@end 任务完成` `` → exit_content = ``任务完成` ``（尾反引号保留，前端用户会看到残留的右反引号）。这是剥前缀原逻辑保持不变的结果，本 plan 不处理（属 cosmetic 问题，不影响功能）。

- [ ] **Step 5: 语法检查**

```bash
python3 -c "import ast; ast.parse(open('/Users/lilei/tools/ai-bot/agent/generic/agent_loop.py').read()); print('syntax ok')"
```

- [ ] **Step 6: 跑既有 @前缀测试**

```bash
cd /Users/lilei/tools/ai-bot && pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -40
```

预期：20 个测试全部 PASS。既有测试要么以标记开头（新识别逻辑位置 0 也匹配，向后兼容），要么不进入 @ 识别分支（主 Agent 路径或 tool_calls 非空，行为不变）。

如果有 FAIL：分析失败原因。可能是某个测试依赖"标记必须在开头"的旧行为，需要评估是测试预期过时还是新逻辑有 bug。

- [ ] **Step 7: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "$(cat <<'EOF'
feat(agent_loop): @end/@niu-agent 识别从 startswith 改为未转义包含

三处 startswith 改为调 _find_unescaped_marker：
- L108 @niu-agent 识别
- L149 @end 识别
- L703 @end 剥前缀位置

解决 LLM 用反引号/引号包装 @end 导致程序不识别的 bug。
剥前缀、优先级、返回值等原有逻辑保持不变。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 新增测试覆盖反引号/引号/中间位置/转义场景

**Files:**
- Modify: `tests/test_at_prefix_interception.py`（在文件末尾追加新测试）

- [ ] **Step 1: Read 确认现有测试的 fixture 模式**

```bash
sed -n '1,50p' /Users/lilei/tools/ai-bot/tests/test_at_prefix_interception.py
```

确认 mock handler 和 NiuRunner 构造的模式（一般是 `__new__` + 手工补属性）。

- [ ] **Step 2: Edit 在文件末尾追加新测试**

在 `tests/test_at_prefix_interception.py` 末尾追加（参照既有 `test_at_end_prefix_allows_exit_with_space` 的 fixture 模式，使用相同的 mock 结构）：

```python
def test_at_end_with_backtick_wrapper_allows_exit(monkeypatch):
    """@end 被反引号包装时也允许退出（识别范围放宽）"""
    # 参照 test_at_end_prefix_allows_exit_with_space 的 fixture 结构
    # content = "`@end 任务完成`"
    # 预期返回 (EXIT, None)
    pass  # implementer 按既有测试模式补全


def test_at_end_with_double_quote_wrapper_allows_exit(monkeypatch):
    """@end 被双引号包装时也允许退出"""
    # content = '"@end 任务完成"'
    # 预期返回 (EXIT, None)
    pass  # implementer 按既有测试模式补全


def test_at_end_in_middle_allows_exit(monkeypatch):
    """@end 在 content 中间位置时也允许退出"""
    # content = "blah blah @end 任务完成"
    # 预期返回 (EXIT, None)
    pass  # implementer 按既有测试模式补全


def test_at_end_with_escape_prefix_not_recognized(monkeypatch):
    r"""@end 前字符是 \\ 时不识别为指令（转义）"""
    # content = r"\@end 任务完成"
    # 预期返回 (FORMAT_ERROR, None)（不是 EXIT）
    pass  # implementer 按既有测试模式补全


def test_at_end_double_backslash_not_recognized(monkeypatch):
    r"""@end 前两个字符是 \\\\ 时按简单规则仍不识别（紧邻前字符是 \\）"""
    # content = r"\\@end 任务完成"
    # 简单规则：紧邻前一个字符是 \\ 就不识别
    # 预期返回 (FORMAT_ERROR, None)
    pass  # implementer 按既有测试模式补全


def test_at_niu_with_backtick_wrapper_triggers_intercept(monkeypatch):
    """@niu-agent 被反引号包装时也触发询问"""
    # content = "`@niu-agent 我该选哪个？`"
    # 预期返回 (INTERCEPTED, None) 或 (INTERCEPTED_SYNC, ...)
    pass  # implementer 按既有测试模式补全


def test_at_niu_priority_over_at_end(monkeypatch):
    """@niu-agent 和 @end 同时出现时，@niu-agent 优先（代码顺序保证）"""
    # content = "@niu-agent 问个问题 @end 顺便退出"
    # 预期返回 (INTERCEPTED, None)，不是 (EXIT, None)
    pass  # implementer 按既有测试模式补全
```

**implementer 注意**：上面 7 个 `pass` 是占位，需要参照既有测试（如 `test_at_end_prefix_allows_exit_with_space`、`test_at_niu_prefix_triggers_ask_main_agent`）的 fixture 结构补全 mock 代码。补全后删除 `pass` 和注释。

**implementer 注意**：@niu-agent 两个新测试（`test_at_niu_with_backtick_wrapper_triggers_intercept` 和 `test_at_niu_priority_over_at_end`）必须参照 `test_at_niu_prefix_triggers_ask_main_agent` 的 fixture 结构：
- mock `_ask_main_agent_impl`（异步路径）
- 显式设 `_is_sync_subagent = False`（避免走同步路径）
- 设 `_subagent_unique_name` 为非空值

只有异步路径才能断言 `(INTERCEPTED, None)`。若错用 `test_at_end_prefix_allows_exit_with_space` 的 fixture（裸 MagicMock），`_is_sync_subagent` 是 truthy → 会走同步路径调未 mock 的 `_ask_main_agent_impl_sync`，返回非预期结果。

- [ ] **Step 3: 跑全部 @前缀测试**

```bash
pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -50
```

预期：27 个测试（20 既有 + 7 新增）全部 PASS。

如果有 FAIL：
- 检查新测试的 mock 结构是否和既有测试一致
- 检查 `_find_unescaped_marker` 的转义判断是否正确

- [ ] **Step 4: Commit**

```bash
git add tests/test_at_prefix_interception.py
git commit -m "$(cat <<'EOF'
test(agent_loop): 新增 @前缀识别范围放宽测试

7 个新测试覆盖：
- 反引号/双引号包装 @end
- @end 在 content 中间位置
- 转义 @end（\\@end 不识别）
- 双反斜杠 @end（\\\\@end 简单规则仍不识别）
- 反引号包装 @niu-agent
- @niu-agent 和 @end 同时出现时 @niu-agent 优先

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 全量测试回归 + 真实环境验证

**Files:**
- 不改代码，仅验证

- [ ] **Step 1: 全量 pytest 回归**

```bash
cd /Users/lilei/tools/ai-bot && pytest tests/test_at_prefix_interception.py tests/test_context_manager_bypass_at_prefix.py -v 2>&1 | tail -30
```

预期：所有 @前缀相关测试 PASS。

- [ ] **Step 2: 真实环境验证（用户配合）**

提示用户：

> 改动完成，请你做一次真实环境验证：
>
> 1. 重启程序 `./niu`
> 2. 让 context-manager 触发一次模式一压缩（睡眠整理）
> 3. 观察日志：LLM 第一次输出即使是 `` `@end ...` ``（带反引号），程序也应该正确识别为 EXIT，不会 FORMAT_ERROR 重试
> 4. 验证 context-manager 一轮就完成，不需要第二轮
>
> 同时可以验证子 Agent 询问场景：
> 1. 让某个子 Agent 输出 `` `@niu-agent 我该选哪个？` ``（带反引号）
> 2. 程序应该正确识别为 INTERCEPTED，把问题传给主 Agent

- [ ] **Step 3: 无 commit**

本 Task 只是验证，不改代码。

---

## Self-Review

**1. Spec coverage**:
- ✅ 新增 `_find_unescaped_marker` helper：Task 1
- ✅ 替换 3 处 startswith：Task 2
- ✅ 转义判断（前字符是 `\` 不识别）：Task 1 helper 实现 + Task 3 测试
- ✅ 反引号/引号包装识别：Task 2 改动 + Task 3 测试
- ✅ 中间位置识别：Task 3 测试
- ✅ `@niu-agent` 优先于 `@end`：代码顺序保证（L108 在 L149 前）+ Task 3 测试
- ✅ 剥前缀、返回值等原逻辑保持不变：Task 2 只改识别行，剥前缀切片位置跟随标记 index
- ✅ 既有 20 个测试向后兼容：Task 2 Step 6 验证

**2. Placeholder scan**:
- ✅ 无 TBD/TODO
- ✅ 所有 Step 含具体代码和命令
- ⚠️ Task 3 Step 2 的 7 个测试函数体用 `pass` 占位 + 注释说明——这是为了不在 plan 里重复既有测试的 mock 代码（implementer 需要参照既有测试补全）。可接受，因为既有测试就是最佳模板。

**3. Type consistency**:
- ✅ `_find_unescaped_marker(content: str, marker: str) -> int` 在 Task 1 定义、Task 2 三处调用一致
- ✅ 返回值 `int`：找到返回 index（>=0），未找到返回 -1。三处调用都用 `>= 0` 判断，类型一致

**4. 风险点**:
- **剥前缀切片位置**：Task 2 中 L110 `question = stripped[at_niu_idx + len(_AT_NIU_PREFIX):]` 和 L704 `exit_content = stripped_content[at_end_idx + 4:]` 都从"标记假设在位置 0"改为"标记实际 index + 标记长度"。implementer 必须确认切片起点正确。
- **既有测试兼容性**：既有测试要么以标记开头（新识别逻辑位置 0 也匹配，向后兼容），要么不进入 @ 识别分支（主 Agent 路径或 tool_calls 非空，行为不变）。Task 2 Step 6 验证。
- **优先级保证**：`@niu-agent` 识别代码在 `@end` 识别代码之前（L108 < L149），即使 content 同时含两个标记也只触发 `@niu-agent`。Task 3 测试覆盖此场景。
- **转义循环**：helper 的 `while True` 循环处理"前一个转义但后面还有未转义"的情况（如 `\@end @end`）。Task 1 Step 5 手工验证覆盖。
- **不改变主 Agent 路径**：L102-105 的主 Agent 分支（`memory_context is None and not is_sync_subagent`）不受影响，仍走 `_check_main_agent_content_reply_to_suspended`。
- **子串假阳性已知情接受**：helper 用子串匹配，可能误判 `@endOfFile` / `user@endexample.com` 等包含 `@end` 子串的内容。用户已明确接受这个代价（原话："只要出现了就表明是指令"），不加"标记后必须跟空白/标点/结尾"的弱约束。模式一 context-manager 的 LLM 如果在 analysis 中提到 `@end`，会被静默退出——这是本设计的已知代价。

**5. 不要改的部分**（确认范围）:
- `agent_loop.py:108-146` 的 @niu-agent INTERCEPTED / INTERCEPTED_SYNC 路径保持不变
- `agent_loop.py:152-154` 的 FORMAT_ERROR 路径保持不变
- `_check_main_agent_content_reply_to_suspended` 函数保持不变
- `agent_loop.py:700-712` 的 EXIT 后续 yield StreamEvent 逻辑保持不变
- `config/agents/context-manager.md` 的 prompt 文本保持不变（按你的要求不改 prompt）
- `agent/subagent.py` 的 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 保持不变
- `_FORMAT_ERROR_PROMPT` 文案保持不变（仍引导 LLM 把标记放开头，这是最佳实践）。新代码接受任意位置是向后兼容，不是要求 LLM 改变输出习惯。

---

## 执行交付条件

1. 所有 4 个 Task 完成，Task 1-3 各自单独 commit
2. `pytest tests/test_at_prefix_interception.py` 27 个测试全 PASS（20 既有 + 7 新增）
3. `pytest tests/test_context_manager_bypass_at_prefix.py` 全 PASS
4. 用户在真实环境验证：context-manager 一轮就完成（即使 LLM 输出带反引号），不需要第二轮
