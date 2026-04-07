# 简化重构方案

**目标**：傻瓜式启动，Agent 主导一切，工具最小化
**状态**：待用户确认后实施
**日期**：2026-04-07

---

## 一、整体流程关联图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        启动流程                                      │
│                                                                     │
│  1. Go launcher 启动 Python API                                     │
│  2. Python API preload（embedding + MCP tools）                      │
│  3. Go 检测 /api/preload-status = ready                             │
│  4. Go 检测 /api/llm-status                                         │
│       ├─ LLM 可用 → 打开 assistant 窗口                             │
│       └─ LLM 不可用 → 打开 settings 窗口（用户输入 API Key）         │
│                              ↓                                       │
│                     用户保存配置                                     │
│                              ↓                                       │
│               用户关闭 settings → Go 重新检测 → 打开 assistant        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        AI 对话流程                                   │
│                                                                     │
│  每次对话：                                                          │
│  1. 加载 memory.json → _load_memory_for_prompt()                    │
│       ├─ identity → 身份设定                                        │
│       ├─ workspace.path → 工作目录                                 │
│       ├─ user.name / preferences → 用户信息                         │
│       └─ firstRun → ??? 提示词注入（本次新增）                       │
│                              ↓                                       │
│  2. 动态注入相关资源（Skills/MCP工具/知识）                         │
│                              ↓                                       │
│  3. 组装完整 system_prompt → LLM                                     │
│                              ↓                                       │
│  4. LLM 响应，AI 发现 firstRun → 引导用户完成初始设置                │
│       ├─ 用户告诉 AI 工作目录                                       │
│       ├─ AI 直接写 memory.json（bash工具）                          │
│       └─ AI 删除 firstRun 字段                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、核心逻辑说明：firstRun 完整生命周期

### 2.1 firstRun 在哪里？

**memory.json** 中已有：
```json
{
  "firstRun": false,
  ...
}
```

### 2.2 现状问题

`_load_memory_for_prompt()` 只加载了 identity/workspace/user，**没有加载 firstRun 到提示词**。

所以 AI 不知道这是首次运行。

### 2.3 完整生命周期（本次要实现的）

```
[新安装] memory.json 不存在
    ↓
系统自动创建 memory.json，firstRun = true
    ↓
_load_memory_for_prompt() 检测到 firstRun = true
    ↓
注入提示词：「## 首次使用\n\n你尚未完成初始设置，请询问用户...」
    ↓
AI 看到提示词，主动问用户：
  "嗨！我是妞妞。我需要知道你的工作目录在哪里，方便我帮你管理知识。"
    ↓
用户回答：「E:/我的工作目录」
    ↓
AI 执行 bash 命令：
  - 写入 memory.json 的 workspace.path
  - 删除 memory.json 的 firstRun 字段
    ↓
下次对话：firstRun 已删除，不再注入首次使用提示
```

### 2.4 AI 如何知道要删除 firstRun？

**通过 SYSTEM_MANUAL.md 向量库 L1 文档**。文档写清楚：
```
## 首次使用（firstRun）处理流程

触发条件：memory.json 中存在 firstRun 字段

处理步骤：
1. 主动询问用户：工作目录路径
2. 用户回答后，AI 直接写入 ~/.niu/memory.json：
   - 修改 workspace.path
   - 删除 firstRun 字段
3. 使用 bash 工具操作，不要使用 MCP 工具
```

向量库检索到这段内容后，AI 会按照执行。

---

## 三、配置文件清理

### 3.1 config/user-config.json

**清理前**：
```json
{
  "llm": { "apiKey": "...", "apiBase": "...", "model": "...", "type": "openai", "presetId": "..." },
  "storage": { "documentRoot": "...", "databasePath": "..." },  // ❌ 冗余，workspace 在 memory.json
  "firstRun": false                                          // ❌ 冗余，firstRun 在 memory.json
}
```

**清理后**：
```json
{
  "llm": { "apiKey": "", "apiBase": "", "model": "", "type": "openai", "presetId": "" }
}
```

### 3.2 memory.json（不变，只说明）

```json
{
  "version": 1,
  "identity": { "name": "妞妞", "gender": "female", "personality": [...], "greetingStyle": "..." },
  "workspace": { "path": "", "createdAt": "" },           // 首次设置后填充
  "user": { "name": "", "preferences": [] },
  "firstRun": true,                                        // 首次使用后删除
  "createdAt": "",
  "lastActiveAt": ""
}
```

---

## 四、修改清单

### 4.1 `_load_memory_for_prompt()` — 新增 firstRun 注入

**文件**：`agent/runner.py`

**改动**：在函数末尾新增 firstRun 检测：

```python
def _load_memory_for_prompt() -> str:
    """从 memory.json 加载身份设定和用户偏好，格式化为提示词"""
    ...
    # 现有逻辑：identity / workspace / user

    # 新增：firstRun 检测
    first_run = memory.get("firstRun")
    if first_run:
        parts.append(
            "## 首次使用\n\n"
            "你尚未完成初始设置。用户还没有告诉你工作目录在哪里。"
            "请主动询问用户：\"你的工作目录想放在哪里？我需要知道这个路径来帮你管理知识。\"\n"
            "用户回答路径后，直接用 bash 写入 ~/.niu/memory.json，完成后删除 firstRun 字段。"
        )

    return "\n\n".join(parts)
```

### 4.2 `SYSTEM_MANUAL.md` — 丰富 LLM 配置修改说明

**文件**：`docs/SYSTEM_MANUAL.md`

**改动**：在"七、用户指南 > 7.2 LLM 配置"中增加：

```markdown
### 7.2 LLM 配置

**配置文件位置**：`config/user-config.json`

```json
{
  "llm": {
    "apiKey": "你的API Key",
    "apiBase": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "type": "openai",
    "presetId": ""
  }
}
```

**修改方式**：
- **方式一（推荐）**：告诉 AI "我的 API Key 是 xxx"，AI 用 bash 工具直接写入
- **方式二**：手动编辑 `config/user-config.json`

**AI 修改配置流程**（向量库 L1 检索）：
当用户要求修改 LLM 配置时，AI 执行：
```bash
# 读取当前配置
cat config/user-config.json

# 修改配置（使用 jq 或 python）
python -c "import json; d=json.load(open('config/user-config.json')); d['llm']['apiKey']='sk-xxx'; json.dump(d, open('config/user-config.json','w'), indent=2)"
```

### 4.3 SYSTEM_MANUAL.md — 增加 firstRun 处理说明

**文件**：`docs/SYSTEM_MANUAL.md`

**新增章节**：

```markdown
### 7.5 首次使用流程（firstRun）

**触发条件**：memory.json 中存在 `firstRun: true`

**AI 处理流程**：
1. 在 system prompt 中看到"## 首次使用"段落
2. 主动询问用户工作目录
3. 用户回答路径（如：E:/我的知识库）
4. AI 执行 bash 命令完成设置：
   ```bash
   # 写入 workspace.path
   python -c "
   import json
   from pathlib import Path
   mem = json.load(open(Path.home() / '.niu' / 'memory.json'))
   mem['workspace'] = {'path': 'E:/我的知识库', 'createdAt': '2026-04-07'}
   # 删除 firstRun
   del mem['firstRun']
   json.dump(mem, open(Path.home() / '.niu' / 'memory.json', 'w'), indent=2)
   "
   ```
5. 完成后，下次对话不再出现首次使用提示

**禁止事项**：
- 不要使用 config-manager MCP 工具（已删除）
- 不要询问用户 API Key（由 settings 窗口处理）
- 只询问工作目录
```

### 4.4 Go launcher — 增加 LLM 可用性检测

**文件**：`main.go`

**改动**：在 preload 完成后，增加 LLM 检测：

```go
// 在 preloadReady 检测之后，增加：
if !preloadReady {
    slog.Warn("Preload may not be complete, proceeding anyway")
}

// 新增：检测 LLM 可用性
type LLMStatus struct {
    Ready bool `json:"ready"`
    Error string `json:"error,omitempty"`
}

llmStatus := checkLLMStatus(port)
if !llmStatus.Ready {
    slog.Info("LLM not configured, opening settings window...")
    if _, err := launchWindow("settings"); err != nil {
        slog.Error("Failed to launch settings window", "error", err)
    }

    // 等待用户关闭 settings 窗口，然后重新检测
    // 实现方式：启动一个后台 goroutine 轮询 settings 进程是否退出
    // 退出后再次检测 LLM 状态，ready 则启动 assistant
    go waitForSettingsAndRetry(port)
    <-ctx.Done()  // 等待 shutdown
    return
}

// LLM 可用，直接启动 assistant
if _, err := launchWindow("assistant"); err != nil {
    slog.Error("Failed to launch assistant window", "error", err)
}
```

### 4.5 niu_api — 新增 `/api/llm-status` 端点

**文件**：`niu_api/compat.py`

**新增**：

```python
@router.get("/api/llm-status")
async def get_llm_status() -> dict:
    """检测 LLM 是否可用"""
    from niu_api.config import get_config
    config = get_config()

    if not config.llm or not config.llm.api_key:
        return {"ready": False, "error": "API key not configured"}

    if not config.llm.api_base or not config.llm.model:
        return {"ready": False, "error": "API base or model not configured"}

    return {"ready": True}
```

### 4.6 删除 config-manager MCP 工具

**文件**：
- `mcp-servers/config-manager/src/niu_config_manager/__init__.py`
- `mcp-servers/config-manager/`
- `config/mcp-servers.yaml` 中的 config-manager 条目
- `config/agents/niu.md` 中的 config-manager 引用

**删除理由**：Agent 有 bash 工具，可以直接读写 JSON 文件，不需要专门的 MCP 工具。

**保留的处理**：不删除整个目录（用户可能有其他用途），仅从 mcp-servers.yaml 和 niu.md 移除引用。

### 4.7 删除向量库中的 config-manager 工具注册

**文件**：`scripts/init_vector_db.py`

**改动**：删除 `register_mcp_tools()` 中 config-manager 相关工具注册（read_config、write_config 等）。

### 4.8 删除 skills 目录

**文件**：`agent/memory/skills/test-watchdog.md`

**改动**：删除该文件。Agent 有 bash 和 README，不需要 skills。

---

## 五、启动流程完整对比

### 5.1 修改前

```
Go launcher 启动
    ↓
Python API preload
    ↓
preload-ready = true
    ↓
打开 assistant 窗口 ← 不管 LLM 是否配置好
    ↓
AI 尝试对话 → 报错 API Key 为空
```

### 5.2 修改后

```
Go launcher 启动
    ↓
Python API preload
    ↓
preload-ready = true
    ↓
检查 /api/llm-status
    ├─ ready=true → 打开 assistant
    └─ ready=false → 打开 settings
                        ↓
              用户输入 API Key，保存
                        ↓
              用户关闭 settings
                        ↓
              重新检查 llm-status
                        ↓
              打开 assistant
                    ↓
AI 发现 firstRun → 引导用户设置工作目录
```

---

## 六、firstRun 提示词注入逻辑

### 6.1 注入条件

```python
# agent/runner.py - _load_memory_for_prompt() 末尾
first_run = memory.get("firstRun")
if first_run:
    parts.append("## 首次使用\n\n...")
```

### 6.2 过滤逻辑

如果 memory.json 中**没有** `firstRun` 字段，`memory.get("firstRun")` 返回 `None`，条件不成立，不注入。

所以：
- 新安装（firstRun=true）→ 注入
- 已配置（firstRun 不存在）→ 不注入

### 6.3 AI 删除 firstRun 的触发

AI 通过向量库检索到 SYSTEM_MANUAL.md 中的 L1 文档，知道要删除 firstRun。执行方式：用 bash 工具操作 memory.json，不依赖任何 MCP 工具。

---

## 七、风险和注意事项

| 风险 | 级别 | 缓解 |
|------|------|------|
| AI 删除 firstRun 后无法恢复 | 低 | 用户可通过 settings 重新触发 |
| settings 窗口保存后 API Key 写入失败 | 低 | 保留旧版 config-manager 作为 fallback |
| Go launcher 增加 LLM 检测导致启动延迟 | 低 | 检测是 HTTP 请求，<1 秒 |
| AI 误删 memory.json 其他字段 | 中 | AI 通过 SYSTEM_MANUAL.md 精确知道要改什么 |

---

## 八、实施顺序

```
Phase 1: 核心 firstRun 逻辑（必须先做）
  1. 修改 _load_memory_for_prompt() 注入 firstRun
  2. 丰富 SYSTEM_MANUAL.md（firstRun + LLM 配置）
  3. 新增 /api/llm-status 端点

Phase 2: 启动器检测
  4. Go launcher 增加 LLM 状态检测 + settings 自动弹窗

Phase 3: 清理
  5. 删除 config-manager MCP 工具引用
  6. 删除向量库 config-manager 工具注册
  7. 删除 skills 目录
  8. 清理 config/user-config.json 中的冗余字段

Phase 4: 测试
  9. 完整启动测试
  10. firstRun 对话测试
```

---

**确认后开始实施 Phase 1**
