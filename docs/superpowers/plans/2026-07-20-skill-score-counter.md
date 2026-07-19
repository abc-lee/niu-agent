# Skill 计数器衰减注入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **⚠️ 项目记忆约束（重要）**：禁止使用 git worktree（[No Worktree] 项目记忆，曾导致 19 个残留分支和版本混乱）。若使用 subagent-driven-development，子 Agent 必须在主工作区直接修改，**不得**创建 worktree 隔离环境。

**Goal:** 在现有 Skill 动态注入逻辑上叠加"两阶段 Top_K + 衰减计数器"机制，让被向量库命中的 skill 不会因下一轮检索没命中就被立即丢弃，而是按 7→6→5→4→3→2 的轨迹缓慢淘汰出 prompt。

**Architecture:** 现有逻辑每轮跑一次 LightRAG top_k=10 检索，命中的 skill 直接拼到 system prompt。本方案在进入 prompt 拼装前插一层"计数器层"——先用计数器对候选集合做加分/衰减，再用计数器 ≥3 分的 skill 集合做第二阶段排序，最后注入 prompt。计数器挂在 NiuRunner 实例上（纯内存，重启清零），key 用 `entity_name`（即现有 `seen_names` 的 key），与现有去重逻辑完全兼容。

**Tech Stack:** Python 3.11+，pytest，agent/runner.py，niu_api/internal/lightrag_adapter.py

---

## 现状速览（implementer 必读）

**现有注入路径**（`agent/runner.py:1941-2088` `_inject_dynamic_resources`）：
1. 跑 LightRAG `search_multi_lightrag(top_k=10)` 得到 `lightrag_results = {"skill": [...], "knowledge": [...], "other": [...]}`
2. 调 `_format_lightrag_entities_for_prompt(lightrag_results["skill"], "相关技能", seen_names)` 拼成 prompt 文本
3. `_format_lightrag_entities_for_prompt` 内部用 `entity_name`（即 `display_name`）做 `seen_names` 去重，输出 `路径: ~/.niu/skills/{display_name}.md`

**现有回调链**：
- `agent_runner_loop` 每轮末调 `on_turn_end` 回调（`runner.py:2228` 注册）
- `_on_turn_end`（`runner.py:797-820`）调 `_extract_context_from_messages` 提取最近 3 条消息 + 工具名，再调 `_inject_dynamic_resources(context)` 重跑向量检索

**关键文件**：
- `agent/runner.py:797-820` — `_on_turn_end` 回调
- `agent/runner.py:706-748` — `_extract_context_from_messages` 拼装检索字段
- `agent/runner.py:1941-2088` — `_inject_dynamic_resources` 注入主逻辑
- `agent/runner.py:1862-1907` — `_format_lightrag_entities_for_prompt` prompt 拼装

**用户期望的算法**（每轮执行顺序）：
1. 第一阶段：向量库检索得候选 skill 集合（现有逻辑，不动）
2. 未命中衰减：所有计数器 > 0 且不在候选集合里的 skill，各 -1 分
3. 命中加分（已熟悉）：候选集合里计数器 ≥7 且 <10 的，+1 分（封顶 10）—— **7 分本身走这条分支到 8**
4. 命中置位（新命中或低分）：候选集合里计数器 <7 的，直接置为 7 —— **7 分不走这条分支**
5. **entity dict 缓存更新**：候选集合里的 skill 用本轮检索到的 entity dict 更新 `self._skill_entity_cache`（跨轮缓存，下一轮没检索到时仍可从这里取 entity dict 注入 prompt）
6. 清理 0 分项：counter 字典里所有 ≤0 分的 skill 删除，同时从 `_skill_entity_cache` 删除对应项，防止两个 dict 无界增长
7. 第二阶段：所有计数器 ≥3 的 skill 按分数倒序取前 N 个，从 `_skill_entity_cache` 取对应 entity dict 注入 prompt

> **算法边界规则**（避免歧义）：分数 7 是"已熟悉"与"低分"的分界点——`<7` 走置位分支（直接置 7），`≥7 且 <10` 走加分分支（+1 封顶 10）。具体在代码里用 `if current < _SKILL_SCORE_FIRST_HIT:` 判定，else 走加分。

> **entity dict 缓存设计（关键）**：counter 只存 `name→score`，但注入 prompt 需要 entity dict（含 description 等字段）。如果下一轮向量检索没命中该 skill，从 lightrag_results 取不到 entity dict，就无法注入——这就违背了"缓慢淘汰"目标。所以必须用 `_skill_entity_cache: dict[str, dict]` 跨轮缓存最近一次检索到的 entity dict。命中时刷新缓存（用最新描述），未命中时仍从缓存取旧 dict 注入（直到 counter 衰减到 ≤0 被清理）。

**衰减轨迹示例**（每一步代表一轮）：
- 首次命中：0 → 7（第 4 步置位），entity dict 写入 cache
- 连续命中：7 → 8 → 9 → 10 → 10 → 10（第 3 步加分封顶），每次命中刷新 cache
- 不再命中：10 → 9 → 8 → 7 → 6 → 5 → 4 → 3（第 7 轮后仍进 prompt，从 cache 取旧 dict 注入）→ 2（第 8 轮后被淘汰出 prompt，counter 仍留）→ 1（第 9 轮）→ 0（第 10 轮，Step 6 清理时从 counter 和 cache 同时移除）

**设计决策**：
- 计数器挂在 NiuRunner 实例上（`self._skill_score_counter: dict[str, int]`），纯内存，重启清零
- **entity dict 缓存**挂在 NiuRunner 实例上（`self._skill_entity_cache: dict[str, dict]`），与 counter 同步维护，跨轮缓存最近一次检索到的 entity dict
- 计数器 key 用 `entity_name`，与现有 `seen_names` 完全一致
- 第二阶段 Top_K 数量 N = 5（保持现有注入 prompt 的 skill 数量上限，避免 prompt 被占太多 token）
- 第二阶段注入仍走 `_format_lightrag_entities_for_prompt`，但传给它的是"按计数器排序后从 cache 取出的 entity dict 列表"而非"原始 lightrag_results['skill']"
- 知识（knowledge）注入逻辑不动，只对 skill 做计数器排序
- 脑区检索（region_results）命中的 skill 也参与计数器加分（与全局检索并列）
- **多脑区同时激活**时所有命中 skill 进同一 candidate_names 集合并加分（设计权衡：脑区已激活即代表相关，统一加分符合"脑区激活=相关"的语义）
- **计数器清理**：每轮 `_update_skill_counter` 末尾删除所有 ≤0 分的项，同时从 `_skill_entity_cache` 删除对应项，防止两个 dict 无界增长

**合规前置条件**（implementer 必须先做）：
1. **临时备份**（CLAUDE.md 铁律第 3 条）：修改 `agent/runner.py` 前先 `git add -A && git commit -m "backup: before skill counter integration"` 备份当前状态
2. **gitnexus impact 分析**（CLAUDE.md 铼律第 4 条）：实施前跑 `gitnexus_impact({target: "_inject_dynamic_resources", direction: "upstream"})` + `gitnexus_impact({target: "NiuRunner.__init__", direction: "upstream"})`，向用户报告 blast radius（预期：主 Agent 每轮对话、所有子 Agent if 共用 NiuRunner，HIGH 风险，需用户确认）
3. **不使用 git worktree**（项目记忆 [No Worktree]）：subagent-driven-development 若默认创建 worktree，必须显式跳过；子 Agent 在主工作区直接修改
4. **真实数据测试**（CLAUDE.md 铁律第 5 条）：Task 2 集成测试用 mock 是允许的（仅测计数器与注入路径协作逻辑），但 Task 3 必须用真实程序 + 真实 LightRAG 验证；Task 3 真实验证必须断言"真实 entity dict 字段格式（`entity_name`/`entity_type`/`description`）与 Task 2 mock 一致"

---

## File Structure

**Modify only**（不创建新文件）：
- `agent/runner.py` — 新增计数器管理方法 + 修改 `_inject_dynamic_resources` 注入路径
- `tests/test_skill_score_counter.py` — 新增测试文件（独立模块，专注计数器逻辑）

---

## Task 0: 实施前置 — gitnexus impact 分析 + 临时备份

**Files:** 无（只读分析 + git 操作）

**设计**：CLAUDE.md 铁律第 3、4 条要求修改前必须先做 gitnexus impact 分析 + 临时提交备份。本任务是 Task 1-3 的前置，不通过不可进入 Task 1。

- [ ] **Step 1: gitnexus impact 分析（由主对话通过 MCP 工具完成）**

⚠️ 本步骤不通过 shell 调用 CLI，由主对话（Claude）通过 GitNexus MCP 工具调用。implementer 不能跑这条命令，需要主对话代为执行：

主对话调用以下 MCP 工具（不是 shell 命令）：
```
gitnexus_impact({target: "_inject_dynamic_resources", direction: "upstream"})
gitnexus_impact({target: "NiuRunner.__init__", direction: "upstream"})
```

implementer 在 Task 0 阶段应：
1. 在主对话里请求"帮我跑 gitnexus impact 分析 _inject_dynamic_resources 和 NiuRunner.__init__"
2. 主对话调用上述 MCP 工具，返回 blast radius 报告
3. implementer 接收报告后向用户报告 HIGH 风险并获确认

风险等级预期：HIGH（每轮对话都受影响），需向用户报告并获确认。

- [ ] **Step 2: 临时备份**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before skill counter integration (baseline)"
```

如果当前工作区干净（git status 无变化），跳过本步（CLAUDE.md 铁律第 3 条不强制空提交）。

- [ ] **Step 3: 报告 blast radius 给用户**

把 Step 1 输出的 impact 分析结果整理成简短报告（直接调用方、影响进程、风险等级），等用户确认后才能进入 Task 1。

---

## Task 1: 计数器核心逻辑 — 纯函数 + 单元测试

**Files:**
- Modify: `agent/runner.py`（在 NiuRunner 类内新增方法）
- Create: `tests/test_skill_score_counter.py`

**设计**：把"按算法更新计数器"做成 NiuRunner 的静态方法（或类方法），方便单测，且不依赖 LightRAG。

### Task 1 Step 1: 写失败测试 — 计数器更新算法

- [ ] **Step 1: 写失败测试**

```python
# tests/test_skill_score_counter.py
"""Skill 计数器衰减注入单元测试。

算法（每轮执行顺序）：
1. 第一阶段：向量库检索得候选 skill 集合（外部已得）
2. 未命中衰减：所有计数器 > 0 且不在候选集合里的 skill，各 -1 分
3. 命中加分（已熟悉）：候选集合里计数器 ≥7 且 <10 的，+1 分（封顶 10，7 分走这条分支到 8）
4. 命中置位（新命中或低分）：候选集合里计数器 <7 的，直接置为 7
5. entity dict 缓存更新：候选集合里的 skill 用本轮 entity dict 覆盖 cache
6. 清理 0 分项：counter 和 entity_cache 同步删除 ≤0 分项
7. 第二阶段：所有计数器 ≥3 的 skill 按分数倒序取前 N 个注入 prompt
"""
from agent.runner import NiuRunner


def _make_entity(name: str, desc: str = "test") -> dict:
    """构造测试用 entity dict"""
    return {"entity_name": name, "entity_type": "skill", "description": desc}


def test_first_hit_sets_score_to_7():
    """首次命中：0 → 7（第 4 步置位）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 7


def test_second_hit_increments_to_8():
    """连续第二次命中：7 → 8（第 3 步加分）"""
    counter: dict[str, int] = {"skillA": 7}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 8


def test_consecutive_hits_cap_at_10():
    """连续命中封顶 10：10 → 10（第 3 步加分但不超 10）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 10


def test_low_score_hit_resets_to_7():
    """计数器低于 7 的命中直接置 7：5 → 7（第 4 步置位，不是 +2）"""
    counter: dict[str, int] = {"skillA": 5}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 7


def test_non_hit_decrements_when_score_above_zero():
    """未命中且分数 > 0：-1（第 2 步衰减）"""
    counter: dict[str, int] = {"skillA": 8}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, {})  # 没命中
    assert counter["skillA"] == 7


def test_zero_score_cleaned_when_not_hit():
    """未命中且分数已为 0：被 Step 6 清理（不衰减但被清理出 dict）"""
    counter: dict[str, int] = {"skillA": 0}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, {})
    # 0 分不衰减，但 Step 6 清理 ≤0 分项
    assert "skillA" not in counter
    assert "skillA" not in cache  # cache 同步清理


def test_decay_trajectory_10_to_2_six_rounds():
    """完整衰减轨迹：连续 6 轮不命中，10 → 9 → 8 → 7 → 6 → 5 → 4

    第 6 轮后还剩 4 分（仍 ≥3 进 prompt），第 7 轮才降到 3，第 8 轮才被淘汰出 prompt。
    """
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    expected = [9, 8, 7, 6, 5, 4]
    for expected_score in expected:
        NiuRunner._update_skill_counter(counter, cache, {})
        assert counter["skillA"] == expected_score, f"轮次期望 {expected_score}，实际 {counter['skillA']}"


def test_decay_drops_below_3_after_8_rounds():
    """10 分连续不命中 8 轮后降到 2（被淘汰出 prompt 门槛）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    for _ in range(8):
        NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 2


def test_mixed_hit_and_non_hit():
    """混合场景：skillA 命中加分，skillB 未命中衰减"""
    counter: dict[str, int] = {"skillA": 8, "skillB": 5}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA"), "skillB": _make_entity("skillB")}
    candidates = {"skillA": _make_entity("skillA")}  # 只 skillA 命中
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 9  # 8 + 1
    assert counter["skillB"] == 4  # 5 - 1


def test_new_skill_in_candidate_sets_to_7():
    """候选集合含新 skill（counter 里没有）：直接置 7"""
    counter: dict[str, int] = {"skillA": 8}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    candidates = {"skillA": _make_entity("skillA"), "skillB": _make_entity("skillB")}  # skillB 新
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 9
    assert counter["skillB"] == 7


def test_candidate_set_empty_all_decay():
    """候选集合空，所有 skill 衰减 1 分（0 分项被清理）"""
    counter: dict[str, int] = {"skillA": 5, "skillB": 0, "skillC": 10}
    cache: dict[str, dict] = {
        "skillA": _make_entity("skillA"),
        "skillB": _make_entity("skillB"),
        "skillC": _make_entity("skillC"),
    }
    NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 4  # 5 - 1
    assert "skillB" not in counter  # 0 分被清理（不衰减但被 Step 6 清理）
    assert "skillB" not in cache  # cache 同步清理
    assert counter["skillC"] == 9  # 10 - 1


def test_counter_does_not_grow_unbounded():
    """连续命中不会无限增长，封顶 10"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    for _ in range(20):
        NiuRunner._update_skill_counter(counter, cache, candidates)
    assert counter["skillA"] == 10


def test_select_top_skills_filters_below_3():
    """第二阶段筛选：只保留 ≥3 分的 skill"""
    counter: dict[str, int] = {"skillA": 10, "skillB": 5, "skillC": 2, "skillD": 3}
    result = NiuRunner._select_top_skills(counter, top_n=10)
    result_names = [name for name, _ in result]
    assert "skillA" in result_names
    assert "skillB" in result_names
    assert "skillD" in result_names
    assert "skillC" not in result_names  # 2 分被筛掉


def test_select_top_skills_sorted_descending():
    """第二阶段排序：分数倒序"""
    counter: dict[str, int] = {"low": 3, "mid": 5, "high": 10}
    result = NiuRunner._select_top_skills(counter, top_n=10)
    names = [name for name, _ in result]
    assert names == ["high", "mid", "low"]


def test_select_top_skills_limits_to_n():
    """第二阶段 Top_N：分数相同时按 name 字典序兜底"""
    counter: dict[str, int] = {"a": 5, "b": 5, "c": 5, "d": 5, "d2": 5, "e": 5}
    result = NiuRunner._select_top_skills(counter, top_n=3)
    assert len(result) == 3
    names = [name for name, _ in result]
    assert names == ["a", "b", "c"]


def test_select_top_skills_empty_counter():
    """空计数器返回空列表"""
    result = NiuRunner._select_top_skills({}, top_n=5)
    assert result == []


def test_update_counter_does_not_modify_candidate_dict():
    """算法不应修改入参 candidate_entities dict"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"skillA": _make_entity("skillA")}
    original = dict(candidates)
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert candidates == original


def test_zero_score_entries_are_cleaned_up():
    """Step 6 清理：0 分项从 counter 和 cache 同步移除（防止无界增长）"""
    counter: dict[str, int] = {"skillA": 1, "skillB": 7, "skillC": 0}
    cache: dict[str, dict] = {
        "skillA": _make_entity("skillA"),
        "skillB": _make_entity("skillB"),
        "skillC": _make_entity("skillC"),
    }
    # 本轮没命中 → skillA: 1→0(被清理), skillB: 7→6, skillC: 0(被清理)
    NiuRunner._update_skill_counter(counter, cache, {})
    assert "skillA" not in counter  # 衰减到 0 被清理
    assert "skillA" not in cache  # cache 同步清理
    assert "skillC" not in counter  # 原本 0 分被清理
    assert "skillC" not in cache  # cache 同步清理
    assert counter["skillB"] == 6  # 7 - 1
    assert "skillB" in cache  # 6 分仍保留


def test_zero_score_cleaned_after_decay_below_zero():
    """连续不命中 10 轮后从 10 降到 0 被清理（含 cache）"""
    counter: dict[str, int] = {"skillA": 10}
    cache: dict[str, dict] = {"skillA": _make_entity("skillA")}
    # 10→9→8→7→6→5→4→3→2（8 轮）→1（9 轮）→0（10 轮，被清理）
    for _ in range(10):
        NiuRunner._update_skill_counter(counter, cache, {})
    # 10 轮后 counter 和 cache 都应被清理
    assert "skillA" not in counter
    assert "skillA" not in cache


def test_empty_string_key_in_candidate_ignored():
    """空字符串 candidate name 不应进入 counter（防御）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    candidates = {"": _make_entity(""), "skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert "" not in counter
    assert "" not in cache
    assert counter["skillA"] == 7


def test_empty_string_existing_key_cleaned_up():
    """counter 里历史遗留的空 key 应被清理（cache 同步）"""
    counter: dict[str, int] = {"": 5, "skillA": 7}
    cache: dict[str, dict] = {"": _make_entity(""), "skillA": _make_entity("skillA")}
    candidates = {"skillA": _make_entity("skillA")}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert "" not in counter  # 空 key 被清理
    assert "" not in cache
    assert counter["skillA"] == 8  # 7 + 1


def test_entity_cache_updated_on_hit():
    """命中时 entity dict 写入 cache（Step 5）"""
    counter: dict[str, int] = {}
    cache: dict[str, dict] = {}
    new_entity = _make_entity("skillA", "new description")
    candidates = {"skillA": new_entity}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert cache["skillA"] == new_entity  # cache 被写入


def test_entity_cache_refreshed_on_repeated_hit():
    """重复命中时 cache 被最新 entity dict 覆盖"""
    counter: dict[str, int] = {"skillA": 7}
    old_entity = _make_entity("skillA", "old")
    cache: dict[str, dict] = {"skillA": old_entity}
    new_entity = _make_entity("skillA", "new description")
    candidates = {"skillA": new_entity}
    NiuRunner._update_skill_counter(counter, cache, candidates)
    assert cache["skillA"] == new_entity  # 被覆盖
    assert cache["skillA"]["description"] == "new description"


def test_entity_cache_preserved_when_not_hit():
    """未命中时 cache 保留旧 entity dict（关键：跨轮注入能力）"""
    counter: dict[str, int] = {"skillA": 8}
    old_entity = _make_entity("skillA", "old")
    cache: dict[str, dict] = {"skillA": old_entity}
    # 本轮没命中 skillA
    NiuRunner._update_skill_counter(counter, cache, {})
    assert counter["skillA"] == 7  # 8 - 1
    assert cache["skillA"] == old_entity  # cache 保留（关键：下一轮可从这里取注入）


def test_default_top_n_constant_is_5():
    """默认注入 Top_N 常量为 5（防止误改）"""
    assert NiuRunner._SKILL_INJECT_TOP_N == 5


def test_default_inject_threshold_is_3():
    """默认注入门槛常量为 3"""
    assert NiuRunner._SKILL_SCORE_INJECT_THRESHOLD == 3
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_skill_score_counter.py -v`
Expected: FAIL with "AttributeError: type object 'NiuRunner' has no attribute '_update_skill_counter'"

- [ ] **Step 3: 实现核心算法**

在 `agent/runner.py` 的 `NiuRunner` 类内（建议放在 `_inject_dynamic_resources` 方法之前，约 L1940 附近）新增两个方法：

```python
    # ============== Skill Score Counter ==============

    # 衰减算法常量
    _SKILL_SCORE_MIN = 0          # 计数器下限
    _SKILL_SCORE_MAX = 10         # 计数器上限（封顶）
    _SKILL_SCORE_FIRST_HIT = 7    # 首次命中/低分命中直接置为此值
    _SKILL_SCORE_HIT_INCREMENT = 1  # 已熟悉命中加分
    _SKILL_SCORE_DECAY = 1        # 未命中衰减减分
    _SKILL_SCORE_INJECT_THRESHOLD = 3  # 进入 prompt 的最低分门槛
    _SKILL_INJECT_TOP_N = 5       # 第二阶段注入 prompt 的 skill 数量上限

    @staticmethod
    def _update_skill_counter(
        counter: dict[str, int],
        entity_cache: dict[str, dict],
        candidate_entities: dict[str, dict],
    ) -> None:
        """按算法更新 skill 计数器 + entity dict 缓存。

        算法（每轮执行顺序）：
        1. 未命中衰减：所有计数器 > 0 且不在候选集合里的 skill，各 -1 分
        2. 命中加分（已熟悉）：候选集合里计数器 ≥7 且 <10 的，+1 分（7 分走这条分支到 8）
        3. 命中置位（新命中或低分）：候选集合里计数器 <7 的，直接置为 7（7 分不走这条分支）
        4. entity dict 缓存更新：候选集合里的 skill 用本轮 entity dict 覆盖 cache
        5. 清理 0 分项：删除 counter 字典里所有 ≤0 分的项，同时从 entity_cache 删除对应项

        Args:
            counter: 计数器 dict（会被原地修改），key=skill name, value=分数
            entity_cache: entity dict 缓存（会被原地修改），key=skill name, value=entity dict
            candidate_entities: 本轮向量库检索命中的 skill entity dict，key=skill name, value=entity dict
        """
        candidate_names = set(candidate_entities.keys())

        # Step 1: 未命中衰减（counter 里已存在但不在 candidate 里的）
        # 跳过空名 key（防御历史脏数据）
        for name, score in list(counter.items()):
            if not name:
                continue
            if name not in candidate_names and score > NiuRunner._SKILL_SCORE_MIN:
                counter[name] = max(
                    NiuRunner._SKILL_SCORE_MIN,
                    score - NiuRunner._SKILL_SCORE_DECAY,
                )

        # Step 2 & 3: 命中加分或置位（跳过空名 candidate）
        for name in candidate_names:
            if not name:
                continue
            current = counter.get(name, NiuRunner._SKILL_SCORE_MIN)
            if current < NiuRunner._SKILL_SCORE_FIRST_HIT:
                # 低于 7 分直接置 7（第 4 步置位）
                counter[name] = NiuRunner._SKILL_SCORE_FIRST_HIT
            else:
                # ≥7 且 <10：+1 分（封顶 10）
                counter[name] = min(
                    NiuRunner._SKILL_SCORE_MAX,
                    current + NiuRunner._SKILL_SCORE_HIT_INCREMENT,
                )

        # Step 4: entity dict 缓存更新（命中即用本轮最新 entity dict 覆盖 cache）
        for name, entity in candidate_entities.items():
            if name and entity:
                entity_cache[name] = entity

        # Step 5: 清理 ≤0 分项（counter 和 cache 同步清理，防止无界增长）
        # 不能在迭代 counter.items() 时修改 dict，先收集再删
        to_remove = [
            name for name, score in counter.items()
            if score <= NiuRunner._SKILL_SCORE_MIN or not name
        ]
        for name in to_remove:
            counter.pop(name, None)
            entity_cache.pop(name, None)

    @staticmethod
    def _select_top_skills(
        counter: dict[str, int],
        top_n: int,
    ) -> list[tuple[str, int]]:
        """第二阶段：从计数器筛 ≥3 分的 skill，按分数倒序取前 N 个。

        分数相同时按 name 字典序兜底（保证排序稳定）。

        Returns:
            [(name, score), ...] 按分数倒序，最多 top_n 条
        """
        qualified = [
            (name, score)
            for name, score in counter.items()
            if name and score >= NiuRunner._SKILL_SCORE_INJECT_THRESHOLD
        ]
        # 排序：分数倒序，name 字典序正序兜底
        qualified.sort(key=lambda x: (-x[1], x[0]))
        return qualified[:top_n]
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_skill_score_counter.py -v`
Expected: 26 个测试全 PASS

- [ ] **Step 5: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/runner.py tests/test_skill_score_counter.py
git commit -m "feat(skill-inject): 新增 skill 计数器核心算法（衰减-加分-置位）"
```

---

## Task 2: 在 NiuRunner 实例上初始化计数器 + 改造注入路径

**Files:**
- Modify: `agent/runner.py` — `__init__` 初始化计数器 + 改造 `_inject_dynamic_resources`

**设计**：
- `__init__` 新增 `self._skill_score_counter: dict[str, int] = {}`
- `_inject_dynamic_resources` 内，把 `lightrag_results["skill"]` 和 `region_results["skill"]` 的 skill name 提取成集合，调 `_update_skill_counter` 更新计数器
- 再用 `_select_top_skills` 选出 top N，从原始 `lightrag_results["skill"]` + `region_results["skill"]` 里按这个顺序挑出对应 entity dict 列表，传给 `_format_lightrag_entities_for_prompt`

### Task 2 Step 1: 写集成测试 — 计数器与注入路径协作

- [ ] **Step 1: 写失败测试**

```python
# tests/test_skill_inject_integration.py
"""Skill 计数器与 _inject_dynamic_resources 集成测试。

验证：
1. _inject_dynamic_resources 调用后计数器被正确更新
2. 第二阶段排序后的 skill 列表被注入 prompt
3. 计数器跨轮维持状态（多次调用累积）
"""
import pytest
from unittest.mock import MagicMock, patch
from agent.runner import NiuRunner


@pytest.fixture
def runner(monkeypatch):
    """构造一个不依赖 LightRAG/Brain 的 NiuRunner 实例

    使用真实 LightRAGAdapter 类的 mock 实例直接注入 `_brain_adapter` 属性，
    跳过 `_inject_dynamic_resources` 内的局部 import 分支（L1969-1970）
    """
    runner = NiuRunner.__new__(NiuRunner)
    runner._skill_score_counter = {}
    runner._skill_entity_cache = {}  # entity dict 跨轮缓存（关键：未命中时仍可注入）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    return runner


def _make_mock_adapter(lightrag_results, region_results, habits=None):
    """构造一个 mock LightRAGAdapter 实例，注入 _brain_adapter 跳过局部 import"""
    adapter = MagicMock()
    adapter.search_multi_lightrag.return_value = lightrag_results
    adapter.search_within_region.return_value = region_results
    adapter.search_interaction_habits.return_value = habits or []
    return adapter


def _make_skill_entity(name: str, desc: str = "test desc") -> dict:
    return {
        "entity_name": name,
        "entity_type": "skill",
        "description": desc,
    }


def test_inject_updates_counter_on_first_hit(runner):
    """首次向量检索命中 skill → 计数器记 7"""
    lightrag_results = {"skill": [_make_skill_entity("skillA")], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("test context")

    assert runner._skill_score_counter.get("skillA") == 7


def test_inject_accumulates_counter_across_rounds(runner):
    """两轮检索命中同一 skill → 计数器 7 → 8"""
    lightrag_results = {"skill": [_make_skill_entity("skillA")], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("ctx1")
        runner._inject_dynamic_resources("ctx2")

    assert runner._skill_score_counter["skillA"] == 8


def test_inject_decays_non_hit_skills(runner):
    """第二轮没命中 skillA → 计数器 -1"""
    # 第一轮命中 skillA → 7
    runner._skill_score_counter = {"skillA": 7}
    # 第二轮没命中任何 skill
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        runner._inject_dynamic_resources("ctx2")

    assert runner._skill_score_counter["skillA"] == 6  # 7 - 1


def test_inject_second_stage_filters_below_3(runner):
    """计数器 <3 分的 skill 不进 prompt（cache 里有 entity dict 但 counter 不够 3 分被筛掉）"""
    # skillA=2 分（被淘汰出 prompt），skillB=10 分（保留）
    runner._skill_score_counter = {"skillA": 2, "skillB": 10}
    # cache 里两个 skill 都有 entity dict（模拟上一轮命中过）
    runner._skill_entity_cache = {
        "skillA": _make_skill_entity("skillA"),
        "skillB": _make_skill_entity("skillB"),
    }
    # 本轮没命中，衰减后 skillA=1(<3 被筛出 prompt), skillB=9
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # 精确断言：检查 prompt 里的 skill 文件路径标志（注入格式为 路径: ~/.niu/skills/{name}.md）
    assert "路径: ~/.niu/skills/skillB.md" in injection
    assert "路径: ~/.niu/skills/skillA.md" not in injection


def test_inject_second_stage_sorts_by_score_desc(runner):
    """注入 prompt 时按分数倒序（cache 里有 entity dict，本轮未命中仍能注入）"""
    runner._skill_score_counter = {"low": 3, "high": 10, "mid": 5}
    # cache 里有三个 skill 的 entity dict
    runner._skill_entity_cache = {
        "low": _make_skill_entity("low"),
        "high": _make_skill_entity("high"),
        "mid": _make_skill_entity("mid"),
    }
    # 本轮都没命中（衰减后 low=2(<3 被筛出 prompt), high=9, mid=4）
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # high 应在 mid 之前（按计数器分数 9 > 4 倒序）
    high_pos = injection.find("路径: ~/.niu/skills/high.md")
    mid_pos = injection.find("路径: ~/.niu/skills/mid.md")
    assert high_pos != -1, f"high 应进 prompt: {injection}"
    assert mid_pos != -1, f"mid 应进 prompt: {injection}"
    assert high_pos < mid_pos, f"high 应在 mid 之前"


def test_inject_uses_cache_when_not_hit_this_round(runner):
    """核心：本轮没命中某 skill，但 cache 有 entity dict + counter 仍 ≥3 → 仍进 prompt

    这是"缓慢淘汰"机制的关键测试：被命中过的 skill 不会因下一轮没命中就被立即丢弃。
    """
    # 上一轮命中过 skillA → counter=8, cache 里有 entity dict
    runner._skill_score_counter = {"skillA": 8}
    runner._skill_entity_cache = {"skillA": _make_skill_entity("skillA", "cached desc")}
    # 本轮没命中 skillA（lightrag_results.skill 为空）
    lightrag_results = {"skill": [], "knowledge": [], "other": []}
    region_results = {"skill": [], "knowledge": [], "other": []}
    runner._brain_adapter = _make_mock_adapter(lightrag_results, region_results)

    with patch.object(runner, "_get_brain_injector", return_value=None):
        injection, _ = runner._inject_dynamic_resources("ctx")

    # skillA 仍应进 prompt（counter 8→7，从 cache 取 entity dict）
    assert "路径: ~/.niu/skills/skillA.md" in injection
    assert runner._skill_score_counter["skillA"] == 7  # 8 - 1
```


- [ ] **Step 2: 运行测试验证失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_skill_inject_integration.py -v`
Expected: FAIL（计数器没初始化、_inject_dynamic_resources 没调 _update_skill_counter）

- [ ] **Step 3: 在 `__init__` 初始化计数器 + entity cache**

先找到 `NiuRunner.__init__`，在合适位置（其他 `self._xxx = {}` 附近）加：

```python
self._skill_score_counter: dict[str, int] = {}
self._skill_entity_cache: dict[str, dict] = {}  # entity dict 跨轮缓存
```

具体行号需要 implementer 读 `agent/runner.py` 找到 `__init__` 后确定。

**实施前置**：本步骤修改 `agent/runner.py` 前必须先备份当前状态（CLAUDE.md 铁律第 3 条）：

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A && git commit -m "backup: before skill counter __init__ integration"
```

- [ ] **Step 4: 改造 `_inject_dynamic_resources`**

在 `agent/runner.py` 的 `_inject_dynamic_resources` 方法内，找到"Skills (global vector search)"代码块（约 L2038-2044）：

**修改前**：
```python
# Skills (global vector search)
lightrag_skills = lightrag_results.get("skill", [])
skills_text, seen_names = self._format_lightrag_entities_for_prompt(
    lightrag_skills, "相关技能", seen_names,
)
if skills_text:
    parts.append(skills_text)
```

**修改后**：
```python
# Skills (global vector search + 计数器两阶段 Top_K)
lightrag_skills = lightrag_results.get("skill", [])
region_skills = region_results.get("skill", [])
# 第一阶段：向量库已检索得候选集合（lightrag_skills + region_skills）
# 注意：region_skills 在前、lightrag_skills 在后，dict 推导式让 lightrag_skills 覆盖 region_skills
# （全局检索 top_k 更宽，数据更完整，优先级更高）
candidate_entities: dict[str, dict] = {
    e.get("entity_name", ""): e for e in region_skills + lightrag_skills
}
candidate_entities.pop("", None)  # 去掉空 key（防止历史脏数据）
# 计数器 + entity cache 同步更新
self._update_skill_counter(
    self._skill_score_counter, self._skill_entity_cache, candidate_entities,
)

# 第二阶段：按计数器排序选 top N（从 cache 取 entity dict 注入）
top_skills = self._select_top_skills(
    self._skill_score_counter, self._SKILL_INJECT_TOP_N,
)
# 从 cache 里按 top_skills 顺序挑出对应 entity dict
# 关键：本轮没命中的 skill 也能从 cache 取出（缓跨轮维持注入能力）
ordered_skill_entities = [
    self._skill_entity_cache[name]
    for name, _ in top_skills
    if name in self._skill_entity_cache
]
# 过滤掉已在 seen_names 里的（理论上 seen_names 在此段开始时为空，但保留防御逻辑）
# 同时记日志辅助诊断"计数器有分但 prompt 看不到"的边界场景
unseen_skill_entities = []
for e in ordered_skill_entities:
    name = e.get("entity_name", "")
    if name and name not in seen_names:
        unseen_skill_entities.append(e)
    elif name:
        logger.debug(
            f"[Inject] Skill '{name}' in counter top but already in seen_names, skipping"
        )
skills_text, seen_names = self._format_lightrag_entities_for_prompt(
    unseen_skill_entities, "相关技能", seen_names,
)
if skills_text:
    parts.append(skills_text)
logger.debug(
    f"Skill injection: candidates={len(candidate_entities)}, "
    f"top_selected={len(top_skills)}, injected={len(unseen_skill_entities)}"
)
```

**关键点**：
- 原来的 `lightrag_skills` 直接传给 `_format_lightrag_entities_for_prompt`，现在改成 `unseen_skill_entities`（按计数器排序 + 过滤已注入）
- `region_skills` 从"活跃脑区知识"段拆出来用计数器合并；`region_knowledge` 仍走原路径
- `entity_by_name` 推导式让 `lightrag_skills` 覆盖 `region_skills` 同名 entity（全局检索优先级更高）
- 在传给 `_format_lightrag_entities_for_prompt` 之前预先过滤 `seen_names`，并记日志辅助诊断"计数器有分但 prompt 看不到"的边界场景

**"活跃脑区知识"段行为变化说明（C2 修复）**：

原代码（L2054-2067）：
```python
region_knowledge = region_results.get("knowledge", [])
region_skills = region_results.get("skill", [])
region_all = region_skills + region_knowledge
if region_all:
    region_text, seen_names = self._format_lightrag_entities_for_prompt(
        region_all, "活跃脑区知识", seen_names,
    )
    if region_text:
        parts.append(region_text)
        parts.append(
            "\n\n### [知识探索指引]\n"
            "优先参考上述活跃脑区知识回答用户问题，脑区内容与你当前关注领域最相关。"
        )
```

改造后（保留"知识探索指引"提示语注入逻辑）：
```python
region_knowledge = region_results.get("knowledge", [])
# region_skills 已被计数器合并到"相关技能"段，这里只处理 knowledge
if region_knowledge:
    region_text, seen_names = self._format_lightrag_entities_for_prompt(
        region_knowledge, "活跃脑区知识", seen_names,
    )
    if region_text:
        parts.append(region_text)
        parts.append(
            "\n\n### [知识探索指引]\n"
            "优先参考上述活跃脑区知识回答用户问题，脑区内容与你当前关注领域最相关。"
        )
```

**预期行为变化**：
- 原行为：region_skills 在"活跃脑区知识"段注入
- 改造后：region_skills 走计数器路径在"相关技能"段注入；"活跃脑区知识"段只剩 knowledge
- 边界场景：region_knowledge 为空但 region_skills 非空时，"活跃脑区知识"段 + "知识探索指引"提示语**不会注入**——这是预期行为（用户在脑区里只有 skill 没有 knowledge 时，没必要注入空的"活跃脑区知识"段和提示语）

- [ ] **Step 5: 运行测试验证通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_skill_inject_integration.py tests/test_skill_score_counter.py -v`
Expected: 全 PASS

- [ ] **Step 6: 运行现有 lightrag 相关测试确保没回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_manager.py tests/test_tool_lifecycle.py -v 2>&1 | tail -30`
Expected: 现有测试全 PASS（`test_tool_lifecycle.py` 本来就断言 tool_lifecycle 模块不存在，应继续 PASS）

- [ ] **Step 7: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/runner.py tests/test_skill_inject_integration.py
git commit -m "feat(skill-inject): _inject_dynamic_resources 接入计数器两阶段 Top_K"
```

---

## Task 3: 真实程序验证 + 日志检查

**Files:** 无（只跑真实程序）

**设计**：用真实程序 + 真实 LightRAG 验证计数器跨轮维持状态、衰减轨迹符合预期。

- [ ] **Step 1: 启动程序并触发 skill 检索**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./niu &
```

启动后通过聊天触发 skill 注入（说一句包含 skill 关键词的话，例如"帮我整理照片"应该命中 photo 相关 skill）。

- [ ] **Step 2: 检查日志验证计数器被调用**

```bash
grep -E "Dynamic injection|Skill injection|_update_skill_counter|_select_top_skills" logs/api_stderr.log 2>/dev/null | tail -20
```

Expected: 看到日志里 `Dynamic injection | Skills: N` 里的 N 是本轮检索到的 skill 数量（不是注入 prompt 的数量）；`Skill injection: candidates=X, top_selected=Y, injected=Z` 里的 Z 是注入 prompt 的 skill 数量（≤ N，受 counter 筛选 + top_n=5 限制）。

- [ ] **Step 3: 多轮对话验证计数器跨轮维持**

连续 3 轮说类似话题（每轮都应命中同一 skill），观察：
- 第 1 轮：计数器从 0 → 7，cache 写入 entity dict
- 第 2 轮：7 → 8，cache 刷新 entity dict
- 第 3 轮：8 → 9，cache 再次刷新

实施方法：在 `_update_skill_counter` 末尾**临时**加 `logger.debug(f"SkillCounter update: candidates={list(candidate_entities.keys())}, counter={counter}, cache_keys={list(entity_cache.keys())}")`，跑完 3 轮对话后从 `logs/api_stderr.log` 读计数器值。

**关键约束（CLAUDE.md 铁律第 5 条 — 真实数据测试）**：
- 本步骤必须用真实 LightRAG + 真实 LLM 对话
- 在 `_inject_dynamic_resources` 内 `lightrag_results = adapter.search_multi_lightrag(...)` 后**临时**加 `logger.debug(f"Lightrag results sample: {lightrag_results.get('skill', [])[:1]}")`，从 `logs/api_stderr.log` 验证真实 entity dict 字段格式（`entity_name`/`entity_type`/`description`）与 Task 2 mock 的 `_make_skill_entity` 一致
- 临时日志验证完必须撤销（见 Step 6），不进 commit

- [ ] **Step 4: 切换话题验证衰减**

第 4-9 轮说**完全无关的话题**（如数学题"计算 17*23"、外语翻译"how do you say hello in japanese"），通过临时日志（Step 3 加的 `candidates=...` 输出）确认 `candidate_entities` 不含目标 skill name，观察计数器从 9 → 8 → 7 → 6 → 5 → 4，第 7 轮后（9→8→7→6→5→4→3）仍进 prompt（从 cache 取 entity dict），第 8 轮后降到 2（被淘汰出 prompt）。

如果某轮意外命中目标 skill（向量检索语义近邻），记录日志并跳过该轮的衰减观察，下一轮再试。

- [ ] **Step 5: 关闭程序**

```bash
kill -TERM $(pgrep -f "niu")
```

- [ ] **Step 6: 撤销临时日志**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git diff HEAD agent/runner.py tests/  # 确认无临时日志残留
grep -rn "SkillCounter update\|Lightrag results sample" agent/ tests/  # 兜底检查临时日志字符串
```

如果 grep 有结果，必须删除临时日志后才能进入 Step 7。

- [ ] **Step 7: 提交验证记录（无代码改动则不 commit）**

如果 Step 3 加了临时日志且已撤销，跳过本步。如果有其他小修，commit：
```bash
git add -A
git commit -m "test(skill-inject): 真实程序验证计数器衰减轨迹"
```

---

## Self-Review

**Spec coverage 检查**：
- ✅ 用户描述的"最近 3 条消息 + 工具名"拼装 → 现有逻辑 `_extract_context_from_messages` 已实现，不动
- ✅ 向量库检索得候选集合 → Task 2 Step 4 复用现有 `search_multi_lightrag` + `search_within_region`
- ✅ 未命中衰减 -1 分 → Task 1 `_update_skill_counter` Step 1（算法第 1 步）
- ✅ 命中 ≥7 且 <10 加 +1 分（7 分走这条分支） → Task 1 `_update_skill_counter` Step 2（算法第 2 步）+ 边界规则注释
- ✅ 命中 <7 直接置 7（7 分不走这条分支） → Task 1 `_update_skill_counter` Step 3（算法第 3 步）
- ✅ entity dict 缓存更新 → Task 1 `_update_skill_counter` Step 4（算法第 5 步）+ 3 个 cache 测试
- ✅ 清理 0 分项防止无界增长 → Task 1 `_update_skill_counter` Step 5（算法第 6 步）+ `test_zero_score_entries_are_cleaned_up` / `test_zero_score_cleaned_after_decay_below_zero` / `test_zero_score_cleaned_when_not_hit` / `test_candidate_set_empty_all_decay`
- ✅ 第二阶段筛 ≥3 分排序取前 N → Task 1 `_select_top_skills` + Task 2 接入注入路径
- ✅ 计数器跨轮维持 → Task 2 `__init__` 初始化 `self._skill_score_counter` 为实例属性
- ✅ entity cache 跨轮维持 → Task 2 `__init__` 初始化 `self._skill_entity_cache` 为实例属性 + `test_inject_uses_cache_when_not_hit_this_round` 验证"未命中仍能从 cache 注入"
- ✅ gitnexus impact 分析 + 临时备份 → Task 0 前置
- ✅ 真实数据测试 → Task 3 用真实程序+真实 LLM 验证 + 临时日志断言真实 entity dict 字段格式与 mock 一致

**Placeholder 扫描**：无 TBD/TODO/handle edge cases 等占位符。所有步骤含完整代码。

**Type consistency**：
- `_update_skill_counter(counter, entity_cache, candidate_entities)` — Task 1 定义为 staticmethod（3 参数：counter dict、entity_cache dict、candidate_entities dict），Task 2 调用签名一致 ✅
- `_select_top_skills(counter, top_n) -> list[tuple[str, int]]` — Task 1 定义为 staticmethod，Task 2 调用一致 ✅
- 常量 `_SKILL_SCORE_FIRST_HIT=7` / `_SKILL_SCORE_MAX=10` / `_SKILL_SCORE_INJECT_THRESHOLD=3` / `_SKILL_INJECT_TOP_N=5` — 定义与测试断言值一致 + 加 `test_default_top_n_constant_is_5` / `test_default_inject_threshold_is_3` 防误改 ✅
- `entity_name` 作为计数器 key + cache key — 与现有 `_format_lightrag_entities_for_prompt` 的 `display_name` 一致 ✅
- 空字符串防御：`candidate_entities.pop("", None)` + `_update_skill_counter` 内 `if not name: continue` + 清理 0 分项时一并清理空 key ✅

**Test count**：
- Task 1 单元测试：26 个（17 算法逻辑 + 6 清理/空名防御 + 3 entity cache 跨轮维持）
- Task 2 集成测试：6 个（5 原有 + 1 cache 跨轮维持真值测试 `test_inject_uses_cache_when_not_hit_this_round`）

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-skill-score-counter.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
