"""
Brain region prompt injection for LightRAG LLM extraction requests.

When LightRAG calls the LLM to extract entities/relationships, we inject
brain region architecture information so the LLM considers brain regions
when building the knowledge graph.

IMPORTANT: This module MUST NOT call any LightRAG API (query, aquery, etc.)
because it is invoked from the LLM proxy, which is called by LightRAG itself.
Calling LightRAG from here would cause an event loop deadlock:
  LightRAG query -> LLM call -> llm_proxy -> build_dynamic_brain_region_prompt()
  -> adapter.query() -> call_async(rag.aquery()) -> same event loop -> DEADLOCK
Instead, we read the graph data directly from the JSON storage file.
"""

from pathlib import Path
from loguru import logger

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"

_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱中存在脑区节点（`xxx脑区`），脑区通过 `_region:contains` 边包含其成员实体（方向：脑区→成员），脑区通过 `brain_region_anchor` 边连接到根节点 `Niu`。

默认脑区：
- `聊天历史脑区` — 聊天对话中的实体
- `文档库脑区` — 文档内容的实体
- `知识体系脑区` — 结构化知识、概念等实体

提取实体时根据语义归入对应脑区，无法判断的归入"知识体系"。

## 命名约定

- `未命名人物_{n}` 是人物实体的临时命名方法，命名后会改为真实姓名。
- 如果图谱中已存在同名实体，更新描述即可，不要创建新实体。"""


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
    """Build dynamic brain region list by reading the graph storage file directly.

    Reads vdb_entities.json from the LightRAG storage directory and filters
    entities whose name contains '脑区' to build the current brain region list.

    This avoids calling any LightRAG API which would cause an event loop
    deadlock (see module docstring for details).

    Returns:
        A string listing current brain regions from the graph,
        or fallback defaults if the file is missing or unreadable.
    """
    try:
        entities_path = Path.home() / ".niu" / "lightrag_storage" / "vdb_entities.json"
        if not entities_path.exists():
            logger.debug("Brain region file not found: %s, using fallback", entities_path)
            return f"当前图谱中的脑区（默认）：{FALLBACK_REGIONS}"

        import json
        data = json.loads(entities_path.read_text(encoding="utf-8"))
        entity_list = data.get("data", [])

        # Filter brain region entities by name containing '脑区'
        brain_regions = [
            e["entity_name"]
            for e in entity_list
            if "脑区" in e.get("entity_name", "")
        ]

        if brain_regions:
            logger.debug("Found %d brain regions from file: %s", len(brain_regions), brain_regions)
            region_str = "、".join(brain_regions)
            return f"当前图谱中的脑区：\n{region_str}"
        else:
            logger.debug("No brain regions found in file, using fallback")
    except Exception as e:
        logger.debug("Brain region file read failed, using fallback: %s", e)

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
