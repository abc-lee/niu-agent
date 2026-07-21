# 向量库初始化完整清单

> **版本：** v1.0
> **创建日期：** 2026-04-12
> **目的：** 解决反复丢失初始化清单的问题，确保向量库初始化可重复、可追溯

---

## 一、向量库的作用

向量库是系统的**语义大脑**，用于：
1. **MCP 工具语义检索** - 根据用户意图匹配工具
2. **递归查询桥梁** - 连接用户口语化表达和正式工具描述
3. **知识语义搜索** - 文档、技能、系统手册检索

---

## 二、递归查询机制

### 2.1 核心原理

```
用户输入："五分钟后提醒我吃药"
    ↓ 第一轮检索
查询模式库（query_pattern）
    匹配到："remind me in X minutes"
    提取：refined_query = "schedule task"
    ↓ 第二轮检索
MCP 工具库（mcp_tool）
    匹配到：scheduler-server/schedule_task
```

### 2.2 数据流向

```
用户口语化表达 → query_pattern (is_recursive=True) → refined_query → mcp_tool
```

**关键标记：**
- `is_recursive: True` - 触发递归查询
- `refined_query` - 第二轮检索使用的关键词
- `category: "query_pattern"` - 查询模式分类
- `category: "mcp_tool"` - MCP 工具分类

---

## 三、初始化流程

### 3.1 主脚本

**文件：** `scripts/init_vector_db.py`

**功能：**
1. 创建向量库表结构
2. 同步 Skills 到向量库
3. 注册 MCP 工具描述
4. 注册查询模式
5. 注入系统说明书摘要

**执行：**
```bash
cd E:/tools/ai-bot
python scripts/init_vector_db.py
```

### 3.2 辅助脚本

| 脚本 | 功能 | 使用场景 |
|------|------|---------|
| `export_all_mcp_tools.py` | 导出所有 MCP 工具到 JSON | 初始化前导出工具定义 |
| `register_all_mcp_tools_from_json.py` | 从 JSON 批量注册工具 | 快速批量注册 |
| `check_mcp_tools_in_db.py` | 检查向量库中的工具状态 | 验证注册结果 |
| `index_query_patterns.py` | 注册查询模式 | 初始化查询模式 |
| `optimize_all_mcp_tools.py` | 优化工具 L1 描述 | 改进工具描述质量 |

**数据文件：**
- `data/mcp_tools.json` - 所有 MCP 工具定义的快照（67个工具的完整描述）

---

## 四、MCP 工具分层架构

### 4.1 基础工具层（BASE_MCP_TOOLS，12个）

**特征：** 固定注入到主 Agent，**不需要在向量库注册**

**定义位置：** `agent/runner.py` 第 33-51 行

**工具列表：**
```python
BASE_MCP_TOOLS = [
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

    # browser-server (1个)
    "browser-server/browser_navigate",
]
```

**为什么不需要注册？**
- 这些工具已经在代码中固定注入
- 每轮对话都可用，无需通过向量库发现
- 避免重复注入造成混淆

### 4.2 动态注入层（需要向量库注册）

**特征：** 通过向量库语义检索发现，按需注入

**注入机制：**
- `ToolLifecycleManager` 管理生命周期
- 初始分数 100，每轮衰减 10 分
- 分数 < 50 时自动移除

**需要注册的工具类别：**

1. **scheduler-server (4个)** - 定时任务管理
   - schedule_task
   - list_scheduled_tasks
   - cancel_task
   - update_task

2. **file-parser (2个)** - 文档解析
   - parse_file
   - list_supported_formats

3. **photo-server (14个)** - 照片处理与人脸识别
   - ingest_document
   - ingest_documents
   - ingest_photo
   - ingest_photos
   - name_person
   - merge_persons
   - search_persons
   - get_unnamed_persons
   - delete_person
   - cleanup_deleted_photos
   - get_person_photos
   - store_document_l1
   - store_documents_l1
   - unload_face_model

4. **kg-server (12个)** - 知识图谱
   - create_document
   - create_entity
   - create_concept
   - link_document_entity
   - link_document_concept
   - link_entities
   - get_document
   - list_documents
   - search_documents
   - get_related_entities
   - get_related_concepts
   - query_graph

5. **config-manager (20个)** - 配置管理
   - get_llm_config
   - set_llm_config
   - list_llm_presets
   - test_llm_connection
   - get_storage_config
   - set_storage_config
   - get_identity
   - update_identity
   - get_workspace
   - set_workspace
   - get_user_info
   - set_user_info
   - add_user_preference
   - is_first_run
   - complete_setup
   - get_full_memory
   - mkdir
   - copy_to_path
   - move_to_path
   - list_files_in_workspace

6. **session-manager (2个)** - 会话管理
   - get_messages
   - delete_messages

**总计：54 个工具需要注册到向量库**

### 4.3 子 Agent 专用工具（不注册）

**特征：** 主 Agent 通过 `chat-with-xxx` 委托给子 Agent，不在主 Agent 工具列表中出现

**说明：**
- 这些工具由子 Agent 管理
- 主 Agent 不直接调用
- 无需在主 Agent 的向量库中注册

---

## 五、查询模式库

### 5.1 已定义的查询模式（20个）

**文件：** `scripts/index_query_patterns.py`

**分类：**

1. **记忆管理类（8个）**
   - recall_memory_1, recall_memory_2
   - remember_this, remember_what_i_like
   - zh_recall_1, zh_recall_2
   - zh_remember_1, zh_remember_2

2. **文档检索类（7个）**
   - search_documents, find_documents, retrieve_knowledge
   - zh_search_1, zh_search_2
   - zh_retrieve_1

3. **文档添加类（3个）**
   - add_document, save_document
   - zh_add_1, zh_save_1

4. **复杂场景（2个）**
   - complex_memory_1, complex_search_1

### 5.2 TDD 自动生成的查询模式

**位置：** `scripts/query_pattern/`

**流程：**
1. `step1_generate.py` - 生成候选模式
2. `step2_write.py` - 写入向量库
3. `step3_test.py` - 测试递归查询效果
4. `verified_patterns.jsonl` - 验证通过的模式

**已生成的模式：**
- `candidates.jsonl` - 12 条候选模式（针对 scheduler-server/list_scheduled_tasks）
- `verified_patterns.jsonl` - 2 条已验证模式

**说明：** TDD 流水线会根据工具描述自动生成口语化查询模式，并验证递归检索效果。

---

## 六、初始化脚本修正方案

### 6.1 问题诊断

**当前问题：**
```python
# scripts/init_vector_db.py 第 105-122 行
tools = [
    # ==================== 动态加载工具 ====================
    # browser-server (1个) - 按需启动，浏览器自动化
    # 注意：browser_navigate 已加入 BASE_MCP_TOOLS（见 agent/runner.py）
    # 其他浏览器操作通过 code_run + BrowserManager 完成

    # ==================== 子Agent专用工具（不注册）====================
    # photo-server (14个) - 子Agent专用，主Agent通过 chat-with-file-processor 委托
    # scheduler-server (4个) - 子Agent专用，主Agent通过 chat-with-event-manager 委托
    # kg-server (12个) - 底层操作，不暴露给主Agent
    # file-parser (2个) - 底层操作，不暴露给主Agent
    # config-manager - 已删除，用 bash + file 操作替代

    # 注意：
    # 1. 基础工具（memory-server + vector-store）已在 BASE_MCP_TOOLS 固定注入
    # 2. 子Agent专用工具不在向量库注册，避免主Agent误用
    # 3. 浏览器自动化架构：browser_navigate (MCP) + code_run + BrowserManager
]
```

**问题：** `tools` 列表是空的，导致向量库中没有 MCP 工具描述。

### 6.2 解决方案

**方案一：使用辅助脚本批量注册**

```bash
# 1. 导出所有工具定义
python scripts/export_all_mcp_tools.py

# 2. 从 JSON 批量注册
python scripts/register_all_mcp_tools_from_json.py

# 3. 验证
python scripts/check_mcp_tools_in_db.py
```

**方案二：修正 `init_vector_db.py` 的 `register_mcp_tools()` 函数**

**原则：**
- 注册所有 54 个非基础工具
- 使用 `export_all_mcp_tools.py` 导出的工具定义
- 工具描述使用英文（符合 L1 规范）

**修正代码：**
```python
def register_mcp_tools():
    """注册 MCP 工具描述到向量库（L1级别）

    注册策略：
    - 从 logs/all_mcp_tools.json 读取工具定义
    - 排除 BASE_MCP_TOOLS 中的基础工具
    - 注册所有其他工具用于递归检索
    """
    logger.info("注册 MCP 工具描述...")

    # 读取工具定义
    json_file = Path(__file__).parent.parent / "logs" / "all_mcp_tools.json"
    if not json_file.exists():
        logger.error("✗ 工具定义文件不存在，请先运行: python scripts/export_all_mcp_tools.py")
        return

    with open(json_file, 'r', encoding='utf-8') as f:
        tools_by_server = json.load(f)

    # 展平为单个列表
    all_tools = []
    for server, tools in tools_by_server.items():
        for tool in tools:
            all_tools.append(tool)

    # 排除基础工具
    base_tool_names = set(BASE_MCP_TOOLS)
    tools_to_register = [
        tool for tool in all_tools
        if f"{tool['server']}/{tool['name']}" not in base_tool_names
    ]

    logger.info(f"需要注册 {len(tools_to_register)} 个工具（排除 {len(base_tool_names)} 个基础工具）")

    # 注册每个工具
    # ... 后续注册逻辑 ...
```

---

## 七、执行清单

### 7.1 完整初始化流程

```bash
# 1. 确保服务已停止
# （避免并发写入向量库）

# 2. 导出所有 MCP 工具定义
cd E:/tools/ai-bot
python scripts/export_all_mcp_tools.py
# 输出：data/mcp_tools.json

# 3. 初始化向量库
python scripts/init_vector_db.py
# 根据提示选择：
# - 是否初始化 Query Patterns？[y/N] → 建议选 y

# 4. 验证初始化结果
python scripts/check_mcp_tools_in_db.py
# 预期输出：
# MCP tools in vector DB: 54
# By server:
#   config-manager: 20
#   photo-server: 14
#   kg-server: 12
#   scheduler-server: 4
#   file-parser: 2
#   session-manager: 2

# 5. 测试递归查询
python scripts/query_pattern/test_recursive_search.py
```

### 7.2 快速修复流程（如果向量库已存在但为空）

```bash
# 1. 注册 MCP 工具
python scripts/export_all_mcp_tools.py
python scripts/register_all_mcp_tools_from_json.py

# 2. 注册查询模式
python scripts/index_query_patterns.py

# 3. 验证
python scripts/check_mcp_tools_in_db.py
```

---

## 八、故障排查

### 8.1 向量库文件路径错误

**症状：** `check_mcp_tools_in_db.py` 显示 0 个工具

**排查：**
```bash
# 检查向量库路径
cat ~/.niu/memory.json | python -m json.tool | grep -A 2 workspace

# 预期输出：
# "workspace": {
#   "path": "REDACTED_WIN_PATH",
#   ...
# }

# 检查向量库文件
ls -lh REDACTED_WIN_PATH/vectors.db
```

**解决：** 确保 `memory.json` 中的 `workspace.path` 正确。

### 8.2 工具注册失败

**症状：** 日志中出现 "工具定义文件不存在"

**原因：** `data/mcp_tools.json` 不存在

**解决：**
```bash
# 导出工具定义
python scripts/export_all_mcp_tools.py

# 验证文件
ls -lh data/mcp_tools.json
```

**症状：** 日志中出现 "向量生成失败"

**原因：** embedding 服务未启动或过载

**解决：**
```bash
# 方案1: 分批注册
# 修改 register_all_mcp_tools_from_json.py 中的 time.sleep 参数，增加延迟

# 方案2: 重启 embedding 服务
# 停止所有 Python 进程，重新运行初始化脚本
```

### 8.3 递归查询无结果

**症状：** `test_recursive_search.py` 显示 "第一轮无结果"

**原因：** 查询模式未注册或向量未生成

**解决：**
```bash
# 重新注册查询模式
python scripts/index_query_patterns.py

# 验证
sqlite3 REDACTED_WIN_PATH/vectors.db "SELECT id, content FROM documents WHERE json_extract(metadata, '$.category') = 'query_pattern'"
```

### 8.4 UnicodeDecodeError 错误

**症状：** 初始化脚本运行时出现 UnicodeDecodeError

**原因：** Windows 默认编码（cp1252）无法处理 UTF-8 字符

**解决：** 已在脚本中添加 `encoding='utf-8', errors='replace'` 参数

**验证：** 确保使用修正后的 `init_vector_db.py`

---

## 九、维护指南

### 9.1 新增 MCP 工具

**步骤：**
1. 在 `mcp-servers/` 中实现工具
2. 运行 `python scripts/export_all_mcp_tools.py` 更新工具定义
3. 运行 `python scripts/register_all_mcp_tools_from_json.py` 注册新工具
4. 验证：`python scripts/check_mcp_tools_in_db.py`

### 9.2 新增查询模式

**手动添加：**
1. 编辑 `scripts/index_query_patterns.py` 的 `QUERY_PATTERNS` 列表
2. 运行 `python scripts/index_query_patterns.py`
3. 测试：`python scripts/query_pattern/test_recursive_search.py`

**自动生成：**
1. 编辑 `scripts/query_pattern/candidates.jsonl` 添加候选模式
2. 运行 `python scripts/query_pattern/pipeline.py`
3. 验证生成的模式

### 9.3 备份与恢复

**备份向量库：**
```bash
# 备份
cp REDACTED_WIN_PATH/vectors.db REDACTED_WIN_PATH/vectors.db.backup

# 恢复
cp REDACTED_WIN_PATH/vectors.db.backup REDACTED_WIN_PATH/vectors.db
```

**备份工具定义：**
```bash
# 备份
cp data/mcp_tools.json data/mcp_tools_$(date +%Y%m%d).json

# 恢复
cp data/mcp_tools_20260412.json data/mcp_tools.json
```

---

## 十、关键文件清单

| 文件 | 用途 | 修改频率 |
|------|------|---------|
| `docs/VECTOR_DB_INIT_CHECKLIST.md` | 本文档，初始化清单 | 低 |
| `scripts/init_vector_db.py` | 主初始化脚本 | 中 |
| `scripts/export_all_mcp_tools.py` | 导出工具定义 | 低 |
| `scripts/register_all_mcp_tools_from_json.py` | 批量注册工具 | 低 |
| `scripts/index_query_patterns.py` | 注册查询模式 | 中 |
| `scripts/check_mcp_tools_in_db.py` | 验证工具状态 | 低 |
| `data/mcp_tools.json` | 工具定义快照（67个工具） | 每次导出更新 |
| `scripts/query_pattern/verified_patterns.jsonl` | 验证通过的查询模式 | 每次生成更新 |
| `agent/runner.py` | BASE_MCP_TOOLS 定义 | 低 |

---

## 十一、Playwright 补丁说明

> **已废弃（2026-07-21）**：browser-server 已迁移到 WebSocket Bridge + 系统 Chrome 启动器（见 `mcp-servers/browser-server/src/niu_browser_server/launcher.py` 和 `ws_bridge.py`），不再依赖 playwright。原补丁脚本 `scripts/patch_playwright_asyncio.py` 已删除，本节保留标题仅作历史记录。

---

## 十一、总结

### 11.1 关键原则

1. **基础工具（12个）不注册** - 已在代码中固定注入
2. **其他工具（54个）必须注册** - 支持递归检索
3. **查询模式（20+）必须注册** - 连接用户表达和工具描述
4. **工具描述统一英文** - 符合 L1 规范
5. **向量必须 L2 归一化** - 保证检索质量

### 11.2 初始化检查清单

- [ ] 服务已停止
- [ ] 工具定义已导出（`logs/all_mcp_tools.json` 存在）
- [ ] 向量库路径正确（`memory.json` 中的 `workspace.path`）
- [ ] MCP 工具已注册（`check_mcp_tools_in_db.py` 显示 54 个工具）
- [ ] 查询模式已注册（`index_query_patterns.py` 执行成功）
- [ ] 递归查询测试通过（`test_recursive_search.py` 显示正确结果）

---

**文档维护者：** Claude Sonnet 4.6
**最后更新：** 2026-04-12
**下次更新触发条件：** 工具列表变更、架构调整、初始化流程优化
