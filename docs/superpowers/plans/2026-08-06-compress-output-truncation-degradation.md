# 压缩输出超长三级降级策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 压缩路径输出超长时，三级降级（关思考链→砍半消息→报失败）替代破坏性的 `_emergency_clear`。

**Architecture:** 在 compat.py 新增纯逻辑降级函数 `_compact_with_degradation_sync`（不含 IO），三条 COMPACT_TRUNCATED 路径（compat.py Mode-2、compat.py Force、runner.py `_on_context_high_usage`）改为调用该函数。降级函数通过 `call_fn` 参数适配 async/sync 调用方。compat.py 的 async 路径用 `await asyncio.to_thread()` 包装降级调用；runner.py 直接同步调用。

**Tech Stack:** Python 3.11, asyncio, SQLite (MessageStore)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `niu_api/compat.py` | 新增 `_build_degraded_config` / `_halve_history` / `_renumber_history` / `_compact_with_degradation_sync`；修改 Mode-2 和 Force 的 COMPACT_TRUNCATED 处理 |
| `agent/runner.py` | 修改 `_on_context_high_usage` 的 COMPACT_TRUNCATED 处理 |
| `tests/test_compress_degradation.py` | 新增测试文件 |

---

### Task 1: `_build_degraded_config` 降级参数构造

**Files:**
- Create: `tests/test_compress_degradation.py`
- Modify: `niu_api/compat.py`（在 `_emergency_clear` 函数之后新增）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_compress_degradation.py
"""压缩输出超长三级降级策略测试。"""
import copy
import pytest


def test_build_degraded_config_disables_thinking():
    """关闭 thinking，降 reasoning_effort 一级。"""
    from niu_api.compat import _build_degraded_config

    llm_config = {
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}, "max_tokens": 32000},
    }
    result = _build_degraded_config(llm_config)
    assert result["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert result["reasoning_effort"] == "medium"
    # max_tokens 保留
    assert result["litellm_kwargs"]["max_tokens"] == 32000


def test_build_degraded_config_effort_map():
    """reasoning_effort 降级映射：xhigh→high→medium→low→minimal。"""
    from niu_api.compat import _build_degraded_config

    cases = {"xhigh": "high", "high": "medium", "medium": "low", "low": "minimal"}
    for orig, expected in cases.items():
        result = _build_degraded_config({"reasoning_effort": orig, "litellm_kwargs": {}})
        assert result["reasoning_effort"] == expected, f"{orig} should degrade to {expected}"


def test_build_degraded_config_minimal_not_degraded():
    """minimal/none/空 不再降。"""
    from niu_api.compat import _build_degraded_config

    for val in ["minimal", "none", "", None]:
        result = _build_degraded_config({"reasoning_effort": val, "litellm_kwargs": {}})
        assert result["reasoning_effort"] == val


def test_build_degraded_config_deepcopy():
    """不修改原始 llm_config。"""
    from niu_api.compat import _build_degraded_config

    original = {"reasoning_effort": "high", "litellm_kwargs": {"thinking": {"type": "enabled"}}}
    original_snapshot = copy.deepcopy(original)
    _build_degraded_config(original)
    assert original == original_snapshot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_degraded_config'`

- [ ] **Step 3: Write minimal implementation**

在 `niu_api/compat.py` 的 `_emergency_clear` 函数之后（约 L910 后）新增：

```python
def _build_degraded_config(llm_config: dict) -> dict:
    """
    构造降级第一步的 LLM 配置（deepcopy，不修改原始）。
    传入的 llm_config 应已注入 max_tokens（即 llm_config_with_max）。
    """
    import copy

    degraded = copy.deepcopy(llm_config)

    # 关闭 thinking
    litellm_kwargs = dict(degraded.get("litellm_kwargs", {}))
    litellm_kwargs["thinking"] = {"type": "disabled"}
    degraded["litellm_kwargs"] = litellm_kwargs

    # reasoning_effort 降一级
    effort = degraded.get("reasoning_effort", "")
    effort_map = {"xhigh": "high", "high": "medium", "medium": "low", "low": "minimal"}
    if effort in effort_map:
        degraded["reasoning_effort"] = effort_map[effort]

    return degraded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_compress_degradation.py niu_api/compat.py
git commit -m "feat: add _build_degraded_config for thinking-chain degradation"
```

---

### Task 2: `_halve_history` + `_renumber_history` 砍半消息函数

**Files:**
- Modify: `tests/test_compress_degradation.py`
- Modify: `niu_api/compat.py`（在 `_build_degraded_config` 之后新增）

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_compress_degradation.py`：

```python
def test_halve_history_basic():
    """N/2 截断，向前找 role=user 对齐。"""
    from niu_api.compat import _halve_history

    history = [{"role": "user", "content": "[idx:1] msg1"},
               {"role": "assistant", "content": "[idx:2] msg2"},
               {"role": "user", "content": "[idx:3] msg3"},
               {"role": "assistant", "content": "[idx:4] msg4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut = 4//2 = 2, compress_history[2] 是 user(idx:3) → cut_idx=2
    assert len(halved_h) == 2
    assert halved_ids == ["id3", "id4"]
    assert removed_ids == ["id1", "id2"]
    assert cut_idx == 2


def test_halve_history_fallback_no_user():
    """找不到 role=user → 从 target_cut 截断。"""
    from niu_api.compat import _halve_history

    history = [{"role": "assistant", "content": "[idx:1] m1"},
               {"role": "assistant", "content": "[idx:2] m2"},
               {"role": "assistant", "content": "[idx:3] m3"},
               {"role": "assistant", "content": "[idx:4] m4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut=2, 全是 assistant, found_user=False → fallback cut_idx=2
    assert len(halved_h) == 2
    assert cut_idx == 2


def test_halve_history_user_at_index_0():
    """唯一 user 在索引 0 → found_user=True, cut_idx=0, 保留全部。"""
    from niu_api.compat import _halve_history

    history = [{"role": "user", "content": "[idx:1] only user"},
               {"role": "assistant", "content": "[idx:2] reply"},
               {"role": "assistant", "content": "[idx:3] reply2"},
               {"role": "assistant", "content": "[idx:4] reply3"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut=2, 从 2 向前找: idx2=assistant, idx1=assistant, idx0=user → found_user=True, cut_idx=0
    # 不 fallback, 保留全部（cut_idx=0 时 compress_history[0:] = 全部）
    assert len(halved_h) == 4
    assert cut_idx == 0


def test_halve_history_empty():
    """空列表边界。"""
    from niu_api.compat import _halve_history

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history([], [])
    assert halved_h == []
    assert halved_ids == []
    assert removed_ids == []
    assert cut_idx == 0


def test_renumber_history():
    """[idx:N] 重新编号为连续 1, 2, 3...（只替换第一个前缀）"""
    from niu_api.compat import _renumber_history

    history = [{"role": "user", "content": "[idx:51] old msg"},
               {"role": "assistant", "content": "[idx:52] old reply"}]
    result = _renumber_history(history)
    assert result[0]["content"] == "[idx:1] old msg"
    assert result[1]["content"] == "[idx:2] old reply"


def test_renumber_history_no_idx_prefix():
    """没有 [idx:N] 前缀的消息不受影响。"""
    from niu_api.compat import _renumber_history

    history = [{"role": "user", "content": "plain message"}]
    result = _renumber_history(history)
    assert result[0]["content"] == "plain message"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -k "halve or renumber" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write minimal implementation**

在 `niu_api/compat.py` 的 `_build_degraded_config` 之后新增：

```python
def _halve_history(compress_history: list, compress_msg_ids: list) -> tuple[list, list, list, int]:
    """
    砍半消息历史。在 N/2 附近向前找第一条 role=user 消息对齐截断。
    返回 (后半段 history, 后半段 msg_ids, 前半段 msg_ids, cut_idx)。
    cut_idx 是 0-based Python 列表索引。
    [idx:N] 前缀需要重新编号（由 _renumber_history 执行）。
    """
    total = len(compress_history)
    if total == 0:
        return [], [], [], 0
    target_cut = total // 2

    # 从 target_cut 向前找第一条 role=user
    cut_idx = target_cut
    found_user = False
    while cut_idx >= 0:
        msg = compress_history[cut_idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            found_user = True
            break
        cut_idx -= 1

    # 没找到 user 消息 → 从 target_cut 截断
    if not found_user:
        cut_idx = target_cut

    halved_history = compress_history[cut_idx:]
    halved_msg_ids = compress_msg_ids[cut_idx:]
    removed_msg_ids = compress_msg_ids[:cut_idx]

    return halved_history, halved_msg_ids, removed_msg_ids, cut_idx


def _renumber_history(history: list) -> list:
    """重新编号 history 中的 [idx:N] 前缀为连续的 1, 2, 3...（只替换第一个前缀）"""
    import re

    renumbered = []
    for i, msg in enumerate(history):
        if isinstance(msg, dict) and "content" in msg:
            content = msg["content"]
            content = re.sub(r"\[idx:\d+\]", f"[idx:{i + 1}]", content, count=1)
            msg = {**msg, "content": content}
        renumbered.append(msg)
    return renumbered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -v`
Expected: 11 PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_compress_degradation.py niu_api/compat.py
git commit -m "feat: add _halve_history and _renumber_history for message halving"
```

---

### Task 3: `_compact_with_degradation_sync` 降级主函数

**Files:**
- Modify: `tests/test_compress_degradation.py`
- Modify: `niu_api/compat.py`（在 `_renumber_history` 之后新增）

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_compress_degradation.py`：

```python
def test_degradation_step1_success():
    """降级第一步（关思考链）成功 → 返回 (方案, 原始 msg_ids, None)。
    函数只执行降级调用，不执行原始调用（原始调用由调用方在调用本函数之前完成）。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，直接返回成功
        return "keep=1,2\nupdate=1|[摘要] summary"

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=[{"role": "user", "content": "[idx:1] msg"}],
        compress_msg_ids=["id1"],
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is not None
    assert "keep=" in result_str
    assert actual_ids == ["id1"]  # 未砍半，返回原始 msg_ids
    assert halved_ids is None
    assert call_count[0] == 1  # 只有降级第一步1次


def test_degradation_step2_success():
    """降级第二步（砍半）成功 → 返回 (方案, 后半段 msg_ids, 前半段 msg_ids)。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "COMPACT_TRUNCATED:truncated"  # 降级第一步截断
        return "keep=1\nupdate=1|[摘要] summary"  # 降级第二步成功

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is not None
    assert "keep=" in result_str
    # 砍半后 actual_ids 是后半段（target_cut=2, history[2] 是 user → cut_idx=2）
    assert actual_ids == ["id3", "id4"]
    assert halved_ids == ["id1", "id2"]  # 前半段
    assert call_count[0] == 2  # 降级第一步1 + 降级第二步1


def test_degradation_all_fail():
    """全部失败 → 返回 (None, 原始 msg_ids, None)。"""
    from niu_api.compat import _compact_with_degradation_sync

    def mock_call_fn(**kwargs):
        return "COMPACT_TRUNCATED:always truncated"

    history = [{"role": "user", "content": "[idx:1] msg"},
               {"role": "assistant", "content": "[idx:2] reply"},
               {"role": "user", "content": "[idx:3] msg3"},
               {"role": "assistant", "content": "[idx:4] reply4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is None
    assert actual_ids == msg_ids  # 返回原始
    assert halved_ids is None


def test_degradation_dream_idx_in_halved_range():
    """Force 路径 dream_idx 落在裁剪范围内 → 不执行砍半，报失败。
    dream_idx 是 1-based, cut_idx 是 0-based。
    dream_idx=1 <= cut_idx=2 → dream 边界在 0-based idx 0（前半段），报失败。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，返回截断
        return "COMPACT_TRUNCATED:truncated"

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "force_history": [],
                               "last_compress_id": None,
                               "dream_idx_in_force": 1},  # 1-based, <= cut_idx=2
        call_fn=mock_call_fn,
    )
    # dream_idx=1 <= cut_idx=2 → 报失败，不执行砍半
    assert result_str is None
    assert call_count[0] == 1  # 只有降级第一步1次，没有降级第二步


def test_degradation_subagent_error():
    """降级第一步返回 SUBAGENT_ERROR → 报失败。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        return "SUBAGENT_ERROR:AuthenticationError: invalid key"

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=[{"role": "user", "content": "[idx:1] msg"}],
        compress_msg_ids=["id1"],
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is None
    assert call_count[0] == 1  # 只有降级第一步，SUBAGENT_ERROR 直接报失败
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -k "degradation" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write minimal implementation**

在 `niu_api/compat.py` 的 `_renumber_history` 之后新增：

```python
def _compact_with_degradation_sync(
    agent_name: str,
    prompt: str,
    compress_history: list,
    compress_msg_ids: list,
    llm_config: dict,
    prompt_builder: callable,
    prompt_builder_kwargs: dict,
    call_fn: callable,
) -> tuple[str | None, list, list | None]:
    """
    三级降级压缩（纯逻辑，不含 IO）。
    返回值：
    - 成功（未砍半）：(压缩方案字符串, 实际使用的 compress_msg_ids, None)
    - 成功（砍半后）：(压缩方案字符串, 后半段 compress_msg_ids, 前半段被砍掉的 msg_ids)
    - 失败：(None, 原始 compress_msg_ids, None)

    注意：本函数不执行原始调用——原始调用由调用方在调用本函数之前完成。
    本函数只执行降级第一步（call 1）和降级第二步（call 2）。
    """
    # 降级第一步：关闭思考链 + reasoning_effort 降一级
    # 只在原配置开启了思考链时才执行此步（避免无意义的相同配置重试）
    thinking_cfg = llm_config.get("litellm_kwargs", {}).get("thinking", {})
    effort = llm_config.get("reasoning_effort", "")
    thinking_enabled = (isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled") or effort not in ("none", "", None)

    if thinking_enabled:
        degraded_config = _build_degraded_config(llm_config)

        step1_result = call_fn(
            agent_name=agent_name,
            task=prompt,
            llm_config=degraded_config,
            mcp_client=None,
            context_fifo_threshold=-1,
            history=compress_history,
            bypass_at_prefix=True,
        )

        # SUBAGENT_ERROR 检查（LLM 调用失败）
        if step1_result and step1_result.startswith("SUBAGENT_ERROR:"):
            logger.warning(f"[Compact] Degradation step 1 LLM error: {step1_result}")
            return None, compress_msg_ids, None

        # 降级第一步成功（非截断、非错误）
        if step1_result and not step1_result.startswith("COMPACT_TRUNCATED:"):
            logger.info("[Compact] Degradation step 1 (disable thinking) succeeded")
            return step1_result, compress_msg_ids, None

        if step1_result and step1_result.startswith("COMPACT_TRUNCATED:"):
            logger.warning("[Compact] Degradation step 1 still truncated, trying step 2 (halve history)")
        elif not step1_result:
            logger.warning("[Compact] Degradation step 1 returned empty, trying step 2 (halve history)")
    else:
        logger.info("[Compact] Thinking already disabled, skipping step 1, going directly to step 2 (halve history)")

    # 停止检查
    try:
        from agent.runner import is_stop_requested
        if is_stop_requested():
            logger.warning("[Compact] Stop requested during degradation, aborting")
            return None, compress_msg_ids, None
    except Exception:
        pass

    # 降级第二步：砍半消息
    halved_history, halved_msg_ids, removed_msg_ids, cut_idx = _halve_history(
        compress_history, compress_msg_ids
    )

    if len(halved_history) <= 1:
        logger.warning("[Compact] Degradation step 2: halved history too small, aborting")
        return None, compress_msg_ids, None

    # 重新构建 prompt（先 shallow copy 再修改，避免修改原始入参）
    kwargs = dict(prompt_builder_kwargs)

    # Force 路径 dream_idx 重新计算
    # dream_idx 是 1-based（来自 _f_id_to_idx 的 _i+1），cut_idx 是 0-based（Python 列表索引）
    # dream 边界被砍掉的条件：1-based D <= 0-based cut_idx（等价于 0-based D-1 <= cut_idx-1）
    orig_dream_idx = kwargs.get("dream_idx_in_force", 0)
    if orig_dream_idx and orig_dream_idx > 0:
        if orig_dream_idx <= cut_idx:
            # dream 边界在前半段（被砍掉），后半段全部受保护无法删除
            logger.warning(f"[Compact] dream_idx={orig_dream_idx} <= cut_idx={cut_idx}, cannot halve")
            return None, compress_msg_ids, None
        kwargs["dream_idx_in_force"] = orig_dream_idx - cut_idx

    # 重新编号
    halved_history = _renumber_history(halved_history)

    # Mode-2 用 compress_history，Force 用 force_history
    if "compress_history" in kwargs:
        kwargs["compress_history"] = halved_history
    if "force_history" in kwargs:
        kwargs["force_history"] = halved_history

    rebuilt_prompt = prompt_builder(**kwargs)

    step2_result = call_fn(
        agent_name=agent_name,
        task=rebuilt_prompt,
        llm_config=degraded_config if thinking_enabled else llm_config,
        mcp_client=None,
        context_fifo_threshold=-1,
        history=halved_history,
        bypass_at_prefix=True,
    )

    # SUBAGENT_ERROR 检查
    if step2_result and step2_result.startswith("SUBAGENT_ERROR:"):
        logger.warning(f"[Compact] Degradation step 2 LLM error: {step2_result}")
        return None, compress_msg_ids, None

    if step2_result and not step2_result.startswith("COMPACT_TRUNCATED:"):
        logger.info("[Compact] Degradation step 2 (halve history) succeeded")
        return step2_result, halved_msg_ids, removed_msg_ids

    logger.warning("[Compact] Degradation step 2 still truncated, aborting")
    return None, compress_msg_ids, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py -v`
Expected: 16 PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/test_compress_degradation.py niu_api/compat.py
git commit -m "feat: add _compact_with_degradation_sync three-level degradation"
```

---

### Task 4: compat.py Mode-2 调用方改造

**Files:**
- Modify: `niu_api/compat.py:2776-2786`（Mode-2 COMPACT_TRUNCATED 处理块）

- [ ] **Step 1: Read current code to confirm line numbers**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "
with open('niu_api/compat.py') as f:
    lines = f.readlines()
for i, line in enumerate(lines[2770:2790], start=2771):
    print(f'{i}: {line}', end='')
"`
Expected: 看到 L2777 `if compress_result and compress_result.startswith("COMPACT_TRUNCATED:"):` 到 L2786 `)`。

- [ ] **Step 2: Replace the COMPACT_TRUNCATED block**

在 `compress_result = await asyncio.to_thread(run_context_manager_mode2)` 之后（is_stop_requested 检查之前）初始化 `_halved_msg_ids`：

```python
                    compress_result = await asyncio.to_thread(run_context_manager_mode2)
                    _halved_msg_ids = None  # 降级砍半的前半段 msg_ids（正常路径为 None）
```

然后将 L2777-2786 的 `_emergency_clear` 调用替换为（用 asyncio.to_thread 包装降级调用避免阻塞事件循环）：

```python
                    # 截断时触发三级降级（关思考链→砍半消息→报失败）
                    if compress_result and compress_result.startswith("COMPACT_TRUNCATED:"):
                        logger.warning("[Compact] Mode-2 output truncated, starting degradation")
                        result_str, actual_msg_ids, halved_msg_ids = await asyncio.to_thread(
                            _compact_with_degradation_sync,
                            agent_name="context-manager",
                            prompt=prompt,
                            compress_history=compress_history,
                            compress_msg_ids=compress_msg_ids,
                            llm_config=llm_config_with_max,
                            prompt_builder=_build_mode2_prompt,
                            prompt_builder_kwargs={
                                "display_tokens": display_tokens,
                                "compress_target_tokens": target_tokens,
                                "usage_percent": usage_percent,
                                "compress_history": compress_history,
                            },
                            call_fn=call_subagent_with_auto_answer,
                        )
                        if result_str is None:
                            return {"status": "skipped", "mode": "sleep",
                                    "reason": "compress failed: output truncated after all degradation steps"}
                        # 降级成功，用返回值替代 compress_result
                        compress_result = result_str
                        compress_msg_ids = actual_msg_ids
                        _halved_msg_ids = halved_msg_ids
```

- [ ] **Step 3: Add halved_msg_ids to deletes list**

找到 `deletes = [_idx_to_id[i] for i in sorted(delete_idxs) if i in _idx_to_id]`（约 L2823）之后追加：

```python
                        # 砍半掉的前半段 msg_ids 加入删除列表
                        if _halved_msg_ids:
                            deletes.extend(_halved_msg_ids)
```

- [ ] **Step 4: Verify syntax**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 5: Run regression tests**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py tests/test_compress_quality.py -v 2>&1 | tail -15`
Expected: 新测试 PASS，压缩质量测试不回归（3 个 pre-existing 失败可接受）

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/compat.py
git commit -m "feat: Mode-2 compression uses degradation instead of emergency_clear"
```

---

### Task 5: compat.py Force 调用方改造

**Files:**
- Modify: `niu_api/compat.py:3449-3458`（Force COMPACT_TRUNCATED 处理块）

- [ ] **Step 1: Replace the COMPACT_TRUNCATED block**

在 `result = await asyncio.to_thread(run_context_manager_force)` 之后初始化 `_force_halved_msg_ids`：

```python
            result = await asyncio.to_thread(run_context_manager_force)
            _force_halved_msg_ids = None  # 降级砍半的前半段 msg_ids
```

然后将 L3449-3458 的 `_emergency_clear` 调用替换为（用 asyncio.to_thread 包装）：

```python
            if result and result.startswith("COMPACT_TRUNCATED:"):
                logger.warning("[Compact] Force output truncated, starting degradation")
                result_str, actual_msg_ids, halved_msg_ids = await asyncio.to_thread(
                    _compact_with_degradation_sync,
                    agent_name="context-manager",
                    prompt=prompt,
                    compress_history=_force_history,
                    compress_msg_ids=_force_msg_ids,
                    llm_config=llm_config_with_max,
                    prompt_builder=_build_force_prompt,
                    prompt_builder_kwargs={
                        "display_tokens": display_tokens,
                        "compress_target_tokens": target_tokens,
                        "usage_percent": usage_percent,
                        "force_history": _force_history,
                        "last_compress_id": last_compress_id,
                        "dream_idx_in_force": _dream_idx_in_force,
                    },
                    call_fn=call_subagent_with_auto_answer,
                )
                if result_str is None:
                    return {"status": "skipped", "mode": "force",
                            "reason": "compress failed: output truncated after all degradation steps"}
                # 降级成功，用返回值替代 result
                result = result_str
                _force_msg_ids = actual_msg_ids
                _force_halved_msg_ids = halved_msg_ids
                # 重建 idx→UUID 映射（砍半后 msg_ids 变化，旧映射失效）
                _f_idx_to_id = {}
                for _i, _mid in enumerate(_force_msg_ids):
                    _f_idx_to_id[_i + 1] = _mid
```

- [ ] **Step 2: Add halved_msg_ids to deletes list**

找到 Force 路径的 `deletes = [_f_idx_to_id[i] for i in sorted(delete_idxs) if i in _f_idx_to_id]` 之后追加：

```python
                # 砍半掉的前半段 msg_ids 加入删除列表
                if _force_halved_msg_ids:
                    deletes.extend(_force_halved_msg_ids)
```

- [ ] **Step 3: Verify syntax**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 4: Run regression tests**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py tests/test_compress_quality.py -v 2>&1 | tail -15`
Expected: PASS（3 个 pre-existing 失败可接受）

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/compat.py
git commit -m "feat: Force compression uses degradation instead of emergency_clear"
```

---

### Task 6: runner.py `_on_context_high_usage` 调用方改造

**Files:**
- Modify: `agent/runner.py:1863-1889`（COMPACT_TRUNCATED 内联 _emergency_clear 块）

- [ ] **Step 1: Read current code to confirm line numbers**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "
with open('agent/runner.py') as f:
    lines = f.readlines()
for i, line in enumerate(lines[1860:1895], start=1861):
    print(f'{i}: {line}', end='')
"`
Expected: 看到 L1865 `if result and result.startswith("COMPACT_TRUNCATED:"):` 到 L1889 `return {"status": "skipped", ...}`。

- [ ] **Step 2: Replace the inline emergency_clear block**

在 `result = run_context_manager_force()` 之后（L1849 之后）初始化 `_force_halved_msg_ids`：

```python
            result = run_context_manager_force()  # 同步调用，不用 asyncio.to_thread
            _force_halved_msg_ids = None  # 降级砍半的前半段 msg_ids
```

然后将 L1863-1889 替换为（runner.py 是同步路径，直接调用降级函数）：

```python
            # 截断时触发三级降级（关思考链→砍半消息→报失败）
            if result and result.startswith("COMPACT_TRUNCATED:"):
                logger.warning("[Compact] runner.py force output truncated, starting degradation")
                from niu_api.compat import _compact_with_degradation_sync, _build_force_prompt as _bfp
                result_str, actual_msg_ids, halved_msg_ids = _compact_with_degradation_sync(
                    agent_name="context-manager",
                    prompt=prompt,
                    compress_history=_force_history,
                    compress_msg_ids=_force_msg_ids,
                    llm_config=llm_config_with_max,
                    prompt_builder=_bfp,
                    prompt_builder_kwargs={
                        "display_tokens": display_tokens,
                        "compress_target_tokens": target_tokens,
                        "usage_percent": usage_percent,
                        "force_history": _force_history,
                        "last_compress_id": last_compress_id,
                        "dream_idx_in_force": _dream_idx_in_force,
                    },
                    call_fn=call_subagent_with_auto_answer,
                )
                if result_str is None:
                    return {"status": "skipped", "reason": "compress failed: output truncated after all degradation steps"}
                # 降级成功，用返回值替代 result
                result = result_str
                _force_msg_ids = actual_msg_ids
                _force_halved_msg_ids = halved_msg_ids
                # 重建 idx→UUID 映射（砍半后 msg_ids 变化，旧映射失效）
                _f_idx_to_id = {}
                for _i, _mid in enumerate(_force_msg_ids):
                    _f_idx_to_id[_i + 1] = _mid
```

- [ ] **Step 3: Add halved_msg_ids to deletes list**

找到 runner.py 的 `deletes = [_f_idx_to_id[i] for i in sorted(delete_idxs) if i in _f_idx_to_id]` 之后追加：

```python
                # 砍半掉的前半段 msg_ids 加入删除列表
                if _force_halved_msg_ids:
                    deletes.extend(_force_halved_msg_ids)
```

- [ ] **Step 4: Verify syntax**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 5: Run regression tests**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py tests/test_truncation_marker.py tests/test_llm_error_handling.py -v 2>&1 | tail -15`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "feat: runner.py _on_context_high_usage uses degradation instead of emergency_clear"
```

---

### Task 7: 端到端验证

**Files:**
- 无修改

- [ ] **Step 1: Full syntax check on all modified files**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['niu_api/compat.py', 'agent/runner.py']]; print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 2: Run all related tests**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_compress_degradation.py tests/test_compress_quality.py tests/test_truncation_marker.py tests/test_llm_error_handling.py tests/test_at_prefix_interception.py tests/test_sync_subagent_interaction.py -v 2>&1 | tail -20`
Expected: 新测试 PASS + pre-existing 失败不变（4 个：3 compress_quality + 1 sync_subagent）

- [ ] **Step 3: Verify commit chain**

Run: `cd /Users/lilei/tools/ai-bot && git log --oneline -8`
Expected: 看到 Task 1-6 的提交链。
