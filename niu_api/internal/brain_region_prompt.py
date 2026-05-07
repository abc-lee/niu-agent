"""
Brain region prompt injection for LightRAG LLM extraction requests.

When LightRAG calls the LLM to extract entities/relationships, we inject
brain region architecture information so the LLM considers brain regions
when building the knowledge graph.
"""

from loguru import logger

BRAIN_REGION_MARKER = "Knowledge Graph Specialist"

_STATIC_BRAIN_REGION_PROMPT = """\
## 大脑区域架构

知识图谱使用"大脑区域"来组织相关实体。脑区由 Leiden 社区检测算法自动创建和管理，你**不得**创建或修改脑区节点。

### 禁止事项

- **禁止创建** `brain:region:*` 节点。脑区由算法根据社区检测自动创建（至少10个实体才形成脑区）。
- **禁止创建** `brain_region_anchor` 边。锚点边由算法在创建脑区时自动建立。
- **禁止嵌套脑区**。`brain:region:*` 只能通过 `brain_region_anchor` 连接到 `brain:Niu`，不能连接到其他 `brain:region:*`。

### 实体归入脑区规则

提取实体时，通过 `belongs_to_region` 边将实体连接到现有脑区：

- 唯一合法：实体 → `belongs_to_region` → `brain:region:*`
- 禁止：实体 → `belongs_to_region` → `brain:Niu`（实体不能直接挂根节点）
- 每个实体只能属于一个脑区（只能有一条 `belongs_to_region` 边）
- 如果实体不属于任何现有脑区，不要强行归入，也不要创建新脑区。算法会在后续自动处理。

### 默认脑区

绝大多数实体应归入以下三个默认脑区之一：
- `brain:region:聊天历史` — 聊天对话中产生的实体
- `brain:region:文档库` — 来自文档内容的实体
- `brain:region:知识体系` — 结构化知识、概念、技术栈、工作项目等实体"""


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
            "brain:region",
            mode="local",
            only_need_context=True,
        )

        if result and result.strip():
            return f"当前图谱中的脑区：\n{result.strip()}"
    except Exception as e:
        logger.debug("Brain region dynamic query failed, using fallback: %s", e)

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
        if msg.get("role") == "system" and BRAIN_REGION_MARKER in msg.get("content", ""):
            new_msg = {**msg, "content": msg.get("content", "") + injection}
            result.append(new_msg)
        else:
            result.append(msg)

    return result
