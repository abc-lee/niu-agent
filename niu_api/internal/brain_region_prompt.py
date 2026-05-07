"""
Brain region prompt injection for LightRAG LLM extraction requests.

When LightRAG calls the LLM to extract entities/relationships, we inject
brain region architecture information so the LLM considers brain regions
when building the knowledge graph.
"""

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"

_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱使用"大脑区域"来组织相关实体。大脑区域是知识图谱中的子图，将语义相关的实体归类到同一区域。

### 核心结构

- 根节点 `brain:Niu` 代表整个知识图谱。
- 每个大脑区域通过 `brain_region_anchor` 边连接到 `brain:Niu`。
- 区域内的成员实体通过 `belongs_to_region` 边连接到所属区域。

### 默认区域

存在三个默认区域：
- `brain:region:聊天历史` — 聊天对话中产生的实体
- `brain:region:文档库` — 来自文档内容的实体
- `brain:region:知识体系` — 结构化知识和概念的实体

### 提取规则

在构建实体关系时，请考虑将实体分配到合适的大脑区域：
1. 根据实体来源判断应归属的区域。
2. 通过添加 `belongs_to_region` 边将实体连接到相应区域。
3. 如果没有合适的现有区域，可以创建新区域：创建 `brain:region:{标签}` 节点，通过 `brain_region_anchor` 边连接到 `brain:Niu`，再将实体通过 `belongs_to_region` 连接到新区域。\
"""


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


def build_dynamic_brain_region_prompt(adapter) -> str:
    """Build dynamic brain region list by querying the graph.

    Uses local mode + only_need_context=True to avoid LLM calls.
    This prevents infinite loops (proxy → query → LLM → proxy → ...).

    Args:
        adapter: LightRAGAdapter instance with query() method.

    Returns:
        A string listing current brain regions from the graph,
        or fallback defaults if the query fails.
    """
    try:
        result = adapter.query(
            "brain region nodes",
            mode="local",
            only_need_context=True,
        )

        if result and result.strip():
            return f"当前图谱中的脑区：\n{result.strip()}"
    except Exception:
        pass

    return f"当前图谱中的脑区（默认）：{FALLBACK_REGIONS}"


def build_static_brain_region_prompt() -> str:
    """Return the static part of the brain region prompt.

    This prompt explains the brain region architecture and rules for the LLM
    to follow when extracting entities and relationships for LightRAG.
    """
    return _STATIC_BRAIN_REGION_PROMPT


def inject_brain_region_context(
    messages: list[dict], adapter
) -> list[dict]:
    """Inject brain region architecture info into LightRAG extraction requests.

    If the messages are a LightRAG extraction request, appends brain region
    context to the system prompt. Otherwise, returns messages unchanged.

    Returns a NEW list — does not mutate the input.

    Args:
        messages: LiteLLM-format message list.
        adapter: LightRAGAdapter instance for querying brain regions.

    Returns:
        New message list with brain region context injected (or original if
        not an extraction request).
    """
    if not is_lightrag_extraction_request(messages):
        return messages

    # Build injection content
    static_part = build_static_brain_region_prompt()
    dynamic_part = build_dynamic_brain_region_prompt(adapter)
    injection = f"\n\n{static_part}\n\n{dynamic_part}"

    # Create new list with modified system prompt
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            new_msg = {**msg, "content": msg.get("content", "") + injection}
            result.append(new_msg)
        else:
            result.append(msg)

    return result
