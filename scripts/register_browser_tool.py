"""
注册 browser_navigate 工具到向量库
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.vector_search import get_vector_search
import json

# 工具定义
tool = {
    "server": "browser-server",
    "name": "browser_navigate",
    "description": "浏览器导航工具 - 启动浏览器并导航到指定 URL。使用场景：打开网页、访问网站、浏览页面。参数：url (目标 URL)，wait_until (等待策略)。返回导航结果。其他浏览器操作（点击、填充、截图）请使用 code_run 工具调用 BrowserManager。",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "default": "domcontentloaded"
            }
        },
        "required": ["url"]
    }
}

# 注册到向量库
vs = get_vector_search()

# 构造 L1 元数据
metadata = {
    "level": "l1",
    "category": "mcp_tool",
    "language": "zh",
    "name": f"{tool['server']}/{tool['name']}",
    "description": tool['description'],
    "source": f"mcp-servers/{tool['server']}",
    "priority": 70,
    "tags": ["browser", "navigation", "web", "playwright"],
}

# 内容
content = f"{tool['name']}: {tool['description']}"

# 添加
doc_id = f"mcp_tool:{tool['server']}/{tool['name']}"

print(f"正在注册工具: {tool['server']}/{tool['name']}")

# 检查是否已存在
conn = vs._get_connection()
cursor = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
if cursor.fetchone():
    print(f"工具已存在，将更新: {doc_id}")
else:
    print(f"工具不存在，将添加: {doc_id}")

# 获取 embedding
embedding = vs._get_embedding(content)
if embedding is None:
    print("错误：无法获取 embedding")
    sys.exit(1)

# L2 归一化
import numpy as np
vec = np.array(embedding, dtype=np.float32)
norm = np.linalg.norm(vec)
if norm > 0:
    vec = vec / norm
embedding_blob = vec.tobytes()

# 插入或更新
conn.execute(
    """
    INSERT INTO documents (id, content, embedding, metadata)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        content = excluded.content,
        embedding = excluded.embedding,
        metadata = excluded.metadata
    """,
    (doc_id, content, embedding_blob, json.dumps(metadata, ensure_ascii=False)),
)
conn.commit()

print(f"✓ 工具已注册: {tool['server']}/{tool['name']}")

# 验证
cursor = conn.execute("SELECT content, metadata FROM documents WHERE id = ?", (doc_id,))
row = cursor.fetchone()
if row:
    print(f"\n验证成功:")
    print(f"  Content: {row[0][:100]}...")
    print(f"  Metadata: {json.loads(row[1])['name']}")
else:
    print("错误：验证失败")
    sys.exit(1)
