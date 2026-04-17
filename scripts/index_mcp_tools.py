"""
索引MCP工具描述到向量库

用途：
- 将主Agent基础MCP工具写入向量库
- 支持向量递归检索

遵循规范：
- docs/spec-L1-summary.md
- docs/design-vector-recursive-query.md

使用：
    python scripts/index_mcp_tools.py
"""

import sys
import sqlite3
import json
import numpy as np
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agent.mcp_loader import load_mcp_tools

# 主Agent基础MCP工具列表
MAIN_AGENT_MCP_TOOLS = [
    # memory-server (6个)
    "memory-server/remember",
    "memory-server/recall",
    "memory-server/update_memory",
    "memory-server/get_memory_stats",
    "memory-server/cleanup_memories",
    "memory-server/link_memories",

    # vector-store (5个)
    "vector-store/add_document",
    "vector-store/search_documents",
    "vector-store/get_document",
    "vector-store/delete_document",
    "vector-store/list_documents",
]


def get_embedding(text: str) -> list[float] | None:
    """获取文本向量"""
    from niu_api.internal.embedding import get_model

    model = get_model()
    if model is None:
        return None

    embedding = model.encode([text], normalize_embeddings=True)
    return embedding[0].tolist()


def format_tool_description(tool: dict) -> str:
    """
    格式化工具描述为L1标准格式

    格式：{tool_name}: {description} | Parameters: {param_names}
    """
    tool_name = tool.get("name", "")
    description = tool.get("description", "")
    input_schema = tool.get("input_schema", {})

    # 提取参数名
    properties = input_schema.get("properties", {})
    param_names = list(properties.keys())

    # 构造L1内容
    content = f"{tool_name}: {description}"

    if param_names:
        content += f" | Parameters: {', '.join(param_names)}"

    return content


def index_mcp_tools():
    """索引MCP工具到向量库"""

    print("=== 开始索引MCP工具 ===\n")

    # 1. 加载MCP工具
    print("1. 加载MCP工具...")
    registry = load_mcp_tools()
    all_tools = registry.get_schemas()
    print(f"   总工具数: {len(all_tools)}")

    # 2. 过滤主Agent基础工具
    print("\n2. 过滤主Agent基础工具...")
    main_agent_tools = []
    for tool in all_tools:
        tool_name = tool.get("name", "")
        if tool_name in MAIN_AGENT_MCP_TOOLS:
            main_agent_tools.append(tool)
            print(f"   ✓ {tool_name}")

    print(f"\n   主Agent基础工具数: {len(main_agent_tools)}")

    # 3. 连接向量库数据库
    print("\n3. 连接向量库...")
    from agent.vector_search import resolve_vector_db_path
    db_path = Path(resolve_vector_db_path())
    if not db_path.exists():
        print(f"   ✗ 数据库不存在: {db_path}")
        return 0, len(main_agent_tools)

    conn = sqlite3.connect(str(db_path))
    print("   ✓ 数据库连接成功")

    # 4. 写入向量库
    print("\n4. 写入向量库...")
    success_count = 0
    error_count = 0

    for tool in main_agent_tools:
        tool_name = tool.get("name", "")
        server = tool_name.split("/")[0] if "/" in tool_name else "unknown"
        simple_name = tool_name.split("/")[1] if "/" in tool_name else tool_name

        try:
            # 格式化内容
            content = format_tool_description(tool)

            # 构造metadata
            metadata = {
                "level": "l1",
                "category": "mcp_tool",
                "language": "en",  # 统一英文
                "name": simple_name,
                "server": server,
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {}),
            }

            # 获取embedding
            print(f"   生成向量: {tool_name}...")
            embedding = get_embedding(content)
            embedding_blob = None
            if embedding:
                embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

            # 写入数据库
            doc_id = f"mcp_tool:{server}:{simple_name}"
            metadata_json = json.dumps(metadata)

            conn.execute(
                "INSERT OR REPLACE INTO documents (id, content, embedding, metadata) VALUES (?, ?, ?, ?)",
                (doc_id, content, embedding_blob, metadata_json),
            )
            conn.commit()

            success_count += 1
            print(f"   ✓ {tool_name}")

        except Exception as e:
            error_count += 1
            print(f"   ✗ {tool_name}: {e}")

    conn.close()

    # 5. 统计结果
    print("\n=== 索引完成 ===")
    print(f"成功: {success_count}")
    print(f"失败: {error_count}")
    print(f"总计: {len(main_agent_tools)}")

    # 6. 验证
    print("\n=== 验证索引 ===")
    from agent.vector_search import get_vector_search
    vs = get_vector_search()

    for test_query in ["recall memory", "search documents", "save memory"]:
        results = vs.search(
            query=test_query,
            limit=3,
            min_score=0.3,
            filter={"category": "mcp_tool"}
        )
        print(f"\n查询: '{test_query}'")
        if results:
            for i, r in enumerate(results, 1):
                score = getattr(r, 'score', 0)
                title = getattr(r, 'metadata', {}).get('name', 'N/A')
                print(f"  {i}. {title} (score: {score:.3f})")
        else:
            print("  无结果")

    return success_count, error_count


if __name__ == "__main__":
    success, error = index_mcp_tools()

    if error > 0:
        sys.exit(1)
    else:
        print("\n✅ 所有工具索引成功！")
        sys.exit(0)
