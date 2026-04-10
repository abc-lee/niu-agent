"""
完整测试：照片处理端到端流程
"""
import sys
import io
from pathlib import Path

# 添加项目根目录到sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 修复编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

print("=" * 60)
print("完整测试：照片处理端到端流程")
print("=" * 60)

# Step 1: 初始化ToolRegistry
print("\n[Step 1] 初始化 ToolRegistry...")
from agent.mcp_loader import load_mcp_tools
tool_registry = load_mcp_tools()
print(f"✓ Registry loaded: {len(tool_registry.list_tools())} tools")

# Step 2: 测试file-processor子Agent获取工具
print("\n[Step 2] 测试子Agent工具获取...")
from agent.subagent import get_subagent_mcp_tools_schema
tools = get_subagent_mcp_tools_schema('file-processor')
print(f"✓ file-processor: {len(tools)} tools")

# 检查关键工具
tool_names = [t.get("function", {}).get("name") for t in tools]
critical_tools = [
    "photo-server/ingest_photo",
    "photo-server/ingest_photos",
    "photo-server/ingest_document"
]
for tool_name in critical_tools:
    if tool_name in tool_names:
        print(f"  ✓ {tool_name}")
    else:
        print(f"  ✗ {tool_name} MISSING!")

# Step 3: 测试工具schema格式
print("\n[Step 3] 验证工具schema格式...")
if tools:
    import json
    first_tool = tools[0]
    has_type = "type" in first_tool
    has_function = "function" in first_tool
    has_name = "name" in first_tool.get("function", {})
    has_parameters = "parameters" in first_tool.get("function", {})

    print(f"  has 'type': {has_type}")
    print(f"  has 'function': {has_function}")
    print(f"  has 'function.name': {has_name}")
    print(f"  has 'function.parameters': {has_parameters}")

    if all([has_type, has_function, has_name, has_parameters]):
        print("  ✓ Schema格式正确")
    else:
        print("  ✗ Schema格式错误")

# Step 4: 测试直接调用工具
print("\n[Step 4] 测试直接调用photo-server工具...")
from agent.tool_registry import get_registry
registry = get_registry()
tool_fn = registry.get("photo-server/ingest_photo")

if tool_fn is None:
    print("  ✗ 工具函数未找到")
else:
    print(f"  ✓ 工具函数已注册: {tool_fn.__name__}")

    # 测试调用（不存在文件，应该快速返回错误）
    import time
    start = time.time()
    result = tool_fn(file_path="E:/tmp/nonexistent_test.jpg", category="测试")
    elapsed = time.time() - start

    print(f"  ✓ 调用成功 (耗时: {elapsed:.3f}s)")
    print(f"  ✓ 返回status: {result.get('status')}")
    print(f"  ✓ 错误码: {result.get('error_code')}")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
