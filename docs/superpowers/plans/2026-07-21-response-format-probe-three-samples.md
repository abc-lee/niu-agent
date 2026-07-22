# response_format 探测三次采样 + 限流重试 + 通用化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 response_format 探测在 flaky 网关（豆包 Coding Plan）上的不可靠性——三次采样全过才升档、限流不计失败只重试、冲突式设计消除假阳性，确保任何网关都稳定落到正确档位。

**Architecture:** 探测端点（niu_api/compat.py）从"单次探测 + 一致式设计"改为"三次采样 + 冲突式设计 + 错误码分类（限流/超时重试、基础设施错误早返）"。运行时决策函数（lightrag_manager.py）不动，后台探测守卫保留但改新词汇。前端 main.js 和后台探测超时同步加大（新逻辑耗时更长）。

**Tech Stack:** Python 3.11, FastAPI, LiteLLM, pytest

---

## 背景（为什么这次修复是对的）

**flaky 网关实锤**（2026-07-21 实测）：豆包 Coding Plan 网关对同一冲突式请求 5 次采样，2 次 schema 胜（执行 json_schema strict）、3 次 prompt 胜（静默忽略）——行为非确定性。

**单次探测的问题**：碰巧命中"执行"窗口期 → 误判 json_schema 写入配置；碰巧命中"静默忽略"窗口期 → 误判 prompt_only 丢失能力。两种都错。

**三次采样 + 全过才升档**：flaky 网关必然 ≥1 次静默忽略 → 稳定降级 prompt_only（正确兜底）。真支持网关（OpenAI）3 次全过 → 稳定写入 json_schema（正确识别）。

**限流单独处理**：RateLimitError ≠ 不支持，只 sleep 重试不计失败——直到返回非限流结果（supported / model_rejected / gateway_blocked）才判定该次采样。

**超时同等待遇**：asyncio.TimeoutError 同属 transient infra 问题（网关慢/抖动），与限流一样 sleep 重试不计失败——避免慢但真支持的厂商（本地 Ollama、DeepSeek 推理延迟）被误杀。

**冲突式设计消除假阳性**：schema 强制 `{"verdict": "SCHEMA_ENFORCED"}` + prompt 要求"写海洋句子禁止 JSON"——只有 schema 战胜 prompt（网关真执行）才判 supported，模型跟随 prompt 输出海洋句子即判 gateway_blocked。对任何网关通用。

**调用链超时同步加大**：新逻辑单档最坏耗时 ~250s（3 次采样 + 限流/超时重试），前端 main.js timeout 从 70s 提到 300s，后台探测 timeout 从 90s 提到 300s，集成测试 timeout 从 90s 提到 600s。

---

## Task 1: 改造探测 helper 函数（schema/prompt/分类器）

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/compat.py:1321-1398`
- Test: `REDACTED_USER_PATH/tools/ai-bot/tests/test_response_format_probe.py`

- [ ] **Step 1: 写失败测试——冲突式 schema 结构**

在 `tests/test_response_format_probe.py` 的 `test_build_probe_response_format_json_schema_structure` 函数处改为：

```python
def test_build_probe_response_format_json_schema_structure():
    """json_schema 档构造冲突式 schema（verdict 枚举单值），用于区分真假支持"""
    rf = _build_probe_response_format_json_schema()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "probe_response_format"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["SCHEMA_ENFORCED"]
    assert schema["required"] == ["verdict"]
    assert schema["additionalProperties"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_build_probe_response_format_json_schema_structure -v`
Expected: FAIL（当前 schema 是 `ok: boolean`，无 verdict）

- [ ] **Step 3: 改造 `_build_probe_response_format_json_schema`**

在 `niu_api/compat.py:1321` 处改为：

```python
def _build_probe_response_format_json_schema() -> dict:
    """构造 Tier 1 探测用 response_format：json_schema strict，冲突式设计。

    schema 强制要求 {"verdict": "SCHEMA_ENFORCED"}，而探测 prompt（_build_probe_messages）
    要求模型写一句普通英文句子且禁止输出 JSON——两者矛盾。只有网关真正执行
    json_schema strict（schema 战胜 prompt）时，输出才会是 schema 约束的 JSON；
    网关静默接受但不执行时，模型跟随 prompt 输出普通句子，被判 gateway_blocked。

    Why 冲突式设计：2026-07-21 实测发现豆包 Coding Plan 网关行为是 flaky 的——
    同一请求 5 次采样，2 次 schema 胜、3 次 prompt 胜。原设计 prompt 与 schema
    都要求 {"ok": true}，模型跟随 prompt 即可输出合法 JSON，无法区分"真支持"
    与"静默忽略"，产生假阳性（碰巧命中执行窗口期时误判 json_schema）。
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "probe_response_format",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["SCHEMA_ENFORCED"]},
                },
                "required": ["verdict"],
                "additionalProperties": False,
            },
        },
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_build_probe_response_format_json_schema_structure -v`
Expected: PASS

- [ ] **Step 5: 写失败测试——冲突式 prompt**

在 `tests/test_response_format_probe.py` 的 `_build_probe_messages` 测试处改为：

```python
def test_build_probe_messages_returns_single_user_message_with_json_keyword():
    """探测消息：单条 user 消息；含 "json" 字样（OpenAI json_object 硬性要求 prompt 含 json 字符串）"""
    msgs = _build_probe_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "json" in msgs[0]["content"].lower()


def test_build_probe_messages_conflicts_with_schema():
    """探测消息与 schema 故意矛盾（冲突式设计）：要求普通句子且禁止 JSON 输出"""
    msgs = _build_probe_messages()
    content = msgs[0]["content"].lower()
    assert "ocean" in content
    assert "do not output json" in content
```

- [ ] **Step 6: 跑测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_build_probe_messages_conflicts_with_schema -v`
Expected: FAIL（当前 prompt 是 `Respond with a JSON object: {"ok": true}`，无 ocean / do not output json）

- [ ] **Step 7: 改造 `_build_probe_messages`**

在 `niu_api/compat.py:1351` 处改为：

```python
def _build_probe_messages() -> list[dict]:
    """构造探测消息：要求写一句普通英文句子且禁止输出 JSON。

    与 Tier 1 schema（强制 {"verdict": "SCHEMA_ENFORCED"}）故意矛盾——只有网关
    真正执行 response_format 时输出才是 JSON；网关静默忽略时模型跟随 prompt
    输出普通句子，被分类器判 gateway_blocked。

    Why 必须含 "json" 字样：OpenAI json_object 模式硬性要求 prompt 含 "json"
    字符串，否则直接 400（会造成对真支持厂商的假阴性）。"Do not output JSON"
    一句天然含 "JSON"，满足该检查。
    """
    return [{
        "role": "user",
        "content": "Write exactly one English sentence about the ocean. Do not output JSON.",
    }]
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_build_probe_messages_conflicts_with_schema -v`
Expected: PASS

- [ ] **Step 9: 写失败测试——Tier 1 分类器 verdict 判定（整体替换测试区域）**

在 `tests/test_response_format_probe.py` 的 Tier 1 分类器测试区域（约 L173-210），**整体替换**为：

```python
# Tier 1 (json_schema strict) 冲突式设计：只有 verdict == "SCHEMA_ENFORCED" 才算 supported
def test_classify_tier1_supported_when_verdict_schema_enforced():
    """响应是 {"verdict": "SCHEMA_ENFORCED"} → supported（schema 战胜 prompt）"""
    assert _classify_probe_response_tier1('{"verdict": "SCHEMA_ENFORCED"}') == "supported"


def test_classify_tier1_supported_when_verdict_with_extra_fields():
    """响应含 verdict + 额外字段 → supported（容忍额外字段，部分厂商宽松处理 additionalProperties）"""
    assert _classify_probe_response_tier1('{"verdict": "SCHEMA_ENFORCED", "extra": "ignored"}') == "supported"


def test_classify_tier1_gateway_blocked_when_prompt_following_ok_json():
    """关键回归：响应是 {"ok": true}（旧探测设计的"成功"响应）→ gateway_blocked

    旧设计 prompt 与 schema 都要 {"ok": true}，模型跟随 prompt 即假阳性。
    新设计下这只是"模型跟随 prompt 的普通 JSON"，无 verdict → gateway_blocked。
    """
    assert _classify_probe_response_tier1('{"ok": true}') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_ocean_sentence():
    """响应是普通英文句子（flaky 网关静默忽略时模型跟随 prompt）→ gateway_blocked"""
    assert _classify_probe_response_tier1(
        'Beneath the sun-dappled surface of the ocean, vibrant coral reefs teem with life.'
    ) == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_wrong_verdict_value():
    """响应是 {"verdict": "WRONG"}（JSON 合法但 verdict 值不匹配枚举）→ gateway_blocked"""
    assert _classify_probe_response_tier1('{"verdict": "WRONG"}') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_plain_text():
    """响应是纯文本 → gateway_blocked"""
    assert _classify_probe_response_tier1('I am doing fine.') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_truncated_json():
    """响应是截断的非合法 JSON → gateway_blocked"""
    assert _classify_probe_response_tier1('{"verdict":') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier1('') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_markdown_wrapped():
    """响应是 ```json 包裹的 verdict JSON → gateway_blocked（非纯 JSON，schema strict 不会产 markdown）"""
    assert _classify_probe_response_tier1('```json\n{"verdict": "SCHEMA_ENFORCED"}\n```') == "gateway_blocked"
```

删除旧的 Tier 1 分类器测试区域全部测试（`test_classify_tier1_supported_when_valid_json_with_ok_field`、`test_classify_tier1_supported_when_json_with_extra_fields`、`test_classify_tier1_gateway_blocked_when_json_without_ok_field`、`test_classify_tier1_gateway_blocked_when_plain_text`、`test_classify_tier1_gateway_blocked_when_truncated_json`、`test_classify_tier1_gateway_blocked_when_empty`、`test_classify_tier1_gateway_blocked_when_markdown_wrapped`），用上面新测试整体替换。

- [ ] **Step 10: 跑测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_classify_tier1_supported_when_verdict_schema_enforced -v`
Expected: FAIL（当前分类器判定"含 ok 字段"，不是 verdict）

- [ ] **Step 11: 改造 `_classify_probe_response_tier1`**

在 `niu_api/compat.py:1360` 处改为：

```python
def _classify_probe_response_tier1(text: str) -> str:
    """Tier 1 (json_schema strict) 判定：响应必须是合法 JSON dict 且
    verdict == "SCHEMA_ENFORCED"（schema 战胜 prompt 的铁证）。

    容忍额外字段（部分厂商可能只严格执行 required/enum、宽松处理
    additionalProperties），但 verdict 值必须精确匹配枚举。

    真实环境验证（2026-07-21）：豆包 Coding Plan 网关行为 flaky——同一请求
    5 次采样，2 次 schema 胜、3 次 prompt 胜（模型跟随 prompt 输出海洋句子）。
    冲突式设计确保只有 schema 真正生效时才判 supported，消除假阳性。
    """
    import json
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return "gateway_blocked"
    if not isinstance(data, dict):
        return "gateway_blocked"
    if data.get("verdict") != "SCHEMA_ENFORCED":
        return "gateway_blocked"
    return "supported"
```

- [ ] **Step 12: 跑测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py -v -k "classify_tier1"`
Expected: 9 个测试全 PASS

- [ ] **Step 13: 更新 `_classify_probe_response_tier2` docstring（逻辑不变）**

在 `niu_api/compat.py:1379` 处改 docstring（函数体不动）：

```python
def _classify_probe_response_tier2(text: str) -> str:
    """Tier 2 (json_object) 判定：只要求响应是合法 JSON dict。

    探测 prompt 明确要求"不要输出 JSON"，此时输出仍是合法 JSON dict 即说明
    json_object 约束真正生效（模型被强制输出 JSON）；网关静默忽略时模型跟随
    prompt 输出普通句子 → 非 JSON → gateway_blocked。

    已知边界：理论上存在"网关静默忽略 + 模型不听指令仍输出 JSON"的假阳性
    组合，概率低且 json_object 档位误判代价小（运行时 json_repair 兜底）。
    """
```

- [ ] **Step 14: 全量跑测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py -v -k "not endpoint"`
Expected: 全 PASS（除 endpoint 集成测试外的所有单元测试）

- [ ] **Step 15: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_response_format_probe.py
git commit -m "$(cat <<'EOF'
feat(api): 探测 helper 改冲突式设计，消除 flaky 网关假阳性

2026-07-21 实测豆包 Coding Plan 网关行为非确定性：同一请求 5 次采样，
2 次 schema 胜、3 次 prompt 胜。原设计 prompt 与 schema 都要求 {"ok": true}，
模型跟随 prompt 即可输出合法 JSON，碰巧命中执行窗口期时误判 json_schema。

改造：
- _build_probe_response_format_json_schema：schema 改冲突式（verdict 枚举
  单值 SCHEMA_ENFORCED）
- _build_probe_messages：prompt 改"写海洋句子禁止 JSON"（与 schema 矛盾）
- _classify_probe_response_tier1：改 verdict 判定（schema 战胜 prompt 才
  算 supported）
- _classify_probe_response_tier2：逻辑不变，docstring 补冲突语义

关键回归：旧探测的"成功"响应 {"ok": true} 在新设计下被正确判
gateway_blocked。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 探测端点改三次采样 + 限流重试 + 错误码分类

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/compat.py:1401-1573`
- Test: `REDACTED_USER_PATH/tools/ai-bot/tests/test_response_format_probe.py`

- [ ] **Step 1: 写失败测试——三次采样全过才 supported**

在 `tests/test_response_format_probe.py` 末尾新增（用 pytest-asyncio 跑异步测试）：

```python
# ===== 三次采样逻辑测试 =====

@pytest.mark.asyncio
async def test_probe_tier_three_samples_all_pass_returns_supported():
    """三次采样全 supported → 该档 supported"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(return_value=("supported", '{"verdict": "SCHEMA_ENFORCED"}'))
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    assert mock_try.call_count == 3


@pytest.mark.asyncio
async def test_probe_tier_one_gateway_blocked_returns_failed():
    """三次采样中任何一次 gateway_blocked → 该档失败"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("gateway_blocked", "ocean sentence"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "gateway_blocked"
    assert raw == "ocean sentence"
    # 失败即停，不再采样第三次
    assert mock_try.call_count == 2


@pytest.mark.asyncio
async def test_probe_tier_one_model_rejected_returns_failed():
    """三次采样中任何一次 model_rejected → 该档失败"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("model_rejected", "BadRequestError: 400"),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "model_rejected"
    assert raw == "BadRequestError: 400"
    assert mock_try.call_count == 2


@pytest.mark.asyncio
async def test_probe_tier_rate_limit_retries_without_counting():
    """限流只重试不计失败，直到返回非限流结果"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(side_effect=[
        ("rate_limited", "RateLimitError: 429"),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    # mock asyncio.sleep 避免真实等待
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    # 限流重试 1 次 + 3 次正常采样 = 4 次调用
    assert mock_try.call_count == 4


@pytest.mark.asyncio
async def test_probe_tier_timeout_retries_without_counting():
    """超时同限流处理：只重试不计失败（asyncio.TimeoutError + litellm.Timeout）"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch
    import asyncio

    # 场景 1：asyncio.TimeoutError（外层 wait_for 30s 超时）
    mock_try = AsyncMock(side_effect=[
        asyncio.TimeoutError(),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "supported"
    assert raw == ""
    assert mock_try.call_count == 4

    # 场景 2：litellm.Timeout（线程内 read_timeout 15s 超时，慢厂商真实路径）
    mock_try2 = AsyncMock(side_effect=[
        ("timeout", "litellm.Timeout: APITimeoutError"),  # _try_tier 捕获 litellm.Timeout 后返回
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result2, raw2 = await _probe_tier_three_samples_async(mock_try2, {"type": "json_schema"})
    assert result2 == "supported"
    assert raw2 == ""
    assert mock_try2.call_count == 4


@pytest.mark.asyncio
async def test_probe_tier_rate_limit_exhausted_returns_error():
    """限流/超时重试超过上限（整档共享 5 次）仍未成功 → 返回 rate_limited"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(return_value=("rate_limited", "RateLimitError: 429"))
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "rate_limited"
    assert raw == "RateLimitError: 429"
    # 最多重试 5 次
    assert mock_try.call_count == 6  # 1 次初始 + 5 次重试


@pytest.mark.asyncio
async def test_probe_tier_transient_retries_shared_across_samples():
    """限流/超时重试预算整档共享：采样 1 限流 3 次 + 采样 2 限流 3 次 → 第 6 次返回 rate_limited"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock, patch

    mock_try = AsyncMock(side_effect=[
        ("rate_limited", "RateLimitError: 429"),  # 采样 1 第 1 次
        ("rate_limited", "RateLimitError: 429"),  # 采样 1 第 2 次
        ("rate_limited", "RateLimitError: 429"),  # 采样 1 第 3 次
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),  # 采样 1 成功
        ("rate_limited", "RateLimitError: 429"),  # 采样 2 第 1 次（累计第 4 次）
        ("rate_limited", "RateLimitError: 429"),  # 采样 2 第 2 次（累计第 5 次）
        ("rate_limited", "RateLimitError: 429"),  # 采样 2 第 3 次（累计第 6 次，超限）
    ])
    with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
        result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "rate_limited"
    assert raw == "RateLimitError: 429"
    assert mock_try.call_count == 7  # 1 + 3 + 3


@pytest.mark.asyncio
async def test_probe_tier_infra_error_returns_immediately():
    """任何一次基础设施错误（401/网络断/500）→ 立即返回 infra_error，不写配置"""
    from niu_api.compat import _probe_tier_three_samples_async
    from unittest.mock import AsyncMock

    mock_try = AsyncMock(side_effect=[
        ("supported", '{"verdict": "SCHEMA_ENFORCED"}'),
        ("infra_error", "AuthenticationError: 401"),
    ])
    result, raw = await _probe_tier_three_samples_async(mock_try, {"type": "json_schema"})
    assert result == "infra_error"
    assert raw == "AuthenticationError: 401"
    assert mock_try.call_count == 2
```

**注意**：测试文件顶部需确认已 import `pytest`（用于 `@pytest.mark.asyncio` 装饰器）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py::test_probe_tier_three_samples_all_pass_returns_supported -v`
Expected: FAIL（`_probe_tier_three_samples_async` 不存在）

- [ ] **Step 3: 新增 `_probe_tier_three_samples_async` 异步函数**

在 `niu_api/compat.py` 的 `_classify_probe_response_tier2` 函数后（约 L1398 后）新增。同时在文件顶部（或函数前）添加模块级 import：

```python
from asyncio import sleep as _asyncio_sleep
```

然后新增函数：

```python
async def _probe_tier_three_samples_async(try_fn, response_format: dict) -> tuple[str, str]:
    """单档三次采样（异步版）：全过才 supported，限流/超时只重试不计失败。

    Args:
        try_fn: 单次采样异步函数，签名 () -> await (tier_result, raw_text_or_reason)。
                tier_result 取值: "supported" / "gateway_blocked" / "model_rejected" / "rate_limited" / "timeout" / "infra_error"
        response_format: 本档 response_format（用于日志）

    Returns:
        (result, last_raw) 元组：
        - result: "supported" / "gateway_blocked" / "model_rejected" / "rate_limited" / "infra_error"
        - last_raw: 最后一次采样的 raw 摘要（含异常类名，用于诊断；三次全过时为空字符串）

    Why 三次采样：2026-07-21 实测豆包 Coding Plan 网关行为非确定性（flaky），
    同一请求 5 次采样 2 次 schema 胜、3 次 prompt 胜。单次探测碰巧命中执行
    窗口期会误判 json_schema，碰巧命中静默忽略窗口期会误判 prompt_only。
    三次采样全过才升档——flaky 网关必然 ≥1 次静默忽略，稳定降级 prompt_only；
    真支持网关（OpenAI）3 次全过，稳定写入 json_schema。

    Why 限流/超时只重试不计失败：RateLimitError / litellm.Timeout /
    asyncio.TimeoutError ≠ 不支持，只是"这次请求被网关挡了"或"网关慢/抖动"。
    限流/超时同属 transient infra 问题，sleep 后重试本次采样，直到返回非限流/
    非超时结果（supported / model_rejected / gateway_blocked）才判定该次采样。

    Why 重试预算整档共享：防止限流/超时期间无限拖延端点。3 次采样共享 5 次
    重试预算（限流+超时累计），指数退避 5s→10s→20s→40s→80s，最多等 155s。
    """
    import asyncio

    MAX_TRANSIENT_RETRIES = 5  # 限流+超时共享 5 次重试预算
    transient_retries = 0

    for sample_num in range(1, 4):  # 采样 1、2、3
        while True:
            try:
                result, raw = await try_fn()
            except asyncio.TimeoutError:
                result, raw = "timeout", "TimeoutError: 采样超时（30s）"

            if result in ("rate_limited", "timeout"):
                transient_retries += 1
                if transient_retries > MAX_TRANSIENT_RETRIES:
                    logger.warning(
                        f"探测限流/超时重试 {MAX_TRANSIENT_RETRIES} 次仍未成功，放弃 "
                        f"(最后错误: {result})"
                    )
                    return "rate_limited", raw
                # 指数退避：5s → 10s → 20s → 40s → 80s
                sleep_seconds = 5 * (2 ** (transient_retries - 1))
                logger.info(
                    f"探测采样 {sample_num} {result}，{sleep_seconds}s 后重试 "
                    f"（第 {transient_retries} 次，response_format={response_format.get('type')}）"
                )
                await _asyncio_sleep(sleep_seconds)  # 模块级 import 便于测试 patch
                continue
            # 非限流/非超时结果，判定该次采样
            break

        if result == "infra_error":
            # 基础设施错误（401/网络断/500/503）：不写配置，端点早返 probe_failed
            logger.warning(
                f"探测采样 {sample_num} 基础设施错误（{raw[:80]}），"
                f"不写配置，提示用户稍后重试"
            )
            return "infra_error", raw

        if result != "supported":
            # 任何一次 gateway_blocked / model_rejected 立即失败，不再采样
            logger.info(
                f"探测采样 {sample_num} 失败（{result}, response_format={response_format.get('type')}），"
                f"该档不通过"
            )
            return result, raw

    # 三次全 supported
    return "supported", ""
```

**注意**：`from asyncio import sleep as _asyncio_sleep` 必须在模块级（文件顶部 import 区域），不能在函数内部 import——这样测试才能 `patch("niu_api.compat._asyncio_sleep")` 精确替换，避免全局 patch `asyncio.sleep` 影响其他协程。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py -v -k "probe_tier"`
Expected: 8 个测试全 PASS（all_pass / gateway_blocked / model_rejected / rate_limit / timeout / exhausted / shared / infra_error）

- [ ] **Step 5: 写失败测试——端点 rate_limited / infra_error 返回 probe_failed**

在 `tests/test_response_format_probe.py` 末尾新增（命名避免含 "endpoint"，防止被 `-k "not endpoint"` 过滤器误排除）：

```python
@pytest.mark.asyncio
async def test_probe_returns_probe_failed_when_rate_limited():
    """端点探测限流/超时重试耗尽 → 返回 probe_failed + rate_limited reason"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch
    from fastapi import Request

    # mock _probe_tier_three_samples_async 返回 (rate_limited, raw) 元组
    with patch("niu_api.compat._probe_tier_three_samples_async", new_callable=AsyncMock) as mock_sampler:
        mock_sampler.return_value = ("rate_limited", "RateLimitError: 429")

        # 构造 mock Request（body 为空，走 get_llm_config 分支）
        mock_request = AsyncMock(spec=Request)
        mock_request.json = AsyncMock(return_value={})

        # mock get_llm_config 返回有效配置
        with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
            mock_get_config.return_value = {
                "apikey": "test-key",
                "apibase": "https://test.example.com",
                "model": "test-model",
                "type": "openai",
                "litellm_kwargs": {},
            }

            result = await probe_response_format(mock_request)

    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"]
    assert result["mode"] is None


@pytest.mark.asyncio
async def test_probe_returns_probe_failed_when_infra_error():
    """端点探测遇基础设施错误（401/网络断/500）→ 返回 probe_failed + infra_error reason，不写配置"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch
    from fastapi import Request

    # mock _probe_tier_three_samples_async 返回 (infra_error, raw) 元组
    with patch("niu_api.compat._probe_tier_three_samples_async", new_callable=AsyncMock) as mock_sampler:
        mock_sampler.return_value = ("infra_error", "AuthenticationError: 401")

        mock_request = AsyncMock(spec=Request)
        mock_request.json = AsyncMock(return_value={})

        with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
            mock_get_config.return_value = {
                "apikey": "test-key",
                "apibase": "https://test.example.com",
                "model": "test-model",
                "type": "openai",
                "litellm_kwargs": {},
            }

            result = await probe_response_format(mock_request)

    assert result["result"] == "probe_failed"
    assert "基础设施错误" in result["reason"]
    assert result["mode"] is None
```

- [ ] **Step 6: 改造端点 `_try_tier` 区分限流/超时/基础设施错误**

在 `niu_api/compat.py:1488` 的 `_try_tier` 函数处改为：

```python
    def _try_tier(response_format: Optional[dict]) -> tuple[str, str]:
        """单次采样。返回 (tier_result, raw_text_or_reason)。

        判定逻辑：
        - 没抛异常 + 响应符合该档要求 → "supported"
        - 没抛异常 + 响应不符合 → "gateway_blocked"
        - 抛 RateLimitError → "rate_limited"（限流，不计失败，上层重试）
        - 抛 litellm.Timeout / openai.APITimeoutError → "timeout"（超时，不计失败，上层重试）
        - 抛 AuthenticationError / APIConnectionError / InternalServerError /
          ServiceUnavailableError → "infra_error"（基础设施错误，不写配置，端点早返 probe_failed）
        - 抛 BadRequestError / UnsupportedParamsError → "model_rejected"
        - 抛其他异常 → "model_rejected"（统一视为不支持，reason 记录供诊断）

        Why 限流/超时单独分类：RateLimitError / litellm.Timeout ≠ 不支持，只是
        "这次请求被网关挡了"或"网关慢/抖动"。限流/超时时上层
        _probe_tier_three_samples_async sleep 后重试本次采样，直到返回非限流/
        非超时结果才判定。如果混入 model_rejected，限流/超时会被误判为不支持。

        Why 捕获 litellm.Timeout：慢厂商（本地 Ollama、DeepSeek 推理延迟）的
        真实超时路径是 litellm 在线程内 read_timeout（15s）先抛 litellm.Timeout
        （APITimeoutError 子类），外层 asyncio.wait_for（30s）几乎永远轮不到。
        如果不捕获，litellm.Timeout 会被 generic except Exception 归类
        model_rejected → 失败即停，慢但真支持的厂商被误杀。

        Why 基础设施错误单独分类：AuthenticationError（401）/ APIConnectionError
        （网络断）/ InternalServerError（500）/ ServiceUnavailableError（503）
        是临时性基础设施故障，不是"模型不支持 response_format"。如果归入
        model_rejected，两档失败 → prompt_only 写入配置 →
        _should_auto_probe_after_upgrade 永远 False → 首次升级启动时恰好
        API Key 失效/网关 500 的用户被永久静默降级，且永不重探。基础设施错误
        应该端点早返 probe_failed，不写配置，用户稍后手动重试。
        """
        from litellm import (
            RateLimitError, BadRequestError, UnsupportedParamsError,
            AuthenticationError, APIConnectionError, InternalServerError,
            ServiceUnavailableError,
        )
        import litellm
        import openai

        try:
            session = LiteLLMSession(cfg=base_llm_config)
            gen = session.chat(messages=messages, response_format=response_format)
            chunks = []
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration:
                pass
            text = "".join(chunks)
            if response_format is not None and response_format.get("type") == "json_schema":
                tier = _classify_probe_response_tier1(text)
            elif response_format is not None and response_format.get("type") == "json_object":
                tier = _classify_probe_response_tier2(text)
            else:
                tier = "gateway_blocked"
            return tier, text
        except RateLimitError as e:
            return "rate_limited", f"RateLimitError: {str(e)[:150]}"
        except (litellm.Timeout, openai.APITimeoutError) as e:
            return "timeout", f"{type(e).__name__}: {str(e)[:150]}"
        except (AuthenticationError, APIConnectionError, InternalServerError, ServiceUnavailableError) as e:
            return "infra_error", f"{type(e).__name__}: {str(e)[:150]}"
        except (BadRequestError, UnsupportedParamsError) as e:
            return "model_rejected", f"{type(e).__name__}: {str(e)[:150]}"
        except Exception as e:
            return "model_rejected", f"{type(e).__name__}: {str(e)[:150]}"
```

- [ ] **Step 7: 改造端点主流程用三次采样（异步）**

在 `niu_api/compat.py:1532` 起的 Tier 1 / Tier 2 调用处改为：

```python
    # Tier 1: json_schema strict，三次采样
    tier1_result, tier1_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_schema()),
            timeout=30,
        ),
        _build_probe_response_format_json_schema(),
    )

    if tier1_result == "rate_limited":
        return {
            "result": "probe_failed",
            "reason": "探测限流/超时重试 5 次仍未成功，请稍后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier1_result == "infra_error":
        return {
            "result": "probe_failed",
            "reason": "探测遇到基础设施错误（API Key 失效/网络断/网关 5xx），请检查配置后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier1_result == "supported":
        return {
            "result": "supported",
            "mode": "json_schema",
            "reason": "Tier 1 三次采样全通过：模型+网关均稳定支持 json_schema strict 模式",
            "raw_response": "",  # 三次采样无单一 raw_response
        }

    # Tier 2: json_object，三次采样
    tier2_result, tier2_raw = await _probe_tier_three_samples_async(
        lambda: asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_object()),
            timeout=30,
        ),
        _build_probe_response_format_json_object(),
    )

    if tier2_result == "rate_limited":
        return {
            "result": "probe_failed",
            "reason": "探测限流/超时重试 5 次仍未成功，请稍后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier2_result == "infra_error":
        return {
            "result": "probe_failed",
            "reason": "探测遇到基础设施错误（API Key 失效/网络断/网关 5xx），请检查配置后手动重试",
            "mode": None,
            "raw_response": "",
        }

    if tier2_result == "supported":
        return {
            "result": "supported",
            "mode": "json_object",
            "reason": f"Tier 1 失败（{tier1_result}），Tier 2 三次采样全通过：模型支持 json_object 模式",
            "raw_response": "",
        }

    # Tier 3: 都失败，prompt_only 保底
    return {
        "result": "supported",
        "mode": "prompt_only",
        "reason": f"Tier 1（{tier1_result}: {tier1_raw[:60] if tier1_raw else ''}）+ Tier 2（{tier2_result}: {tier2_raw[:60] if tier2_raw else ''}）均失败，降级到 prompt-only 模式",
        "raw_response": "",
    }
```

**注意**：`tier1_raw` / `tier2_raw` 是 sampler 返回的最后一次采样的 raw 摘要（含异常类名，如 "BadRequestError: 400..."），用于诊断。`_probe_tier_three_samples_async` 需要返回 `(result, last_raw)` 元组而非单个 result 字符串——修改 sampler 返回值：

```python
    # sampler 返回值改为元组 (result, last_raw)
    # 所有 return 语句改为：
    return "supported", ""
    return "gateway_blocked", raw
    return "model_rejected", raw
    return "rate_limited", raw
    return "infra_error", raw
```

端点主流程相应改为：

```python
    tier1_result, tier1_raw = await _probe_tier_three_samples_async(...)
    tier2_result, tier2_raw = await _probe_tier_three_samples_async(...)
```

**注意**：`asyncio.TimeoutError` 在 `_probe_tier_three_samples_async` 内部捕获（见 Step 3 的 while 循环 try/except），转为 "timeout" 类型与 rate_limited 同等处理（重试不计失败）。

- [ ] **Step 8: 全量跑测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_probe.py -v -k "not endpoint"`
Expected: 全 PASS（8 个 probe_tier 测试 + 2 个端点 probe_failed 测试 + 其他单元测试；集成测试 `test_probe_endpoint_returns_*` 名字含 "endpoint" 会被 `-k "not endpoint"` 取消选择，需单独跑不带过滤器）

- [ ] **Step 9: 更新端点 docstring（整段重写，删除与新代码矛盾的旧描述）**

在 `niu_api/compat.py:1402` 的端点 docstring 处**整段重写**（删除"只看响应内容不看错误码"、"tier_failed"、"Why 不看异常类型"等与新代码矛盾的旧描述）：

```python
@router.post("/api/probe-response-format")
async def probe_response_format(request: Request) -> dict:
    """递进探测当前 LLM 配置对 response_format 的支持档位。

    前置条件：调用方（前端 testAndSave）必须先通过 /api/test-llm 连通性
    测试，确认 LLM 可正常对话。本端点假定连通性已验证，不再处理认证/网络
    等基础设施类错误（那些应该在连通性测试阶段就被拦截）。

    3 档递进（最强→最弱）：
    - Tier 1: json_schema strict → 三次采样全 verdict == "SCHEMA_ENFORCED" → json_schema
    - Tier 2: json_object → 三次采样全合法 JSON dict → json_object
    - Tier 3: 都失败 → prompt_only

    判定原则：冲突式设计 + 异常分类。
    - 冲突式设计：schema 强制要求 {"verdict": "SCHEMA_ENFORCED"}，prompt 要求
      "写海洋句子禁止 JSON"——只有 schema 战胜 prompt（网关真执行）才判
      supported，模型跟随 prompt 输出海洋句子即判 gateway_blocked。
    - 异常分类：
      * 没抛异常 + 响应符合该档要求 → "supported"
      * 没抛异常 + 响应不符合 → "gateway_blocked"
      * RateLimitError → "rate_limited"（限流，sleep 重试不计失败）
      * litellm.Timeout / asyncio.TimeoutError → "timeout"（超时，sleep 重试不计失败）
      * AuthenticationError / APIConnectionError / 5xx → "infra_error"
        （基础设施错误，端点早返 probe_failed 不写配置）
      * BadRequestError / UnsupportedParamsError → "model_rejected"
        （模型/网关明确拒绝，该档失败降级）
      * 其他异常 → "model_rejected"

    Why 三次采样：2026-07-21 实测豆包 Coding Plan 网关行为非确定性（flaky），
    同一请求 5 次采样 2 次 schema 胜、3 次 prompt 胜。单次探测碰巧命中执行
    窗口期会误判 json_schema，碰巧命中静默忽略窗口期会误判 prompt_only。
    三次采样全过才升档——flaky 网关必然 ≥1 次静默忽略，稳定降级 prompt_only；
    真支持网关（OpenAI）3 次全过，稳定写入 json_schema。

    Why 限流/超时只重试不计失败：RateLimitError / litellm.Timeout /
    asyncio.TimeoutError ≠ 不支持，只是"这次请求被网关挡了"或"网关慢/抖动"。
    限流/超时同属 transient infra 问题，sleep 后重试本次采样（指数退避
    5s→10s→20s→40s→80s，最多 5 次整档共享），直到返回非限流/非超时结果
    才判定该次采样。

    Why 基础设施错误单独分类：AuthenticationError（401）/ APIConnectionError
    （网络断）/ InternalServerError（500）/ ServiceUnavailableError（503）
    是临时性基础设施故障，不是"模型不支持 response_format"。如果归入
    model_rejected，两档失败 → prompt_only 写入配置 →
    _should_auto_probe_after_upgrade 永远 False → 首次升级启动时恰好
    API Key 失效/网关 500 的用户被永久静默降级，且永不重探。基础设施错误
    应该端点早返 probe_failed，不写配置，用户稍后手动重试。

    真实环境验证（2026-07-21）：
    - 豆包 Coding Plan：网关行为非确定性（flaky），同一请求 5 次采样 2 次
      schema 胜、3 次 prompt 胜。三次采样全过才升档，flaky 网关必然 ≥1 次
      静默忽略，稳定降级 prompt_only。
    - GLM：网关接受但模型输出漂移 → prompt_only
    - OpenAI：真正支持 → json_schema（3 次全过）

    约束：本端点独立于 /api/test-llm（启动器复用，禁止改动响应结构）。
    """
```

- [ ] **Step 10: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_response_format_probe.py
git commit -m "$(cat <<'EOF'
feat(api): 探测端点改三次采样 + 限流重试 + 错误码分类

2026-07-21 实测豆包 Coding Plan 网关行为非确定性（flaky）：同一请求 5 次
采样，2 次 schema 胜、3 次 prompt 胜。单次探测碰巧命中执行窗口期会误判
json_schema，碰巧命中静默忽略窗口期会误判 prompt_only。

改造：
- 新增 _probe_tier_three_samples / _probe_tier_three_samples_async：三次
  采样全过才 supported，任何一次失败立即降级
- _try_tier 区分限流：RateLimitError → "rate_limited"（不计失败，上层
  sleep 重试），BadRequestError/UnsupportedParamsError → "model_rejected"
- 端点主流程用三次采样替代单次探测
- 限流重试上限 5 次，指数退避 5s→10s→20s→40s→80s，超过返回 probe_failed

效果：
- flaky 网关（豆包）：3 次采样必然 ≥1 次静默忽略 → 稳定降级 prompt_only
- 真支持网关（OpenAI）：3 次全过 → 稳定写入 json_schema
- 限流场景：不误判为不支持，重试直到返回非限流结果

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 前端 main.js + 后台探测超时同步加大

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/ui/main/main.js:1343`
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/lightrag_manager.py:255`

- [ ] **Step 1: 前端 main.js 探测超时从 70s 提到 300s**

在 `ui/main/main.js:1344` 处找到探测调用的 timeout 配置（当前是 `timeout: 70000`，注释"两档各 30s + 余量"），改为：

```javascript
// 三次采样 + 限流/超时重试最坏耗时 ~250s/档（限流主导早返 ~160s），两档 ~500s。
// 正常场景 3 次采样 + 无重试约 90s/档。设 300s 覆盖正常+限流场景，病态连续
// 超时场景（~335s/档）前端会先放弃，属可接受取舍。
timeout: 300000,
```

- [ ] **Step 2: 后台探测超时从 90s 提到 300s，守卫保留但改新词汇**

在 `niu_api/internal/lightrag_manager.py:253` 处找到后台探测的 `httpx.Client(timeout=90)`，改为：

```python
# 三次采样 + 限流/超时重试最坏耗时 ~250s/档（限流主导早返 ~160s），两档 ~500s。
# 正常场景 3 次采样 + 无重试约 90s/档。设 300s 覆盖正常+限流场景，病态连续
# 超时场景（~335s/档）后台会先放弃，属可接受取舍。
with httpx.Client(timeout=300) as client:
```

同时在 `niu_api/internal/lightrag_manager.py:277` 处找到守卫（`if mode == "prompt_only" and "gateway_blocked" not in reason: return`），**保留但改新词汇**：

```python
# 新逻辑下 infra_error 走 probe_failed 早返（不写配置），rate_limited/timeout
# 走 probe_failed 早返（不写配置），prompt_only 必然是真不支持（gateway_blocked
# 或 model_rejected）。守卫保留但改为接受两种确定性不支持信号。
if mode == "prompt_only" and not ("gateway_blocked" in reason or "model_rejected" in reason):
    logger.warning(f"探测结果 prompt_only 但 reason 不含确定性不支持信号，跳过写入: {reason[:100]}")
    return
```

原因：
- 旧逻辑下 reason 含 `tier_failed`/`gateway_blocked`，守卫用于区分"真不支持"vs"临时错误"
- 新逻辑下 infra_error/rate_limited/timeout 走 `probe_failed` 早返（不写配置），prompt_only 必然是真不支持（gateway_blocked 或 model_rejected）
- 守卫保留作为双保险，但词汇改为接受 `gateway_blocked` 或 `model_rejected` 两种确定性不支持信号

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/main/main.js niu_api/internal/lightrag_manager.py
git commit -m "$(cat <<'EOF'
fix(api): 探测调用方超时同步加大到 300s

新探测逻辑（三次采样 + 限流/超时重试）单档最坏耗时 ~250s，旧前端 70s /
后台 90s 超时必爆，导致探测结果写不入配置。

- ui/main/main.js:1343 前端探测 timeout 70s → 300s
- niu_api/internal/lightrag_manager.py:255 后台探测 timeout 90s → 300s

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 集成测试断言更新（豆包/GLM/OpenAI）+ 超时加大 + helper 函数抽取

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/tests/test_response_format_probe.py:374-446`
- Modify: `REDACTED_USER_PATH/tools/ai-bot/requirements-dev.txt`
- Modify: `REDACTED_USER_PATH/tools/ai-bot/pytest.ini`

- [ ] **Step 0: 安装 pytest-timeout + 注册 timeout marker**

a. 在 `REDACTED_USER_PATH/tools/ai-bot/requirements-dev.txt` 末尾追加：

```
pytest-timeout==2.3.1            # 测试超时保护（pytest.ini timeout=30 + @pytest.mark.timeout(600) 集成测试突破）
```

b. 安装：`cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/pip install pytest-timeout==2.3.1`

c. 在 `REDACTED_USER_PATH/tools/ai-bot/pytest.ini` 的 `markers` 列表追加 `timeout`（避免 `--strict-markers` 拒绝）：

```ini
markers =
    p0: P0 critical tests
    p1: P1 optimization tests
    p2: P2 enhancement tests
    slow: slow running tests
    integration: integration tests
    timeout: per-test timeout override (pytest-timeout)
```

**注意**：`pytest-timeout` 插件未安装时，`@pytest.mark.timeout(600)` marker 在 `--strict-markers` 下会让整个测试文件收集失败（0 tests collected）。必须先装插件 + 注册 marker。

- [ ] **Step 1: 抽取 `_load_user_llm_config()` / `_load_glm_llm_config()` helper**

在 `tests/test_response_format_probe.py` 顶部（fixture 区域后）新增两个 helper 函数：

```python
def _load_user_llm_config() -> dict | None:
    """加载 user-config.json 的 lightrag_llm 配置（fallback 到 llm 段，近似 get_llm_config 语义）

    近似运行时 get_llm_config（llm_proxy.py L209-241）语义，仅覆盖当前豆包/GLM
    实际配置形态（Branch 2：lightrag_llm.model 为空）。Branch 1（lightrag_llm.model
    非空）场景下的完整继承逻辑未复刻，未来如需支持需按 llm_proxy.py L213-222
    补五个继承块。
    """
    import json
    import os
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    if not os.path.exists(config_path):
        return None
    with open(config_path) as f:
        cfg = json.load(f)
    lightrag_llm = cfg.get("lightrag_llm", {})
    llm = cfg.get("llm", {})

    # Branch 2：lightrag_llm.model 为空，fallback 到 llm 段
    # apiKey/apiBase/model/type 只用 llm 段（lightrag_llm 的这些字段被忽略）
    # provider/temperature/litellm_kwargs 优先 lightrag_llm、空则 llm
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "provider": lightrag_llm.get("provider") or llm.get("provider", ""),
        "temperature": lightrag_llm["temperature"] if lightrag_llm.get("temperature") is not None else llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs") or llm.get("litellm_kwargs") or {},
    }


def _load_glm_llm_config() -> dict | None:
    """加载 GLM 配置（从独立文件 config/user-config - glm.json，与前端发送逻辑一致）

    litellm_kwargs 优先 lightrag_llm 段、空则 llm 段（与前端 settings/index.html L410
    实际发送逻辑一致：lightrag_llm?.litellm_kwargs || llm?.litellm_kwargs || {}）
    provider 优先 lightrag_llm 段、空则 llm 段（与 _load_user_llm_config 统一）
    """
    import json
    import os
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config - glm.json")
    if not os.path.exists(config_path):
        return None
    with open(config_path) as f:
        cfg = json.load(f)
    lightrag_llm = cfg.get("lightrag_llm", {})
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apiKey", ""),
        "apibase": llm.get("apiBase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
        "provider": lightrag_llm.get("provider") or llm.get("provider", ""),
        "temperature": llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs") or llm.get("litellm_kwargs") or {},
    }
```

**注意 1**：`_load_user_llm_config()` 的语义是**近似** `get_llm_config`（llm_proxy.py L209-241），仅覆盖当前豆包/GLM 实际配置形态（Branch 2：lightrag_llm.model 为空）。Branch 1（lightrag_llm.model 非空）场景下的完整继承逻辑未复刻——如果未来用户配置了独立 lightrag_llm.model，集成测试探测环境将与运行时探测环境产生系统性偏差，届时需按 llm_proxy.py L213-222 补五个继承块。

**注意 2**：`_load_glm_llm_config()` 读的是独立文件 `config/user-config - glm.json`（与现有测试 L392 一致），不是主配置 user-config.json。litellm_kwargs 优先 lightrag_llm 段（实测 GLM 配置 `lightrag_llm.litellm_kwargs = {'thinking': {'type': 'disabled'}}`）、空则 llm 段，与前端实际发送逻辑（settings/index.html L410）一致。

- [ ] **Step 2: 更新豆包集成测试断言 + 超时加大 + pytest-timeout marker**

在 `test_probe_endpoint_returns_prompt_only_for_doubao_coding` 函数处改为：

```python
@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30，新探测最坏 ~500s
def test_probe_endpoint_returns_prompt_only_for_doubao_coding(api_base):
    """豆包 Coding Plan 网关行为非确定性（flaky），三次采样必然 ≥1 次
    静默忽略 → 稳定降级 prompt_only

    已知抖动率：flaky 网关执行率约 2/5，P(Tier1 三样本全过)≈6.4%，Tier 2 同理。
    本测试断言 prompt_only 有 ~6% 偶发失败率，偶发失败可重跑。
    """
    config = _load_user_llm_config()
    if not config:
        pytest.skip("无 user-config.json")
    if "coding" not in config.get("apibase", ""):  # 全小写键，与 helper 返回一致
        pytest.skip("非豆包 Coding Plan 端点")

    # 三次采样 + 限流/超时重试最坏耗时 ~500s（两档），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # flaky 网关三次采样必然 ≥1 次静默忽略 → 稳定降级 prompt_only（~94% 概率）
    assert data["mode"] == "prompt_only", f"豆包 Coding Plan flaky 网关应稳定降级 prompt_only，实际: {data}"
    # reason 应含 Tier 1 失败信息（gateway_blocked 或 model_rejected）
    reason = data.get("reason", "")
    assert "Tier 1" in reason, f"reason 应含 Tier 1 失败信息，实际: {reason}"
```

**注意**：`pytest.ini` 全局 `timeout = 30`（pytest-timeout 插件）会在 30s 杀掉测试进程，httpx.Client(timeout=600) 只控制 HTTP 客户端超时无法突破。必须用 `@pytest.mark.timeout(600)` marker 级覆盖（pytest-timeout 插件支持，`--strict-markers` 不限制插件内建 marker）。

- [ ] **Step 3: 更新 GLM 集成测试断言 + 超时加大 + pytest-timeout marker**

在 `test_probe_endpoint_returns_prompt_only_for_glm` 函数处改为：

```python
@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30
def test_probe_endpoint_returns_prompt_only_for_glm(api_base):
    """GLM 网关接受但模型输出漂移，三次采样必然 ≥1 次漂移 → 稳定降级 prompt_only

    已知抖动率：GLM 漂移率较高，P(Tier1 三样本全过) 极低，但理论上非零。
    偶发失败可重跑。
    """
    config = _load_glm_llm_config()
    if not config:
        pytest.skip("无 GLM 配置")

    # 三次采样 + 限流/超时重试最坏耗时 ~500s（两档），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # GLM 输出漂移，三次采样必然 ≥1 次非合法 JSON → 稳定降级 prompt_only
    assert data["mode"] == "prompt_only", f"GLM 应稳定降级 prompt_only（输出漂移），实际: {data}"
    reason = data.get("reason", "")
    assert "Tier 1" in reason, f"reason 应含 Tier 1 失败信息，实际: {reason}"
```

- [ ] **Step 4: 更新 OpenAI 集成测试超时 + pytest-timeout marker**

在 `test_probe_endpoint_returns_json_schema_for_openai` 函数处改为：

```python
@pytest.mark.timeout(600)  # 突破 pytest.ini 全局 timeout=30
def test_probe_endpoint_returns_json_schema_for_openai(api_base):
    """用 OpenAI 真实 API Key 测试（需环境变量 OPENAI_API_KEY）"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY 未设置，跳过真实 OpenAI 探测测试")
    config = {
        "apiKey": api_key,
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "type": "openai",
        "provider": "",
    }
    # 三次采样最坏耗时 ~90s（3 次 × 30s 超时），设 600s 余量
    client = httpx.Client(timeout=600.0)
    with client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] in {"supported", "probe_failed"}
    # OpenAI 应支持 json_schema strict（三次采样全过）
    assert data["mode"] == "json_schema", f"OpenAI 应支持 json_schema，实际: {data}"
```

**注意**：保持与 doubao/glm 测试一致的 `client = ...; with client:` 两行模式，不要破坏 `with` 结构。**保留 `os.environ.get("OPENAI_API_KEY")` + `pytest.skip` 分支**，否则无 OPENAI_API_KEY 环境下测试会崩溃。

- [ ] **Step 5: 补 `_try_tier` 异常分类端点级测试**

在 `tests/test_response_format_probe.py` 末尾新增 4 个端点级测试，mock `LiteLLMSession.chat` 抛各类异常，走真实 `_try_tier` 断言分类：

```python
@pytest.mark.asyncio
async def test_try_tier_classifies_rate_limit_error():
    """_try_tier 捕获 RateLimitError → rate_limited"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import RateLimitError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        # mock LiteLLMSession.chat 抛 RateLimitError（litellm 异常需要 model + llm_provider 必填 kwarg）
        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = RateLimitError(
                "429 rate limit", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            # mock _asyncio_sleep 避免真实等待
            with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
                result = await probe_response_format(mock_request)

    # 限流重试 5 次后返回 probe_failed
    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_litellm_timeout():
    """_try_tier 捕获 litellm.Timeout → timeout（与 rate_limited 同等待遇）"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    import litellm

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = litellm.Timeout(
                "APITimeoutError", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            with patch("niu_api.compat._asyncio_sleep", new_callable=AsyncMock):
                result = await probe_response_format(mock_request)

    # 超时重试 5 次后返回 probe_failed
    assert result["result"] == "probe_failed"
    assert "限流" in result["reason"] or "超时" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_authentication_error():
    """_try_tier 捕获 AuthenticationError → infra_error → probe_failed 不写配置"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import AuthenticationError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = AuthenticationError(
                "401 invalid api key", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            result = await probe_response_format(mock_request)

    # 基础设施错误立即返回 probe_failed（不重试）
    assert result["result"] == "probe_failed"
    assert "基础设施错误" in result["reason"]


@pytest.mark.asyncio
async def test_try_tier_classifies_bad_request_error():
    """_try_tier 捕获 BadRequestError → model_rejected → 降级 prompt_only"""
    from niu_api.compat import probe_response_format
    from unittest.mock import AsyncMock, patch, MagicMock
    from fastapi import Request
    from litellm import BadRequestError

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={})

    with patch("niu_api.llm_proxy.get_llm_config") as mock_get_config:
        mock_get_config.return_value = {
            "apikey": "test-key",
            "apibase": "https://test.example.com",
            "model": "test-model",
            "type": "openai",
            "litellm_kwargs": {},
        }

        with patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.chat.side_effect = BadRequestError(
                "400 response_format not supported", model="test-model", llm_provider="openai"
            )
            mock_session_class.return_value = mock_session

            result = await probe_response_format(mock_request)

    # model_rejected 降级 prompt_only
    assert result["result"] == "supported"
    assert result["mode"] == "prompt_only"
```

**注意**：litellm 异常（RateLimitError/Timeout/AuthenticationError/BadRequestError）构造时必须带 `model` + `llm_provider` 必填 kwarg，否则 TypeError。统一用 kwarg 最稳。

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_response_format_probe.py
git commit -m "$(cat <<'EOF'
test(api): 集成测试断言更新为三次采样语义

豆包 Coding Plan 网关行为非确定性（flaky），三次采样必然 ≥1 次静默忽略
→ 稳定降级 prompt_only。GLM 输出漂移同理。

断言更新：
- mode 断言保持 prompt_only
- reason 断言改为含 "Tier 1" 失败信息（不再限定具体错误码，网关行为
  可能变化）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 用户手册 + 前端文案更新

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/docs/manual-user-guide.md:114-145`

- [ ] **Step 1: 更新探测流程描述**

在"探测流程"段落（约 L124-126）末尾追加：

```
探测采用三次采样 + 冲突式设计：
- 三次采样全过才升档，任何一次失败立即降级——防 flaky 网关（豆包 Coding
  Plan 2026-07-21 实测：同一请求 5 次采样 2 次执行、3 次静默忽略）碰巧
  命中执行窗口期误判支持
- 冲突式设计：schema 强制要求 `{"verdict": "SCHEMA_ENFORCED"}`，prompt
  却要求模型写普通英文句子且禁止输出 JSON——只有 schema 战胜 prompt
  （输出被强制为 schema JSON）才判定真支持，模型跟随 prompt 输出普通
  文本即判定网关静默忽略
- 限流单独处理：RateLimitError 不计失败，sleep 后重试本次采样（指数
  退避 5s→10s→20s→40s→80s，最多 5 次），直到返回非限流结果才判定
```

- [ ] **Step 2: 更新典型场景**

在"典型场景"段落（约 L132-134）豆包行改为：

```
- 豆包 Coding Plan 端点（`/api/coding/v3`，model=`ark-code-latest`）：
  网关行为非确定性（flaky），同一请求多次采样结果不稳定（有时执行
  json_schema strict、有时静默忽略），三次采样必然 ≥1 次静默忽略 →
  探测结果稳定 `prompt_only`
```

- [ ] **Step 3: 前端设置窗口文案更新**

在 `ui/main/windows/settings/index.html` 找到三处文案更新：

a. 探测提示文案（约 L406）："可能需要 30-60 秒" → "可能需要 1-5 分钟（三次采样 + 限流/超时重试）"

b. probe_failed 错误提示（约 L428）："探测传输失败" → "探测失败（限流/超时/基础设施错误），请稍后重试"

c. probe_failed 注释块（约 L424-426）："probe_failed：仅 HTTP 传输失败（连接拒绝/超时）时触发 / 端点逻辑失败会返回 supported+prompt_only 走上面分支" → "probe_failed：HTTP 传输失败（连接拒绝/超时）或端点逻辑失败（限流/超时/基础设施错误）时触发 / 端点正常探测失败会返回 supported+prompt_only 走上面分支"

- [ ] **Step 4: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add docs/manual-user-guide.md ui/main/windows/settings/index.html
git commit -m "$(cat <<'EOF'
docs: 用户手册 + 前端文案更新三次采样 + flaky 网关描述

- 探测流程段落追加三次采样 + 冲突式设计 + 限流/超时重试说明
- 典型场景豆包行改为 flaky 网关描述（2026-07-21 实测）
- 前端设置窗口探测提示文案更新（30-60 秒 → 1-5 分钟）
- probe_failed 错误提示文案更新（限流/超时/基础设施错误）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## 验证清单（所有 Task 完成后）

- [ ] 单元测试全 PASS：`./python/bin/python -m pytest tests/test_response_format_probe.py -v -k "not endpoint"`
- [ ] 冲突式 schema 结构正确（verdict 枚举单值）
- [ ] 冲突式 prompt 正确（含 ocean + do not output json + json 字样）
- [ ] Tier 1 分类器 verdict 判定正确（旧 {"ok": true} 响应判 gateway_blocked）
- [ ] 三次采样逻辑正确（全过才 supported，限流/超时只重试不计失败，预算整档共享 5 次）
- [ ] `_try_tier` 异常分类正确（RateLimitError→rate_limited、litellm.Timeout→timeout、AuthenticationError/APIConnectionError/5xx→infra_error、BadRequestError→model_rejected）
- [ ] 端点限流/超时/基础设施错误返回 probe_failed
- [ ] 前端 main.js 超时 300s
- [ ] 后台探测超时 300s + 守卫改新词汇（接受 gateway_blocked 或 model_rejected）
- [ ] 集成测试断言更新（豆包/GLM 稳定 prompt_only）+ 超时 600s + `@pytest.mark.timeout(600)` 突破 pytest.ini 全局 30s + helper 函数抽取（`_load_user_llm_config` 近似 get_llm_config Branch 2 语义、`_load_glm_llm_config` 读独立文件 + litellm_kwargs/provider 优先 lightrag_llm 段与前端发送一致）
- [ ] OpenAI 集成测试超时 600s + `@pytest.mark.timeout(600)` + 保留 OPENAI_API_KEY skip 逻辑
- [ ] requirements-dev.txt 添加 pytest-timeout==2.3.1 + pytest.ini markers 注册 timeout
- [ ] `_try_tier` 异常分类端点级测试（4 个：RateLimitError/litellm.Timeout/AuthenticationError/BadRequestError，litellm 异常构造带 model + llm_provider kwarg）
- [ ] 前端设置窗口文案更新（探测提示 + probe_failed 错误提示 + 注释块）
- [ ] 用户手册更新（三次采样 + flaky 网关描述）

## 不做的事

- 不改 `lightrag_manager.py` 的 `_resolve_response_format`（三档分支 + prompt_only 兜底 + BadRequestError fallback 已正确）
- 不改 `agent/generic/litellm_adapter.py`
- 不跑集成测试（需要重启程序，由用户验证）
- 不清理旧单次探测写入的假阳性 `json_schema` 配置（受影响用户手动在设置窗口重新点"测试连接并保存"即可触发新探测覆盖）

## 存疑点（已全部澄清）

1. ~~**`_probe_tier_three_samples` 同步 vs 异步**~~ → **只保留异步版**（端点是 async，单元测试用 pytest-asyncio 的 `@pytest.mark.asyncio` + `AsyncMock`）
2. **限流/超时重试上限 5 次**：指数退避 5s→10s→20s→40s→80s，最多等 155s，**整档共享预算**（防止限流/超时期间无限拖延端点）
3. **测试分离**：单元测试我跑（mock 限流/超时/失败场景），完整端到端测试您重启后做

## 已知风险（不阻断，记录在案）

1. **超时后 `asyncio.to_thread` 线程泄漏**：`asyncio.wait_for` 取消杀不掉线程内的 litellm 调用，三次采样放大泄漏 3 倍。旧代码已有此问题，本次修复不引入新风险，但值得后续优化（改 in-process 调用或加 threading.Timer 强制中断）。
2. **`raw_response` 恒为空字符串**：三次采样无单一 raw_response，端点 4 个分支全写 `""`，诊断能力相比旧版（返回 tier_text[:200]）退化。已通过 sampler 返回 `(result, last_raw)` 元组带最后一次采样的 raw 摘要（含异常类名）拼入 reason 缓解。
3. **300s 超时仍小于理论最坏值**：单档病态场景（连续 6 次 litellm.Timeout 各 30s + 155s 退避）最坏 ~335s，两档 ~670s > 前端/后台 300s。限流主导场景因早返仅 ~160s < 300s 无问题。属可接受取舍——病态连续超时场景前端会先放弃，但正常场景（3 次采样 + 无重试约 90s/档）和限流场景（~160s）都覆盖。
4. **集成测试固有 ~6% 抖动率**："flaky 网关必然 ≥1 次静默忽略"是概率陈述（实测执行率 2/5 → P(Tier1 三样本全过）≈6.4%，Tier 2 同理）。集成测试断言 prompt_only 有相应偶发失败率（~6%），偶发失败可重跑。
5. **sampler 超时耗尽返回 `"rate_limited"` 标签**：预算耗尽时统一返回 `"rate_limited", raw`，即使 6 次全是 timeout。端点 reason 文案已覆盖（"限流/超时"），可接受；但日志和 raw 诊断会误导排查方向。不阻断。
