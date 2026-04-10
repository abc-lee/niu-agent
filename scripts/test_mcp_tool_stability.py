"""
测试 MCP 工具调用链路的稳定性
"""
import sys
import time
import io
from pathlib import Path

# 修复 Windows 控制台编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Add MCP server workdirs to sys.path
config_dir = project_root / "config"
mcp_config_path = config_dir / "mcp-servers.yaml"
if mcp_config_path.exists():
    import yaml
    with open(mcp_config_path, "r", encoding="utf-8") as f:
        mcp_config = yaml.safe_load(f) or {}

    for server_name, server_config in mcp_config.items():
        if isinstance(server_config, dict) and "workdir" in server_config:
            workdir = (project_root / server_config["workdir"]).resolve()
            if workdir.exists() and str(workdir) not in sys.path:
                sys.path.insert(0, str(workdir))

def test_tool_registry():
    """测试 ToolRegistry 是否正确加载"""
    print("=" * 60)
    print("测试 1: ToolRegistry 加载")
    print("=" * 60)

    try:
        from agent.mcp_loader import load_mcp_tools

        print("调用 load_mcp_tools()...")
        start_time = time.time()
        registry = load_mcp_tools()
        elapsed = time.time() - start_time

        print(f"✓ 加载成功（耗时 {elapsed:.2f}s）")
        print(f"  工具总数: {len(registry.list_tools())}")

        # 列出 photo-server 的工具
        photo_tools = [t for t in registry.list_tools() if t.startswith("photo-server/")]
        print(f"  photo-server 工具数: {len(photo_tools)}")
        for tool in photo_tools[:5]:
            print(f"    - {tool}")

        return registry

    except Exception as e:
        print(f"✗ 加载失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_photo_tool_direct():
    """测试直接调用 photo-server 工具"""
    print("\n" + "=" * 60)
    print("测试 2: 直接调用 ingest_photo 工具")
    print("=" * 60)

    try:
        from agent.tool_registry import get_registry

        registry = get_registry()
        tool_fn = registry.get("photo-server/ingest_photo")

        if tool_fn is None:
            print("✗ 工具未注册")
            return False

        print(f"✓ 工具函数已注册: {tool_fn}")

        # 测试调用（用一个不存在的文件，应该返回错误而不是崩溃）
        result = tool_fn(file_path="E:/tmp/test_nonexistent.jpg", category="测试")

        print(f"✓ 工具调用成功（返回: {result.get('status')}）")
        print(f"  错误码: {result.get('error_code', 'N/A')}")

        return True

    except Exception as e:
        print(f"✗ 调用失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_face_model_loading():
    """测试 InsightFace 模型加载"""
    print("\n" + "=" * 60)
    print("测试 3: InsightFace 模型加载")
    print("=" * 60)

    try:
        print("导入 photo-server 模块...")
        import niu_photo_server

        print("调用 get_face_model()...")
        start_time = time.time()
        model = niu_photo_server.get_face_model()
        elapsed = time.time() - start_time

        if model is None:
            print("✗ 模型加载失败（返回 None）")
            return False

        print(f"✓ 模型加载成功（耗时 {elapsed:.2f}s）")
        print(f"  模型类型: {type(model)}")

        # 第二次调用应该使用缓存
        print("第二次调用（应该使用缓存）...")
        start_time = time.time()
        model2 = niu_photo_server.get_face_model()
        elapsed2 = time.time() - start_time

        print(f"✓ 缓存命中（耗时 {elapsed2:.3f}s）")
        print(f"  是同一个实例: {model is model2}")

        return True

    except Exception as e:
        print(f"✗ 模型加载失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_multiple_sequential_calls():
    """测试多次连续调用工具"""
    print("\n" + "=" * 60)
    print("测试 4: 连续调用工具 10 次")
    print("=" * 60)

    try:
        from agent.tool_registry import get_registry

        registry = get_registry()
        tool_fn = registry.get("photo-server/ingest_photo")

        if tool_fn is None:
            print("✗ 工具未注册")
            return False

        success_count = 0
        for i in range(10):
            try:
                result = tool_fn(file_path=f"E:/tmp/test_{i}.jpg", category="测试")
                if result.get("status") in ["error", "success"]:
                    success_count += 1
            except Exception as e:
                print(f"  第 {i+1} 次调用失败: {e}")

        print(f"✓ 成功调用: {success_count}/10")
        return success_count == 10

    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("MCP 工具调用链路稳定性测试")
    print("=" * 60)

    results = {}

    # 测试 1: ToolRegistry 加载
    registry = test_tool_registry()
    results["ToolRegistry"] = registry is not None

    if registry is None:
        print("\n❌ ToolRegistry 加载失败，后续测试跳过")
        return results

    # 测试 2: 直接调用工具
    results["DirectCall"] = test_photo_tool_direct()

    # 测试 3: 模型加载
    results["FaceModel"] = test_face_model_loading()

    # 测试 4: 连续调用
    results["SequentialCalls"] = test_multiple_sequential_calls()

    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    for name, passed in results.items():
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name:20s}: {status}")

    all_passed = all(results.values())
    print("\n" + ("✓ 所有测试通过" if all_passed else "✗ 部分测试失败"))

    return results


if __name__ == "__main__":
    main()
