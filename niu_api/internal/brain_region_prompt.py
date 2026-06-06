"""
Brain region prompt injection for LightRAG LLM extraction requests.

When LightRAG calls the LLM to extract entities/relationships, we inject
brain region architecture information so the LLM considers brain regions
when building the knowledge graph.

IMPORTANT: This module MUST NOT call any LightRAG API (adapter.query,
call_async, rag.aquery, etc.) because it is invoked from the LLM proxy,
which is called by LightRAG itself. Calling LightRAG from here would cause
an event loop deadlock:
  LightRAG query -> LLM call -> llm_proxy -> build_dynamic_brain_region_prompt()
  -> adapter.query() -> call_async(rag.aquery()) -> same event loop -> DEADLOCK

Instead, we read brain region entities directly from the NetworkX in-memory
graph (rag.chunk_entity_relation_graph._graph). This is a pure synchronous
read that does NOT enter the asyncio event loop, so no deadlock can occur.
"""

from loguru import logger

from niu_api.internal.lightrag_manager import get_brain_regions

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"

_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构（追加规则，不影响上方已有的提取规则）

### 什么是 Niu

`Niu` 是知识图谱的根节点，代表用户本人。它的类型是 `person`（不可修改），描述是用户画像。Niu 只通过语义边连接到用户的偏好、技能和知识实体——允许的关系类型为：`remembers`（记住人物）、`skilled_in`（擅长技能）、`knows_about`（了解概念）、`uses`（使用工具）、`prefers`（明确偏好）。**Niu 不是万能连接点，不要把所有实体都连到 Niu 上。**

### 什么是脑区

脑区是知识图谱中模仿人脑功能的组织结构。就像人的大脑在专注某项工作时，相关脑区会被"点亮"——检索优先级提高、相关知识更容易被想起——知识图谱中的脑区也遵循同样的机制：当你提问涉及某个领域时，对应脑区的实体和关系会优先出现在检索结果中。

### 脑区的结构

脑区是图谱中的特殊实体节点，名称格式为 `XXX脑区`，类型为 `brainregion`。每个脑区通过 `_region:contains` 边包含一组语义相关的实体，方向**必须是脑区→实体**（source=脑区，target=实体）。例如：

```
知识体系脑区
  ├── _region:contains → Python
  ├── _region:contains → NumPy
  └── _region:contains → 数据分析

人际关系脑区
  ├── _region:contains → 小明
  └── _region:contains → 安安
```

脑区是**动态的**——随着知识积累，系统会自动发现新的社区并创建新脑区（如"Python开发脑区"），也会合并或消解不再活跃的脑区。当前存在的脑区列表会在下面动态注入。

### 实体归入脑区

提取实体时，如果你能判断实体属于哪个脑区，可以用 `_region:contains` 边将实体归入对应脑区（source=脑区名，target=实体名）。如果不确定应归入哪个脑区，**不要创建 `_region:contains` 边**，后续流程会自动处理。

### 禁止事项

- **不要创建新的脑区节点**。脑区由系统算法自动创建和管理。
- **不要修改 Niu 节点**。Niu 是根节点，代表用户本人，描述固定，不要修改它的类型或描述。
- **提取时实体严禁直接连接到 Niu 节点**。Niu 只连用户的偏好/技能/知识（remembers/skilled_in/knows_about/uses/prefers），其他实体不要直连 Niu。程序会在后续流程自动为合适的实体创建 Niu anchor 边，你不需要做。

## 命名约定

- `未命名人物_{n}` 是人物实体的临时命名方法，命名后会改为真实姓名。
- 如果图谱中已存在同名实体，**不要覆盖原有描述，用 `<SEP>` 分隔符追加新内容**。例如：原有描述是"Python编程语言"，你要补充"用户主要用于AI开发"，新描述应写成"Python编程语言<SEP>用户主要用于AI开发"。

## 照片实体保护

- 不要创建照片(photo)类型的实体。照片实体由入库程序自动创建，包含照片文件路径等属性。如果你创建了一个新的照片实体，它没有照片文件，前端无法预览，是一个空壳。
- 如果文档中提到了照片，你可以创建其他类型的实体（如地点、事件、人物等），然后用关系连接到已有的照片实体。
- 修改已有实体时，保留原有描述，用 `<SEP>` 追加新内容，不要覆盖。照片实体的文件路径属性绝对不能修改。"""


def is_lightrag_extraction_request(messages: list[dict]) -> bool:
    """Detect whether a message list is a LightRAG extraction request.

    LightRAG extraction requests always have a system prompt starting with
    '---Role---\nYou are a Knowledge Graph Specialist...'.
    """
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if BRAIN_REGION_MARKER in content:
                return True
    return False


def _get_fallback_regions_text() -> str:
    from niu_api.internal.region_manager import get_default_regions_config, REGION_SUFFIX
    defaults = get_default_regions_config()
    names = [f"{d['label']}{REGION_SUFFIX}" for d in defaults]
    return "、".join(names)

FALLBACK_REGIONS = _get_fallback_regions_text()


def build_dynamic_brain_region_prompt() -> str:
    """Build dynamic brain region list by reading the NetworkX in-memory graph.

    Uses get_brain_regions() which directly reads the graph without calling
    LightRAG API, avoiding potential event loop deadlocks.

    Returns:
        A string listing current brain regions from the graph,
        or fallback defaults if LightRAG is unavailable or graph is empty.
    """
    brain_regions = get_brain_regions()

    if brain_regions:
        logger.debug("Found %d brain regions from graph: %s", len(brain_regions), brain_regions)
        region_str = "、".join(brain_regions)
        return f"当前图谱中的脑区：\n{region_str}"
    else:
        logger.debug("No brain regions found, using fallback")
        return f"当前图谱中的脑区（默认）：{FALLBACK_REGIONS}"


def build_static_brain_region_prompt() -> str:
    """Return the static part of the brain region prompt.

    This prompt explains the brain region architecture and rules for the LLM
    to follow when extracting entities and relationships for LightRAG.
    """
    return _STATIC_BRAIN_REGION_PROMPT


def inject_brain_region_context(
    messages: list[dict],
) -> list[dict]:
    """Inject brain region architecture info into LightRAG extraction requests.

    If the messages are a LightRAG extraction request, appends brain region
    context to the system prompt. Otherwise, returns messages unchanged.

    Returns a NEW list — non-injection path returns a shallow copy.

    Args:
        messages: LiteLLM-format message list.

    Returns:
        New message list with brain region context injected (or original if
        not an extraction request).
    """
    if not is_lightrag_extraction_request(messages):
        return list(messages)

    # Build injection content
    static_part = build_static_brain_region_prompt()
    dynamic_part = build_dynamic_brain_region_prompt()
    injection = f"\n\n{static_part}\n\n{dynamic_part}"

    # Create new list with modified system prompt
    result = []
    for msg in messages:
        if msg.get("role") == "system" and BRAIN_REGION_MARKER in msg.get("content", ""):
            new_msg = {**msg, "content": msg.get("content", "") + injection}
            result.append(new_msg)
        else:
            result.append(msg)

    return result
