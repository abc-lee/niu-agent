# MCP同进程架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将MCP工具从子进程stdio通信改为同进程Python函数调用，消除性能开销

**Architecture:** 新增ToolRegistry管理工具注册和调用；MCP服务器模块移除stdio通信，提供get_tool_schemas()函数；启动时加载所有模块并严格检查

**Tech Stack:** Python 3.11+, 无新增依赖

---

## File Structure

### 新增文件
- `agent/tool_registry.py` - 工具注册表（管理工具注册、提供函数引用和schema）
- `agent/mcp_loader.py` - MCP工具加载器（启动时加载、严格检查）
- `tests/test_tool_registry.py` - ToolRegistry单元测试
- `tests/test_mcp_loader.py` - MCP加载器测试
- `tests/integration/test_mcp_in_process.py` - 集成测试

### 修改文件
- `mcp-servers/*/src/niu_*/__init__.py` - 所有MCP服务器模块添加get_tool_schemas()
- `agent/handler.py` - 简化do_mcp_tool方法
- `niu_api/__main__.py` - 修改预加载逻辑
- `niu_api/chat.py` - 修改init_runner签名

---

## Task 1: 创建ToolRegistry基础设施

**Files:**
- Create: `agent/tool_registry.py`
- Create: `tests/test_tool_registry.py`

### Substeps

- [ ] **Step 1: 写失败的测试 - 工具注册**

创建 `tests/test_tool_registry.py`，包含测试工具注册、获取、schema返回的单元测试

- [ ] **Step 2: 运行测试验证失败**

Expected: ModuleNotFoundError

- [ ] **Step 3: 实现ToolRegistry类**

创建 `agent/tool_registry.py`，实现：
- `register_server()` - 注册MCP服务器的所有工具
- `get()` - 获取工具函数
- `get_schemas()` - 返回工具schema列表
- 全局registry实例管理

- [ ] **Step 4: 运行测试验证通过**

Expected: PASS

- [ ] **Step 5: 提交ToolRegistry基础实现**

Commit message: "feat: add ToolRegistry for managing MCP tools"

---

## Task 2: 创建MCP加载器

**Files:**
- Create: `agent/mcp_loader.py`
- Create: `tests/test_mcp_loader.py`

### Substeps

- [ ] **Step 1: 写失败的测试 - 加载Mock模块**

测试load_mcp_tools()能否正确加载Mock模块并注册到ToolRegistry

- [ ] **Step 2: 运行测试验证失败**

Expected: ModuleNotFoundError

- [ ] **Step 3: 实现load_mcp_tools函数**

创建 `agent/mcp_loader.py`，实现：
- 定义REQUIRED_SERVERS列表
- 遍历加载所有MCP模块
- 严格检查：任何失败抛出RuntimeError
- 设置全局ToolRegistry实例

- [ ] **Step 4: 运行测试验证通过**

Expected: PASS

- [ ] **Step 5: 写测试 - 加载失败时的错误处理**

测试模块缺失时是否正确抛出RuntimeError

- [ ] **Step 6: 运行测试验证通过**

Expected: PASS

- [ ] **Step 7: 提交MCP加载器**

Commit message: "feat: add MCP tool loader with strict validation"

---

## Task 3: 改造photo-server模块（示例）

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

### Substeps

- [ ] **Step 1: 查看photo-server现有工具**

列出所有公开工具函数（ingest_photo, ingest_document等）

- [ ] **Step 2: 创建TOOL_SCHEMAS字典**

为每个工具添加详细的schema定义，包括：
- name: 完整工具名
- description: 详细描述（包含参数、返回值、处理流程）
- input_schema: 参数schema

至少添加：ingest_photo, ingest_document, store_document_l1

- [ ] **Step 3: 添加get_tool_schemas函数**

返回list(TOOL_SCHEMAS.values())

- [ ] **Step 4: 测试模块导入**

验证get_tool_schemas()能正确返回工具列表

- [ ] **Step 5: 提交photo-server改造**

Commit message: "feat(photo-server): add tool schemas for registry"

---

## Task 4: 修改工具调用链路

**Files:**
- Modify: `agent/handler.py`

### Substeps

- [ ] **Step 1: 查看当前do_mcp_tool实现**

理解现有的MCPSyncBridge调用逻辑

- [ ] **Step 2: 实现新的do_mcp_tool方法**

改为直接从ToolRegistry获取工具函数并调用：
- 从ToolRegistry.get()获取工具函数
- 检查工具是否存在
- 直接调用工具函数
- 完善的错误处理

- [ ] **Step 3: 提交handler改造**

Commit message: "refactor(handler): simplify MCP tool calling"

---

## Task 5: 修改启动流程

**Files:**
- Modify: `niu_api/__main__.py`
- Modify: `niu_api/chat.py`

### Substeps

- [ ] **Step 1: 修改预加载逻辑**

替换 `list_mcp_tools()` 为 `load_mcp_tools()`

- [ ] **Step 2: 修改init_runner签名**

接受ToolRegistry实例而非mcp_tools列表

- [ ] **Step 3: 从ToolRegistry获取schema**

调用 `tool_registry.get_schemas()` 传递给runner

- [ ] **Step 4: 测试启动流程**

验证所有MCP服务器加载成功

- [ ] **Step 5: 提交启动流程修改**

Commit message: "refactor(api): use MCP loader instead of stdio client"

---

## Task 6: 集成测试和验证

**Files:**
- Create: `tests/integration/test_mcp_in_process.py`

### Substeps

- [ ] **Step 1: 写集成测试 - 文档入库流程**

测试 ingest_document → store_document_l1 完整流程

- [ ] **Step 2: 运行集成测试**

Expected: PASS

- [ ] **Step 3: 写集成测试 - 照片入库流程**

测试 ingest_photo 流程

- [ ] **Step 4: 运行照片入库测试**

Expected: PASS

- [ ] **Step 5: 写性能基准测试**

测试10次工具调用总时间 < 1秒（远快于stdio模式的40秒）

- [ ] **Step 6: 运行性能测试**

Expected: PASS，输出显示每次调用 < 0.1秒

- [ ] **Step 7: 提交集成测试**

Commit message: "test: add integration tests for MCP in-process architecture"

---

## Task 7: 批量改造其他MCP服务器

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py`
- Modify: `mcp-servers/memory-server/src/niu_memory_server/__init__.py`
- Modify: `mcp-servers/vector-store/src/niu_vector_store/__init__.py`
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py`
- Modify: `mcp-servers/file-parser/src/niu_file_parser/__init__.py`
- Modify: `mcp-servers/session-manager/src/niu_session_manager/__init__.py`
- Modify: `mcp-servers/scheduler-server/src/niu_scheduler_server/__init__.py`

### Substeps

- [ ] **Step 1: 为每个服务器添加TOOL_SCHEMAS**

参考photo-server的模式，为每个MCP服务器：
- 添加TOOL_SCHEMAS字典
- 为每个公开工具添加schema
- 添加get_tool_schemas()函数

- [ ] **Step 2: 测试所有模块导入**

验证所有MCP服务器都能正确加载并返回工具schema

- [ ] **Step 3: 提交所有MCP服务器改造**

Commit message: "feat(mcp-servers): add tool schemas for all servers"

---

## Task 8: 清理和文档

### Substeps

- [ ] **Step 1: 更新CLAUDE.md**

添加MCP同进程架构说明

- [ ] **Step 2: 在mcp_client.py添加废弃警告**

标记stdio通信代码为废弃

- [ ] **Step 3: 提交文档更新**

Commit message: "docs: update architecture docs for MCP in-process mode"

---

## Self-Review

### 1. Spec Coverage

✅ 所有设计组件都有对应任务：
- ToolRegistry: Task 1
- MCP加载器: Task 2
- 模块适配: Task 3, 7
- 工具调用: Task 4
- 启动流程: Task 5
- 测试: Task 6

### 2. Placeholder Scan

无占位符，所有任务有明确的目标和输出。

### 3. Type Consistency

函数签名在所有任务中保持一致：
- `get_tool_schemas() -> list`
- `load_mcp_tools() -> ToolRegistry`
- `ToolRegistry.get() -> Optional[Callable]`

---

## Plan Complete

实现计划已完成。所有任务遵循TDD模式，每个任务有明确的文件、步骤和预期输出。
