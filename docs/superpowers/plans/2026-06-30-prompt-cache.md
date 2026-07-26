# Prompt Cache 实施计划 v3（修订版 — 二次深度审查后修正）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让主 Agent 和子 Agent 的 LLM 请求命中 prompt cache，降低成本、提高首 token 响应速度。把系统提示词拆成「Current Time 之前的静态段（cache）+ Current Time 之后的动态段（不 cache）」，Claude 模型用显式 `cache_control` 标记 system 静态段和 tools 末尾，其他模型靠前缀稳定让服务端自动命中。

**Architecture:** `agent_runner_loop` 新增可选参数 `system_message: dict`（已组装好的 system message，content 可能是 str 或 list）。传入时首轮就用它创建 messages[0]（首轮即带 cache_control）；不传时回退到老逻辑 `system_prompt: str`（39 处现有测试零改动）。`_assemble_system_message` 统一负责组装，被 `runner.chat`（首轮）、`_on_turn_end`（每轮重写）、`_on_context_high_usage`（压缩后重建）三处调用。子 Agent 走同样改造（函数名 `_run_agent_loop`）。`_refresh_user_memories` 同步更新 `static_system_prompt` 后重算 `base_system_prompt`。`count_messages_tokens` 兼容 list content。tools_schema 稳定，给 tools 末尾打 cache_control。

**Tech Stack:** Python 3.11, litellm 1.88.1, Anthropic prompt caching API, 火山方舟 OpenAI 兼容协议

---

## 问题分析

### 当前结构（cache 不友好）

主 Agent（`agent/runner.py`）：
- `get_system_prompt()`（L137-174）拼接：niu.md + memory + Current Time
- `__init__`（L370）：`base_system_prompt = get_system_prompt()` + disk_desc（L388）
- `chat`（L1717）把 `system_prompt` 字符串传给 `agent_runner_loop`
- `agent_loop.py:206`：`messages = [{"role": "system", "content": system_prompt}]` 创建首轮
- `_on_turn_end`（L527）：`messages[0]["content"] = self.base_system_prompt + injection` 每轮重写
- `_refresh_user_memories`（L1428-1436）：memory 变化时 re.sub 重写 `base_system_prompt`
- `_on_context_high_usage`（L1316-1320）：压缩后保留旧 system_msg

子 Agent（`agent/subagent.py`）：
- `call_subagent`（L386-401）：`system_prompt = get_subagent_prompt(agent_name)` + Current Time + user_info
- `_run_agent_loop`（L107-158，注意实际函数名）：把 system_prompt 字符串传给 `agent_runner_loop`

### 三个根因

1. **Current Time 在静态段中间**：niu.md + memory 是静态的，Current Time 紧跟其后切断前缀。
2. **首轮 system content 是字符串**：`agent_loop.py:206` 用 chat 传来的字符串创建 messages[0]，且 `on_turn_end` 在 L319 `client.chat()` **之后**才执行（L589），首轮 Claude 请求无 cache_control。
3. **`count_messages_tokens` 不兼容 list content**：Claude 的 list 格式 content 会让 `len(content)` 崩溃。

### 已验证的技术可行性（二次审查确认）

- **litellm anthropic provider 透传 cache_control**：
  - messages content：`python/lib/python3.11/site-packages/litellm/llms/anthropic/chat/transformation.py:1646-1674`
  - tools：同文件 L795-811 主动处理 `tool["cache_control"]` 和 `tool["function"]["cache_control"]`
- **litellm openai provider 剥离 cache_control**：`litellm/llms/openai/chat/gpt_transformation.py:406,419,444`。对火山方舟无害。
- **`custom_provider` 路由准确**：`litellm_adapter.py:337` Claude 走 anthropic，其他走 openai。
- **`_load_memory_for_prompt` 字节稳定**：无时间戳/随机数/PID。
- **tools_schema 实际稳定**：`_on_turn_end` L529 注释 "No schema refresh — tools_schema stays base + disk"。子 Agent `on_turn_end=None`，tools 也稳定。
- **`on_turn_end` 在 L319 client.chat() 之后**（L589）：首轮不会被重写，必须首轮就传入组装好的 system_message。
- **`agent_runner_loop` 有 39 处测试调用**（grep 确认）：不能改签名，必须用可选新参数。

### 设计原则

1. **cache 边界 = Current Time 之前**：niu.md + memory 是静态段。
2. **Current Time 移到动态段开头**：保证静态段字节稳定。
3. **`agent_runner_loop` 新增可选 `system_message: dict` 参数**：传入则首轮用它创建 messages[0]；不传回退 `system_prompt: str`。39 处测试零改动。
4. **`_assemble_system_message` 统一组装**：被首轮传入、每轮重写、压缩重建三处调用。
5. **子 Agent 同步改造**：`_run_agent_loop` 新增 `system_message` 参数。
6. **tools 也打 cache_control**：tools_schema 稳定，末尾打 breakpoint。
7. **`_refresh_user_memories` 同步更新 `static_system_prompt` 后重算 `base_system_prompt`**。
8. **`count_messages_tokens` 兼容 list content**：把 list 转成字符串再算长度。
9. **不改 niu.md 内容**，不改历史消息结构，不改 `system_prompt` 老参数语义。

### 待用户确认的假设

**火山方舟 ark-code-latest 是否支持自动 prefix cache**：本计划假设支持。ark-code-latest 启用了 thinking 模式，thinking 模式下 prefix cache 行为未知。**需用户向火山方舟确认**。即使不支持，重构本身无害。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `agent/runner.py` | 拆分静态/动态段，`_assemble_system_message`，改造 chat/`_on_turn_end`/`_refresh_user_memories`/`_on_context_high_usage` | Modify |
| `agent/generic/agent_loop.py` | `agent_runner_loop` 新增可选 `system_message` 参数；`count_messages_tokens` 兼容 list | Modify |
| `agent/generic/litellm_adapter.py` | 给 Claude 的 tools 末尾打 cache_control | Modify |
| `agent/subagent.py` | 子 Agent 拆分 static/dynamic 段，`_run_agent_loop` 新增 `system_message` 参数 | Modify |
| `tests/test_prompt_cache.py` | 验证拆分、组装、cache_control 标记、前缀稳定性 | Create |

---

## Task 1: 拆分主 Agent 系统提示词为静态段 + 动态段

**Files:**
- Modify: `agent/runner.py:137-174`（`get_system_prompt`）
- Modify: `agent/runner.py:350`（`NiuRunner` 类，新增 `_build_static_system_prompt`）
- Modify: `agent/runner.py:365-390`（`__init__` 初始化 `static_system_prompt`/`dynamic_system_prefix`）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写失败测试 — 静态段不含 Current Time**

Create `tests/test_prompt_cache.py`:

```python
"""Prompt cache 实施测试：验证系统提示词静态/动态段拆分。"""
from agent.runner import NiuRunner


def test_build_static_system_prompt_excludes_current_time():
    """静态段不应包含 Current Time（Current Time 每分钟变化，会切断前缀 cache）。"""
    static = NiuRunner._build_static_system_prompt()
    assert "Current Time" not in static, \
        f"静态段不应包含 Current Time，但找到: {static[-200:]}"
    assert len(static) > 500, \
        f"静态段应包含 niu.md 正文，长度应 > 500，实际 {len(static)}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_build_static_system_prompt_excludes_current_time -v`
Expected: FAIL with `AttributeError: type object 'NiuRunner' has no attribute '_build_static_system_prompt'`

- [ ] **Step 3: 新增 `_build_static_system_prompt` 静态方法**

Read `agent/runner.py:137-174` 确认 `get_system_prompt()` 当前逻辑，Read `agent/runner.py:350` 确认 `class NiuRunner:` 定义位置。

在 `NiuRunner` 类内新增静态方法（建议放在 `__init__` 之前）：

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

- [ ] **Step 4: 改造 `get_system_prompt()` 调用静态方法**

修改 `agent/runner.py:137-174`：

```python
def get_system_prompt() -> str:
    """获取系统提示词（向后兼容：静态段 + Current Time）。

    注意：此函数保留向后兼容。新的 cache 逻辑应直接用
    NiuRunner._build_static_system_prompt() 获取静态段，
    Current Time 由调用方在动态段开头拼接。
    """
    sys_prompt = NiuRunner._build_static_system_prompt()
    now = datetime.now()
    sys_prompt += f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"
    return sys_prompt
```

- [ ] **Step 5: 改造 `__init__` 初始化静态/动态段属性**

Read `agent/runner.py:365-390` 确认当前 `base_system_prompt` 初始化逻辑。

修改 `agent/runner.py:370` 附近，把：

```python
        self.base_system_prompt = get_system_prompt()
```

改为：

```python
        # 静态段：niu.md + memory（cache 友好，字节稳定）
        # memory 变化时由 _refresh_user_memories 同步更新此属性
        self.static_system_prompt = self._build_static_system_prompt()

        # 动态前缀段：Current Time + disk_desc（启动时固定，不每轮更新）
        now = datetime.now()
        dynamic_prefix = f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        disk_desc = self._build_disk_description()
        if disk_desc:
            dynamic_prefix += disk_desc
        self.dynamic_system_prefix = dynamic_prefix

        # 向后兼容：base_system_prompt = 静态段 + 动态前缀段（不含 injection）
        self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix
```

注意：保留 `self.base_system_prompt` 属性（向后兼容）。`_refresh_user_memories` 改完 `static_system_prompt` 后必须重算 `base_system_prompt`（Task 4 处理）。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_build_static_system_prompt_excludes_current_time -v`
Expected: PASS

- [ ] **Step 7: 临时提交**

```bash
cd <repo_root>
git add agent/runner.py tests/test_prompt_cache.py
git commit -m "refactor(runner): split system prompt into static + dynamic segments

静态段（niu.md + memory）与动态段（Current Time + disk_desc + injection）
分离。静态段字节稳定，为 prompt cache 前缀。Current Time 移到动态段开头，
避免切断静态前缀。"
```

---

## Task 2: 实现 `_assemble_system_message` 统一组装方法

**Files:**
- Modify: `agent/runner.py`（`NiuRunner` 类新增 `_assemble_system_message`）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写失败测试 — Claude 和非 Claude 的组装**

在 `tests/test_prompt_cache.py` 追加：

```python
def test_assemble_system_message_non_claude():
    """非 Claude 模型：system content 是字符串，静态段在开头且稳定。"""
    runner = NiuRunner.__new__(NiuRunner)  # 绕过 __init__ 的重资源加载
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    runner._assemble_system_message(messages, injection, model="ark-code-latest")

    content = messages[0]["content"]
    assert isinstance(content, str), "非 Claude 模型 content 应为字符串"
    assert content.startswith("STATIC_PART"), "静态段应在开头"
    assert "Current Time" in content
    assert "skill1" in content


def test_assemble_system_message_claude():
    """Claude 模型：system content 是 list，静态段末尾打 cache_control breakpoint。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC_PART"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "claude-sonnet-4-6"

    injection = "\n\n### [相关技能]\n- skill1"
    messages = [{"role": "system", "content": ""}]

    runner._assemble_system_message(messages, injection, model="claude-sonnet-4-6")

    content = messages[0]["content"]
    assert isinstance(content, list), "Claude 模型 content 应为 list"
    assert len(content) == 2, "应为两段：静态段 + 动态段"

    static_block = content[0]
    assert static_block["type"] == "text"
    assert static_block["text"] == "STATIC_PART"
    assert static_block.get("cache_control") == {"type": "ephemeral"}, \
        "静态段末尾必须有 cache_control breakpoint"

    dynamic_block = content[1]
    assert dynamic_block["type"] == "text"
    assert "Current Time" in dynamic_block["text"]
    assert "skill1" in dynamic_block["text"]
    assert "cache_control" not in dynamic_block, "动态段不应有 cache_control"


def test_assemble_system_message_empty_injection():
    """injection 为空时动态段只含 Current Time + disk_desc。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    messages = [{"role": "system", "content": ""}]
    runner._assemble_system_message(messages, "", model="ark-code-latest")

    content = messages[0]["content"]
    assert content == "STATIC\n\nCurrent Time: 2026-06-30 10:51:00"


def test_assemble_system_message_non_system_first_msg():
    """messages[0] 不是 system 时应跳过（不抛异常）。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.default_model = "ark-code-latest"

    messages = [{"role": "user", "content": "hello"}]
    runner._assemble_system_message(messages, "inj", model="ark-code-latest")

    assert messages[0]["content"] == "hello"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v -k assemble`
Expected: FAIL with `AttributeError: 'NiuRunner' object has no attribute '_assemble_system_message'`

- [ ] **Step 3: 实现 `_assemble_system_message` 方法**

在 `agent/runner.py` 的 `NiuRunner` 类中新增方法（建议放在 `_on_turn_end` 之前，约 L505 附近）：

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

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v -k assemble`
Expected: 4 个 assemble 测试全部 PASS

- [ ] **Step 5: 临时提交**

```bash
cd <repo_root>
git add agent/runner.py tests/test_prompt_cache.py
git commit -m "feat(runner): add _assemble_system_message for cache-aware system content

Claude 模型把 system content 改为 list 格式，静态段末尾打 cache_control
breakpoint；其他模型保持字符串格式靠前缀稳定命中自动 prefix cache。"
```

---

## Task 3: `agent_runner_loop` 新增可选 `system_message` 参数 + `count_messages_tokens` 兼容 list

**Files:**
- Modify: `agent/generic/agent_loop.py:40-54`（`count_messages_tokens` 兼容 list）
- Modify: `agent/generic/agent_loop.py:186-206`（`agent_runner_loop` 签名 + messages 创建）
- Modify: `agent/runner.py:1717,1735-1740,1794-1799`（`chat` 方法传 `system_message`）
- Modify: `agent/runner.py:520-530`（`_on_turn_end` 用 `_assemble_system_message`）
- Modify: `agent/runner.py:1315-1320`（`_on_context_high_usage` 压缩后重建）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写守护测试 — count_messages_tokens 兼容 list content**

注意：`TokenCalculator.count_messages`（`agent/token_calculator.py:104-106`）已原生处理 list content（用 `" ".join` 拼接 text blocks）。主路径不会抛异常。本测试是**守护性测试**，确保未来如果 TokenCalculator 行为变化或被移除，except 兜底分支仍能处理 list。

在 `tests/test_prompt_cache.py` 追加：

```python
def test_count_messages_tokens_handles_list_content():
    """count_messages_tokens 应兼容 list 格式 content（Claude cache_control 模式）。

    守护性测试：TokenCalculator 已原生支持 list，此测试确保 except 兜底
    分支也兼容 list（防止未来 TokenCalculator 变化时崩溃）。
    """
    from agent.generic.agent_loop import count_messages_tokens

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "static content here"},
                {"type": "text", "text": "dynamic content here"},
            ],
        },
        {"role": "user", "content": "hello"},
    ]
    # 不应抛 TypeError，应返回正整数
    tokens = count_messages_tokens(messages)
    assert isinstance(tokens, int)
    assert tokens > 0
```

- [ ] **Step 2: 运行测试确认通过（主路径已支持）**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_count_messages_tokens_handles_list_content -v`
Expected: PASS（`TokenCalculator.count_messages` 已原生处理 list content，主路径直接成功）

- [ ] **Step 3: 加固 except 兜底分支兼容 list content**

虽然主路径已支持，但 except 兜底分支（`agent_loop.py:50-54`）在 list content 上会 `len(content)` 崩溃。为防止未来 TokenCalculator 不可用时崩溃，加固 except 分支。

Read `agent/generic/agent_loop.py:40-54` 确认当前实现。

修改 `agent/generic/agent_loop.py:52`：

```python
def count_messages_tokens(messages: list) -> int:
    """
    估算消息列表的 token 数量

    使用 TokenCalculator，回退到字符数估算。
    """
    try:
        from agent.token_calculator import TokenCalculator
        return TokenCalculator.get().count_messages(messages)
    except Exception:
        total = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            # 兼容 list 格式 content（Claude cache_control 模式）
            # 用 " ".join 与 TokenCalculator 主路径一致（token_calculator.py:105）
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            total += max(1, len(content) // 2) + 4
        return total
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_count_messages_tokens_handles_list_content -v`
Expected: PASS

- [ ] **Step 5: 写失败测试 — agent_runner_loop 接收 system_message 参数**

在 `tests/test_prompt_cache.py` 追加：

```python
def test_agent_runner_loop_accepts_system_message_param():
    """agent_runner_loop 应支持可选 system_message 参数（首轮即带 cache_control）。"""
    from agent.generic.agent_loop import agent_runner_loop
    import inspect

    sig = inspect.signature(agent_runner_loop)
    params = sig.parameters
    assert "system_message" in params, \
        "agent_runner_loop 应新增 system_message 可选参数"
    assert params["system_message"].default is None, \
        "system_message 应有默认值 None（向后兼容）"
    # system_prompt 应保留（向后兼容，39 处测试依赖）
    assert "system_prompt" in params, \
        "system_prompt 参数应保留（向后兼容）"
```

- [ ] **Step 6: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_agent_runner_loop_accepts_system_message_param -v`
Expected: FAIL（当前无 `system_message` 参数）

- [ ] **Step 7: 改造 `agent_runner_loop` 签名和 messages 创建**

Read `agent/generic/agent_loop.py:186-210` 确认完整签名和 messages 创建逻辑。

修改签名（L195 附近），在 `system_prompt: str,` 后新增：

```python
    system_prompt: str = "",  # 向后兼容（system_message 优先）
    system_message: Optional[dict] = None,  # 已组装好的 system message（首轮即带 cache_control）
```

注意：`system_prompt` 改为默认 `""` 以支持 `system_message` 单独传入。如果 `system_message` 为 None，回退到 `system_prompt`。

修改 messages 创建（L206）：

```python
    # Build messages: system + history + current user
    # system_message 优先（首轮即带 cache_control）；否则回退到 system_prompt 字符串
    if system_message is not None:
        messages = [system_message]
    else:
        messages = [{"role": "system", "content": system_prompt}]
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_agent_runner_loop_accepts_system_message_param -v`
Expected: PASS

- [ ] **Step 9: 验证现有测试不被破坏**

Run: `cd <repo_root> && python -m pytest tests/test_agent_loop_return_messages.py tests/test_agent_loop_assistant_msg.py tests/test_agent_loop_stream_events.py tests/test_supplement_queue.py -v 2>&1 | tail -20`
Expected: 全部 PASS（现有测试用 `system_prompt=` 关键字，回退逻辑保持兼容）

- [ ] **Step 10: 改造 `NiuRunner.chat` 传 `system_message`**

Read `agent/runner.py:1717-1800` 确认 `chat` 方法调用 `agent_runner_loop` 的完整上下文。

修改 `agent/runner.py:1735-1740` 附近：

```python
        injection, _ = self._inject_dynamic_resources(context)

        # 组装 system message（首轮就按 model 决定格式：Claude list / 其他 str）
        # _assemble_system_message 原地修改 system_message 的 content
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], injection, self.default_model)
```

修改 `agent/runner.py:1794-1799` 附近调用 `agent_runner_loop`，新增 `system_message=` 参数：

```python
        gen = agent_runner_loop(
            client=self.client,
            system_prompt="",  # 向后兼容（system_message 非 None 时由分支选择生效，不读此参数）
            system_message=system_message,  # 首轮即带 cache_control
            tools_schema=tools_schema,
            # ... 其他参数不变
        )
```

注意：`system_prompt=""` 保留是为了不破坏关键字参数顺序（如果有的话）。实际由 `system_message` 生效。

- [ ] **Step 11: 改造 `_on_turn_end` 用 `_assemble_system_message`**

Read `agent/runner.py:520-530` 确认上下文。

修改 `agent/runner.py:523-527`：

```python
        # Extract context and re-inject skills/knowledge
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # Update system_prompt（静态段 + 动态段，Claude 走 cache_control）
        # messages 是 agent_loop 内部列表的引用，原地修改生效
        self._assemble_system_message(messages, injection, self.default_model)

        # No schema refresh — tools_schema stays base + disk
        return tools_schema
```

- [ ] **Step 12: 改造 `_on_context_high_usage` 压缩后重建 system**

Read `agent/runner.py:1315-1320` 确认上下文。

修改 `agent/runner.py:1316-1320`：

```python
                # 压缩后重建 system message（确保 Claude cache_control 不丢失）
                # 旧 system_msg 可能是首轮字符串格式，统一重建为 cache 友好格式
                # 注意：压缩后这一轮 LLM 请求无 skills/knowledge 注入（injection 为空），
                # 下一轮 _on_turn_end 会重新注入。这是预期行为（压缩后上下文已变，重新检索）
                system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
                if system_msg:
                    self._assemble_system_message([system_msg], "", self.default_model)
                    messages[:] = [system_msg] + fresh_msgs
                else:
                    messages[:] = fresh_msgs
                logger.info(f"[Runner] Force: Reloaded {len(fresh_msgs)} messages from DB after compress")
```

- [ ] **Step 13: 运行全部 prompt cache 测试**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v`
Expected: 全部 PASS

- [ ] **Step 14: 运行现有测试确认不破坏**

Run: `cd <repo_root> && python -m pytest tests/test_agent_loop_return_messages.py tests/test_agent_loop_assistant_msg.py tests/test_agent_loop_stream_events.py tests/test_supplement_queue.py tests/test_stop_flag.py tests/test_integration_tool_flow.py tests/test_subagent_overflow.py tests/test_multi_turn_persist.py tests/test_e2e_message_persist.py tests/test_dynamic_injection_per_turn.py tests/test_agent_loop_tool_results.py tests/test_context_overflow.py tests/test_chat_sse_persist.py 2>&1 | tail -20`
Expected: 全部 PASS（或与改造前状态一致，无新增 FAIL）

- [ ] **Step 15: 临时提交**

```bash
cd <repo_root>
git add agent/generic/agent_loop.py agent/runner.py tests/test_prompt_cache.py
git commit -m "feat(agent_loop): add optional system_message param for first-turn cache

agent_runner_loop 新增可选 system_message 参数（dict），传入则首轮用它
创建 messages[0]（首轮即带 cache_control）；不传回退 system_prompt 字符串
（39 处现有测试零改动）。count_messages_tokens 兼容 list content。
runner.chat/_on_turn_end/_on_context_high_usage 统一用 _assemble_system_message。"
```

---

## Task 4: 同步 `_refresh_user_memories` 更新静态段并重算 base

**Files:**
- Modify: `agent/runner.py:1403-1436`（`_refresh_user_memories`）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写失败测试 — memory 变化后静态段同步且 base 重算**

注意：`_refresh_user_memories` 内部 `from niu_memory_server import _memory_file_lock`（runner.py:1412 函数内局部 import）。mock 必须 patch 源模块属性 `niu_memory_server._memory_file_lock`，且测试需要先把 `mcp-servers/memory-server/src` 加进 sys.path（参考 `tests/test_user_memory.py:10` 的做法），否则 `mock.patch` 触发 `ModuleNotFoundError`。

在 `tests/test_prompt_cache.py` 追加：

```python
def test_refresh_user_memories_updates_static_and_recomputes_base():
    """memory 变化时 _refresh_user_memories 应同步更新 static_system_prompt
    并重算 base_system_prompt = static + dynamic_system_prefix。"""
    # niu_memory_server 不在默认 sys.path，需手动添加
    # （参考 tests/test_user_memory.py:10 的做法）
    import sys
    from pathlib import Path
    mem_src = Path(__file__).parent.parent / "mcp-servers" / "memory-server" / "src"
    if str(mem_src) not in sys.path:
        sys.path.insert(0, str(mem_src))

    import threading
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC <!--USER_MEMORY_START-->old<!--USER_MEMORY_END-->"
    runner.dynamic_system_prefix = "\n\nCurrent Time: 2026-06-30 10:51:00"
    runner.base_system_prompt = runner.static_system_prompt + runner.dynamic_system_prefix
    runner._memory_dirty = threading.Event()
    runner._memory_dirty.set()

    # 直接调用 _refresh_user_memories，mock 内部读取
    import unittest.mock as mock
    new_memory_json = '{"permanent": [{"type": "memory", "content": "new memory"}]}'

    # runner.py:1412 是函数内局部 import: from niu_memory_server import _memory_file_lock
    # 必须patch源模块属性，import时才能拿到patched引用
    # mock.pathlib.Path.read_text 会影响所有 Path 实例（包括 niu_memory_server 内部）
    fake_lock = type('FakeLock', (), {
        '__enter__': lambda self: None,
        '__exit__': lambda self, *a: None,
    })()
    with mock.patch('niu_memory_server._memory_file_lock', fake_lock), \
         mock.patch('pathlib.Path.read_text', return_value=new_memory_json), \
         mock.patch('agent.runner._render_permanent_section', return_value="<!--USER_MEMORY_START-->new memory<!--USER_MEMORY_END-->"):
        runner._refresh_user_memories([])

    # static_system_prompt 应已更新（old → new memory）
    assert "new memory" in runner.static_system_prompt, \
        f"static_system_prompt 应含 new memory，实际: {runner.static_system_prompt}"
    assert "<!--USER_MEMORY_START-->old<!--USER_MEMORY_END-->" not in runner.static_system_prompt, \
        "static_system_prompt 不应再含 old memory"

    # base_system_prompt 应等于 static + dynamic（重算后）
    assert runner.base_system_prompt == runner.static_system_prompt + runner.dynamic_system_prefix, \
        "base_system_prompt 应重算为 static + dynamic"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_refresh_user_memories_updates_static_and_recomputes_base -v`
Expected: FAIL（当前 `_refresh_user_memories` 只更新 `base_system_prompt`，不更新 `static_system_prompt`，也不重算 base）

- [ ] **Step 3: 改造 `_refresh_user_memories`**

Read `agent/runner.py:1403-1436` 确认完整逻辑。

修改 `agent/runner.py:1428-1436`：

```python
        # Update static_system_prompt（cache 前缀，必须同步）
        # 然后重算 base_system_prompt = static + dynamic_system_prefix（保持不变量）
        base = self.static_system_prompt
        if re.search(pattern, base, re.DOTALL):
            if new_section:
                self.static_system_prompt = re.sub(pattern, new_section, base, flags=re.DOTALL)
            else:
                self.static_system_prompt = re.sub(r'\n*' + pattern + r'\n*', '', base, flags=re.DOTALL)
        elif new_section:
            self.static_system_prompt = base + "\n\n" + new_section

        # 重算 base_system_prompt（保持 base = static + dynamic 不变量）
        self.base_system_prompt = self.static_system_prompt + self.dynamic_system_prefix
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_refresh_user_memories_updates_static_and_recomputes_base -v`
Expected: PASS

- [ ] **Step 5: 临时提交**

```bash
cd <repo_root>
git add agent/runner.py tests/test_prompt_cache.py
git commit -m "fix(runner): sync static_system_prompt and recompute base on memory refresh

_refresh_user_memories 同步更新 static_system_prompt，并重算
base_system_prompt = static + dynamic_system_prefix，保持不变量。
避免 memory 变化后静态段与 base 不一致导致 cache 命中旧 memory。"
```

---

## Task 5: 子 Agent 同步改造

**Files:**
- Modify: `agent/subagent.py:386-401`（拆分 static/dynamic 段，组装 system_message）
- Modify: `agent/subagent.py:107-158`（`_run_agent_loop` 签名新增 `system_message`，调用 `agent_runner_loop`）
- Modify: `agent/subagent.py:464-470`（`call_subagent` 调用 `_run_agent_loop` 传 `system_message`）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写失败测试 — 子 Agent 拆分静态/动态段**

在 `tests/test_prompt_cache.py` 追加：

```python
def test_subagent_builds_static_and_dynamic_segments():
    """子 Agent 应构建静态段（agent.md + user_info）+ 动态段（Current Time）。"""
    from agent.subagent import build_subagent_system_segments

    static, dynamic = build_subagent_system_segments("file-processor")
    assert "Current Time" not in static, "静态段不应含 Current Time"
    assert "Current Time" in dynamic, "动态段应含 Current Time"
    assert len(static) > 100, "静态段应含 agent.md 正文"


def test_run_agent_loop_accepts_system_message_param():
    """_run_agent_loop 应支持可选 system_message 参数。"""
    from agent.subagent import _run_agent_loop
    import inspect

    sig = inspect.signature(_run_agent_loop)
    params = sig.parameters
    assert "system_message" in params, \
        "_run_agent_loop 应新增 system_message 可选参数"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py::test_subagent_builds_static_and_dynamic_segments tests/test_prompt_cache.py::test_run_agent_loop_accepts_system_message_param -v`
Expected: FAIL with `ImportError: cannot import name 'build_subagent_system_segments'` 和 `system_message not in params`

- [ ] **Step 3: 新增 `build_subagent_system_segments` 函数**

Read `agent/subagent.py:383-401` 确认当前 `call_subagent` 中 system_prompt 组装逻辑。

在 `agent/subagent.py` 中新增函数（放在 `get_subagent_prompt` 附近）：

```python
def build_subagent_system_segments(agent_name: str) -> tuple:
    """构建子 Agent 的静态/动态系统提示词段（cache 友好）。

    Returns:
        (static_system, dynamic_system):
        - static_system: agent.md 正文 + user_info_section（字节稳定，cache 前缀）
        - dynamic_system: Current Time（每分钟变化，不 cache）
    """
    # 1. 获取子 Agent 提示词（从配置文件）
    static_system = get_subagent_prompt(agent_name)

    # 2. 注入用户信息和偏好（静态段，子 Agent 需要了解用户背景）
    user_info_section = _build_user_info_section()
    if user_info_section:
        static_system += "\n\n" + user_info_section

    # 3. 动态段：Current Time
    from datetime import datetime
    now = datetime.now()
    dynamic_system = f"\n\nCurrent Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    return static_system, dynamic_system
```

- [ ] **Step 4: 改造 `call_subagent` 使用新函数 + 组装 system_message**

修改 `agent/subagent.py:386-401`，把原来的 system_prompt 拼接替换为：

```python
    # 构建静态/动态段（cache 友好）
    static_system, dynamic_system = build_subagent_system_segments(agent_name)

    # 组装 system message（按 model 决定格式：Claude list / 其他 str）
    model_lower = (llm_config.get("model", "") or "").lower()
    if "claude" in model_lower:
        system_message = {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_system,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": dynamic_system},
            ],
        }
    else:
        system_message = {
            "role": "system",
            "content": static_system + dynamic_system,
        }
```

- [ ] **Step 5: 改造 `_run_agent_loop` 签名和调用**

Read `agent/subagent.py:107-158` 确认 `_run_agent_loop` 当前签名和调用 `agent_runner_loop` 的逻辑。

修改签名（L109 附近），在 `system_prompt: str,` 后新增：

```python
    system_prompt: str = "",  # 向后兼容
    system_message: Optional[dict] = None,  # 已组装好的 system message（首轮即带 cache_control）
```

修改 L143-148 调用 `agent_runner_loop`，新增 `system_message=` 参数：

```python
    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        system_message=system_message,
        tools_schema=tools_schema,
        # ... 其他参数不变
    )
```

- [ ] **Step 6: 改造 `call_subagent` 调用 `_run_agent_loop` 传 `system_message`**

Read `agent/subagent.py:464-470` 确认调用点。

修改为：

```python
    result_text, return_value = _run_agent_loop(
        # ... 其他参数不变
        system_prompt="",  # 向后兼容（system_message 非 None 时由分支选择生效，不读此参数）
        system_message=system_message,
        # ...
    )
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v`
Expected: 全部 PASS

- [ ] **Step 8: 临时提交**

```bash
cd <repo_root>
git add agent/subagent.py tests/test_prompt_cache.py
git commit -m "feat(subagent): split static/dynamic system segments for cache

子 Agent 同步改造：build_subagent_system_segments 拆分静态段（agent.md +
user_info）和动态段（Current Time）。_run_agent_loop 新增 system_message
参数，首轮即带 cache_control。Claude 模型走 list + cache_control，
其他模型走字符串前缀稳定。"
```

---

## Task 6: 给 Claude 的 tools 打 cache_control breakpoint

**Files:**
- Modify: `agent/generic/litellm_adapter.py:271-306`（`_convert_tools_schema`）
- Modify: `agent/generic/litellm_adapter.py:339`（调用时传 model）
- Test: `tests/test_prompt_cache.py`

- [ ] **Step 1: 写失败测试 — Claude tools 末尾打 cache_control**

在 `tests/test_prompt_cache.py` 追加：

```python
def test_claude_tools_get_cache_control():
    """Claude 模型的 tools_schema 末尾应打 cache_control breakpoint。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
        {"type": "function", "function": {"name": "tool2", "parameters": {}}},
    ]

    converted = _convert_tools_schema(tools, model="claude-sonnet-4-6")
    assert len(converted) == 2
    assert converted[-1].get("cache_control") == {"type": "ephemeral"}, \
        "Claude tools 末尾应有 cache_control breakpoint"
    assert "cache_control" not in converted[0]


def test_non_claude_tools_no_cache_control():
    """非 Claude 模型的 tools 不应有 cache_control。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
    ]

    converted = _convert_tools_schema(tools, model="ark-code-latest")
    assert len(converted) == 1
    assert "cache_control" not in converted[0]


def test_convert_tools_schema_backward_compatible():
    """不传 model 参数时应向后兼容（不给 tools 加 cache_control）。"""
    from agent.generic.litellm_adapter import _convert_tools_schema

    tools = [
        {"type": "function", "function": {"name": "tool1", "parameters": {}}},
    ]

    # 不传 model（老调用方式）
    converted = _convert_tools_schema(tools)
    assert len(converted) == 1
    assert "cache_control" not in converted[0]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v -k tools`
Expected: FAIL with `TypeError: _convert_tools_schema() takes 1 positional argument but 2 were given`

- [ ] **Step 3: 改造 `_convert_tools_schema` 接收 model 参数**

Read `agent/generic/litellm_adapter.py:271-306` 确认当前实现。

修改签名和实现：

```python
def _convert_tools_schema(tools: Optional[List], model: str = "") -> Optional[List]:
    """将工具schema转换为LiteLLM格式（OpenAI格式）。

    Claude 模型在最后一个 tool 打 cache_control breakpoint，
    让 tools 也命中 prompt cache（tools_schema 稳定，每轮不变）。
    """
    if not tools:
        return None

    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if "type" in tool and "function" in tool:
            converted.append(tool)
        elif "name" in tool and "input_schema" in tool:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                }
            })
        elif tool.get("type") == "function":
            converted.append(tool)
        elif "name" in tool and "parameters" in tool:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["parameters"],
                }
            })

    if not converted:
        return None

    # Claude: 最后一个 tool 打 cache_control breakpoint
    # tools_schema 每轮稳定（base + static MCP + disk），可安全 cache
    model_lower = (model or "").lower()
    if "claude" in model_lower:
        converted[-1] = {**converted[-1], "cache_control": {"type": "ephemeral"}}

    return converted
```

- [ ] **Step 4: 改造 `LiteLLMSession.chat` 调用时传 model**

Read `agent/generic/litellm_adapter.py:339` 确认当前调用。

修改 `agent/generic/litellm_adapter.py:339`：

```python
        litellm_tools = _convert_tools_schema(tools, self.default_model)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v -k tools`
Expected: 3 个 tools 测试 PASS

- [ ] **Step 6: 运行全部测试**

Run: `cd <repo_root> && python -m pytest tests/test_prompt_cache.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 临时提交**

```bash
cd <repo_root>
git add agent/generic/litellm_adapter.py tests/test_prompt_cache.py
git commit -m "feat(adapter): add cache_control to Claude tools

Claude 模型的 tools_schema 末尾打 cache_control breakpoint。tools_schema
每轮稳定（base + static MCP + disk），可安全 cache。_convert_tools_schema
新增 model 参数（默认空字符串，向后兼容）。"
```

---

## Task 7: 端到端验证（真实 LLM 调用）

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动程序，发一条消息触发 LLM 调用**

用户执行：
1. `./niu` 启动程序
2. 在聊天窗口发一条简单消息（如"你好"）
3. 等 Agent 回复完成

- [ ] **Step 2: 检查最新请求日志的结构**

Run: `cd <repo_root> && python3 -c "
import json, glob, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_request.json'))
if not files:
    print('No request log found')
    exit()
with open(files[-1]) as f:
    req = json.load(f)
print('Model:', req.get('model'))
print('Provider:', req.get('provider'))
msgs = req.get('messages', [])
print('Messages count:', len(msgs))
sys_msg = msgs[0] if msgs else {}
content = sys_msg.get('content', '')
if isinstance(content, list):
    print('System content format: LIST (Claude cache_control 模式)')
    for i, block in enumerate(content):
        has_cc = 'cache_control' in block
        print(f'  Block[{i}] type={block.get(\"type\")} cache_control={has_cc} text_len={len(block.get(\"text\",\"\"))}')
elif isinstance(content, str):
    print('System content format: STRING (自动 prefix cache 模式)')
    print('  静态段在开头:', 'Role: Niu' in content[:200] or '全能型' in content[:200])
    print('  Current Time 位置:', content.find('Current Time'))
    print('  总长度:', len(content))
tools = req.get('tools', [])
if tools:
    last_tool = tools[-1]
    print('Last tool has cache_control:', 'cache_control' in last_tool)
pp = req.get('provider_params', {})
if pp.get('extra_headers'):
    print('Provider params extra_headers:', pp['extra_headers'])
"
```

Expected（火山方舟 ark-code-latest）：
- `System content format: STRING`
- `静态段在开头: True`
- `Current Time 位置` 应大于 6000（在 niu.md + memory 之后）
- `Last tool has cache_control: False`（非 Claude）
- `Provider params extra_headers` 为 None 或不含 anthropic-beta

Expected（切到 Claude 后）：
- `System content format: LIST`
- `Block[0] cache_control=True`
- `Block[1] cache_control=False`
- `Last tool has cache_control: True`
- `Provider params extra_headers` 含 `anthropic-beta: prompt-caching-2024-07-31`

- [ ] **Step 3: 验证前缀稳定性（连发两条消息对比静态段）**

用户执行：在聊天窗口再发一条消息（如"现在几点"），等回复完成。运行：

```bash
cd <repo_root> && python3 -c "
import json, glob, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_request.json'))
if len(files) < 2:
    print('需要至少 2 个请求日志来对比')
    exit()
def get_static(path):
    with open(path) as f:
        req = json.load(f)
    content = req['messages'][0]['content']
    if isinstance(content, list):
        return content[0]['text']
    return content.split('Current Time')[0]
s1 = get_static(files[-2])
s2 = get_static(files[-1])
print('请求1静态段长度:', len(s1))
print('请求2静态段长度:', len(s2))
print('静态段字节相同:', s1 == s2)
if s1 != s2:
    for i, (a, b) in enumerate(zip(s1, s2)):
        if a != b:
            print(f'第一个差异在位置 {i}: {s1[max(0,i-20):i+20]!r} vs {s2[max(0,i-20):i+20]!r}')
            break
"
```

Expected: `静态段字节相同: True`

- [ ] **Step 4: 验证 Claude cache 命中（需用户切到 Claude 模型）**

**注意**：此验证需要用户切到 Claude 模型才能做。如果用户一直用火山方舟，跳过此步。

如果用户切到 Claude 模型，发两条消息后检查 response 日志的 usage：

```bash
cd <repo_root> && python3 -c "
import json, glob, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_response.json'))
for f in files[-2:]:
    with open(f) as fh:
        resp = json.load(fh)
    usage = resp.get('usage', {})
    print(f'{f}:')
    print('  prompt_tokens:', usage.get('prompt_tokens'))
    print('  cache_creation_input_tokens:', usage.get('cache_creation_input_tokens'))
    print('  cache_read_input_tokens:', usage.get('cache_read_input_tokens'))
"
```

Expected（第二条消息起）：
- `cache_read_input_tokens` > 0（命中 cache）
- `cache_creation_input_tokens` 只在第一条消息 > 0

- [ ] **Step 5: 最终提交（清理调试代码，如有）**

```bash
cd <repo_root>
git status
git add -A
git commit -m "feat(cache): prompt cache for static system prompt and tools

- 静态段（niu.md + memory）与动态段（Current Time + disk_desc + injection）分离
- Claude 模型用显式 cache_control breakpoint（system 静态段 + tools 末尾）
- 其他模型靠前缀稳定命中服务端自动 prefix cache
- agent_runner_loop 新增可选 system_message 参数，首轮即带 cache_control
- count_messages_tokens 兼容 list content
- 子 Agent 同步改造
- _refresh_user_memories 同步 static_system_prompt 并重算 base
- _on_context_high_usage 压缩后重建 system message"
```

---

## 自审检查

### 1. Spec 覆盖

- 静态/动态段拆分 → Task 1 ✅
- `_assemble_system_message` 统一组装 → Task 2 ✅
- 首轮创建带 cache_control → Task 3（system_message 参数）✅
- `count_messages_tokens` 兼容 list → Task 3 Step 1-4 ✅
- `_on_turn_end` 每轮重写 → Task 3 Step 11 ✅
- `_on_context_high_usage` 压缩后重建 → Task 3 Step 12 ✅
- `_refresh_user_memories` 同步静态段 + 重算 base → Task 4 ✅
- 子 Agent 同步改造（`_run_agent_loop`）→ Task 5 ✅
- tools 打 cache_control → Task 6 ✅
- 向后兼容（39 处测试零改动）→ Task 3 Step 7-9/14 ✅
- 端到端验证 → Task 7 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `static_system_prompt`: str
- `dynamic_system_prefix`: str
- `_assemble_system_message(messages, injection, model)`: 签名一致
- `agent_runner_loop(system_prompt="", system_message=None, ...)`: 签名一致
- `_run_agent_loop(system_prompt="", system_message=None, ...)`: 签名一致
- `build_subagent_system_segments(agent_name) -> tuple`: 返回 (static, dynamic)
- `_convert_tools_schema(tools, model="")`: 签名一致
- Claude content: `list[dict]`
- 其他模型 content: `str`

### 4. 边界条件

- `messages[0]` 不是 system role → `_assemble_system_message` 直接 return ✅
- `injection` 为空 → 动态段只含 Current Time + disk_desc ✅
- `system_message` 为 None → `agent_runner_loop` 回退到 `system_prompt` 字符串 ✅
- `system_message` 为 None 且 `system_prompt` 也空 → messages[0] content 为空字符串（不崩）✅
- `static_system_prompt` 为空 → 用默认值 ✅
- tools 为空 → `_convert_tools_schema` 返回 None ✅
- Claude 但 `api_type` 不是 anthropic → `custom_provider` 仍是 anthropic，cache_control 透传 ✅
- 子 Agent model 不含 claude → 走字符串格式 ✅
- `_convert_tools_schema` 不传 model → 用默认值 `""`，不加 cache_control ✅

### 5. 向后兼容

- `get_system_prompt()` 保留，返回静态段 + Current Time ✅
- `base_system_prompt` 属性保留（= 静态段 + 动态前缀段，由 `_refresh_user_memories` 重算保持不变量）✅
- `agent_runner_loop` 保留 `system_prompt` 参数（默认 `""`），39 处测试零改动 ✅
- `_run_agent_loop` 保留 `system_prompt` 参数 ✅
- `_convert_tools_schema` 新增 `model` 参数有默认值 `""`，老调用点不报错 ✅
- 历史消息结构不变 ✅
- niu.md 内容不变 ✅

### 6. 风险点

- **火山方舟是否支持自动 prefix cache**：本计划假设支持。如果不支持，重构仍无害。用户需向火山方舟确认。
- **ark-code-latest thinking 模式**：thinking 启用下 prefix cache 行为未知，需用户确认。
- **Claude cache TTL**：ephemeral cache TTL 5 分钟。多轮对话中静态段稳定则持续命中。memory 变化时 cache 失效重建（预期行为）。
- **tools 变化**：tools_schema 实际稳定。如果变化，tools cache 失效重建。
- **首轮 cache miss**：首轮 Claude 请求现在带 cache_control（通过 `system_message` 参数），会创建 cache（`cache_creation_input_tokens > 0`）。第二轮起命中（`cache_read_input_tokens > 0`）。
- **`_on_context_high_usage` 压缩后空 injection**：压缩后那一轮 LLM 请求无 skills/knowledge 注入，下一轮 `_on_turn_end` 重新注入。预期行为（压缩后上下文已变，重新检索）。

### 7. 不改动的部分

- niu.md 内容
- 历史消息结构（messages[1:]）
- `_inject_dynamic_resources` 逻辑
- `_build_disk_description` 逻辑
- `_load_memory_for_prompt` 逻辑
- `litellm_adapter.py` 的 `LiteLLMSession.chat` 主体逻辑
- `get_provider_params` 的 Claude header 逻辑（已正确实现）
- 39 处现有测试的 `system_prompt=` 调用

### 8. 三次审查问题修复对照

| v2 审查问题 | v3 修复 |
|---------|---------|
| 39 处测试 `system_prompt=` 会 TypeError | 改为可选 `system_message` 参数，不传则回退 `system_prompt`，测试零改动 ✅ |
| `_run_subagent_loop` 函数名错误 | 改为 `_run_agent_loop`（subagent.py:107 实际名）✅ |
| Task 3 测试与方案不一致 | 重写测试为检查 `system_message` 参数存在且默认 None ✅ |
| `count_messages_tokens` list content TypeError | v3 审查发现 TokenCalculator 已原生支持 list（token_calculator.py:104-106），主路径无 bug；v3.1 加固 except 兜底分支 + 守护测试 ✅ |
| `_refresh_user_memories` base 不变量破坏 | Task 4 重算 `base = static + dynamic` ✅ |
| `test_refresh_user_memories` mock 失效 | v3 用 `agent.runner._memory_file_lock` 错误（函数内局部 import）；v3.1 改为 `niu_memory_server._memory_file_lock`（patch 源模块）✅ |
| `test_refresh_user_memories` 断言过弱（or） | Task 4 测试改为独立断言 ✅ |
| Task 7 火山方舟 vs Claude 假设矛盾 | Task 7 Step 4 明确"需用户切到 Claude"✅ |
| 假测试（list 格式透传） | Task 6 用真实 `_convert_tools_schema` 测试 ✅ |
| `_on_context_high_usage` 空 injection 未说明 | Task 3 Step 12 注释说明 ✅ |
| Step 10 注释"被覆盖"措辞不准 | v3.1 改为"分支选择生效"✅ |
| except 分支 `"".join` 与主路径 `" ".join` 不一致 | v3.1 统一为 `" ".join` ✅ |
| v3.1 Task 4 mock 触发 `ModuleNotFoundError` | v3.2 测试顶部加 `sys.path.insert(0, mcp-servers/memory-server/src)`（参考 test_user_memory.py:10）✅ |
