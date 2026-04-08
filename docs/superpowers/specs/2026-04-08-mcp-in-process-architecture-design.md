# MCP同进程架构设计

## 背景

### 当前问题

**MCP stdio通信模式存在严重性能问题**：

1. **每次工具调用启动子进程**
   - 调用 `photo-server/ingest_document` → 启动子进程 → 预加载模块（~4秒）
   - 调用 `photo-server/store_document_l1` → 启动子进程 → 预加载模块（~4秒）
   - 文档入库需要两次工具调用 → 总耗时 ~8秒+

2. **系统资源浪费**
   - 每次启动子进程：进程创建、销毁开销
   - 重复加载模块：InsightFace、cv2 等大型库
   - 内存碎片化

3. **打包后的问题**
   - 多个Python进程
   - 不符合"单一可执行文件 + 便携式Python环境"的部署目标

### 设计目标

1. ✅ **高性能**：消除进程通信开销，工具调用零延迟
2. ✅ **稳定性**：单一Python进程，易于管理
3. ✅ **可维护性**：简化架构，移除stdio通信层
4. ✅ **兼容性**：保持工具接口不变，Agent调用方式不变
5. ✅ **可打包**：符合便携式部署需求

---

## 架构设计

### 目标架构

```
Python API主进程
  ├── 启动时：
  │   ├── 导入所有MCP工具模块代码
  │   ├── 启动Embedding服务（常驻，立即加载）
  │   └── 注册工具到ToolRegistry
  ├── 工具调用：
  │   ├── 向量相关工具 → 调用Embedding服务（已加载）
  │   └── 照片相关工具 → 按需加载InsightFace
  └── 模型管理：
      ├── Embedding：常驻内存（永不卸载）
      └── InsightFace：按需加载（5分钟空闲卸载）
```

### 关键组件

#### 1. ToolRegistry（新增）

**文件**：`agent/tool_registry.py`

**职责**：
- 管理所有MCP工具的注册表
- 提供工具函数引用
- 提供工具schema（用于LLM）

**接口**：
```python
class ToolRegistry:
    def register_server(self, server_name: str, module) -> bool:
        """注册整个MCP服务器的工具"""
        pass

    def get(self, tool_name: str) -> Optional[Callable]:
        """获取工具函数"""
        pass

    def get_schemas(self) -> list:
        """获取所有工具schema（用于LLM）"""
        pass
```

#### 2. MCP工具加载器（新增）

**文件**：`agent/mcp_loader.py`

**职责**：
- 启动时加载所有MCP模块
- 严格检查加载结果
- 失败时终止启动

**接口**：
```python
def load_mcp_tools() -> ToolRegistry:
    """
    加载所有MCP工具（严格模式）
    任何模块加载失败都会抛出异常
    """
    pass
```

#### 3. MCP服务器模块适配

**修改文件**：所有 `mcp-servers/*/src/niu_*/__init__.py`

**改动**：
- ❌ 删除：MCP Server装饰器、stdio通信代码、main()函数
- ✅ 保留：纯Python工具函数（业务逻辑不变）
- ✅ 新增：`get_tool_schemas()` 函数、`__TOOLS__` 列表

**示例**：
```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py

def ingest_photo(file_path: str, category: str = None) -> dict:
    """照片入库工具（带人脸识别）"""
    # 业务逻辑（完全不变）
    return {"status": "success", "photo_id": "..."}

def ingest_document(file_path: str, category: str, mode: str = "copy") -> dict:
    """文档入库工具"""
    # 业务逻辑（完全不变）
    return {"status": "need_l1", "file_path": "...", "content": "..."}

# 工具schema定义
TOOL_SCHEMAS = {
    "ingest_photo": {
        "name": "photo-server/ingest_photo",
        "description": "照片入库工具（带人脸识别）...",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "照片文件绝对路径"},
                "category": {"type": "string", "description": "分类", "enum": [...]}
            },
            "required": ["file_path"]
        }
    },
    # ... 其他工具
}

def get_tool_schemas() -> list:
    """返回所有工具的schema（兼容MCP格式）"""
    return list(TOOL_SCHEMAS.values())
```

---

## 启动流程

### 当前启动流程（问题）

```
Go启动器
  ↓ HTTP等待
Python API启动
  ↓
1. 预加载embedding服务
2. list_mcp_tools() → 启动所有MCP子进程获取工具列表
3. 标记预加载完成
  ↓
Go显示窗口
  ↓
首次工具调用 → 再次启动子进程 → 预加载模块 → 执行
```

### 目标启动流程

```
Go启动器
  ↓ HTTP等待
Python API启动
  ↓
1. 预加载embedding服务（常驻）
2. load_mcp_tools() → 导入所有MCP模块 → 注册工具
3. 标记预加载完成
  ↓
Go显示窗口
  ↓
所有工具调用 → 直接调用Python函数（零延迟）
```

### 启动代码

```python
# niu_api/__main__.py
async def main():
    try:
        # 1. 预加载embedding服务
        from niu_api.internal.embedding import preload as preload_embedding
        preload_embedding()

        # 2. 加载MCP工具（严格模式）
        from agent.tool_registry import load_mcp_tools
        tool_registry = load_mcp_tools()

        # 3. 初始化runner
        from niu_api.chat import init_runner
        init_runner(tool_registry)

        # 4. 标记完成
        from niu_api.compat import set_preload_complete
        set_preload_complete()

    except Exception as e:
        # 核心模块加载失败，终止启动
        import sys
        print(f"[FATAL] Startup failed: {e}", file=sys.stderr)
        sys.exit(1)
```

---

## 错误处理和严格启动检查

### 设计原则

**所有MCP服务器都是核心功能，没有可有可无的模块**。

### 严格检查策略

```python
# agent/mcp_loader.py
def load_mcp_tools() -> ToolRegistry:
    registry = ToolRegistry()

    REQUIRED_SERVERS = [
        ("photo-server", "niu_photo_server"),
        ("config-manager", "niu_config_manager"),
        ("memory-server", "niu_memory_server"),
        ("vector-store", "niu_vector_store"),
        ("kg-server", "niu_kg_server"),
        ("file-parser", "niu_file_parser"),
        ("session-manager", "niu_session_manager"),
        ("scheduler-server", "niu_scheduler_server"),
    ]

    failed_servers = []

    for server_name, module_name in REQUIRED_SERVERS:
        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            if not registry.register_server(server_name, module):
                failed_servers.append(f"{server_name} (registration failed)")

        except ImportError as e:
            failed_servers.append(f"{server_name} (import failed: {e})")
        except Exception as e:
            failed_servers.append(f"{server_name} (error: {e})")

    # 严格检查：任何失败都终止启动
    if failed_servers:
        error_msg = f"Critical MCP servers failed to load:\n" + "\n".join(f"  - {s}" for s in failed_servers)
        raise RuntimeError(error_msg)

    print(f"[MCP Loader] All {len(REQUIRED_SERVERS)} servers loaded")

    return registry
```

---

## 工具Schema获取机制

### 问题

LLM需要知道工具的参数schema才能正确调用。

### 解决方案

每个MCP模块提供 `get_tool_schemas()` 函数，返回工具schema列表。

**Schema格式**（兼容OpenAI format）：
```python
{
    "name": "photo-server/ingest_photo",
    "description": "照片入库工具...",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "..."},
            "category": {"type": "string", "description": "..."}
        },
        "required": ["file_path"]
    }
}
```

### 工具注册

```python
# agent/tool_registry.py
class ToolRegistry:
    def register_server(self, server_name: str, module) -> bool:
        try:
            # 检查必需的函数
            if not hasattr(module, 'get_tool_schemas'):
                raise AttributeError(f"missing get_tool_schemas()")

            schemas = module.get_tool_schemas()
            if not schemas:
                raise ValueError("returned empty tool schemas")

            # 注册所有工具
            for schema in schemas:
                tool_name = schema['name'].split('/')[-1]
                full_name = schema['name']

                func = getattr(module, tool_name, None)
                if func is None:
                    raise AttributeError(f"{tool_name} not found")

                self._tools[full_name] = func
                self._schemas[full_name] = schema

            return True

        except Exception as e:
            print(f"[ToolRegistry] ERROR: {e}")
            return False
```

---

## 工具调用机制

### 当前调用（复杂）

```python
# agent/handler.py
def do_mcp_tool(self, args: dict, response):
    from agent.mcp_sync_bridge import get_mcp_bridge
    bridge = get_mcp_bridge()

    result = bridge.call_tool(
        server_name, tool_name, params,
        timeout=10
    )

    return StepOutcome(result, next_prompt=...)
```

### 目标调用（简化）

```python
# agent/handler.py
def do_mcp_tool(self, args: dict, response):
    tool_name = args.get("tool_name")
    params = args.get("params", {})

    from agent.tool_registry import get_registry
    func = get_registry().get(tool_name)

    if func is None:
        return StepOutcome(
            {"status": "error", "error_code": "TOOL_NOT_FOUND"},
            next_prompt=self._get_anchor_prompt()
        )

    try:
        result = func(**params)
        return StepOutcome(result, next_prompt=self._get_anchor_prompt())
    except Exception as e:
        return StepOutcome(
            {"status": "error", "error_code": "TOOL_ERROR", "message": str(e)},
            next_prompt=self._get_anchor_prompt()
        )
```

---

## 兼容性和迁移策略

### 向后兼容性

✅ **工具名称格式不变**：`{server}/{tool}`
✅ **工具返回值格式不变**：`{"status": "success", ...}`
✅ **Agent调用方式不变**：`do_mcp_tool`
✅ **配置文件格式不变**：`config/mcp-servers.yaml`

### 需要修改的文件

**新增文件**：
1. `agent/tool_registry.py` - 工具注册表
2. `agent/mcp_loader.py` - 工具加载器

**修改文件**：
1. `agent/handler.py` - 简化工具调用
2. `agent/mcp_client.py` - 大幅简化或删除
3. `agent/mcp_sync_bridge.py` - 可能删除
4. `niu_api/__main__.py` - 修改预加载逻辑
5. 所有MCP服务器模块 - 移除stdio通信，添加schema函数

**删除/废弃文件**：
- `agent/mcp_sync_bridge.py` - 不再需要异步桥接
- MCP Server装饰器相关代码

### 迁移步骤

1. ✅ 创建 `agent/tool_registry.py` 和 `agent/mcp_loader.py`
2. ✅ 修改所有MCP服务器模块，添加 `get_tool_schemas()`
3. ✅ 修改 `agent/handler.py`，简化工具调用
4. ✅ 修改 `niu_api/__main__.py`，使用新的加载机制
5. ✅ 删除或标记废弃 `agent/mcp_sync_bridge.py`
6. ✅ 测试所有工具调用

---

## 性能优化

### 模型管理策略

#### Embedding服务（常驻）

- **加载时机**：程序启动时
- **卸载策略**：永不卸载
- **原因**：向量检索频繁，响应速度要求高

#### InsightFace模型（按需加载）

- **加载时机**：首次使用照片工具时
- **卸载策略**：空闲5分钟自动卸载
- **原因**：照片处理不频繁，节省内存

### 内存占用

**启动时**：
- Python基础运行时：~50 MB
- Embedding模型：~90 MB
- MCP模块代码：~10 MB
- **总计**：~150 MB

**峰值**（使用InsightFace时）：
- 基础 + Embedding：~150 MB
- InsightFace模型：~326 MB
- **总计**：~476 MB

---

## 打包和部署

### 目标打包方式

```
程序目录/
  ├── niu.exe                    # Go启动器（单一可执行文件）
  ├── python/                    # 便携式Python环境
  │   ├── python.exe
  │   ├── Lib/
  │   └── site-packages/
  ├── agent/                     # Agent核心代码
  ├── mcp-servers/               # MCP工具模块
  ├── niu_api/                   # Python API
  └── config/                    # 配置文件
```

### 启动流程

1. **用户双击 `niu.exe`**
2. **Go启动器检查Python环境**
3. **启动Python API**：`python/python.exe -m niu_api`
4. **Python加载所有模块**（单一进程）
5. **Go显示窗口**

### 优势

✅ **单一Python进程**：所有代码在同进程中
✅ **便携式部署**：Python环境在程序目录下
✅ **Agent代码执行**：复用同一个Python环境
✅ **易于维护**：无进程间通信，调试简单

---

## 测试策略

### 单元测试

**工具注册测试**：
```python
def test_tool_registry():
    registry = ToolRegistry()

    # Mock module
    class MockModule:
        @staticmethod
        def get_tool_schemas():
            return [{"name": "test/tool", "input_schema": {...}}]

        @staticmethod
        def tool():
            return {"status": "success"}

    assert registry.register_server("test", MockModule)
    assert registry.get("test/tool") is not None
```

**工具调用测试**：
```python
def test_tool_call():
    registry = load_mcp_tools()

    result = registry.get("photo-server/ingest_photo")(
        file_path="test.jpg",
        category="生活"
    )

    assert result["status"] == "success"
```

### 集成测试

**启动流程测试**：
- 所有模块正确加载
- 工具schema正确返回
- 失败时正确报错退出

**工具调用测试**：
- 照片入库流程（ingest_photo）
- 文档入库流程（ingest_document → store_document_l1）
- 配置读写（get_config → set_config）

---

## 风险和缓解措施

### 风险1：模块导入冲突

**风险**：多个MCP模块可能依赖不同版本的库。

**缓解**：
- 统一依赖管理（requirements.txt）
- 使用虚拟环境隔离

### 风险2：启动时间变长

**风险**：启动时导入所有模块可能较慢。

**缓解**：
- 按需导入非核心模块
- 优化模块加载顺序
- 缓存已导入的模块

### 风险3：内存占用增加

**风险**：所有模块常驻内存。

**缓解**：
- 模型按需加载（InsightFace）
- 空闲时卸载模型
- 监控内存使用

---

## 后续优化方向

### 短期（本次实现）

1. ✅ 实现ToolRegistry和工具加载器
2. ✅ 改造所有MCP服务器模块
3. ✅ 简化工具调用链路
4. ✅ 完整测试

### 中期

1. 🔄 工具热重载（开发环境）
2. 🔄 工具性能监控
3. 🔄 自动生成工具schema（从函数签名）

### 长期

1. 📋 工具版本管理
2. 📋 工具权限控制
3. 📋 分布式工具调用（可选）

---

## 总结

本设计通过将MCP工具从独立的子进程改为同进程Python模块调用，彻底解决了性能、资源、打包等问题。核心思想是：

- **简单**：移除stdio通信层，直接调用Python函数
- **高效**：零进程通信开销，模型按需加载
- **稳定**：单一进程，严格启动检查
- **兼容**：保持接口不变，平滑迁移

这是符合"便携式部署 + 单一可执行文件"目标的最佳架构。
