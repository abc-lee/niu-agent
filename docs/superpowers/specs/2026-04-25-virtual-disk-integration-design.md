# Virtual Disk Integration Design

> Date: 2026-04-25
> Status: Draft — pending user review

---

## Problem

主 Agent 同时注入 100+ MCP 工具 schema 到 LLM prompt，导致：

1. **工具选择混乱** — LLM 在大量相似描述中选错工具
2. **Prompt 膨胀** — 每轮消耗大量 token 在工具描述上
3. **新增工具代价高** — 每个 MCP 服务新增工具都需要配置 visibility
4. **LLM 无法自学** — 工具全部摊开，LLM 没有探索和记忆的空间

## Solution

将所有 MCP 工具收归为 1 个 `disk()` 工具。LLM 用 Unix 命令直觉自主探索和调用。

主 Agent 只看到 5 个工具：code_run, file_read, file_patch, file_write, disk()。

所有 MCP 工具通过 `disk(command)` 访问，不再直接注入 LLM schema。

子 Agent 不受影响，继续用原始 ToolRegistry 调用方式。

---

## Architecture

```
LLM 看到的:              调度层内部:                    实际调用:
┌──────────────┐    ┌──────────────────────┐    ┌──────────────┐
│ disk("ls /") │    │ DiskEngine           │    │ tool_registry │
│ disk("/kg/   │───>│  ├─ DiskParser       │───>│  .get(name)   │
│  explore_node│    │  ├─ DiskNavigator    │    │  func(**args) │
│  Einstein")  │    │  ├─ DiskExecutor     │    └──────────────┘
└──────────────┘    │  └─ DiskErrors       │
                    │                      │
                    │ config/disk/*.yaml    │  ← 工具描述配置
                    └──────────────────────┘
```

---

## Design Decisions

### 1. Schema Injection

**Before**: `set_mcp_tools_schema()` 注入所有 MCP 工具 schema + `_on_turn_end()` 每轮刷新动态 top-10

**After**: `set_mcp_tools_schema()` 只注入 4 个基础工具 + 1 个 `disk()` 工具

- No dynamic schema refresh
- No tool_lifecycle decay/override scoring
- All MCP tools become `visibility: hidden` (not injected into LLM)

### 2. System Prompt Injection

**Before**: `_inject_dynamic_resources()` injects skills + mcp_tools + knowledge + interaction_habits + brain memories + MCP tool scores

**After**: `_inject_dynamic_resources()` injects skills + knowledge + interaction_habits + brain memories (no MCP tool scores)

**New**: System prompt includes disk tool description + dynamic directory listing:

```
你有一个虚拟磁盘工具 disk(command)，可以用 Unix 命令探索和调用所有 MCP 工具。

命令: ls [path] 列出目录, cat <path> 查看工具说明, /<dir>/<tool> [args] 执行工具

当前磁盘目录:
  /kg        — 知识图谱：实体关系探索与图谱查询
  /memory    — 记忆系统：长期记忆存取与检索
  /photos    — 照片管理：人物识别与照片检索
  ...
```

Directory listing is dynamically generated from `DiskConfig.list_directories()`, not hardcoded. This supports future dynamic MCP additions.

### 3. Handler Dispatch

**Before**: `dispatch()` routes MCP tools via `tool_registry.call(server/tool, args)` directly

**After**: `dispatch()` routes `disk` tool via `DiskEngine.execute(command)`, other tools via `tool_registry.call()`

- Disk EXECUTE action: returns raw MCP result, uses real tool path for after_callback
- Disk NAVIGATE/ERROR action: returns text, uses "disk" for after_callback

### 4. MCP Tool Registration

**Before**: MCP tools registered to both ToolRegistry and LightRAG (for dynamic retrieval)

**After**: MCP tools registered only to ToolRegistry (for disk executor to call). No LightRAG registration needed — disk YAML config serves as the discovery mechanism, not LightRAG search.

Dream evolver will consolidate tool usage experience into LightRAG brain regions organically.

### 5. YAML Configuration

**Current YAML files**: 9 server configs (kg-server, memory-server, photo-server, config-manager, vector-store, file-parser, scheduler-server, session-manager, browser-server)

**New**: Add `lightrag-server.yaml` for the 12 lightrag-server tools

**YAML format**: Must comply with Anthropic MCP standard schema format for future external MCP compatibility. The current format was designed for our internal tools; we need to align it with the MCP standard `inputSchema` format so external MCP servers can be plugged in seamlessly.

**Maintenance**: Existing YAML files stay as-is. New MCP additions are handled by the main Agent itself — the configuration method is documented in the system management manual, so the Agent knows how to modify YAML configs to add new MCP servers.

### 6. Cleanup Scope

**Delete entirely**:
- `agent/tool_lifecycle.py` — MCP tool decay/override scoring no longer needed

**Delete from runner.py**:
- `_build_tool_scores_from_lightrag()` — no MCP tool scores needed
- `_search_tool_signal_skills_lightrag()` — no tool-signal skills needed
- `_format_lightrag_entities_for_prompt()` — MCP tool formatting removed
- `_on_turn_end()` dynamic schema refresh logic — no dynamic tools anymore
- `_inject_dynamic_resources()` MCP tool score building — only skills/knowledge/habits remain

**Delete from mcp-servers.yaml**:
- `visibility: dynamic` category — all MCP tools become `visibility: hidden`

**Delete from niu_api/injector.py**:
- MCP tool registration to LightRAG (both single and batch) — disk YAML config replaces LightRAG as discovery mechanism

**Delete from agent/injector/sync.py**:
- MCP tool sync to LightRAG — no longer needed

**Keep unchanged**:
- `agent/tool_registry.py` — disk executor and sub-agents still need it
- `niu_api/internal/lightrag_adapter.py` — skills/knowledge retrieval still uses LightRAG
- `niu_api/internal/lightrag_manager.py` — LightRAG instance management unchanged
- Skills/knowledge/interaction_habits system prompt injection — preserved
- Sub-agent ToolRegistry access — preserved

---

## Implementation Plan (TDD)

### Phase 1: Disable dynamic injection (keep code, just turn off)

1. Modify `runner.py` `set_mcp_tools_schema()` to only inject base_tools + disk_schema
2. Modify `runner.py` `_on_turn_end()` to skip dynamic schema refresh
3. Modify `mcp-servers.yaml` to set all MCP tools to `visibility: hidden`
4. Write integration test: verify LLM only sees 5 tools
5. Run existing tests to verify no breakage

### Phase 2: Create lightrag-server.yaml

1. Create `config/disk/lightrag-server.yaml` with all 12 lightrag-server tools
2. Align YAML format with Anthropic MCP standard `inputSchema` format
3. Run disk tests to verify lightrag directory is accessible
4. Write test: `disk("ls /lightrag")` returns expected tool list

### Phase 3: System prompt disk description

1. Modify `_inject_dynamic_resources()` to add disk tool description + directory listing to system prompt
2. Remove MCP tool score building from `_inject_dynamic_resources()`
3. Write test: verify system prompt contains disk description and directory listing

### Phase 4: Handler dispatch integration

1. Modify `handler.py` `dispatch()` to route `disk` tool via DiskEngine
2. Write test: verify `disk("ls /")` returns directory listing
3. Write test: verify `disk("/memory/remember 'test'")` executes correctly

### Phase 5: Cleanup

1. Delete `agent/tool_lifecycle.py`
2. Delete `_build_tool_scores_from_lightrag()`, `_search_tool_signal_skills_lightrag()` from runner.py
3. Delete MCP tool registration to LightRAG from `niu_api/injector.py` and `agent/injector/sync.py`
4. Update all affected tests
5. Run full test suite

---

## MCP Standard Compliance

The disk YAML configuration format must align with Anthropic's MCP standard `inputSchema` format for future external MCP compatibility.

### MCP Standard Tool Definition

```json
{
  "name": "remember",
  "description": "保存长期记忆（自动生成 L0/L1/L2 三层）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "content": { "type": "string", "description": "记忆内容" },
      "memory_type": { "type": "string", "enum": ["environment", "preferences", "skills", "experiences", "facts"] }
    },
    "required": ["content", "memory_type"]
  }
}
```

### Current YAML Format vs MCP Standard

| Aspect | Current YAML | MCP Standard | Resolution |
|--------|-------------|--------------|------------|
| Description | `short` + `long` | single `description` | `long` → `description`, `short` kept as CLI summary |
| Parameters | `parameters` list | `inputSchema.properties` object | Keep list format (CLI needs ordering), add `inputSchema` field for MCP |
| Required | per-param `required: true` | top-level `required: [...]` | Parser supports both |
| CLI extensions | `position`, `flag`, `cli_format` | none | Keep as custom extensions (disk-specific, not in MCP) |
| Type constraints | `enum`, `constraints` | `enum`, `minimum`, `maximum`, `items` | Already aligned |

### Strategy: Dual-format YAML

YAML files support both CLI extensions (for disk command-line) and MCP standard fields (for external compatibility):

```yaml
tools:
  - name: remember
    description: "保存长期记忆（自动生成L0/L1/L2三层）"  # MCP standard
    short: "保存长期记忆"                                 # CLI summary
    category: write
    parameters:          # CLI format (with position, flag)
      - name: content
        position: 1
        type: string
        required: true
      - name: memory_type
        flag: type
        type: string
        required: true
        enum: [environment, preferences, skills, experiences, facts]
    # Optional: MCP standard inputSchema (for external MCP auto-import)
    # inputSchema:
    #   type: object
    #   properties:
    #     content: { type: string, description: "记忆内容" }
    #     memory_type: { type: string, enum: [...] }
    #   required: [content, memory_type]
```

- `parameters` + CLI extensions: used by DiskExecutor for Unix command-line parsing
- `inputSchema` (optional): used for MCP standard tool discovery (`tools/list`)
- If `inputSchema` is absent, DiskConfig auto-generates it from `parameters`
- External MCP servers imported via `tools/list` auto-populate `inputSchema`, CLI fields derived from it

### External MCP Auto-Discovery (Future)

When a new MCP server is added, the system can:
1. Call `tools/list` to get all tool definitions with `inputSchema`
2. Auto-generate YAML config from `inputSchema` (derive CLI fields: position from required-first, flag from param name)
3. Write YAML to `config/disk/<server-name>.yaml`
4. DiskConfig reloads, new tools immediately available via `disk()`

This is the mechanism by which the main Agent can add new MCP servers by editing YAML configs.

---

## Known Limitations

1. **YAML vs code drift** — startup warning but no block. Manual sync required.
2. **disk_mode: false fallback not implemented** — direct replacement, no fallback path.
3. **External MCP auto-discovery** — not yet implemented. Agent must manually edit YAML.
4. **Token cost of disk exploration** — first interaction requires `ls` + `cat` to discover tools, adding 1-2 extra rounds. But once in context, no extra cost.