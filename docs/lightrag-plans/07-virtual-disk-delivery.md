# Virtual Disk — 交付文档

> 交付时间: 2026-04-25
> 对应设计: `/docs/lightrag-plans/06-brain-region-activation.md` 同级目录下
> TDD 测试: 149 项全绿

---

## 一、解决了什么问题

主 Agent 同时注入 91 个 MCP 工具描述到 LLM prompt，导致：

1. **工具选择混乱** — LLM 在大量相似描述中选错工具
2. **Prompt 膨胀** — 每轮消耗大量 token 在工具描述上
3. **新增工具代价高** — 每个 MCP 服务新增工具都需要在 visibility 体系里配置
4. **LLM 无法自学** — 工具全部摊开，LLM 没有探索和记忆的空间

## 二、实现方案

将 87 个 MCP 工具（排除 4 个 nanobot.system 内建工具）收归为 **1 个 `disk()` 工具**。LLM 用 Unix 命令直觉自主探索和调用。

### LLM 看到的

```
工具: disk(command: str)
描述: Virtual tool disk — Unix-like shell to discover and execute tools.
      Directories: kg=kg, memory=memory, photos=photo, config=config-manager, ...
      Commands: ls [path] list, cat <path> help, /<dir>/<tool> [args] execute.
```

### LLM 的典型交互

```
用户: 帮我查一下Einstein在知识图谱里的关系
LLM:  disk("ls /kg")                    → 看到 explore_node 等工具
LLM:  disk("cat /kg/explore_node")       → 阅读参数说明
LLM:  disk("/kg/explore_node Einstein")  → 执行，返回结果

# 后续对话中 LLM 记住结构，直接调用:
LLM:  disk("/memory/remember '用户喜欢简洁回答' --type preferences")
```

### 错误自修复

LLM 不看 `cat` 直接尝试参数时，错误信息**完全自包含**：

```
输入: disk("/kg/explore_node")
输出: /kg/explore_node: missing required argument <entity_id>.

      Usage: /kg/explore_node <entity_id> [options]

      ARGUMENTS:
        entity_id           实体ID

      OPTIONS:
        --depth N           遍历深度 (default: 2)
        --min-confidence N  最小置信度 (default: 0.0)
        --direction VAL     方向: both|outgoing|incoming (default: both)

      EXAMPLE:
        /kg/explore_node Einstein

(Tip: Use 'cat /kg/explore_node' to review full documentation before first use.)
```

一次错误即可修复，不需要额外的 `cat` 调用。连续 3 次同工具错误自动升级为完整帮助。

## 三、文件清单

### 新增文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `niu_api/internal/disk_config.py` | ~310 | YAML 配置加载 + 10 项启动校验 + Registry 交叉验证 |
| `niu_api/internal/disk_parser.py` | ~270 | 命令分词、Shell 特殊语法检测、--终止符、动作识别 |
| `niu_api/internal/disk_errors.py` | ~280 | 18 种错误场景模板 + 模糊匹配建议 + 循环升级 |
| `niu_api/internal/disk_navigator.py` | ~150 | ls/cat/help 导航 + 分类显示 + ls --all |
| `niu_api/internal/disk_executor.py` | ~220 | 参数校验 + 类型转换 + CLI→MCP kwargs + 执行 |
| `niu_api/internal/disk_engine.py` | ~100 | 主引擎 + DiskResult + get_schema() |
| `config/disk/disk.yaml` | 8 | 全局配置（排除列表、功能开关） |
| `config/disk/kg-server.yaml` | ~200 | 知识图谱服务 (23 工具) |
| `config/disk/memory-server.yaml` | ~80 | 记忆服务 (9 工具) |
| `config/disk/photo-server.yaml` | ~90 | 照片管理 (9 工具) |
| `config/disk/vector-store.yaml` | ~60 | 向量存储 (7 工具) |
| `config/disk/config-manager.yaml` | ~180 | 系统配置 (20 工具) |
| `config/disk/file-parser.yaml` | ~15 | 文档解析 (2 工具) |
| `config/disk/scheduler-server.yaml` | ~30 | 任务调度 (4 工具) |
| `config/disk/session-manager.yaml` | ~30 | 会话管理 (4 工具) |
| `config/disk/browser-server.yaml` | ~50 | 浏览器 (5 工具) |
| `tests/test_disk_config.py` | ~260 | 配置加载 + 校验测试 (27 用例) |
| `tests/test_disk_parser.py` | ~150 | 命令解析测试 (37 用例) |
| `tests/test_disk_errors.py` | ~170 | 错误模板测试 (29 用例) |
| `tests/test_disk_navigator.py` | ~130 | 导航系统测试 (17 用例) |
| `tests/test_disk_executor.py` | ~180 | 执行引擎测试 (25 用例) |
| `tests/test_disk_engine.py` | ~140 | 集成测试 (14 用例) |

### 修改的文件

| 文件 | 修改内容 |
|------|---------|
| `agent/runner.py` L297 | 创建 DiskEngine 实例，传入 handler |
| `agent/runner.py` L342 | `set_mcp_tools_schema()` 末尾注入 disk schema |
| `agent/runner.py` L471 | `_on_turn_end()` 中 disk 加入 static_tools |
| `agent/handler.py` L251 | `__init__()` 新增 disk_engine 参数 |
| `agent/handler.py` L793 | `dispatch()` 新增 disk 命令路由分支 |

## 四、架构说明

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

### 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| DiskEngine 同步/异步 | **同步** | handler.dispatch() 是同步生成器，无法 await |
| 原始 schema 保留 | **双轨制** | `_mcp_tools_schema` 保留原始 + LLM 只看到 disk() |
| EXECUTE 返回值 | **原始 MCP 结果** | 保留 status 检查和 memory dirty flag |
| 导航返回值 | **str** | ls/cat/help 纯文本展示 |
| 功能开关 | `disk_mode: true` | config/disk/disk.yaml 可随时关闭回退 |

### ToolRegistry 保持不变

调度层只改变 LLM 看到和交互的方式。ToolRegistry 内部注册和调用逻辑原封不动。子 Agent 直接读 ToolRegistry，不经过 disk。

## 五、使用指南

### 启用 / 禁用

编辑 `config/disk/disk.yaml`:

```yaml
disk_mode: true   # 启用虚拟磁盘
disk_mode: false  # 禁用，回退到原有工具注入方式
```

> 注意: 当前 `disk_mode` 标志已写入配置，但 runner.py 中的回退逻辑尚未实现。
> 目前启用状态是硬编码的。如需回退，撤销 runner.py 的 3 处修改即可。

### 新增 MCP 服务

1. 在 `config/disk/` 下新建 `<server-name>.yaml`
2. 按 YAML 规范填写目录名、描述、工具列表和参数
3. 重启 niu-agent，启动校验自动检查配置

YAML 规范支持两种格式:

```yaml
# 格式一: dict（推荐，与设计文档一致）
tools:
  tool_name:
    summary: "简短描述"
    description: "完整描述"
    args:
      - name: param1
        position: 1
        type: string
        required: true

# 格式二: list（当前 YAML 文件使用的格式）
tools:
  - name: tool_name
    short: "简短描述"
    long: "完整描述"
    parameters:
      - name: param1
        position: 1
        type: string
        required: true
```

两种格式等效，解析器自动识别。

### 排除工具

在 `config/disk/disk.yaml` 的 `exclude_tools` 列表中添加工具全名:

```yaml
exclude_tools:
  - nanobot.system/code_run    # 已排除: 内建工具保持静态注入
  - nanobot.system/file_read
  - nanobot.system/file_patch
  - nanobot.system/file_write
```

### Hidden 工具

YAML 中设置 `hidden: true` 的工具:
- `ls /dir` 不显示
- `ls --all /dir` 显示在 `[hidden]` 分组
- 直接路径可调用: `disk("/kg/delete_entity xxx")`

## 六、错误场景覆盖

| # | 场景 | 自包含 | 升级机制 |
|---|------|--------|---------|
| E1 | 未知命令 | ✓ | — |
| E2 | 路径不存在（列出可用目录） | ✓ | — |
| E3 | 工具不存在（完整列表不截断） | ✓ | — |
| E4 | 目录当文件读 | ✓ | — |
| E5 | 缺少必填参数（含全部参数说明） | ✓ | E17 |
| E6 | 参数类型错误 | ✓ | E17 |
| E7 | 未知 flag（模糊匹配建议） | ✓ | E17 |
| E8 | 枚举值错误 | ✓ | E17 |
| E9 | 约束越界 | ✓ | E17 |
| E10 | 位置参数过多 | ✓ | E17 |
| E11 | 空命令 | ✓ | — |
| E12 | MCP 执行失败（透传错误） | — | — |
| E13 | cd 尝试 | ✓ | — |
| E14 | flag 缺少值 | ✓ | E17 |
| E15 | 尝试执行目录 | ✓ | — |
| E16 | Shell 特殊语法（管道/链接/通配符） | ✓ | — |
| E17 | 连续 3 次错误升级为完整帮助 | ✓✓ | — |
| E18 | 互斥参数冲突 | ✓ | E17 |

## 七、迁移步骤

### 从 ai-bot 同步到 niu-agent

如果 ai-bot 侧也有改动，按以下步骤迁移:

```bash
# 1. 同步新增文件（如果尚未同步）
cp /Users/lilei/tools/ai-bot/niu_api/internal/disk_*.py /Users/lilei/tools/niu-agent/niu_api/internal/
cp -r /Users/lilei/tools/ai-bot/config/disk/ /Users/lilei/tools/niu-agent/config/disk/
cp /Users/lilei/tools/ai-bot/tests/test_disk_*.py /Users/lilei/tools/niu-agent/tests/

# 2. 验证测试
cd /Users/lilei/tools/niu-agent
python -m pytest tests/test_disk_*.py -v

# 3. 检查 runner.py 和 handler.py 的修改是否冲突
git diff agent/runner.py agent/handler.py
```

### 从 niu-agent 迁移到其他项目

1. 复制 6 个 `disk_*.py` 文件到目标项目的内部模块目录
2. 复制 `config/disk/` 目录到目标项目的配置目录
3. 在 LLM 工具注入层（类似 runner.py 的 `set_mcp_tools_schema`）加入 disk schema
4. 在工具分发层（类似 handler.py 的 `dispatch`）加入 disk 路由
5. 运行 149 项测试验证

## 八、已知限制与后续工作

### 当前限制

1. **YAML vs 代码漂移** — 启动时 warning 提醒，但不阻止运行。需要人工同步 YAML 与 MCP server 代码
2. **disk_mode 回退未完全实现** — runner.py 中 `disk_mode: false` 时应回退到原有静态/动态注入，此分支尚未编写
3. **tool_after_callback 粒度** — handler.py 中 disk EXECUTE 的 after_callback 使用真实工具路径，但 before_callback 仍用 "disk"
4. **lightrag-server 未配置** — config/disk/ 中无 lightrag-server.yaml，待 LightRAG 替换 vector-store + kg-server 后添加

### 后续工作

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P1 | disk_mode 回退逻辑 | runner.py 中根据配置选择 disk 或原有注入 |
| P2 | YAML 自动生成脚本的启动校验 | 启动时自动从 ToolRegistry 提取 schema 骨架 |
| P3 | 多模型行为验证 | 在 Claude / GPT-4 上分别测试探索行为 |
| P4 | Token 消耗对比测试 | 量化对比 disk 模式 vs 原有模式的每轮 token 消耗 |
| P5 | lightrag-server.yaml | LightRAG 服务上线后编写配置 |
| P6 | `cat --brief` 模式 | 参数多的工具提供精简版帮助 |
