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

from niu_api.internal.lightrag_manager import get_lightrag, get_brain_regions

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"

_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱中存在脑区节点（`xxx脑区`），脑区通过 `_region:contains` 边包含其成员实体（方向：脑区→成员），脑区通过 `brain_region_anchor` 边连接到根节点 `Niu`。

### 禁止事项

- **禁止创建脑区节点**。脑区由 Leiden 社区检测算法自动创建和管理（至少100个实体才形成脑区），你不得创建或修改脑区节点。
- **禁止创建 `brain_region_anchor` 边**。锚点边由算法在创建脑区时自动建立。
- **禁止实体直接连接到 `Niu` 根节点**。`Niu` 只能与脑区节点通过 `brain_region_anchor` 边连接，实体不能直接挂根节点。实体应通过 `_region:contains` 边归入脑区。

### 实体归入脑区规则

提取实体时，根据语义归入对应脑区：

绝大多数实体应归入以下三个默认脑区之一：
- `聊天历史脑区` — 聊天对话中产生的实体
- `文档库脑区` — 来自文档内容的实体
- `知识体系脑区` — 结构化知识、概念、技术栈、工作项目等实体

无法判断的归入"知识体系脑区"。

## 命名约定

- `未命名人物_{n}` 是人物实体的临时命名方法，命名后会改为真实姓名。
- 如果图谱中已存在同名实体，更新描述即可，不要创建新实体。

## 照片实体保护

- 不要创建照片(Photo)类型的实体。照片实体由入库程序自动创建，包含照片文件路径等属性。如果你创建了一个新的照片实体，它没有照片文件，前端无法预览，是一个空壳。
- 如果文档中提到了照片，你可以创建其他类型的实体（如地点、事件、人物等），然后用关系连接到已有的照片实体。
- 修改已有实体时，保留原有描述，用 | 追加新内容，不要覆盖。照片实体的文件路径属性绝对不能修改。"""


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


FALLBACK_REGIONS = "聊天历史、文档库、知识体系"


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
