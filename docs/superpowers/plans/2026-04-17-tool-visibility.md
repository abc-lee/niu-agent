# 工具可见性与动态注入过滤实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 MCP 工具添加 visibility 标识（static/dynamic/hidden），控制主 Agent 的工具可见性，解决 co-activation 泄漏、向量库冗余、子 Agent 分数污染三个问题。

**Architecture:** 在 mcp-servers.yaml 配置 per-tool visibility，mcp_loader 解析后写入 ToolRegistry schema。runner.py 从 ToolRegistry 获取 static 工具替代硬编码 BASE_MCP_TOOLS。tool_lifecycle 过滤 hidden 工具。handler.py 区分子 Agent 跳过 hit_tool()。向量库只存 dynamic 工具。

**Tech Stack:** Python, YAML (PyYAML), SQLite (vecdb)

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `config/mcp-servers.yaml` | 增加 `tools:` 下的 `visibility` 配置 |
| `agent/mcp_loader.py` | 解析 `tools:` 配置，传给 ToolRegistry |
| `agent/tool_registry.py` | schema 加 `visibility` 字段；新增查询方法 |
| `agent/runner.py` | 删除硬编码 BASE_MCP_TOOLS；mcp_tool_scores 过滤 hidden |
| `agent/tool_lifecycle.py` | co-activation 跳过 hidden；get_active_tools() 过滤 hidden |
| `agent/handler.py` | dispatch() 中判断 _is_subagent，子 Agent 跳过 hit_tool() |
| `agent/subagent.py` | 创建 handler 时标记 _is_subagent = True |
| `scripts/init_vector_db.py` | register_mcp_tools() 只写入 dynamic 工具 |

---

## Task 1: mcp-servers.yaml 增加 visibility 配置

**Files:**
- Modify: `config/mcp-servers.yaml`

- [ ] **Step 1: 在每个 server 下增加 tools 字段**

在 `config/mcp-servers.yaml` 中，为每个 server 增加 `tools:` 配置。只配 `static` 和 `hidden`，未配置的默认 `dynamic`。

```yaml
# Niu MCP Servers Configuration
# These servers provide tools for the Niu assistant

# File Parser - Parse various document formats (PDF, Word, PPT, Excel, MD, HTML)
file-parser:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_file_parser"
  workdir: mcp-servers/file-parser/src
  preload: true
  # 全部默认 dynamic

# Knowledge Graph - Manage documents, entities, and their relationships
kg-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_kg_server"
  workdir: mcp-servers/kg-server/src
  preload: true
  tools:
    explore_node: {visibility: static}
    get_related_entities: {visibility: static}
    search_documents: {visibility: dynamic}
    get_document: {visibility: dynamic}
    list_documents: {visibility: dynamic}
    query_graph: {visibility: dynamic}
    hub_entities: {visibility: dynamic}
    find_path: {visibility: dynamic}
    get_related_concepts: {visibility: dynamic}
    graph_stats: {visibility: dynamic}
    create_document: {visibility: hidden}
    create_entity: {visibility: hidden}
    create_concept: {visibility: hidden}
    link_document_entity: {visibility: hidden}
    link_document_concept: {visibility: hidden}
    link_entities: {visibility: hidden}
    surprising_connections: {visibility: hidden}
    graph_changelog: {visibility: hidden}
    graph_snapshot: {visibility: hidden}
    list_entities: {visibility: hidden}
    list_concepts: {visibility: hidden}

# Vector Store - Semantic search using embeddings
vector-store:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_vector_store"
  workdir: mcp-servers/vector-store/src
  preload: true
  tools:
    add_document: {visibility: static}
    search_documents: {visibility: static}
    get_document: {visibility: static}
    delete_document: {visibility: static}
    list_documents: {visibility: static}
    # count_documents 默认 dynamic

# Config Manager - Read/write user configuration and memory
config-manager:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_config_manager"
  workdir: mcp-servers/config-manager/src
  preload: true
  # 全部默认 dynamic

# Photo Server - File and photo management with face recognition
photo-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_photo_server"
  workdir: mcp-servers/photo-server/src
  preload: true
  # 全部默认 dynamic

# Memory Server - Smart memory extraction and retrieval
memory-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_memory_server"
  workdir: mcp-servers/memory-server/src
  preload: true
  tools:
    remember: {visibility: static}
    recall: {visibility: static}
    update_memory: {visibility: static}
    get_memory_stats: {visibility: static}
    cleanup_memories: {visibility: static}
    link_memories: {visibility: static}

# Scheduler Server - Scheduled tasks and reminders
scheduler-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_scheduler_server"
  workdir: mcp-servers/scheduler-server/src
  preload: true
  # 全部默认 dynamic

# Session Manager - Message management for context compression
session-manager:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_session_manager"
  workdir: mcp-servers/session-manager/src
  preload: false  # 按需启动，不预加载
  # 全部默认 dynamic

# Browser Server - Browser automation using Playwright
browser-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_browser_server"
  workdir: mcp-servers/browser-server/src
  preload: false  # 按需启动，首次使用 ~2 秒启动浏览器
  tools:
    browser_navigate: {visibility: static}
    # browser_interact, browser_new_tab 默认 dynamic

# Nanobot System - Built-in system tools (not a real MCP server, added programmatically)
# This entry exists to satisfy validation that references all defined servers
nanobot.system:
  # No command - this is a built-in server added via registry.AddServer in code
```

- [ ] **Step 2: 提交**

```bash
git add config/mcp-servers.yaml
git commit -m "feat: add per-tool visibility config to mcp-servers.yaml"
```

---

## Task 2: ToolRegistry 存储 visibility + 查询方法

**Files:**
- Modify: `agent/tool_registry.py:47-95`
- Modify: `agent/mcp_loader.py:78-125`

- [ ] **Step 1: 修改 `register_server()` 接受 visibility_map 参数**

在 `agent/tool_registry.py` 第47行，修改 `register_server` 签名，增加 `visibility_map` 参数：

```python
def register_server(self, server_name: str, module, visibility_map: dict = None) -> bool:
```

在第90行 `normalized_schema` 构建中，加入 visibility 字段：

```python
                # 确定 visibility
                tool_vis = "dynamic"  # 默认值
                if visibility_map and tool_name in visibility_map:
                    tool_vis = visibility_map[tool_name].get("visibility", "dynamic")

                # 存储schema（确保使用input_schema格式）
                normalized_schema = {
                    "name": full_name,
                    "description": schema.get("description", ""),
                    "input_schema": schema.get("input_schema", schema.get("inputSchema", {})),
                    "visibility": tool_vis
                }
```

- [ ] **Step 2: 新增 visibility 查询方法**

在 `agent/tool_registry.py` 的 `ToolRegistry` 类中，在 `clear()` 方法之前新增：

```python
    def get_visibility(self, tool_name: str) -> str:
        """
        获取工具的 visibility 标识

        Args:
            tool_name: 完整工具名（如 "kg-server/create_entity"）

        Returns:
            "static" / "dynamic" / "hidden"，未注册工具返回 "dynamic"
        """
        schema = self._schemas.get(tool_name)
        if schema:
            return schema.get("visibility", "dynamic")
        return "dynamic"

    def get_static_tools(self) -> List[str]:
        """
        返回所有 visibility=static 的工具名列表

        替代 runner.py 中硬编码的 BASE_MCP_TOOLS
        """
        return [name for name, schema in self._schemas.items() if schema.get("visibility") == "static"]

    def get_dynamic_tools(self) -> List[str]:
        """
        返回所有 visibility=dynamic 的工具名列表

        向量库初始化时使用：只有 dynamic 工具才存入向量库
        """
        return [name for name, schema in self._schemas.items() if schema.get("visibility", "dynamic") == "dynamic"]
```

- [ ] **Step 3: 修改 `mcp_loader.py` 解析 visibility 配置**

在 `agent/mcp_loader.py` 的 `load_mcp_tools()` 函数中，修改第101行的循环，从 config 中提取每个 server 的 tools 配置并传给 `register_server()`：

```python
    for server_name, module_name in servers:
        try:
            module = __import__(module_name, fromlist=["get_tool_schemas"])

            # 从配置中提取该 server 的 tools visibility 映射
            visibility_map = None
            server_config = config.get(server_name, {})
            if isinstance(server_config, dict) and "tools" in server_config:
                visibility_map = server_config["tools"]

            if not registry.register_server(server_name, module, visibility_map):
                failed_servers.append(f"{server_name} (registration failed)")

        except ImportError as e:
            failed_servers.append(f"{server_name} (import failed: {e})")
        except Exception as e:
            failed_servers.append(f"{server_name} (error: {e})")
```

- [ ] **Step 4: 提交**

```bash
git add agent/tool_registry.py agent/mcp_loader.py
git commit -m "feat: ToolRegistry stores visibility + mcp_loader parses tools config"
```

---

## Task 3: runner.py 删除硬编码 BASE_MCP_TOOLS + 过滤 hidden

**Files:**
- Modify: `agent/runner.py:33-55` (删除 BASE_MCP_TOOLS)
- Modify: `agent/runner.py:407-419` (_on_turn_end 中引用)
- Modify: `agent/runner.py:518-525` (mcp_tool_scores 过滤)
- Modify: `agent/runner.py:602-626` (chat() 中引用)

- [ ] **Step 1: 删除硬编码 BASE_MCP_TOOLS 列表**

删除 `agent/runner.py` 第33-55行的 `BASE_MCP_TOOLS` 列表定义。

- [ ] **Step 2: 新增 `_get_static_tools()` 方法**

在 `NiuRunner` 类中新增方法（替代硬编码列表）：

```python
    def _get_static_tools(self) -> list:
        """获取 visibility=static 的工具名列表（替代硬编码 BASE_MCP_TOOLS）"""
        from agent.tool_registry import get_registry
        return get_registry().get_static_tools()
```

- [ ] **Step 3: 替换所有 BASE_MCP_TOOLS 引用**

在 `agent/runner.py` 中，将所有 `BASE_MCP_TOOLS` 替换为 `self._get_static_tools()`。涉及位置：

- 第407行 `_on_turn_end()`：`for tool_name in BASE_MCP_TOOLS:` → `for tool_name in self._get_static_tools():`
- 第415行：`if tool_name in BASE_MCP_TOOLS:` → `if tool_name in static_tools:` （需在循环前缓存 `static_tools = set(self._get_static_tools())`）
- 第606行 `chat()`：同上
- 第618行：同上
- 第626行 debug 日志：同上

注意：`_get_static_tools()` 每次调用都查 ToolRegistry，在循环中应先缓存为 `static_tools = set(self._get_static_tools())`。

- [ ] **Step 4: mcp_tool_scores 过滤 hidden 工具**

在第518-525行的 `mcp_tool_scores` 构建中，增加 visibility 检查：

```python
        # 3.5 向量检索到的 MCP 工具：注入 system prompt + 返回分数供 update_from_search
        mcp_tool_scores = {}
        from agent.tool_registry import get_registry
        registry = get_registry()
        for tool in mcp_tools:
            name = tool.metadata.get("name", "")
            server = tool.metadata.get("server", "")
            full_name = f"{server}/{name}" if server else name
            score = tool.score if hasattr(tool, "score") else 0
            if full_name and score > 0 and registry.get_visibility(full_name) != "hidden":
                mcp_tool_scores[full_name] = int(score * 100)
```

- [ ] **Step 5: 提交**

```bash
git add agent/runner.py
git commit -m "feat: replace BASE_MCP_TOOLS with ToolRegistry + filter hidden in mcp_tool_scores"
```

---

## Task 4: tool_lifecycle 过滤 hidden 工具

**Files:**
- Modify: `agent/tool_lifecycle.py:93-122` (_coactivate_same_server_tools)
- Modify: `agent/tool_lifecycle.py:146-153` (get_active_tools)

- [ ] **Step 1: `_coactivate_same_server_tools()` 跳过 hidden 工具**

在第113行 `if s == server and name != tool_name:` 之后，增加 visibility 检查：

```python
                        if s == server and name != tool_name:
                            # 跳过 visibility=hidden 的工具（主 Agent 不可见）
                            from agent.tool_registry import get_registry
                            if get_registry().get_visibility(name) == "hidden":
                                continue
                            current = self.active_tools.get(name, 0)
```

注意：`from agent.tool_registry import get_registry` 应提到循环外，避免重复 import：

```python
        if "/" in tool_name:
            server = tool_name.split("/", 1)[0]
            try:
                from agent.runner import get_runner
                from agent.tool_registry import get_registry
                runner = get_runner()
                registry = get_registry()
                if runner and hasattr(runner, '_mcp_tools_schema'):
                    for schema in runner._mcp_tools_schema:
                        name = schema.get("function", {}).get("name", "")
                        if "/" in name:
                            s, _ = name.split("/", 1)
                        else:
                            continue
                        if s == server and name != tool_name:
                            if registry.get_visibility(name) == "hidden":
                                continue
                            current = self.active_tools.get(name, 0)
                            if current < 65:
                                self.active_tools[name] = 65
                                print(f"[ToolLifecycle] Co-activated: {name} (same server: {server})",
                                      file=sys.stderr, flush=True)
                    self._save_scores()
```

- [ ] **Step 2: `get_active_tools()` 过滤 hidden 工具**

修改第146-153行：

```python
    def get_active_tools(self) -> List[str]:
        """
        获取当前应该注入的工具列表

        过滤掉 visibility=hidden 的工具（防御性，防止持久化文件残留）

        Returns:
            活跃工具名列表
        """
        try:
            from agent.tool_registry import get_registry
            registry = get_registry()
            return [name for name in self.active_tools.keys()
                    if registry.get_visibility(name) != "hidden"]
        except Exception:
            # ToolRegistry 未初始化时，返回全部（向后兼容）
            return list(self.active_tools.keys())
```

- [ ] **Step 3: 提交**

```bash
git add agent/tool_lifecycle.py
git commit -m "feat: tool_lifecycle filters hidden tools in co-activation and get_active_tools"
```

---

## Task 5: 子 Agent 隔离 — handler.py + subagent.py

**Files:**
- Modify: `agent/handler.py:1089-1101` (dispatch 中 hit_tool 调用)
- Modify: `agent/subagent.py:146-148` (创建 handler 时标记)

- [ ] **Step 1: subagent.py 标记 _is_subagent**

在 `agent/subagent.py` 第147-148行，创建 handler 后增加标记：

```python
    # 4. 创建 handler（禁用记忆检索，子 Agent 不需要）
    handler = NiuHandler(mcp_client=mcp_client)
    handler._disable_memory_recall = True
    # 重要约定：子 Agent 必须标记 _is_subagent = True
    # 否则子 Agent 的工具调用会通过 hit_tool() 污染主 Agent 的 tool_lifecycle 分数
    # 新增子 Agent 时必须遵守此约定
    handler._is_subagent = True
```

- [ ] **Step 2: handler.py dispatch() 判断 _is_subagent**

在 `agent/handler.py` 第1092行，修改 hit_tool 调用逻辑：

```python
                # 记录工具命中（在真正执行前）
                # hit_tool 记录命中到 _recent_hits，统一注入时通过 consume_recent_hits 获取
                # 分数由 _inject_dynamic_resources 中的向量检索覆盖管理
                # 子 Agent（_is_subagent=True）跳过 hit_tool()，不污染主 Agent 的 tool_lifecycle
                if not getattr(self, '_is_subagent', False):
                    try:
                        from agent.runner import get_runner
                        runner = get_runner()
                        if runner and hasattr(runner, 'tool_lifecycle'):
                            runner.tool_lifecycle.hit_tool(tool_name)
                            current_score = runner.tool_lifecycle.get_tool_score(tool_name)
                            print(f"[ToolHit] {tool_name} executed (lifecycle score: {current_score})", file=sys.stderr, flush=True)
                    except Exception as e:
                        # 命中记录失败不影响主流程
                        print(f"[ToolHit] Failed to record hit: {e}", file=sys.stderr, flush=True)
```

- [ ] **Step 3: 提交**

```bash
git add agent/handler.py agent/subagent.py
git commit -m "feat: sub-agent isolation - skip hit_tool for sub-agents"
```

---

## Task 6: 向量库初始化脚本同步

**Files:**
- Modify: `scripts/init_vector_db.py:94-130`

- [ ] **Step 1: 修改 `register_mcp_tools()` 只写入 dynamic 工具**

将第123-128行的 BASE_MCP_TOOLS 排除逻辑，替换为基于 visibility 的过滤：

```python
    # 从 mcp-servers.yaml 读取 visibility 配置
    from pathlib import Path
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"
    visibility_map = {}  # "server/name" -> visibility
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            mcp_config = yaml.safe_load(f) or {}
        for server, server_cfg in mcp_config.items():
            if not isinstance(server_cfg, dict):
                continue
            tools_cfg = server_cfg.get("tools", {})
            for tool_name, tool_cfg in tools_cfg.items():
                full_name = f"{server}/{tool_name}"
                visibility_map[full_name] = tool_cfg.get("visibility", "dynamic")

    # 只注册 visibility=dynamic 的工具（static 和 hidden 不存入向量库）
    tools_to_register = []
    for tool in all_tools:
        full_name = f"{tool['server']}/{tool['name']}"
        vis = visibility_map.get(full_name, "dynamic")
        if vis == "dynamic":
            tools_to_register.append(tool)

    static_count = sum(1 for v in visibility_map.values() if v == "static")
    hidden_count = sum(1 for v in visibility_map.values() if v == "hidden")
    logger.info(f"需要注册 {len(tools_to_register)} 个工具（排除 {static_count} 个 static + {hidden_count} 个 hidden）")
```

- [ ] **Step 2: 删除 init_vector_db.py 中对 BASE_MCP_TOOLS 的 import**

删除 `from agent.runner import BASE_MCP_TOOLS` 相关的 import 语句（如果存在）。

- [ ] **Step 3: 提交**

```bash
git add scripts/init_vector_db.py
git commit -m "feat: init_vector_db only registers dynamic-visibility tools"
```

---

## Task 7: 数据清理

**Files:**
- Modify: `~/.niu/tool_scores.json` (删除 hidden 工具分数)
- 向量库删除重建

- [ ] **Step 1: 清理 tool_scores.json 中 hidden 工具的分数**

读取 `~/.niu/tool_scores.json`，根据 mcp-servers.yaml 的 visibility 配置，删除所有 `visibility: hidden` 的工具分数条目，保存回文件。

手动执行或写一次性脚本：

```python
import json, yaml
from pathlib import Path

# 读取 visibility 配置
config_path = Path("config/mcp-servers.yaml")
with open(config_path, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

hidden_tools = set()
for server, server_cfg in config.items():
    if not isinstance(server_cfg, dict):
        continue
    for tool_name, tool_cfg in server_cfg.get("tools", {}).items():
        if tool_cfg.get("visibility") == "hidden":
            hidden_tools.add(f"{server}/{tool_name}")

# 清理分数文件
scores_path = Path.home() / ".niu" / "tool_scores.json"
if scores_path.exists():
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    before = len(scores)
    scores = {k: v for k, v in scores.items() if k not in hidden_tools}
    scores_path.write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Cleaned {before - len(scores)} hidden tool scores, {len(scores)} remaining")
```

- [ ] **Step 2: 删除向量库，重新初始化**

```bash
rm -f REDACTED_WIN_PATH/vectors.db
python scripts/init_vector_db.py
```

- [ ] **Step 3: 验证**

1. 启动应用，检查日志中工具数量是否正确（主 Agent 只看到 static + dynamic 工具）
2. 调用 `kg-server/explore_node`，检查不会 co-activate 其他 kg-server 工具
3. 检查 `~/.niu/tool_scores.json` 不会出现 hidden 工具的分数
4. 子 Agent 调用工具后，检查主 Agent 的 tool_lifecycle 不受影响

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "feat: tool visibility system complete - static/dynamic/hidden"
```
