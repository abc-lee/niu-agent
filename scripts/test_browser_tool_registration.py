"""
测试 browser-server 工具注册流程

检查点：
1. browser-server 模块是否正确导出 get_tool_schemas() 函数
2. ToolRegistry 是否正确注册 browser-server 工具
3. API 启动时 load_mcp_tools() 是否成功加载 browser-server
4. BASE_MCP_TOOLS 配置是否正确
5. 工具 schema 是否传递给了 NiuRunner
"""

import sys
import os
from pathlib import Path

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 检查点 1: browser-server 模块导出
print("=" * 60)
print("检查点 1: browser-server 模块导出")
print("=" * 60)

try:
    # 先添加 workdir 到 sys.path
    workdir = project_root / "mcp-servers" / "browser-server" / "src"
    if str(workdir) not in sys.path:
        sys.path.insert(0, str(workdir))
        print(f"✓ Added to sys.path: {workdir}")

    import niu_browser_server
    print(f"✓ 模块导入成功: niu_browser_server")

    # 检查 get_tool_schemas 函数
    if hasattr(niu_browser_server, 'get_tool_schemas'):
        print("✓ get_tool_schemas() 函数存在")

        # 调用并检查返回值
        schemas = niu_browser_server.get_tool_schemas()
        print(f"✓ get_tool_schemas() 返回 {len(schemas)} 个工具")

        # 打印工具详情
        for schema in schemas:
            print(f"\n工具名称: {schema.get('name')}")
            print(f"  描述: {schema.get('description', '')[:100]}...")
            print(f"  参数: {list(schema.get('input_schema', {}).get('properties', {}).keys())}")
    else:
        print("✗ get_tool_schemas() 函数不存在")
        sys.exit(1)

    # 检查 browser_navigate 函数
    if hasattr(niu_browser_server, 'browser_navigate'):
        print("✓ browser_navigate() 工具函数存在")
    else:
        print("✗ browser_navigate() 工具函数不存在")

except ImportError as e:
    print(f"✗ 模块导入失败: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ 错误: {e}")
    sys.exit(1)

# 检查点 2: ToolRegistry 注册
print("\n" + "=" * 60)
print("检查点 2: ToolRegistry 注册")
print("=" * 60)

try:
    from agent.tool_registry import ToolRegistry

    registry = ToolRegistry()
    print("✓ ToolRegistry 实例创建成功")

    # 注册 browser-server
    success = registry.register_server("browser-server", niu_browser_server)
    if success:
        print("✓ browser-server 注册成功")
    else:
        print("✗ browser-server 注册失败")
        sys.exit(1)

    # 检查注册的工具
    tools = registry.list_tools()
    print(f"✓ 已注册工具列表: {tools}")

    # 检查 browser_navigate 是否存在
    if registry.has_tool("browser-server/browser_navigate"):
        print("✓ browser-server/browser_navigate 工具已注册")

        # 获取工具函数
        tool_fn = registry.get("browser-server/browser_navigate")
        if tool_fn:
            print(f"✓ 工具函数获取成功: {tool_fn}")
        else:
            print("✗ 工具函数获取失败")
    else:
        print("✗ browser-server/browser_navigate 工具未注册")
        sys.exit(1)

    # 获取 schema 列表
    schemas = registry.get_schemas()
    print(f"✓ Schema 列表获取成功，共 {len(schemas)} 个工具")

    # 打印 browser_navigate schema
    for schema in schemas:
        if schema.get('name') == 'browser-server/browser_navigate':
            print("\n✓ browser_navigate schema 详情:")
            print(f"  名称: {schema['name']}")
            print(f"  描述: {schema['description'][:100]}...")
            print(f"  参数: {list(schema['input_schema']['properties'].keys())}")
            break

except Exception as e:
    print(f"✗ ToolRegistry 测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 检查点 3: MCP Loader
print("\n" + "=" * 60)
print("检查点 3: MCP Loader 加载")
print("=" * 60)

try:
    from agent.mcp_loader import load_mcp_tools

    print("正在调用 load_mcp_tools()...")
    registry = load_mcp_tools()

    print(f"✓ load_mcp_tools() 成功，共加载 {len(registry.get_schemas())} 个工具")

    # 检查 browser-server 是否在其中
    if registry.has_tool("browser-server/browser_navigate"):
        print("✓ browser-server/browser_navigate 已被 MCP Loader 加载")
    else:
        print("✗ browser-server/browser_navigate 未被 MCP Loader 加载")
        print(f"可用工具: {registry.list_tools()}")
        sys.exit(1)

except RuntimeError as e:
    print(f"✗ MCP Loader 加载失败: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ MCP Loader 测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 检查点 4: BASE_MCP_TOOLS 配置
print("\n" + "=" * 60)
print("检查点 4: BASE_MCP_TOOLS 配置")
print("=" * 60)

try:
    from agent.runner import BASE_MCP_TOOLS

    print(f"✓ BASE_MCP_TOOLS 配置存在，共 {len(BASE_MCP_TOOLS)} 个工具")

    # 检查 browser_navigate 是否在列表中
    if "browser-server/browser_navigate" in BASE_MCP_TOOLS:
        print("✓ browser-server/browser_navigate 在 BASE_MCP_TOOLS 中")
        print(f"  位置: 第 {BASE_MCP_TOOLS.index('browser-server/browser_navigate') + 1} 个")
    else:
        print("✗ browser-server/browser_navigate 不在 BASE_MCP_TOOLS 中")
        print(f"  BASE_MCP_TOOLS 内容: {BASE_MCP_TOOLS}")
        sys.exit(1)

except Exception as e:
    print(f"✗ BASE_MCP_TOOLS 检查失败: {e}")
    sys.exit(1)

# 检查点 5: NiuRunner 工具 schema 传递
print("\n" + "=" * 60)
print("检查点 5: NiuRunner 工具 schema 传递")
print("=" * 60)

try:
    from agent.runner import NiuRunner
    from agent.tool_registry import get_registry

    # 创建模拟配置
    llm_config = {
        "apikey": "test-key",
        "apibase": "https://api.example.com",
        "model": "test-model",
        "type": "openai"
    }

    runner = NiuRunner(llm_config=llm_config, mcp_client=None)
    print("✓ NiuRunner 实例创建成功")

    # 设置 MCP 工具 schema
    registry = get_registry()
    mcp_schemas = registry.get_schemas()
    runner.set_mcp_tools_schema(mcp_schemas)

    print(f"✓ MCP 工具 schema 设置成功，共 {len(runner._mcp_tools_schema)} 个")

    # 检查 browser_navigate 是否在 schema 中
    browser_nav_schema = runner._get_tool_schema_by_name("browser-server/browser_navigate")
    if browser_nav_schema:
        print("✓ browser-server/browser_navigate schema 在 NiuRunner 中")
        print(f"  schema: {browser_nav_schema['function']['name']}")
    else:
        print("✗ browser-server/browser_navigate schema 不在 NiuRunner 中")
        sys.exit(1)

except Exception as e:
    print(f"✗ NiuRunner 测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 最终总结
print("\n" + "=" * 60)
print("✓ 所有检查点通过！")
print("=" * 60)
print("""
总结：
1. ✓ browser-server 模块正确导出 get_tool_schemas() 和 browser_navigate()
2. ✓ ToolRegistry 正确注册 browser-server 工具
3. ✓ API 启动时 load_mcp_tools() 成功加载 browser-server
4. ✓ BASE_MCP_TOOLS 配置包含 browser-server/browser_navigate
5. ✓ 工具 schema 成功传递给 NiuRunner

工具注册流程完整，browser_navigate 工具已正确注册到系统中。
""")
