# 脑区加权检索重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将脑区点亮从"后处理加权排序"改为"检索时过滤"，让点亮脑区的成员实体在脑区范围内做语义检索，发挥图检索的结构深度优势。

**Architecture:** 两条检索路径并行——全局向量检索（top_k=10，语义最相关）+ 脑区内过滤检索（filter_lambda 限定成员范围，语义+图结构最相关）。脑区内检索通过 LightRAG Fork 暴露 `filter_lambda` 参数实现，调用侧传入激活脑区的成员实体名集合。删除无效的 `apply_activation_weight` 后处理加权。新增脑区点亮数量软控制（>5个时注入提示词）。

**Tech Stack:** Python, LightRAG (fork), NanoVectorDB (filter_lambda), NetworkX

---

## 核心原则

1. **检索时过滤，非后处理加权** — filter_lambda 在向量检索阶段就限定范围，而非检索后再调整分数
2. **两条路径互补** — 全局检索保证语义最相关，脑区检索保证领域内最相关
3. **去重不重复** — seen_names 保证同一实体只出现一次
4. **分层注入** — 高激活(>0.7)脑区注入详细内容，中激活(0.3-0.7)注入摘要
5. **软控制而非硬限制** — 脑区点亮>5个时注入提示词，由 Agent 自行决定是否关闭

---

## 执行顺序

1. **提交1**：Task 1（LightRAG Fork 暴露 filter_lambda）
2. **提交2**：Task 2（lightrag_adapter 新增 search_within_region + runner 重构注入逻辑）
3. **提交3**：Task 3（删除 apply_activation_weight + 脑区点亮数量软控制）
4. **提交4**：Task 4（验证 + 文档更新）

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `<lightrag_fork_path>/lightrag/base.py` | 向量存储抽象基类 | 修改：query() 签名加 filter_lambda |
| `<lightrag_fork_path>/lightrag/kg/nano_vector_db_impl.py` | NanoVectorDB 存储实现 | 修改：query() 透传 filter_lambda |
| `<lightrag_fork_path>/lightrag/operate.py` | LightRAG 检索核心 | 修改：_get_node_data() 透传 filter_lambda |
| `<repo_root>/niu_api/internal/lightrag_adapter.py` | LightRAG 检索适配器 | 修改：新增 search_within_region() |
| `<repo_root>/agent/runner.py` | 动态注入主流程 | 修改：_inject_dynamic_resources() 重构 |
| `<repo_root>/niu_api/internal/region_injector.py` | 脑区上下文注入器 | 修改：删除 apply_activation_weight，format_region_map 加软控制 |

---

### Task 1: LightRAG Fork 暴露 filter_lambda 参数

**Files:**
- Modify: `<lightrag_fork_path>/lightrag/base.py:261-272` — BaseVectorStorage.query() 抽象方法
- Modify: `<lightrag_fork_path>/lightrag/kg/nano_vector_db_impl.py:144-162` — NanoVectorDBStorage.query() 实现
- Modify: `<lightrag_fork_path>/lightrag/operate.py:4361-4374` — _get_node_data() 调用

- [ ] **Step 1: 修改 BaseVectorStorage.query() 抽象方法签名**

```python
# base.py:261 修改前:
@abstractmethod
async def query(
    self, query: str, top_k: int, query_embedding: list[float] = None
) -> list[dict[str, Any]]:

# 修改后:
@abstractmethod
async def query(
    self, query: str, top_k: int, query_embedding: list[float] = None,
    filter_lambda: Callable[[dict], bool] | None = None,
) -> list[dict[str, Any]]:
```

同时在 base.py 顶部确认 `Callable` 和 `Any` 已从 typing 导入（当前代码已有 `from typing import ...`，需确认包含 `Callable`）。

- [ ] **Step 2: 修改 NanoVectorDBStorage.query() 实现，透传 filter_lambda**

```python
# nano_vector_db_impl.py:144 修改前:
async def query(
    self, query: str, top_k: int, query_embedding: list[float] = None
) -> list[dict[str, Any]]:

# 修改后:
async def query(
    self, query: str, top_k: int, query_embedding: list[float] = None,
    filter_lambda: Callable[[dict], bool] | None = None,
) -> list[dict[str, Any]]:
```

```python
# nano_vector_db_impl.py:158-162 修改前:
results = client.query(
    query=embedding,
    top_k=top_k,
    better_than_threshold=self.cosine_better_than_threshold,
)

# 修改后:
results = client.query(
    query=embedding,
    top_k=top_k,
    better_than_threshold=self.cosine_better_than_threshold,
    filter_lambda=filter_lambda,
)
```

- [ ] **Step 3: 修改 _get_node_data() 透传 filter_lambda**

```python
# operate.py:4361 修改前:
async def _get_node_data(query, knowledge_graph_inst, entities_vdb, query_param, query_embedding=None):

# 修改后:
async def _get_node_data(query, knowledge_graph_inst, entities_vdb, query_param, query_embedding=None, filter_lambda=None):
```

```python
# operate.py:4372-4374 修改前:
results = await entities_vdb.query(
    query, top_k=query_param.top_k, query_embedding=query_embedding
)

# 修改后:
results = await entities_vdb.query(
    query, top_k=query_param.top_k, query_embedding=query_embedding,
    filter_lambda=filter_lambda,
)
```

- [ ] **Step 4: 在 _perform_kg_search 中传递 filter_lambda**

`_perform_kg_search` (operate.py:3575) 调用 `_get_node_data` 的两处需要传递 `filter_lambda`。

local 模式入口 (operate.py:3660-3667):
```python
# 修改前:
local_entities, local_relations = await _get_node_data(
    ll_keywords,
    knowledge_graph_inst,
    entities_vdb,
    query_param,
    query_embedding=ll_embedding,
)

# 修改后:
local_entities, local_relations = await _get_node_data(
    ll_keywords,
    knowledge_graph_inst,
    entities_vdb,
    query_param,
    query_embedding=ll_embedding,
    filter_lambda=getattr(query_param, "filter_lambda", None),
)
```

hybrid/mix 模式入口 (operate.py:3680) 同样添加 `filter_lambda=getattr(query_param, "filter_lambda", None)`。

- [ ] **Step 5: 在 QueryParam 中添加 filter_lambda 字段**

```python
# base.py:84 QueryParam 类中添加字段:
filter_lambda: Callable[[dict], bool] | None = None
"""Optional filter function for entity vector search. When provided, only entities
for which filter_lambda(data) returns True will be considered in vector retrieval.
This enables region-scoped semantic search (e.g., brain region filtered retrieval)."""
```

- [ ] **Step 5b: 修复 aquery_data() 中 data_param 构造遗漏 filter_lambda**

`lightrag.py:2791-2809` 的 `aquery_data()` 方法在构造 `data_param = QueryParam(...)` 时逐字段从 `param` 复制，但新增的 `filter_lambda` 会被遗漏，导致功能失效。

```python
# lightrag.py:2791 修改前:
data_param = QueryParam(
    mode=param.mode,
    only_need_context=True,
    only_need_prompt=False,
    response_type=param.response_type,
    stream=False,
    top_k=param.top_k,
    chunk_top_k=param.chunk_top_k,
    max_entity_tokens=param.max_entity_tokens,
    max_relation_tokens=param.max_relation_tokens,
    max_total_tokens=param.max_total_tokens,
    hl_keywords=param.hl_keywords,
    ll_keywords=param.ll_keywords,
    conversation_history=param.conversation_history,
    history_turns=param.history_turns,
    model_func=param.model_func,
    user_prompt=param.user_prompt,
    enable_rerank=param.enable_rerank,
)

# 修改后 — 添加 filter_lambda=param.filter_lambda:
data_param = QueryParam(
    mode=param.mode,
    only_need_context=True,
    only_need_prompt=False,
    response_type=param.response_type,
    stream=False,
    top_k=param.top_k,
    chunk_top_k=param.chunk_top_k,
    max_entity_tokens=param.max_entity_tokens,
    max_relation_tokens=param.max_relation_tokens,
    max_total_tokens=param.max_total_tokens,
    hl_keywords=param.hl_keywords,
    ll_keywords=param.ll_keywords,
    conversation_history=param.conversation_history,
    history_turns=param.history_turns,
    model_func=param.model_func,
    user_prompt=param.user_prompt,
    enable_rerank=param.enable_rerank,
    filter_lambda=param.filter_lambda,
)
```

同时检查 `aquery_llm()` 中是否有类似的 QueryParam 构造（`lightrag.py:3025` 附近）。如果有，同样添加 `filter_lambda=param.filter_lambda`。

- [ ] **Step 6: 语法检查**（Task 1）

- [ ] **Step 7: 推送 Fork + 重新安装到 Python 环境**

```bash
cd <lightrag_fork_path>
git add lightrag/base.py lightrag/kg/nano_vector_db_impl.py lightrag/operate.py
git commit -m "feat: expose filter_lambda in vector storage query API"
git push origin main
```

然后重新安装到运行环境：
```bash
cd <repo_root>
python -m pip install git+https://github.com/abc-lee/LightRAG.git@main --target python/lib/python3.11/site-packages --upgrade --no-deps 2>&1 | tail -5
```

验证安装：
```bash
python -c "from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage; import inspect; sig = inspect.signature(NanoVectorDBStorage.query); print('filter_lambda' in str(sig))"
```
Expected: `True`

- [ ] **Step 8: 用测试脚本验证 filter_lambda 在 LightRAG 层面生效**

修改 `scripts/test_brain_region_filtered_search.py`，将 Step 4 中的底层直接调用改为通过 LightRAG `query_data` 接口调用：

在 Step 4 部分，替换原来的底层调用为：
```python
# 通过 LightRAG aquery_data 接口测试 filter_lambda
member_set = all_member_names
filter_fn = lambda data: data.get("entity_name") in member_set

# 构造 QueryParam
from lightrag.base import QueryParam
param = QueryParam(mode="local", top_k=10, ll_keywords=[query_text], filter_lambda=filter_fn)

# 调用
filtered_result = call_async(rag.aquery_data(query_text, param=param))
filtered_entities = filtered_result.get("data", {}).get("entities", [])
```

保留全局检索对比不变，重新运行测试。

Run: `cd <repo_root> && python scripts/test_brain_region_filtered_search.py 2>&1 | grep -E "(过滤检索|全局检索|FAIL|OK|验证)"`
Expected: 过滤检索返回的实体名都在脑区成员范围内

- [ ] **Step 9: 提交**

```bash
cd <repo_root>
git add scripts/test_brain_region_filtered_search.py
git commit -m "test: update filtered search test to use LightRAG query_data API"
```

---

### Task 2: lightrag_adapter 新增 search_within_region + runner 重构注入逻辑

**前置条件**: Task 1 Step 7 必须已完成（Fork 已推送并安装到 Python 环境）

**Files:**
- Modify: `<repo_root>/niu_api/internal/lightrag_adapter.py` — 新增 search_within_region() 方法
- Modify: `<repo_root>/agent/runner.py:726-864` — _inject_dynamic_resources() 重构

- [ ] **Step 0: 前置条件验证（必须在 Task 1 Step 7 完成后执行）**

验证 LightRAG Fork 已安装且支持 filter_lambda：
```bash
python -c "from lightrag.base import QueryParam; p = QueryParam(mode='local'); p.filter_lambda = lambda x: True; print('OK')"
```
Expected: `OK`

如果失败，先执行 Task 1 Step 7（推送 Fork + 重装）。

- [ ] **Step 1: 修改 query_data() 签名支持 filter_lambda**

```python
# lightrag_adapter.py:189 修改前:
def query_data(self, query, mode="local", top_k=None, keywords=None) -> Optional[Dict[str, Any]]:

# 修改后:
def query_data(self, query, mode="local", top_k=None, keywords=None, filter_lambda=None) -> Optional[Dict[str, Any]]:
```

在 `query_data()` 内部，保持原有逐属性赋值方式，只添加 `param.filter_lambda = filter_lambda`。**不能改为一揽子构造**，因为第 234-235 行的 `hl_keywords` 根据 mode 条件赋值，改为一揽子构造会丢失 hybrid/mix 模式下的 hl_keywords：

```python
# lightrag_adapter.py:229-237 当前代码（保持不变）:
param = QueryParam(mode=mode)
if top_k is not None:
    param.top_k = top_k
if keywords:
    param.ll_keywords = keywords
    if mode in ("global", "hybrid", "mix"):
        param.hl_keywords = keywords

# 在上述代码之后新增一行:
param.filter_lambda = filter_lambda

# 然后调用:
result = call_async(rag.aquery_data(query, param=param), timeout=120)
```

- [ ] **Step 2: 新增 _categorize_results() 辅助方法**

将 `search_multi_lightrag` 中的实体分桶逻辑提取为独立方法，供 `search_within_region` 复用：

```python
def _categorize_results(self, result: dict) -> dict[str, list[dict]]:
    """Categorize query_data results into skill/knowledge/other buckets by entity_type.

    Includes same fallback logic as search_multi_lightrag for data extraction.
    """
    # Initialize all category buckets from the mapping
    buckets: dict[str, list[dict]] = {cat: [] for cat in set(_ENTITY_TYPE_TO_CATEGORY.values())}

    if not result:
        return buckets

    # Extract entities with fallback (same logic as search_multi_lightrag)
    data = result.get("data", {})
    if not data:
        data = result
    entities = data.get("entities", [])
    if not entities:
        return buckets

    for entity in entities:
        entity_type = entity.get("entity_type", "other").lower()
        category = _ENTITY_TYPE_TO_CATEGORY.get(entity_type, "knowledge")
        buckets[category].append(entity)
    return buckets
```

同时修改 `search_multi_lightrag` 调用 `_categorize_results` 而非内联分桶逻辑。

- [ ] **Step 3: 在 lightrag_adapter.py 新增 search_within_region() 方法**

在 `LightRAGAdapter` 类中（`search_multi_lightrag` 方法之后，约 line 370），添加：

```python
def search_within_region(
    self,
    query: str,
    region_member_names: set[str] | list[str],
    mode: str = "local",
    top_k: int = 10,
    keywords: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Search entities within specified brain region members only.

    Uses filter_lambda to restrict vector search to the given member entity names.
    This enables region-scoped semantic search (e.g., searching only within
    activated brain regions).

    Args:
        query: Search query text
        region_member_names: Set/list of entity names to restrict search to
        mode: LightRAG search mode (default: "local")
        top_k: Number of results to return
        keywords: Optional keywords to skip LLM extraction

    Returns:
        Dict with "skill", "knowledge" and "other" lists, same format as search_multi_lightrag
    """
    if not region_member_names:
        return {"skill": [], "knowledge": [], "other": []}

    member_set = set(region_member_names)
    filter_fn = lambda data: data.get("entity_name") in member_set

    result = self.query_data(
        query, mode=mode, top_k=top_k, keywords=keywords,
        filter_lambda=filter_fn,
    )
    if not result:
        return {"skill": [], "knowledge": [], "other": []}

    return self._categorize_results(result)
```

- [ ] **Step 4: 重构 _inject_dynamic_resources() 的脑区注入逻辑**

修改 `runner.py:726-864` 的 `_inject_dynamic_resources` 方法。

核心变更：
1. 删除 `apply_activation_weight` 调用（line 820-823）
2. 新增脑区内语义检索调用
3. 调整注入顺序和去重逻辑

```python
def _inject_dynamic_resources(self, context: str) -> tuple[str, dict[str, int]]:
    """动态注入相关资源 — 向量检索 + 脑区过滤检索。

    两条检索路径并行:
    1. 全局向量检索 (search_multi_lightrag) — 语义最相关的 top_k 实体
    2. 脑区内过滤检索 (search_within_region) — 激活脑区成员中语义最匹配的实体

    Args:
        context: 3条对话上下文
    """
    # 0. Brain region activation
    effective_query = context
    keywords = [effective_query]
    _brain_injector = None
    try:
        _brain_injector = self._get_brain_injector()
        if _brain_injector is not None:
            _brain_injector.activate_for_query(context)
    except Exception as e:
        logger.warning(f"Brain activation failed: {e}")

    # 1. LightRAG 全局检索 — local + keywords = 0 LLM calls
    lightrag_results: dict[str, list[dict]] = {}
    adapter = None
    try:
        if self._brain_adapter is not None:
            adapter = self._brain_adapter
        else:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            adapter = LightRAGAdapter()
        lightrag_results = adapter.search_multi_lightrag(
            effective_query, mode="local", top_k=10, keywords=keywords,
        )
    except Exception as e:
        logger.warning(f"LightRAG retrieval failed: {e}")

    # 2. 脑区内过滤检索 — 激活脑区成员范围内语义搜索
    region_results: dict[str, list[dict]] = {"skill": [], "knowledge": [], "other": []}
    try:
        if _brain_injector is not None:
            active_regions = _brain_injector.get_active_regions()
            if active_regions:
                # 收集所有激活脑区的成员实体名
                all_region_members = set()
                for region in active_regions:
                    members = _brain_injector.get_members_of_region(region.region_id)
                    all_region_members.update(members)
                if all_region_members:
                    # adapter 复用全局检索的实例，若全局检索失败则重新创建
                    region_adapter = adapter
                    if region_adapter is None:
                        from niu_api.internal.lightrag_adapter import LightRAGAdapter
                        region_adapter = LightRAGAdapter()
                    region_results = region_adapter.search_within_region(
                        effective_query,
                        region_member_names=all_region_members,
                        mode="local",
                        top_k=10,
                        keywords=keywords,
                    )
    except Exception as e:
        logger.warning(f"Region-filtered search failed: {e}")

    # 3. interaction_habits（LightRAG + keywords）
    interaction_habits: list[dict] = []
    try:
        if self._brain_adapter is not None:
            habit_adapter = self._brain_adapter
        else:
            from niu_api.internal.lightrag_adapter import LightRAGAdapter
            habit_adapter = LightRAGAdapter()
        interaction_habits = habit_adapter.search_interaction_habits(
            query=effective_query, top_k=3, keywords=keywords,
        )
    except Exception as e:
        logger.debug(f"Interaction habits search failed (non-blocking): {e}")

    # 4. Brain graph memory recall
    brain_memories_text = ""
    try:
        from niu_api.internal.brain_graph import get_brain_graph, format_memories_for_prompt
        bg = get_brain_graph()
        brain_memories = bg.recall_memories(context, top_k=10, min_weight=0.3, keywords=keywords)
        if brain_memories:
            brain_memories_text = format_memories_for_prompt(brain_memories)
    except Exception as e:
        logger.debug(f"Brain graph recall failed (non-blocking): {e}")

    # ============== Format & Inject ==============
    parts = []
    seen_names: set[str] = set()

    logger.debug(
        f"Dynamic injection | "
        f"Skills: {len(lightrag_results.get('skill', []))}, "
        f"Knowledge: {len(lightrag_results.get('knowledge', []))}, "
        f"Region skills: {len(region_results.get('skill', []))}, "
        f"Region knowledge: {len(region_results.get('knowledge', []))}, "
        f"Habits: {len(interaction_habits)}"
    )

    # Brain region status map (always inject)
    try:
        if _brain_injector is not None:
            brain_context = _brain_injector.format_region_map_only()
            if brain_context:
                parts.append(f"\n{brain_context}")
    except Exception as e:
        logger.warning(f"Brain region map injection failed: {e}")

    # Skills (global vector search)
    lightrag_skills = lightrag_results.get("skill", [])
    skills_text, seen_names = self._format_lightrag_entities_for_prompt(
        lightrag_skills, "相关技能", seen_names,
    )
    if skills_text:
        parts.append(skills_text)

    # Knowledge (global vector search)
    lightrag_knowledge = lightrag_results.get("knowledge", [])
    knowledge_text, seen_names = self._format_lightrag_entities_for_prompt(
        lightrag_knowledge, "参考知识", seen_names,
    )
    if knowledge_text:
        parts.append(knowledge_text)
        parts.append(
            "\n\n### [知识探索指引]\n"
            "优先参考上述注入的历史参考信息回答用户问题。"
        )

    # Region-filtered knowledge (brain region semantic search, deduped with seen_names)
    region_knowledge = region_results.get("knowledge", [])
    region_skills = region_results.get("skill", [])
    region_all = region_skills + region_knowledge
    if region_all:
        region_text, seen_names = self._format_lightrag_entities_for_prompt(
            region_all, "活跃脑区知识", seen_names,
        )
        if region_text:
            parts.append(region_text)

    # Interaction habits (LightRAG)
    if interaction_habits:
        habits_text, seen_names = self._format_lightrag_entities_for_prompt(
            interaction_habits, "交互习惯", seen_names,
        )
        if habits_text:
            parts.append(habits_text)

    # Brain memories
    brain_memories_text = _strip_lightrag_error_lines(brain_memories_text)
    if brain_memories_text:
        parts.append(brain_memories_text)

    injection = "\n".join(parts)
    if injection:
        logger.debug(f"Dynamic injection - Total length: {len(injection)} chars")
    else:
        logger.debug("Dynamic injection - Skipped (no relevant results)")

    return injection, {}
```

- [ ] **Step 5: 在 region_injector.py 新增 format_region_map_only() 和代理方法**

当前 `format_injection_text()` 包含脑区地图 + 详细/摘要内容。重构后，详细内容由脑区过滤检索替代，只需保留脑区状态地图。新增一个只返回地图的方法，同时添加两个代理方法避免 runner.py 直接访问 `_activation_mgr` 私有属性：

```python
def format_region_map_only(self) -> str:
    """Format brain region status map only, without detailed content.

    Used when detailed content is provided by region-filtered search results
    instead of the old layered injection approach.
    """
    regions = self._activation_mgr.get_region_map()
    if not regions:
        return ""
    return self.format_region_map(regions)

def get_active_regions(self) -> list[BrainRegionState]:
    """Get regions with activation > threshold."""
    return self._activation_mgr.get_active_regions()

def get_members_of_region(self, region_id: str) -> list[str]:
    """Get entity names belonging to a specific region."""
    return self._activation_mgr.get_members_of_region(region_id)
```

- [ ] **Step 6: 语法检查**

Run: `python -m py_compile <repo_root>/niu_api/internal/lightrag_adapter.py && python -m py_compile <repo_root>/agent/runner.py && python -m py_compile <repo_root>/niu_api/internal/region_injector.py`
Expected: 无输出

- [ ] **Step 7: 集成测试 — 启动应用验证注入内容**

启动应用后，在对话中触发动态注入，检查 system prompt 中的注入内容是否包含：
1. `## 脑区状态` — 状态灯地图
2. `### [相关技能]` — 全局向量检索
3. `### [参考知识]` — 全局向量检索
4. `### [活跃脑区知识]` — 脑区过滤检索（新增）
5. `### [交互习惯]` — 交互习惯
6. `### [记忆]` — 脑图记忆

特别注意：[参考知识] 和 [活跃脑区知识] 之间不应有重复实体。

- [ ] **Step 8: 提交**

```bash
cd <repo_root>
git add niu_api/internal/lightrag_adapter.py agent/runner.py niu_api/internal/region_injector.py
git commit -m "feat: add region-filtered semantic search and restructure dynamic injection"
```

---

### Task 3: 删除 apply_activation_weight + 脑区点亮数量软控制

**Files:**
- Modify: `<repo_root>/niu_api/internal/region_injector.py` — 删除 apply_activation_weight，format_region_map 加软控制
- Modify: `<repo_root>/tests/test_region_injector.py` — 更新测试

- [ ] **Step 1: 删除 apply_activation_weight() 方法**

删除 `region_injector.py` 第 306-345 行的 `apply_activation_weight` 方法。

同时删除 `runner.py` 中已不存在的调用（Task 2 的重构已移除，此步确认无残留引用）。

Run: `grep -rn "apply_activation_weight" <repo_root>/niu_api/ <repo_root>/agent/`
Expected: 无输出（确认无残留引用）

- [ ] **Step 2: 在 format_region_map() 添加脑区点亮数量软控制**

```python
# region_injector.py:158 format_region_map() 中，在构建 lines 列表后、遍历 sorted_regions 之前添加:

MAX_LIT_REGIONS = 5

# 在 lines = [f"## 脑区状态 ({len(regions)}个脑区)"] 之后添加:
lit_count = sum(1 for r in regions if r.activation > 0.3)
if lit_count > MAX_LIT_REGIONS:
    lines.append(f"> ⚠ {lit_count}个脑区已点亮，建议关闭与当前会话无关脑区以减少干扰")
```

- [ ] **Step 3: 删除 _format_injection_content 中的旧分层注入逻辑**

`_format_injection_content` (region_injector.py:390-474) 中的高激活详细内容/中激活摘要注入逻辑已被 Task 2 的脑区过滤检索替代。删除 `format_injection_text()` 中对 `_format_injection_content()` 的调用，以及 `_format_injection_content` 方法本身。

保留的方法：
- `format_region_map()` — 脑区状态地图
- `format_region_map_only()` — Task 2 新增
- `activate_for_query()` — 脑区激活

可删除的方法：
- `_format_injection_content()` — 旧分层注入
- `format_detailed_region()` — 旧高激活详细注入
- `format_summary_region()` — 旧中激活摘要注入
- `_get_members()` — 被 activation_mgr.get_members_of_region() 替代
- `_get_member_count()` — 被 `len(self._activation_mgr.get_members_of_region())` 替代
- `_get_region_description()` — 被 `self._activation_mgr.get_region_description()` 替代

**必须在删除上述方法之前**，先修改 `format_region_map()` 中对 `_get_member_count` 和 `_get_region_description` 的调用（否则删方法后 format_region_map 崩溃）：

```python
# region_injector.py:191-192 修改前:
member_count = self._get_member_count(region.region_id)
description = self._get_region_description(region.region_id)

# 修改后:
member_count = len(self._activation_mgr.get_members_of_region(region.region_id))
description = self._activation_mgr.get_region_description(region.region_id)
```

同时修改 `format_region_map_only()` 中对 `format_region_map()` 的调用——因 `format_region_map` 需要 `region_members_map` 参数，但当前代码中该参数默认为 None 且未使用，所以无需额外修改。

执行顺序：先修改 `format_region_map()` 内部调用 → 再删除旧方法。

- [ ] **Step 4: 简化 format_injection_text()**

`format_injection_text()` 的外部调用**只有一处**：`runner.py:813`（`_inject_dynamic_resources` 内部）。`inject_brain_context()` 无外部调用（仅在类文档注释中出现）。因此可以安全简化。

```python
# 修改后:
def format_injection_text(self, region_knowledge, entity_to_region, hit_entities):
    """Format brain region injection text.

    After refactoring, only returns the region status map.
    Detailed content is now provided by region-filtered search results.
    """
    regions = self._activation_mgr.get_region_map()
    if not regions:
        return ""
    return self.format_region_map(regions)
```

- [ ] **Step 5: 更新测试文件**

更新 `tests/test_region_injector.py` 中与被删除方法相关的测试：
- 删除 `apply_activation_weight` 相关测试
- 删除 `format_detailed_region` / `format_summary_region` 相关测试
- 新增 `format_region_map_only` 测试
- 新增脑区点亮数量软控制测试

```python
def test_format_region_map_warns_too_many_lit():
    """点亮超过5个脑区时应输出警告提示"""
    injector = BrainContextInjector(...)
    # 模拟6个脑区被点亮
    for i in range(6):
        injector._activation_mgr._regions[f"region_{i}"] = BrainRegionState(
            region_id=f"region_{i}", community_id="",
            label=f"测试脑区{i}", activation=0.8,
            last_activated_at=0, activation_count=1, manually_dimmed=False,
        )
    result = injector.format_region_map_only()
    assert "建议关闭" in result or "过多" in result

def test_format_region_map_no_warn_within_limit():
    """点亮5个以内脑区时不应输出警告"""
    injector = BrainContextInjector(...)
    for i in range(3):
        injector._activation_mgr._regions[f"region_{i}"] = BrainRegionState(
            region_id=f"region_{i}", community_id="",
            label=f"测试脑区{i}", activation=0.8,
            last_activated_at=0, activation_count=1, manually_dimmed=False,
        )
    result = injector.format_region_map_only()
    assert "建议关闭" not in result and "过多" not in result
```

- 确认 `inject_brain_context` 相关测试（`tests/test_region_injector.py:481-529`）在 `format_injection_text` 简化后仍通过。分析：`format_region_map()` 输出包含脑区 label（如"编程开发"），简化后 `format_injection_text()` 只返回 `format_region_map()`，`inject_brain_context` 断言 `"编程开发" in text` 仍为 True，无需修改。

- [ ] **Step 6: 语法检查**

Run: `python -m py_compile <repo_root>/niu_api/internal/region_injector.py && python -m py_compile <repo_root>/tests/test_region_injector.py`
Expected: 无输出

- [ ] **Step 7: 提交**

```bash
cd <repo_root>
git add niu_api/internal/region_injector.py tests/test_region_injector.py
git commit -m "refactor: remove apply_activation_weight and old layered injection, add region count soft limit"
```

---

### Task 4: 验证 + 文档更新

**Files:**
- Modify: `<repo_root>/docs/SYSTEM_MANUAL.md` — 工具注入机制描述
- Modify: `<repo_root>/docs/manual-vector-store.md` — 脑区同步章节

- [ ] **Step 1: 派审查Agent检查所有修改**

审查要点：
1. LightRAG Fork 的 filter_lambda 是否在 query() 签名、实现、调用链三层正确透传
2. search_within_region() 是否正确构造 filter_lambda 并调用 query_data
3. _inject_dynamic_resources() 是否正确调用脑区过滤检索和全局检索
4. seen_names 去重是否正确工作
5. apply_activation_weight 是否完全删除，无残留引用
6. 脑区点亮数量软控制是否在 format_region_map 中正确实现
7. 旧的分层注入逻辑是否完全清理

- [ ] **Step 2: 修复审查发现的问题（如有）**

- [ ] **Step 3: 更新 SYSTEM_MANUAL.md**

在 **2.2 工具注入机制** 中更新描述，反映新的双路径检索架构：

将当前的"衰减-覆盖评分模式"段落更新为：

```markdown
**脑区加权检索（双路径架构）：**
- **全局向量检索**：search_multi_lightrag，top_k=10，返回语义最相关的技能和知识
- **脑区内过滤检索**：search_within_region，在点亮脑区的成员实体范围内做语义检索，top_k=10
- 两条路径结果用 seen_names 去重，保证不重复注入
- 脑区激活度 > 0.3 的脑区参与过滤检索
- 点亮超过 5 个脑区时，注入提示建议关闭无关脑区
```

- [ ] **Step 4: 更新 manual-vector-store.md**

在脑区同步章节中更新描述，添加脑区内过滤检索机制说明：

在"缺省脑区配置化"段落之后新增：

```markdown
**脑区内过滤检索机制**：

点亮脑区后，系统通过 LightRAG 的 `filter_lambda` 参数在脑区成员范围内做语义检索，而非全图谱匹配。这确保了：
- 同一查询在不同脑区范围内返回不同结果（如"差旅费"在财务脑区匹配报销制度，在技术脑区匹配出差部署）
- 脑区成员实体通过 `_region:contains` 边维护，`get_all_region_members()` 直接从 NetworkX 图读取
- 检索结果与全局向量检索结果通过 seen_names 去重，避免重复注入
```

- [ ] **Step 5: 最终提交**

```bash
cd <repo_root>
git add docs/SYSTEM_MANUAL.md docs/manual-vector-store.md
git commit -m "docs: update manuals for region-filtered semantic search architecture"
```

---

## 自审检查

### Spec 覆盖检查

| 需求 | 对应 Task |
|------|----------|
| LightRAG Fork 暴露 filter_lambda | Task 1 |
| lightrag_adapter 新增 search_within_region | Task 2 Step 1-3 |
| runner 重构 _inject_dynamic_resources | Task 2 Step 4 |
| 删除 apply_activation_weight | Task 3 Step 1 |
| 脑区点亮数量软控制 | Task 3 Step 2 |
| 清理旧分层注入逻辑 | Task 3 Step 3-4 |
| 测试更新 | Task 3 Step 5 |
| 文档更新 | Task 4 Step 3-4 |
| 验证 | Task 4 Step 1-2 |

### Placeholder 检查

- 无 TBD、TODO、"implement later" 等
- 每个步骤都包含完整代码
- 测试代码包含具体断言

### 类型一致性检查

- `filter_lambda` 参数类型：`Callable[[dict], bool] | None` — 在 base.py、nano_vector_db_impl.py、operate.py、lightrag_adapter.py 中一致
- `search_within_region` 返回 `dict[str, list[dict]]` — 与 `search_multi_lightrag` 一致
- `region_member_names` 参数类型：`set[str] | list[str]` — 内部统一转为 set
- `format_region_map_only()` 返回 `str` — 与 `format_injection_text()` 返回类型一致
