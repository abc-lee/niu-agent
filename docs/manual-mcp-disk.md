# MCP 服务器与虚拟磁盘配置手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，说明 MCP 服务器的同进程架构、新增服务器步骤、虚拟磁盘 YAML 配置格式，以及常见配置错误的排查方法。

## 一、概述

MCP 服务器是 Agent 调用外部能力的核心通道。虚拟磁盘（Virtual Disk）将所有 MCP 工具收归为一个 `disk()` 工具，LLM 用 Unix 命令直觉自主探索和调用。

两者的关系：

| 层级 | 职责 | 配置文件 |
|------|------|----------|
| MCP 服务器 | 实现工具函数，注册到 ToolRegistry | `config/mcp-servers.yaml`、`agent/mcp_loader.py` |
| 虚拟磁盘 | 定义工具的目录、参数格式、分类，供 LLM 发现和调用 | `config/disk/*.yaml` |

**同进程架构**：MCP 服务器不再通过 stdio 进程通信，而是直接在主进程内通过 ToolRegistry 调用。工具函数是普通同步 Python 函数，性能相比旧 stdio 模式提升约 40000x。

**工具展示统一由虚拟磁盘管理**：所有 MCP 工具在 `mcp-servers.yaml` 中设 `visibility: hidden`，不再通过 static/dynamic 方式注入 Agent 提示词。LLM 通过 `disk()` 工具的 ls/cat/路径调用发现和使用工具。

## 二、新增 MCP 服务器

从零开始添加一个新 MCP 服务器，需完成以下 5 步。

### 2.1 创建服务器代码

在 `mcp-servers/` 下创建目录：

```
mcp-servers/<name>/
├── src/
│   └── niu_<name>/
│       ├── __init__.py      # MCP 工具定义
│       └── __main__.py      # stdio 备用入口
└── pyproject.toml
```

**`__init__.py` 必须定义**：

1. `TOOL_SCHEMAS` 字典 — 每个工具的 name/description/input_schema
2. 工具函数 — 普通同步函数，参数与 TOOL_SCHEMAS 的 input_schema 一致
3. `get_tool_schemas()` — 返回 `list(TOOL_SCHEMAS.values())`
4. `run_server()` — MCP stdio 备用入口（import mcp 包，注册 list_tools/call_tool handler）

示例：

```python
# mcp-servers/my-server/src/niu_my_server/__init__.py

TOOL_SCHEMAS = {
    "my_tool": {
        "name": "my_tool",
        "description": "做某事",
        "input_schema": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "参数1"},
            },
            "required": ["param1"],
        },
    },
}

def my_tool(param1: str) -> str:
    """工具实现"""
    return f"result: {param1}"

def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())

def run_server():
    from mcp import Server
    import asyncio
    server = Server("my-server")
    # ... 注册 list_tools / call_tool handler
    asyncio.run(server.run())
```

**注意**：不需要 `pip install`。`workdir` 指向 `src/` 目录，启动时自动加入 `sys.path`，Python 直接找到 `niu_my_server` 模块。

### 2.2 注册到 mcp-servers.yaml

在 `config/mcp-servers.yaml` 中添加服务器配置：

```yaml
my-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_my_server"
  workdir: mcp-servers/my-server/src
  preload: true        # 仅作文档标记，加载器不读取（所有 REQUIRED_SERVERS 启动时加载）
  optional: true       # 仅作文档标记，加载器不读取（可选性由 mcp_loader.py 的 OPTIONAL_SERVERS 列表控制）
  tools:
    my_tool:
      visibility: hidden   # 所有工具必须 hidden，由虚拟磁盘管理展示
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `command` | 固定 `${PYTHON_PATH}`，由启动器自动替换 |
| `args` | 固定 `["-m", "niu_<name>"]`，对应 Python 模块名 |
| `workdir` | 指向 `src/` 目录，相对于项目根目录 |
| `preload` | 当前仅作文档标记，加载器不区分 preload true/false，所有 REQUIRED_SERVERS 启动时加载 |
| `optional` | 当前由 `mcp_loader.py` 的 `OPTIONAL_SERVERS` 列表控制，yaml 的 `optional` 字段仅作文档标记，加载器不读取 |
| `tools.*.visibility` | **必须设 `hidden`**，由虚拟磁盘统一管理 |

**何时将服务器设为可选**：服务器依赖外部服务（如 ha-server 依赖 Home Assistant、feishu-server 依赖飞书 API），用户环境可能没有这些服务。核心服务器（memory-server、config-manager 等）必须放在 REQUIRED_SERVERS 中。可选性通过在 `mcp_loader.py` 中将元组从 `REQUIRED_SERVERS` 移到 `OPTIONAL_SERVERS` 列表实现（详见 2.3 节）。

### 2.3 注册到 mcp_loader.py

在 `agent/mcp_loader.py` 中添加服务器到对应列表：

**必需服务器**（加载失败终止启动）：

```python
REQUIRED_SERVERS: List[Tuple[str, str]] = [
    # ... 已有服务器
    ("my-server", "niu_my_server"),
]
```

**可选服务器**（加载失败跳过）：

```python
OPTIONAL_SERVERS: List[Tuple[str, str]] = [
    # ... 已有服务器
    ("my-server", "niu_my_server"),
]
```

元组格式：`(服务器名, Python模块名)`。服务器名与 `mcp-servers.yaml` 的 key 一致，模块名与 `workdir/src/` 下的包名一致。

### 2.4 创建虚拟磁盘配置

在 `config/disk/` 下创建 `<server-name>.yaml`：

```yaml
server: my-server
directory: mydir          # 目录名，LLM 通过 /mydir/ 访问
description: "简短描述 — 服务器提供的功能"

tools:
  - name: my_tool
    category: write       # read / write / admin / query / explore
    hidden: false         # 隐藏工具：ls 不显示，ls --all 显示，直接路径可调用
    short: "简短描述"      # 等同 summary
    long: "完整描述"       # 同 description
    parameters:
      - name: param1
        position: 1       # 位置参数序号（从1开始）
        type: string
        required: true
        description: "参数说明"
```

**目录名规则**：
- 不能与已有目录重复
- 不能与内建命令冲突（ls/cat/help/cd/pwd/disk）
- 建议 2-8 字符，简短直观

### 2.5 无需修改主 Agent 配置

主 Agent 通过虚拟磁盘发现和调用所有 MCP 工具，不需要在 `config/agents/niu.md` 的 `mcpServers` 字段中添加服务器名。子 Agent 如需直接调用工具（不经 disk），在对应的 Agent 配置文件的 `mcpServers` 列表中添加服务器名即可。

### 2.6 验证

启动程序后检查日志：

```bash
# 检查服务器是否加载成功
grep "Optional server loaded: my-server" logs/api_stderr.log
grep "All .* servers loaded" logs/api_stderr.log

# 检查虚拟磁盘配置是否通过校验
grep "DiskConfig" logs/api_stderr.log
```

通过 LLM 交互验证：

```
disk("ls /")              → 应出现 mydir 目录
disk("ls /mydir")         → 应出现 my_tool 工具
disk("cat /mydir/my_tool") → 应显示参数说明
disk("/mydir/my_tool value1") → 应执行工具并返回结果
```

## 三、虚拟磁盘配置详解

### 3.1 服务器配置格式

文件：`config/disk/<server-name>.yaml`

支持两种格式，解析器自动识别：

**格式一 — dict（推荐）**：

```yaml
server: my-server
directory: mydir
description: "目录描述"

tools:
  my_tool:                  # 工具名作为 key
    summary: "简短描述"
    description: "完整描述"
    category: write
    hidden: false
    args:
      - name: param1
        position: 1
        type: string
        required: true
        description: "参数说明"
    mutually_exclusive:
      - [param_a, param_b]
    examples:
      - "/mydir/my_tool value1"
```

**格式二 — list（当前项目使用的格式）**：

```yaml
server: my-server
directory: mydir
description: "目录描述"

tools:
  - name: my_tool           # 工具名作为字段
    category: write
    hidden: false
    short: "简短描述"        # 等同格式一的 summary
    long: "完整描述"         # 同格式一的 description
    parameters:              # 同格式一的 args
      - name: param1
        position: 1
        type: string
        required: true
```

两种格式等效。当前项目中 `ha-server.yaml`、`memory-server.yaml` 等使用 list 格式；设计文档推荐 dict 格式。新增配置建议使用 dict 格式，但两种均可正常工作。

### 3.2 参数字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 参数名 |
| `type` | string | 是 | string / integer / number / boolean / object / array |
| `description` | string | 否 | 参数说明，LLM 调用时参考 |
| `position` | integer | 否 | 位置参数序号（从 1 开始，必须连续，不能有间隔） |
| `flag` | string | 否 | flag 名称（kebab-case），默认等于 name |
| `required` | boolean | 否 | 是否必填，默认 false |
| `default` | any | 否 | 默认值（类型必须与 type 匹配） |
| `enum` | list | 否 | 枚举值，仅限 string 和 integer 类型 |
| `cli_format` | string | 条件 | object/array 类型必填，通常为 `"json"` 或 `"repeatable"` |
| `repeatable` | boolean | 否 | 是否可重复传入，默认 false |
| `sensitive` | boolean | 否 | 是否敏感（如 token、密码），默认 false |
| `constraints` | dict | 否 | 约束条件：minimum / maximum / pattern / max_length |
| `requires` | string | 否 | 该参数依赖的另一参数名（同时传入才有意义） |

**位置参数与 flag 参数的区别**：

- **位置参数**（有 position）：LLM 直接按位置传入，不需要 flag 前缀
  - 例：`/ha/ha_control light.xxx turn_on` — entity_id 是 position 1，action 是 position 2
  - 必须从 1 开始连续编号（1, 2, 3...），不能跳号
  - required 的位置参数不能出现在 optional 的位置参数之后

- **flag 参数**（无 position）：LLM 用 `--flag-name value` 传入
  - 例：`/ha/ha_subscribe sensor.xxx_temp above --value 30` — entity_id 和 condition 是位置参数，value 是 flag 参数
  - flag 默认等于 name，可用 `flag` 字段指定 kebab-case 名称（如 `from_state` → `from-state`）

### 3.3 category 分类

| category | 含义 | ls 时的显示分组 |
|----------|------|----------------|
| read | 只读查询 | [query] |
| query | 语义查询 | [query] |
| write | 写入操作 | [write] |
| explore | 掯索浏览 | [explore] |
| admin | 管理操作 | [admin] |

分类影响 LLM 对工具的理解：
- read/query 类工具：LLM 优先用于获取信息
- write 类工具：LLM 知道会改变状态，谨慎调用
- admin 类工具：LLM 知道是管理操作，需用户确认

### 3.4 hidden 工具

设 `hidden: true` 的工具：
- `ls /dir` 不显示
- `ls /dir --all` 显示
- `cat /dir/tool_name` 正常显示参数说明
- `/dir/tool_name args` 正常调用

用途：低频工具（如 `list_supported_formats`、`lightrag_document_status`）设 hidden，避免 ls 列表过长干扰 LLM，但仍可按需调用。

### 3.5 mutually_exclusive 互斥参数组

同一组内的参数不能同时传入：

```yaml
mutually_exclusive:
  - [keyword, index]       # 按关键词删除和按序号删除互斥
```

互斥参数组中引用的参数名必须在 args/parameters 中存在。

### 3.6 sensitive 参数

设 `sensitive: true` 的参数（如 ha_token、密码），LLM 在日志和响应中不会完整显示参数值。

### 3.7 约束条件（constraints）

```yaml
constraints:
  minimum: 0            # 数值最小值
  maximum: 100          # 数值最大值
  pattern: "^\\d+$"     # 正则匹配（string 类型）
  max_length: 200       # 字符串最大长度
```

### 3.8 启动校验规则

`disk_config.py` 在启动时自动校验所有 YAML 配置。以下校验失败会阻止启动（抛出 ValidationError）：

| 编号 | 校验规则 | 错误信息示例 |
|------|----------|-------------|
| 1 | 目录名不能重复 | `Duplicate directory name: 'ha'` |
| 2 | 工具名/目录名不能与内建命令冲突 | `Directory 'ls' conflicts with reserved command` |
| 3 | position 必须连续（从1开始无间隔） | `ha/ha_control: position gap at 3, expected 2` |
| 4 | 同一工具内 flag 名不能重复 | `ha/ha_control: duplicate flag '--value'` |
| 5 | object/array 类型必须有 cli_format | `ha/ha_integrate: arg 'data' type=object requires cli_format` |
| 6 | required 位置参数不能出现在 optional 之后 | `ha/ha_control: required positional 'action' after optional positional` |
| 9 | mutually_exclusive 引用的参数必须存在 | `ha/ha_subscribe: mutually_exclusive references nonexistent arg 'foo'` |

以下校验失败仅输出 warning（不阻止运行）：

| 编号 | 校验规则 | 说明 |
|------|----------|------|
| 7 | enum 仅限 string/integer 类型 | 其他类型用 enum 可能无意义 |
| 8 | default 值类型必须与 type 匹配 | 如 string 参数配 integer 默认值 |

### 3.9 交叉验证

启动时 `disk_config.py` 自动对比 YAML 配置与 ToolRegistry 中的 `input_schema`：

- YAML 中定义了 ToolRegistry 里没有的参数 → warning: `YAML has extra args`
- ToolRegistry 中有但 YAML 没有的参数 → warning: `YAML missing args`
- ToolRegistry 中有但 YAML 里没有的工具 → warning: `not found in ToolRegistry`

交叉验证仅输出 warning，不阻止运行。但这意味着参数漂移，应尽快修正。

### 3.10 用户自建 MCP server 配置（`~/.niu/disk/`）

主 Agent 可以在用户家目录 `~/.niu/disk/` 下自建 yaml 文件，覆盖 bundle 内置的 server 配置或添加全新的 server。这是用户在不修改 bundle 的情况下扩展虚拟磁盘的主要途径。

**加载顺序与覆盖规则**：

- `~/.niu/disk/` 目录由主 Agent 自行创建（launcher 不主动创建，目录不存在时 DiskConfig 跳过该目录并 warning，不影响启动）
- `DiskConfig` 按顺序扫描 `[bundle/config/disk/, ~/.niu/disk/]` 两个目录
- 用户目录中的 yaml 与 bundle 中**同 `server_name`** 的 yaml → **整个 server 被用户版本替换**（不合并 tools）
- 用户目录中的 yaml 定义新 `server_name` → 该 server 被加入虚拟磁盘
- `directory` 字段在跨目录间必须唯一（两个目录中不能出现同名 directory，否则启动报错）

**yaml 格式示例**（最简结构，参考 `memory-server.yaml`）：

```yaml
# ~/.niu/disk/my-custom-server.yaml
server: my-custom-server
directory: custom
description: "我的自建 server — 用于 X 场景"

tools:
  - name: do_something
    category: write
    short: "做某事"
    long: "完整描述：用于 X 场景的某个操作"
    parameters:
      - name: input
        position: 1
        type: string
        required: true
```

**白名单设计**：用户必须在 `~/.niu/disk/` 中显式配置 yaml 才能让 MCP 工具出现在虚拟磁盘。仅注册到 `config/mcp-servers.yaml` 但没有 disk yaml 的 server，其工具不会出现在 `ls /` 中。

**容错规则**：

| 场景 | 行为 |
|------|------|
| 用户目录不存在 | 跳过（warning 日志），不阻塞启动 |
| 用户目录存在但为空 | 正常加载，仅使用 bundle 配置 |
| 用户目录中的单个 yaml 语法错误 | warning + skip 该文件，其他 yaml 正常加载 |
| 用户目录中的 yaml 缺少 `server` key | warning + skip 该文件 |
| 跨文件冲突（directory 重名、与内建命令冲突等） | 启动报错阻塞（设计意图，需用户修复） |

**典型用途**：

1. **覆盖内置 server**：用户想改 `memory-server` 的工具描述、隐藏某些工具 → 在 `~/.niu/disk/memory-server.yaml` 写完整的新配置，整个替换 bundle 版本
2. **添加自建 server**：用户写了自己的 MCP server 模块（注册到 ToolRegistry），在 `~/.niu/disk/my-server.yaml` 添加虚拟磁盘配置，让 Agent 能 `ls /my-server` 发现
3. **本地实验**：临时调试新工具的 yaml 配置，不想污染 bundle 仓库

## 四、LLM 交互方式

LLM 通过 `disk()` 工具与虚拟磁盘交互，使用 Unix 命令风格：

| 命令 | 说明 |
|------|------|
| `disk("ls /")` | 列出所有目录 |
| `disk("ls /ha")` | 列出 ha 目录下的工具（不含 hidden） |
| `disk("ls /ha --all")` | 列出 ha 目录下所有工具（含 hidden） |
| `disk("cat /ha/ha_control")` | 查看 ha_control 的参数说明 |
| `disk("/ha/ha_control light.xxx turn_on")` | 执行工具（位置参数） |
| `disk("/ha/ha_subscribe sensor.xxx_temp above --value 30")` | 执行工具（位置 + flag 参数） |

**参数传递规则**：
- 位置参数：按序直接传入，无需 flag
- flag 参数：用 `--flag-name value` 传入
- object/array 参数：用 JSON 字符串传入（`--data '{"key":"value"}'`）
- sensitive 参数：LLM 正常传入，系统在日志中脱敏显示

## 五、常见问题

### 5.1 工具不显示在 ls 中

**原因排查顺序**：

1. 检查 YAML 中 `hidden: true` — `ls` 不显示 hidden 工具，需用 `ls --all`
2. 检查服务器是否加载成功 — 日志中搜索 `Optional server loaded` 或 `All .* servers loaded`
3. 检查 `mcp_loader.py` 中是否注册 — REQUIRED_SERVERS 或 OPTIONAL_SERVERS 是否包含该服务器

### 5.2 启动报 ValidationError

查看错误信息中的具体规则编号（对应 3.9 节校验表），常见原因：

- **position gap**：位置参数编号不连续（如只有 1 和 3，缺少 2）
- **duplicate directory**：两个服务器用了相同目录名
- **conflicts with reserved command**：目录名或工具名等于 ls/cat/help/cd/pwd/disk
- **requires cli_format**：object 或 array 类型参数缺少 cli_format 字段

修正 YAML 后重启即可。

### 5.3 YAML 与代码参数漂移

启动日志中出现 warning：

```
DiskConfig: ha-server/ha_control YAML missing args: {new_param}
DiskConfig: ha-server/ha_control YAML has extra args: {removed_param}
```

**原因**：MCP 服务器代码中 `TOOL_SCHEMAS` 的 `input_schema` 增删了参数，但磁盘 YAML 未同步更新。

**处理**：更新 `config/disk/<server-name>.yaml` 中对应工具的参数定义，使其与 `TOOL_SCHEMAS` 保持一致。

### 5.4 服务器加载失败导致启动终止

日志中出现：

```
Critical MCP servers failed to load:
  - my-server (import failed: No module named niu_my_server)
```

**排查**：
1. 检查 `workdir` 路径是否正确 — 应指向 `src/` 目录（相对于项目根目录）
2. 检查模块目录名是否与元组中的模块名一致 — `niu_<name>` 包名与目录名匹配
3. 检查 `__init__.py` 是否存在且有 `get_tool_schemas` 函数

**如果服务器是可选的**，将元组从 REQUIRED_SERVERS 移到 OPTIONAL_SERVERS（可选性仅由 `mcp_loader.py` 的 `OPTIONAL_SERVERS` 列表控制，在 `mcp-servers.yaml` 中加 `optional: true` 无效）。

### 5.5 工具调用返回空结果或报错

**排查**：
1. 检查参数是否完整传入 — 位置参数必须按序，flag 参数必须用 `--flag`
2. 检查参数类型是否匹配 — integer 参数传字符串会报错
3. 检查工具函数实现 — 用 ToolRegistry 直接调用测试：

```python
from agent.tool_registry import get_registry
registry = get_registry()
tool_fn = registry.get("server-name/tool-name")
result = tool_fn(param1="value1")
```

### 5.6 新增服务器后工具数量变化不影响其他配置

新增 MCP 服务器只需修改以下文件，不影响已有服务器：

| 文件 | 修改内容 |
|------|----------|
| `mcp-servers/<name>/` | 新建服务器代码目录 |
| `config/mcp-servers.yaml` | 添加服务器配置段 |
| `agent/mcp_loader.py` | 添加到 REQUIRED_SERVERS 或 OPTIONAL_SERVERS |
| `config/disk/<name>.yaml` | 新建虚拟磁盘配置（主 Agent 通过此文件发现工具） |
| `config/agents/<sub-agent>.md` | 添加到 mcpServers 列表（仅子 Agent 需要直接调用时） |

### 5.7 brain-region-server 的 visibility 例外

当前 `brain-region-server` 的工具使用 `visibility: static`（而非 `hidden`），这是因为脑区工具仍通过 static 注入方式提供给 Agent，暂未迁移到虚拟磁盘。其他所有服务器必须使用 `visibility: hidden`。
