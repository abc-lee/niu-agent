# 动态注入检索机制重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将动态注入的检索机制从"3条消息80字符截断 + 脑区向量过滤检索 + 3套衰减"重构为"2条消息按行提取 + 图遍历 + 统一 Ebbinghaus 衰减池 + 真实向量分数"。

**Architecture:** LightRAG Fork 返回 distance 分数 → runner.py 新增 DecayPool 统一管理 Skill/Knowledge/Habit 的衰减 → 脑区知识从 search_within_region 向量过滤改为图遍历1跳 → context 提取从3条80字符改为2条按行+工具名。脑区自身的 *0.92 衰减/spillover/co-activation 完全不动。

**Tech Stack:** Python 3.11, LightRAG (Fork: github.com/abc-lee/LightRAG), NetworkX (图遍历), packaging (版本比较)

**Spec:** `docs/superpowers/specs/2026-08-01-dynamic-injection-refactor-design.md`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `/Users/lilei/tools/LightRAG/lightrag/operate.py` | Fork: `_get_node_data` 加 distance 字段 |
| `/Users/lilei/tools/LightRAG/lightrag/utils.py` | Fork: `convert_to_user_format` 加 distance 字段 |
| `/Users/lilei/tools/LightRAG/lightrag/_version.py` | Fork: 版本号 1.4.17 → 1.4.19 |
| `agent/runner.py` | 核心改造: DecayPool + context + 图遍历 + 废弃旧代码 |
| `niu_api/internal/lightrag_adapter.py` | `_ENTITY_TYPE_TO_CATEGORY` 中 interactionhabit 改独立类别 |
| `niu_api/__main__.py` | 新增 `check_critical_versions()` |
| `niu_api/compat.py` | `/new` 命令处理中清空衰减池 |
| `requirements.txt` | lightrag-hku 行锁 commit hash |
| `tests/test_*.py` | 7个测试文件更新 |

---

### Task 1: LightRAG Fork — 返回向量相似度分数

> ⚠️ **重要约束：LightRAG 运行环境代码不能直接修改**
>
> `python/lib/python3.11/site-packages/lightrag/` 是 pip 安装的运行环境，**不能直接改其中的文件**。
> 只能修改 `/Users/lilei/tools/LightRAG` 源码目录，然后 push 到 Fork（github.com/abc-lee/LightRAG），
> 再用 `pip install` 重新下载安装（见 Step 6）。直接改 site-packages 会被下次 pip install 覆盖。

**Files:**
- Modify: `/Users/lilei/tools/LightRAG/lightrag/operate.py:4453-4462`
- Modify: `/Users/lilei/tools/LightRAG/lightrag/utils.py:3186-3206`
- Modify: `/Users/lilei/tools/LightRAG/lightrag/_version.py`

- [ ] **Step 1: 修改 `operate.py` `_get_node_data` 加 distance 字段**

在 `/Users/lilei/tools/LightRAG/lightrag/operate.py` 第 4453-4462 行，字典推导加 `"distance": k.get("distance")`：

```python
    node_datas = [
        {
            **n,
            "entity_name": k["entity_name"],
            "rank": d,
            "created_at": k.get("created_at"),
            "distance": k.get("distance"),  # 向量余弦相似度分数
        }
        for k, n, d in zip(results, node_datas, node_degrees)
        if n is not None
    ]
```

- [ ] **Step 2: 修改 `utils.py` `convert_to_user_format` 加 distance 字段**

在 `/Users/lilei/tools/LightRAG/lightrag/utils.py` 第 3186-3206 行，两个分支各加 `"distance"` 字段。

original_entity 分支（约第 3186 行）：

```python
            formatted_entities.append(
                {
                    "entity_name": original_entity.get("entity_name", entity_name),
                    "entity_type": original_entity.get("entity_type", "unknown"),
                    "description": original_entity.get("description", ""),
                    "source_id": original_entity.get("source_id", ""),
                    "file_path": original_entity.get("file_path", "unknown_source"),
                    "created_at": original_entity.get("created_at", ""),
                    "distance": original_entity.get("distance"),
                }
            )
```

fallback 分支（约第 3198 行）：

```python
            formatted_entities.append(
                {
                    "entity_name": entity_name,
                    "entity_type": entity.get("type", "unknown"),
                    "description": entity.get("description", ""),
                    "source_id": entity.get("source_id", ""),
                    "file_path": entity.get("file_path", "unknown_source"),
                    "created_at": entity.get("created_at", ""),
                    "distance": entity.get("distance"),
                }
            )
```

- [ ] **Step 3: 升版本号到 1.4.19**

在 `/Users/lilei/tools/LightRAG/lightrag/_version.py`：

```python
__version__ = "1.4.19"
```

- [ ] **Step 4: 提交 Fork 并 push**

```bash
cd /Users/lilei/tools/LightRAG
git add lightrag/operate.py lightrag/utils.py lightrag/_version.py
git commit -m "feat: return vector distance in aquery_data entities (v1.4.19)"
git push
```

- [ ] **Step 5: 记录 commit SHA**

```bash
cd /Users/lilei/tools/LightRAG
git rev-parse HEAD
```

记录输出的 commit SHA，后续 Task 5 会用到。

- [ ] **Step 6: 在项目根目录重新安装 Fork**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/pip install --force-reinstall --no-deps "git+https://github.com/abc-lee/LightRAG.git@<commit-sha>"
```

将 `<commit-sha>` 替换为 Step 5 记录的值。**这是更新 LightRAG 运行环境的唯一合法方式**——绝不直接编辑 `python/lib/python3.11/site-packages/lightrag/` 下的文件，否则下次 pip install 会覆盖且改动无法追踪。

- [ ] **Step 7: 验证 distance 字段返回**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
result = adapter.query_data('定时任务', mode='local', top_k=3, keywords=['定时任务'])
data = result.get('data', result) if result else {}
entities = data.get('entities', [])
for e in entities:
    print(f'{e.get(\"entity_name\")}: distance={e.get(\"distance\")}')"
```

Expected: 每个 entity 有 distance 数值（0~1），不再是 None。

---

### Task 2: 新增 DecayPool 衰减池类

**Files:**
- Create: `agent/decay_pool.py`

- [ ] **Step 1: 创建 decay_pool.py**

在 `agent/decay_pool.py` 创建 DecayPool 类和常量：

```python
"""Ebbinghaus 遗忘曲线衰减池。

统一管理 Skill/Knowledge/InteractionHabit 的注入与衰减。
公式: R_i(t) = s_i × e^(-t/S)
  - s_i: 命中时的向量余弦相似度（0~1）
  - t: 经过轮数
  - S=5: 记忆稳定性参数
  - 阈值=0.35: 低于此值淘汰
脑区 activation（*0.92, 阈值0.3）完全独立，不由此池管理。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


# 衰减池常量（统一定义，避免硬编码散落多处）
DECAY_S = 5.0                   # 记忆稳定性参数
DECAY_THRESHOLD = 0.35           # 注入阈值
DECAY_FACTOR = math.exp(-1 / DECAY_S)  # ≈ 0.8187，每轮衰减因子


@dataclass
class DecayEntry:
    """衰减池中的单个实体条目。"""
    entity_name: str
    entity_dict: dict[str, Any]   # LightRAG entity dict（description, entity_type 等）
    category: str                  # skill / knowledge / interactionhabit
    source: str                    # "vector" / "graph_traversal"
    score: float                   # 当前 R 值


class DecayPool:
    """Ebbinghaus 衰减池，管理跨轮次的知识实体注入与淘汰。

    使用方法:
        pool = DecayPool()
        pool.inject("定时任务", entity_dict, "knowledge", "vector", 0.65)
        pool.decay()  # 每轮调用
        top = pool.get_top_by_category("knowledge", top_n=10)
    """

    def __init__(self) -> None:
        self._entries: dict[str, DecayEntry] = {}  # key = entity_name (lowercase)

    def decay(self) -> None:
        """每轮衰减：所有 entry score *= DECAY_FACTOR，清理低于阈值的。"""
        for entry in self._entries.values():
            entry.score *= DECAY_FACTOR
        self._entries = {
            k: v for k, v in self._entries.items()
            if v.score >= DECAY_THRESHOLD
        }

    def inject(
        self,
        entity_name: str,
        entity_dict: dict[str, Any],
        category: str,
        source: str,
        vector_score: float,
    ) -> None:
        """注入新命中：score = vector_score（覆盖同名旧条目）。

        Args:
            entity_name: 实体名（会被 lower 化作为 key）
            entity_dict: LightRAG entity dict
            category: skill / knowledge / interactionhabit
            source: "vector" / "graph_traversal"
            vector_score: 向量相似度分数（0~1）
        """
        key = entity_name.lower()
        existing = self._entries.get(key)
        # 如果实体已在池中且新分数低于现有分数，不覆盖（保留高分）
        if existing is not None and vector_score < existing.score:
            # 更新 entity_dict（内容可能变化）但不降分
            existing.entity_dict = entity_dict
            return
        self._entries[key] = DecayEntry(
            entity_name=entity_name,
            entity_dict=entity_dict,
            category=category,
            source=source,
            score=vector_score,
        )

    def get_top_by_category(self, category: str, top_n: int) -> list[DecayEntry]:
        """按 category 取 top N（按 score 降序）。"""
        qualified = [
            e for e in self._entries.values()
            if e.category == category and e.score >= DECAY_THRESHOLD
        ]
        qualified.sort(key=lambda e: e.score, reverse=True)
        return qualified[:top_n]

    def get_top_by_source(self, source: str, top_n: int) -> list[DecayEntry]:
        """按 source 取 top N（按 score 降序）。"""
        qualified = [
            e for e in self._entries.values()
            if e.source == source and e.score >= DECAY_THRESHOLD
        ]
        qualified.sort(key=lambda e: e.score, reverse=True)
        return qualified[:top_n]

    def clear(self) -> None:
        """清空衰减池（新会话时调用）。"""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
```

- [ ] **Step 2: 验证衰减池逻辑**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.decay_pool import DecayPool, DECAY_FACTOR, DECAY_THRESHOLD
import math

pool = DecayPool()
# 模拟命中：高分0.9, 中分0.65, 低分0.3
pool.inject('high', {}, 'knowledge', 'vector', 0.9)
pool.inject('mid', {}, 'knowledge', 'vector', 0.65)
pool.inject('low', {}, 'knowledge', 'vector', 0.3)

# 轮1衰减
pool.decay()
top = pool.get_top_by_category('knowledge', 10)
names = [e.entity_name for e in top]
print(f'轮1后: {names}')
assert 'low' not in names, 'low (0.3*0.819=0.246) 应被淘汰'
assert 'high' in names and 'mid' in names

# 轮2衰减
pool.decay()
top = pool.get_top_by_category('knowledge', 10)
names = [e.entity_name for e in top]
print(f'轮2后: {names}')
assert 'mid' in names, 'mid (0.65*0.819^2=0.436≥0.35，轮2仍在)'
assert 'high' in names, 'high (0.9*0.819^2=0.604) 应仍在'

# 轮3衰减
pool.decay()
top = pool.get_top_by_category('knowledge', 10)
names = [e.entity_name for e in top]
print(f'轮3后: {names}')
assert 'mid' in names, 'mid (0.65*0.819^3=0.357≥0.35，轮3仍在)'

# 轮4衰减
pool.decay()
top = pool.get_top_by_category('knowledge', 10)
names = [e.entity_name for e in top]
print(f'轮4后: {names}')
assert 'mid' not in names, 'mid 应在轮4淘汰'
assert 'high' in names, 'high (0.9*0.819^4=0.405) 应仍在'

print('All assertions passed!')
"
```

Expected: 所有断言通过，无 AssertionError。

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/decay_pool.py
git commit -m "feat: add DecayPool class (Ebbinghaus forgetting curve)"
```

---

### Task 3: 修改 `_ENTITY_TYPE_TO_CATEGORY` 映射

**Files:**
- Modify: `niu_api/internal/lightrag_adapter.py:342-361`

- [ ] **Step 1: 将 interactionhabit 改为独立类别**

在 `niu_api/internal/lightrag_adapter.py` 第 347 行，修改映射：

```python
    _ENTITY_TYPE_TO_CATEGORY = {
        "skill": "skill",
        "tool": "knowledge",
        "knowledge": "knowledge",
        "concept": "knowledge",
        "interactionhabit": "interactionhabit",  # 改前: "knowledge"
        "person": "knowledge",
        "photo": "knowledge",
        "organization": "knowledge",
        "technology": "knowledge",
        "location": "knowledge",
        "event": "knowledge",
        "document": "knowledge",
        "video": "knowledge",
        "note": "knowledge",
        "chat": "knowledge",
        "episodicevent": "knowledge",
        "brainregion": "knowledge",
        "other": "other",
    }
```

- [ ] **Step 2: 验证映射**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from niu_api.internal.lightrag_adapter import LightRAGAdapter
adapter = LightRAGAdapter()
cat = adapter._ENTITY_TYPE_TO_CATEGORY.get('interactionhabit')
print(f'interactionhabit -> {cat}')
assert cat == 'interactionhabit', f'应为 interactionhabit，实际为 {cat}'
print('OK')
"
```

Expected: `interactionhabit -> interactionhabit`

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/internal/lightrag_adapter.py
git commit -m "fix: interactionhabit 改为独立 category（不再归入 knowledge）"
```

---

### Task 4: 改造 `_extract_context_from_messages` + 删除死代码

**Files:**
- Modify: `agent/runner.py:725-767`（`_extract_context_from_messages`）
- Delete: `agent/runner.py:1831-1872`（`_extract_context_from_history`，死代码）

- [ ] **Step 1: 改造 `_extract_context_from_messages`**

在 `agent/runner.py` 第 725-767 行，替换整个方法体：

```python
    def _extract_context_from_messages(self, messages: list) -> str:
        """从 messages 列表提取上下文用于向量检索。

        策略：最近2条消息，按行取第一行（完整语义单元），assistant 附带最多5个工具名。
        """
        context_parts = []
        recent = messages[-2:] if len(messages) > 2 else messages

        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and content:
                if content.startswith("工具调用成功") or content.startswith("Tool call succeeded"):
                    # 工具返回JSON，取第一行并限制80字符
                    line = content.split("\n")[0]
                    if len(line) > 80:
                        line = line[:80] + "..."
                    context_parts.append(f"{role}: {line}")
                else:
                    # 取第一行（完整语义单元）
                    context_parts.append(f"{role}: {content.split(chr(10))[0]}")
            elif role == "assistant" and content:
                context_parts.append(f"{role}: {content.split(chr(10))[0]}")

            if role == "assistant":
                for tc in msg.get("tool_calls", [])[:5]:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        context_parts.append(f"tool: {name}")

        return "\n".join(context_parts) if context_parts else ""
```

- [ ] **Step 2: 删除 `_extract_context_from_history`（死代码）**

删除 `agent/runner.py` 第 1831-1872 行的 `_extract_context_from_history` 方法整个定义。

- [ ] **Step 3: 验证 context 提取**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.runner import NiuRunner

runner = NiuRunner.__new__(NiuRunner)  # 不调 __init__
msgs = [
    {'role': 'user', 'content': '帮我创建一个每天下午3点的定时任务'},
    {'role': 'assistant', 'content': '好的，我来帮你创建\n定时任务已创建\n任务ID: 123', 'tool_calls': [
        {'function': {'name': 'list_scheduled_tasks', 'arguments': '{}'}},
    ]},
]
ctx = runner._extract_context_from_messages(msgs)
print(f'Context: {repr(ctx)}')
assert 'user: 帮我创建一个每天下午3点的定时任务' in ctx
assert 'assistant: 好的，我来帮你创建' in ctx
assert '定时任务已创建' not in ctx, '只取第一行'
assert 'tool: list_scheduled_tasks' in ctx
print('OK')
"
```

Expected: context 只含第一行，工具名在列表中。

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "refactor: context 提取改为2条消息按行+工具名，删除死代码 _extract_context_from_history"
```

---

### Task 5: 新增 `_traverse_from_hits` 图遍历方法

**Files:**
- Modify: `agent/runner.py`（新增方法）

- [ ] **Step 1: 新增 `_traverse_from_hits` 方法**

在 `agent/runner.py` 的 `_inject_dynamic_resources` 方法之前（约第 2060 行），新增方法：

```python
    def _traverse_from_hits(self, hits: list[str]) -> dict[str, dict]:
        """从 hit entities 沿知识边1跳图遍历，收集邻居实体。

        Args:
            hits: 向量检索命中的 entity_name 列表

        Returns:
            {entity_name_lower: {description, entity_type, source, ...}}
            source 为 "hit" 或 "neighbor:{关系关键词}"
        """
        from niu_api.internal.lightrag_manager import get_lightrag, graph_read_lock

        rag = get_lightrag()
        if rag is None:
            return {}

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return {}

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return {}

        result: dict[str, dict] = {}

        with graph_read_lock():
            snapshot = nx_graph.copy()
            for hit in hits:
                hit_lower = hit.lower() if isinstance(hit, str) else hit
                if hit_lower not in snapshot:
                    continue

                # hit 实体本身
                node = snapshot.nodes.get(hit_lower, {})
                if hit_lower not in result:
                    result[hit_lower] = {**node, "entity_name": hit_lower, "source": "hit"}

                # 沿知识边找邻居
                for neighbor in snapshot.neighbors(hit_lower):
                    if neighbor.endswith("脑区"):
                        continue

                    edge_data = snapshot.get_edge_data(hit_lower, neighbor)
                    if not edge_data:
                        continue

                    # NetworkX: get_edge_data 返回 {key: attrs} (multigraph) 或 attrs (普通 graph)
                    if isinstance(list(edge_data.values())[0], dict):
                        edge_list = list(edge_data.values())
                    else:
                        edge_list = [edge_data]

                    for ed in edge_list:
                        kw = ed.get("keywords", "") if isinstance(ed, dict) else ""
                        if kw and kw != "包含" and not kw.startswith("_session"):
                            node2 = snapshot.nodes.get(neighbor, {})
                            neighbor_lower = neighbor.lower()
                            if neighbor_lower not in result:
                                result[neighbor_lower] = {
                                    **node2,
                                    "entity_name": neighbor_lower,
                                    "source": f"neighbor:{kw}",
                                }

        return result
```

- [ ] **Step 2: 验证图遍历**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.runner import NiuRunner
from agent.brain_tools import get_activation_mgr
from agent.injector.region_sync import get_region_sync
from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester
from niu_api.internal.region_injector import BrainContextInjector
from niu_api.internal.region_manager import RegionManager

# 初始化
rs = get_region_sync()
rs.run_sync()
mgr = get_activation_mgr()
adapter = LightRAGAdapter()
region_mgr_obj = RegionManager(adapter, LightRAGIngester())
injector = BrainContextInjector(adapter=adapter, activation_mgr=mgr, region_mgr=region_mgr_obj)

# 用定时任务 context 激活
context = 'user: 帮我创建定时任务'
rk, e2r, hits = injector.activate_for_query(context)
print(f'Hits: {len(hits)}')

runner = NiuRunner.__new__(NiuRunner)
traversed = runner._traverse_from_hits(hits)
print(f'Traversed: {len(traversed)} entities')
hit_count = sum(1 for v in traversed.values() if v.get('source') == 'hit')
neighbor_count = sum(1 for v in traversed.values() if v.get('source', '').startswith('neighbor'))
print(f'  hit: {hit_count}, neighbor: {neighbor_count}')
assert hit_count > 0, '应有 hit 实体'
assert neighbor_count > 0, '应有邻居实体'
# 确认没有脑区节点
assert all(not k.endswith('脑区') for k in traversed), '不应含脑区节点'
print('OK')
"
```

Expected: 有 hit 和 neighbor 实体，无脑区节点。

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "feat: 新增 _traverse_from_hits 图遍历方法（1跳知识边）"
```

---

### Task 6: 改造 `_inject_dynamic_resources` 核心方法

**Files:**
- Modify: `agent/runner.py:2063-2239`（`_inject_dynamic_resources`）
- Modify: `agent/runner.py:620-630`（初始化 DecayPool 实例属性）
- Modify: `agent/runner.py:1966-2059`（删除 Skill 计数器代码）
- Modify: `niu_api/compat.py:2164`（`/new` 命令处理中清空衰减池）

这是最大的 Task。分为几个子步骤。

- [ ] **Step 1: 在 `__init__` 中初始化 DecayPool 实例属性**

在 `agent/runner.py` 的 `__init__` 方法中（约第 624 行附近，brain 相关属性之后），新增：

```python
        # Decay pool (Ebbinghaus forgetting curve)
        self._decay_pool = DecayPool()
```

同时在文件顶部 import：

```python
from .decay_pool import DecayPool, DECAY_FACTOR, DECAY_THRESHOLD
```

- [ ] **Step 2: 删除 Skill 计数器相关代码**

删除 `agent/runner.py` 第 1966-2059 行的以下内容：
- `_SKILL_SCORE_MIN`, `_SKILL_SCORE_MAX`, `_SKILL_SCORE_FIRST_HIT`, `_SKILL_SCORE_HIT_INCREMENT`, `_SKILL_SCORE_DECAY`, `_SKILL_SCORE_INJECT_THRESHOLD`, `_SKILL_INJECT_TOP_N` 常量
- `_update_skill_counter` 静态方法
- `_select_top_skills` 静态方法

同时在 `__init__` 中删除 `_skill_score_counter` 和 `_skill_entity_cache` 的初始化。

- [ ] **Step 3: 重写 `_inject_dynamic_resources` 方法**

替换 `agent/runner.py` 第 2063-2239 行的整个 `_inject_dynamic_resources` 方法：

```python
    def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
        """动态注入相关资源 — 向量检索 + 图遍历 + Ebbinghaus 衰减池。

        流程:
        1. 脑区激活 (不变)
        2. 全局向量检索
        3. 衰减池维护 (先衰减旧实体，再注入新命中)
        4. 衰减池注入：全局检索命中 (score=distance)
        5. 图遍历 (从 hit entities 沿知识边1跳) → 衰减池注入 (score=hit×0.8)
        6. 格式化注入 (脑区状态图 + skill + knowledge + 活跃脑区知识 + 习惯)
        """
        # 0. Brain region activation (不变)
        _brain_injector = None
        try:
            _brain_injector = self._get_brain_injector()
            if _brain_injector is not None:
                _brain_injector.activate_for_query(context)
        except Exception as e:
            logger.warning(f"Brain activation failed: {e}")

        # 1. LightRAG 全局检索
        lightrag_results: dict[str, list[dict]] = {}
        adapter = None
        try:
            if self._brain_adapter is not None:
                adapter = self._brain_adapter
            else:
                from niu_api.internal.lightrag_adapter import LightRAGAdapter
                adapter = LightRAGAdapter()
            lightrag_results = adapter.search_multi_lightrag(
                context, mode="local", top_k=10, keywords=[context],
            )
        except Exception as e:
            logger.warning(f"LightRAG retrieval failed: {e}")

        # 2. 衰减池维护（先衰减旧实体，再注入新命中）
        self._decay_pool.decay()

        # 3. 衰减池注入：全局检索命中的实体
        all_hits = []  # 用于图遍历
        for category, entities in lightrag_results.items():
            for i, entity in enumerate(entities):
                name = entity.get("entity_name", "")
                if not name:
                    continue
                # distance fallback: 旧版 lightrag-hku 没有 distance 字段
                distance = entity.get("distance")
                if distance is None:
                    distance = 1.0 - (i / max(len(entities), 1)) * 0.5
                self._decay_pool.inject(
                    entity_name=name,
                    entity_dict=entity,
                    category=category,
                    source="vector",
                    vector_score=distance,
                )
                all_hits.append(name)

        # 4. 图遍历：从 hit entities 沿知识边1跳
        traversed: dict[str, dict] = {}
        try:
            if all_hits:
                traversed = self._traverse_from_hits(all_hits)
        except Exception as e:
            logger.warning(f"Graph traversal failed: {e}")

        # 图遍历结果注入衰减池
        # 建立 entity_name -> distance 映射（从全局检索结果）
        hit_distance_map: dict[str, float] = {}
        for category, entities in lightrag_results.items():
            for i, entity in enumerate(entities):
                name = entity.get("entity_name", "")
                if name:
                    distance = entity.get("distance")
                    if distance is None:
                        distance = 1.0 - (i / max(len(entities), 1)) * 0.5
                    hit_distance_map[name.lower()] = distance

        for entity_name, node_data in traversed.items():
            source = node_data.get("source", "")
            if source == "hit":
                # hit 实体已在 step 3 注入，跳过
                continue
            # 如果实体已通过向量检索注入（source="vector"），不覆盖（避免 source 被改为 graph_traversal）
            existing_entry = self._decay_pool._entries.get(entity_name)
            if existing_entry is not None and existing_entry.source == "vector":
                continue
            # 邻居实体：用 hit 分数 × 0.8
            # 如果邻居自己也有 distance（在全局检索结果中），用真实分数
            own_distance = hit_distance_map.get(entity_name)
            if own_distance is not None:
                neighbor_score = own_distance
            else:
                # 找到关联的 hit 实体的分数 × 0.8
                # 从 source 提取关联 hit（无法直接知道，用 0.8 × 平均 hit 分数近似）
                neighbor_score = 0.8 * (
                    sum(hit_distance_map.values()) / max(len(hit_distance_map), 1)
                    if hit_distance_map else 0.5
                )

            # 确定 category
            entity_type = (node_data.get("entity_type") or "").lower()
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            category = LightRAGAdapter._ENTITY_TYPE_TO_CATEGORY.get(entity_type, "knowledge")

            self._decay_pool.inject(
                entity_name=entity_name,
                entity_dict=node_data,
                category=category,
                source="graph_traversal",
                vector_score=neighbor_score,
            )

        # ============== Format & Inject ==============
        parts: list[str] = []
        seen_names: set[str] = set()

        # Brain region status map (不变)
        try:
            if _brain_injector is not None:
                brain_context = _brain_injector.format_region_map_only()
                if brain_context:
                    parts.append(f"\n{brain_context}")
        except Exception as e:
            logger.warning(f"Brain region map injection failed: {e}")

        # Skills (从衰减池取 category=skill)
        skill_entries = self._decay_pool.get_top_by_category("skill", 5)
        if skill_entries:
            skill_entities = [e.entity_dict for e in skill_entries]
            skills_text, seen_names = self._format_lightrag_entities_for_prompt(
                skill_entities, "相关技能", seen_names,
            )
            if skills_text:
                parts.append(skills_text)

        # Knowledge (从衰减池取 category=knowledge)
        knowledge_entries = self._decay_pool.get_top_by_category("knowledge", 10)
        if knowledge_entries:
            knowledge_entities = [e.entity_dict for e in knowledge_entries]
            knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
                knowledge_entities, "参考知识", seen_names,
            )
            if knowledge_text:
                parts.append(knowledge_text)

        # 活跃脑区知识 (从衰减池取 source=graph_traversal)
        region_entries = self._decay_pool.get_top_by_source("graph_traversal", 5)
        if region_entries:
            # 格式化：标注来源 (hit / 邻居关系)
            region_lines: list[str] = []
            for entry in region_entries:
                name = entry.entity_dict.get("entity_name", entry.entity_name)
                if name in seen_names:
                    continue
                # 黑名单过滤（与 _format_lightrag_entities_for_prompt 一致）
                entity_type = (entry.entity_dict.get("entity_type") or "").lower()
                if entity_type in self._INJECT_ENTITY_TYPE_BLACKLIST:
                    continue
                name_lower = name.lower()
                if name_lower in {n.lower() for n in self._INJECT_ENTITY_NAME_BLACKLIST}:
                    continue
                seen_names.add(name)
                desc = entry.entity_dict.get("description", "")
                source = entry.entity_dict.get("source", "")
                if source == "hit":
                    source_label = "(hit)"
                elif source.startswith("neighbor:"):
                    relation = source.split(":", 1)[1]
                    source_label = f"(邻居: {relation})"
                else:
                    source_label = ""
                desc_line = f"   {desc[:200]}" if desc else ""
                region_lines.append(f"{len(region_lines)+1}. **{name}** {source_label}\n{desc_line}")
            if region_lines:
                parts.append("### [活跃脑区知识]\n" + "\n".join(region_lines))
                parts.append(
                    "\n\n### [知识探索指引]\n"
                    "优先参考上述活跃脑区知识回答用户问题，脑区内容与你当前关注领域最相关。"
                )

        # Interaction habits (从衰减池取 category=interactionhabit)
        habit_entries = self._decay_pool.get_top_by_category("interactionhabit", 3)
        if habit_entries:
            habit_entities = [e.entity_dict for e in habit_entries]
            habits_text, seen_names = self._format_lightrag_entities_for_prompt(
                habit_entities, "交互习惯", seen_names,
            )
            if habits_text:
                parts.append(habits_text)

        logger.debug(
            f"Dynamic injection | pool_size={len(self._decay_pool)}, "
            f"skills={len(skill_entries)}, knowledge={len(knowledge_entries)}, "
            f"region={len(region_entries)}, habits={len(habit_entries)}"
        )

        injection = "\n".join(parts)
        if injection:
            logger.debug(f"Dynamic injection - Total length: {len(injection)} chars")
        else:
            logger.debug("Dynamic injection - Skipped (no relevant results)")

        # 阶段二：注入后台子 Agent 清单
        subagent_section = self._format_running_subagents_section()
        if subagent_section:
            injection = (injection + "\n\n" + subagent_section) if injection else subagent_section

        return injection, {}
```

- [ ] **Step 4: 在 `/new` 命令时清空衰减池**

`reset_working_memory` 在 `agent/handler.py:620` 的 `NiuHandler` 上（不在 `runner.py`），由 `niu_api/compat.py:2164` 的 `/new` 命令处理逻辑调用。在该调用之后清空衰减池。

在 `niu_api/compat.py` 约第 2164 行，`runner.handler.reset_working_memory()` 调用之后，添加 `runner._decay_pool.clear()`：

```python
        runner = get_or_create_runner()
        if runner:
            # 重置 handler 的工作记忆
            if runner.handler:
                runner.handler.reset_working_memory()
                runner.handler._last_prompt_tokens = 0

            # 清空衰减池（新会话开始）
            runner._decay_pool.clear()
```

**注意：** `runner` 是 `NiuRunner` 实例，`_decay_pool` 在 Task 6 Step 1 的 `__init__` 中初始化，此处直接访问即可。

- [ ] **Step 5: 验证方法能被调用**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "
import sys; sys.path.insert(0, '.')
# 只验证语法和 import 不报错
from agent.runner import NiuRunner
from agent.decay_pool import DecayPool
print('Import OK')
# 验证 _inject_dynamic_resources 存在
assert hasattr(NiuRunner, '_inject_dynamic_resources')
assert hasattr(NiuRunner, '_traverse_from_hits')
print('Methods exist')
"
```

Expected: Import OK, Methods exist

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "refactor: _inject_dynamic_resources 改用 DecayPool + 图遍历，废弃 Skill 计数器"
```

---

### Task 7: 版本兼容 — requirements.txt + 启动验证 + fallback

**Files:**
- Modify: `requirements.txt`
- Modify: `niu_api/__main__.py`

- [ ] **Step 1: 锁定 lightrag-hku commit hash**

在 `requirements.txt` 中，将 lightrag-hku 行改为：

```text
lightrag-hku @ git+https://github.com/abc-lee/LightRAG.git@<commit-sha>
```

将 `<commit-sha>` 替换为 Task 1 Step 5 记录的值。

- [ ] **Step 2: 新增 `check_critical_versions()` 到 `niu_api/__main__.py`**

在 `niu_api/__main__.py` 中（lifespan 函数之前），新增函数：

```python
def check_critical_versions() -> list[str]:
    """检查强制依赖的版本号，返回不匹配的警告列表。"""
    warnings = []

    # lightrag-hku 必须是 1.4.19+（含 distance 字段返回）
    try:
        import lightrag
        from packaging.version import Version
        version = Version(getattr(lightrag, "__version__", "0"))
        if version < Version("1.4.19"):
            warnings.append(
                f"lightrag-hku 版本过低 ({version})，需要 1.4.19+。"
                f"动态知识注入功能可能降级。"
            )
    except ImportError:
        warnings.append("lightrag-hku 未安装")
    except Exception:
        pass  # packaging 不可用时跳过版本检查（不阻止启动）

    return warnings
```

- [ ] **Step 3: 在 lifespan 早期调用版本检查**

在 `niu_api/__main__.py` 的 lifespan 函数最早期（Phase 1 检测之前），添加：

```python
    # 0. 版本检查（不阻止启动，仅警告）
    version_warnings = check_critical_versions()
    for w in version_warnings:
        logger.warning(f"[Version Check] {w}")
```

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add requirements.txt niu_api/__main__.py
git commit -m "feat: 版本兼容 — 锁 commit hash + 启动时版本验证 + rank fallback"
```

---

### Task 8: 更新测试文件

**Files:**
- Modify: `tests/test_inject_blacklist.py`
- Modify: `tests/test_inject_running_subagents.py`
- Modify: `tests/test_on_before_llm_method.py`
- Modify: `tests/test_region_case_insensitive.py`
- Modify: `tests/test_skill_inject_integration.py`
- Delete/Skip: `tests/test_skill_score_counter.py` — 整个文件废弃（测试的 `_update_skill_counter` 和 `_select_top_skills` 已删除）
- Modify: `tests/test_lightrag_retrieval_migration.py` — 移除 `_skill_score_counter`/`_skill_entity_cache` mock 和 `search_interaction_habits` 断言

- [ ] **Step 1: 更新 test_on_before_llm_method.py**

移除 `search_within_region` mock，改为 mock DecayPool。在文件中找到第 71 行：

```python
        mock_adapter.return_value.search_within_region.return_value = {"skill": [], "knowledge": [], "other": []}
```

删除此行。同时在 fixture（第 24-27 行附近）中，删除 `_skill_score_counter` 和 `_skill_entity_cache` 的初始化，改为初始化 DecayPool：

```python
from agent.decay_pool import DecayPool

runner = NiuRunner.__new__(NiuRunner)
runner._decay_pool = DecayPool()  # 替代旧的 _skill_score_counter / _skill_entity_cache
```

即删除 fixture 中的以下两行：

```python
runner._skill_score_counter = {}
runner._skill_entity_cache = {}
```

- [ ] **Step 2: 更新 test_inject_running_subagents.py**

移除第 25-26 行的 `search_within_region` mock：

```python
            def search_within_region(self, *args, **kwargs):
                return {"skill": [], "knowledge": [], "other": []}
```

删除这两个方法定义。

**重要：** 本文件使用 `NiuRunner.__new__(NiuRunner)` 绕过 `__init__`，因此 `_decay_pool` 未被初始化。必须在创建 runner 实例后立即添加：

```python
from agent.decay_pool import DecayPool

# ... 在创建 runner 的地方 ...
runner = NiuRunner.__new__(NiuRunner)
runner._decay_pool = DecayPool()  # __new__ 不调 __init__，需手动初始化
```

否则 `_inject_dynamic_resources` 调用 `self._decay_pool.inject(...)` 时会抛 `AttributeError`。

- [ ] **Step 3: 更新 test_inject_blacklist.py**

更新第 10 行的注释，将"通过 search_within_region 检索"改为"通过图遍历检索"。

- [ ] **Step 4: 更新 test_region_case_insensitive.py**

将 `test_search_within_region_member_set_is_lower` 和 `test_search_within_region_handles_none_entity_name` 标记为 deprecated 或删除（因为 `search_within_region` 不再被调用）。

在文件顶部添加注释：

```python
# NOTE: search_within_region 测试已废弃（该方法不再被 _inject_dynamic_resources 调用）。
# 方法本身保留在 lightrag_adapter.py 中但无调用者。
# 图遍历测试见 test_traverse_from_hits。
```

- [ ] **Step 5: 更新 test_skill_inject_integration.py**

将第 42 行的 `search_within_region` mock 改为 DecayPool mock。Skill 计数器已废弃，测试改为验证 DecayPool 中 skill 类型的注入和 top_n。删除所有引用 `runner._skill_score_counter` 和 `runner._skill_entity_cache` 的旧测试函数（`test_inject_updates_counter_on_first_hit`、`test_inject_accumulates_counter_across_rounds`、`test_inject_decays_non_hit_skills`、`test_inject_second_stage_filters_below_3`、`test_inject_second_stage_sorts_by_score_desc`、`test_inject_uses_cache_when_not_hit_this_round`），替换为以下基于 DecayPool 的新测试。

**重要：** 本文件同样使用 `NiuRunner.__new__(NiuRunner)` 绕过 `__init__`，必须手动初始化 `_decay_pool`。在 fixture（`runner`）中删除 `_skill_score_counter` 和 `_skill_entity_cache` 的初始化，改为：

```python
from agent.decay_pool import DecayPool

# ... 在 fixture 中 ...
runner = NiuRunner.__new__(NiuRunner)
runner._decay_pool = DecayPool()  # 替代旧的 _skill_score_counter / _skill_entity_cache
```

同时将 `_make_mock_adapter` 中的 `search_within_region` mock 删除（该方法不再被调用），保留 `search_multi_lightrag` mock。

新增以下测试函数（替换旧的计数器测试）：

```python
def test_skill_injected_into_decay_pool_retrievable(runner):
    """skill 实体被注入衰减池后能通过 get_top_by_category("skill") 检索到。"""
    from agent.decay_pool import DecayPool

    # 直接通过 DecayPool 注入 skill 实体
    skill_entity = _make_skill_entity("定时任务管理", "管理定时任务的创建和查询")
    skill_entity["distance"] = 0.85
    runner._decay_pool.inject(
        entity_name="定时任务管理",
        entity_dict=skill_entity,
        category="skill",
        source="vector",
        vector_score=0.85,
    )

    # 通过 get_top_by_category 检索
    top_skills = runner._decay_pool.get_top_by_category("skill", 5)
    assert len(top_skills) == 1, f"应有1个 skill，实际 {len(top_skills)}"
    assert top_skills[0].entity_name == "定时任务管理"
    assert top_skills[0].category == "skill"
    assert top_skills[0].source == "vector"
    assert abs(top_skills[0].score - 0.85) < 0.01


def test_low_score_skill_evicted_after_decay(runner):
    """衰减后低分 skill 被淘汰（低于 DECAY_THRESHOLD=0.35）。"""
    from agent.decay_pool import DecayPool, DECAY_FACTOR, DECAY_THRESHOLD

    # 注入一个中等分数的 skill（0.5），衰减几轮后应低于阈值被淘汰
    skill_entity = _make_skill_entity("临时技能", "临时技能描述")
    runner._decay_pool.inject(
        entity_name="临时技能",
        entity_dict=skill_entity,
        category="skill",
        source="vector",
        vector_score=0.5,
    )

    # 验证初始在池中
    assert len(runner._decay_pool.get_top_by_category("skill", 5)) == 1

    # 衰减直到低于阈值
    # 0.5 * 0.819^n < 0.35  =>  n >= 3 (0.5*0.819^3 = 0.275 < 0.35)
    runner._decay_pool.decay()  # 0.5*0.819 = 0.410 >= 0.35, 仍在
    assert len(runner._decay_pool.get_top_by_category("skill", 5)) == 1, "轮1后应仍在"

    runner._decay_pool.decay()  # 0.5*0.819^2 = 0.336 < 0.35, 淘汰
    top = runner._decay_pool.get_top_by_category("skill", 5)
    assert len(top) == 0, f"轮2后应被淘汰，实际还有 {len(top)}"
```

**注意：** 如果 `_make_skill_entity` 的签名或返回格式与上述测试不匹配，按实际代码调整。关键是验证 DecayPool 的注入和检索行为，而非具体的 entity dict 结构。

- [ ] **Step 6: 废弃 test_skill_score_counter.py**

该文件测试的 `_update_skill_counter` 和 `_select_top_skills` 静态方法已在 Task 6 Step 2 中删除，因此整个文件废弃。在文件顶部添加 skip 标记（保留文件以便日后参考，但不再运行）：

```python
import pytest

pytestmark = pytest.mark.skip(reason="Skill 计数器已废弃（Task 6 删除 _update_skill_counter / _select_top_skills），改用 DecayPool")
```

或者直接删除该文件（`git rm tests/test_skill_score_counter.py`）。

- [ ] **Step 7: 更新 test_lightrag_retrieval_migration.py**

该文件引用了即将删除的 `_skill_score_counter` / `_skill_entity_cache` 属性和 `search_interaction_habits` 方法。需要：

1. **移除 `_skill_score_counter` / `_skill_entity_cache` mock**：删除所有设置 `runner._skill_score_counter = ...` 和 `runner._skill_entity_cache = ...` 的行。改为初始化 DecayPool：

```python
from agent.decay_pool import DecayPool
runner._decay_pool = DecayPool()  # 替代旧的 _skill_score_counter / _skill_entity_cache
```

2. **删除 `test_calls_search_interaction_habits` 测试方法**：该测试的全部目的就是断言 `search_interaction_habits` 被调用，新代码不再调用它。**删除整个 `test_calls_search_interaction_habits` 方法**（约第 398-410 行）。同时删除其余测试中对 `search_interaction_habits` 的 mock 设置（`mock_adapter.search_interaction_habits.return_value = []`）和返回值断言。

3. **更新 `_inject_dynamic_resources` 测试逻辑**：将原来验证 `search_within_region` / skill 计数器的断言改为验证 DecayPool 注入结果。例如：

```python
# 旧断言（删除）：
# assert runner._skill_score_counter[...] == ...
# assert mock_adapter.search_interaction_habits.called

# 新断言：
pool = runner._decay_pool
skills = pool.get_top_by_category("skill", 10)
assert len(skills) > 0, "应注入 skill 实体到衰减池"
```

- [ ] **Step 8: 运行测试**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_on_before_llm_method.py tests/test_inject_running_subagents.py tests/test_inject_blacklist.py tests/test_skill_inject_integration.py tests/test_skill_score_counter.py tests/test_lightrag_retrieval_migration.py -v 2>&1 | tail -30
```

Expected: 所有测试通过或 skip（deprecated 测试）。

- [ ] **Step 9: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add tests/
git commit -m "test: 更新测试适配 DecayPool + 图遍历（移除 search_within_region mock）"
```

---

### Task 9: 最终代码审查

**Files:**
- Review: `agent/runner.py`, `agent/decay_pool.py`, `niu_api/internal/lightrag_adapter.py`, `niu_api/__main__.py`, `requirements.txt`, `tests/`

- [ ] **Step 1: 派 code-reviewer 子 Agent 审查**

审查重点：
1. DecayPool 的 inject 覆盖逻辑是否正确（不降分规则）
2. 图遍历的 edge_data 处理是否兼容 multigraph 和普通 graph
3. distance fallback 是否正确（旧版用户不崩溃）
4. _inject_dynamic_resources 的 category 分流是否正确
5. 脑区激活/衰减/spillover 是否完全未被影响
6. Skill 计数器代码是否完全删除（无残留引用）
7. 测试是否全部通过

- [ ] **Step 2: 修复审查发现的问题**

根据审查反馈修复。

- [ ] **Step 3: 最终提交**

```bash
cd /Users/lilei/tools/ai-bot
git add -A
git commit -m "chore: 最终审查修复"
```

---

## Self-Review

### Spec 覆盖检查

| Spec Section | Task | 状态 |
|--------------|------|------|
| 2.1 Context 提取策略 | Task 4 | ✅ |
| 2.2 统一 Ebbinghaus 衰减池 | Task 2 + Task 6 | ✅ |
| 2.3 脑区知识图遍历 | Task 5 + Task 6 | ✅ |
| 2.3.4 废弃代码 | Task 6 (Skill计数器) + Task 4 (死代码) | ✅ |
| 2.4 LightRAG Fork 修改 | Task 1 | ✅ |
| 3. 整体流程 | Task 6 | ✅ |
| 7. 版本兼容方案 | Task 7 | ✅ |
| 测试更新 | Task 8 | ✅ |

### Placeholder 扫描
- 无 TBD/TODO
- 所有 Step 都有完整代码或具体命令
- commit hash 占位符 `<commit-sha>` 在 Task 1 Step 5 明确要求记录，Task 7 Step 1 明确要求替换

### 类型一致性
- DecayPool.inject() 签名在 Task 2 定义，Task 6 调用——参数一致
- _traverse_from_hits() 在 Task 5 定义，Task 6 调用——返回值类型一致
- _ENTITY_TYPE_TO_CATEGORY 在 Task 3 修改，Task 6 使用——category 值一致
