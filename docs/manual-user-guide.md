# 用户操作手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含用户指南的详细内容。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、用户指南

### 1.1 首次启动流程

**初始化用户目录**

Rust 启动器首次运行时，会自动执行 `initNiuDir()`：
1. 创建 `~/.niu/` 目录（如果不存在）
2. 将 `memory/` 目录下的模板文件（`memory.json`、`preferences.json`）拷贝到 `~/.niu/`（仅当目标文件不存在时才拷贝，避免覆盖已有配置）

**第一步：配置 LLM**

首次启动时，如果未配置大模型，系统会自动弹出设置窗口让你输入 API Key。
设置完成后点击"测试连接并保存"，窗口关闭，进入下一步。

**第二步：设置工作目录**

大模型配置成功后，主窗口会打开。
如果是首次使用（memory.json 中 `firstRun` 为 `true`），大模型会主动询问你工作目录放在哪里。
`workspace.path` 的默认值为占位文本"请询问用户指定工作目录"，Agent 检测到此占位文本时会主动询问用户设置真实工作目录。
直接告诉大模型路径，例如："E:/我的知识库"
大模型会自动帮你完成初始化配置。

**基本操作：**

| 操作 | 方法 |
|------|------|
| **对话** | 直接输入文字 |
| **入库文档** | 拖入 PDF/Word/PPT/Excel/MD/HTML 文件 |
| **入库照片** | 拖入 JPG/PNG 照片 |
| **搜索知识** | 问："搜索关于 XXX 的知识" |
| **创建提醒** | 说："明天早上 8 点提醒我开会" |
| **查看任务** | 问："查看所有定时任务" |

### 1.2 LLM 配置

**配置文件**：`config/user-config.json`

```json
{
  "llm": {
    "presetId": "openai",
    "apiKey": "sk-xxx",
    "apiBase": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "type": "openai",
    "provider": "",
    "reasoning_effort": "",
    "litellm_kwargs": {}
  },
  "lightrag_llm": {
    "presetId": "",
    "apiKey": "",
    "apiBase": "",
    "model": "",
    "type": "openai",
    "provider": "",
    "reasoning_effort": "none",
    "litellm_kwargs": {}
  }
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `presetId` | 预设 ID，对应 llm-presets.json 中的预设 |
| `apiKey` | 你的 API Key |
| `apiBase` | API 端点基础地址（不含路径后缀，LiteLLM 会自动追加：openai 类型追加 `/chat/completions`，anthropic 类型追加 `/v1/messages`） |
| `model` | 模型名称 |
| `type` | 系统内部参数，用于区分 API 格式转换和认证方式：`openai`（OpenAI 兼容格式）或 `anthropic`（Anthropic 原生格式）。**不是** LiteLLM 的 custom_llm_provider |
| `provider` | LiteLLM 路由参数，映射为 `custom_llm_provider`。常见值：`""`（空，默认由 type 决定）、`"volcengine"`（火山引擎）。填写后模型名无需加厂商前缀 |
| `reasoning_effort` | 思考链深度：`""`（空，由模型默认决定）、`"none"`（禁用）、`"low"`、`"medium"`、`"high"`、`"xhigh"`。主 Agent 默认空，LightRAG 默认 `"none"`。**注意**：该参数的实际效果与模型基础能力强相关，不同模型的最优值差异很大，需实测确定（详见下方"reasoning_effort 配置与测试指南"） |
| `litellm_kwargs` | 厂商特有参数，JSON 对象格式，原样透传给 LiteLLM。用于传递各厂商 SDK 要求的额外参数（如火山引擎的 `thinking`、`allowed_openai_params` 等）。代码不做任何厂商判断，只负责透传 |

**预设列表**：编辑 `config/llm-presets.json` 查看支持的预设。

当前内置预设包括：

| 预设 ID | 名称 | 类型 |
|---------|------|------|
| `openai` | OpenAI GPT-4o Mini | openai |
| `openai-gpt4` | OpenAI GPT-4o | openai |
| `anthropic` | Anthropic Claude 3.5 Sonnet | anthropic |
| `anthropic-haiku` | Anthropic Claude 3.5 Haiku | anthropic |
| `deepseek` | DeepSeek Chat | openai |
| `deepseek-reasoner` | DeepSeek R1 | openai |
| `qwen` | 通义千问 | openai |
| `qianfan` | 百度千帆 | openai |
| `doubao` | 豆包 | openai |
| `moonshot` | Moonshot (Kimi) | openai |
| `glm` | 智谱 GLM-4 Flash | openai |
| `minimax` | MiniMax M2 | openai |
| `minimax-anthropic` | MiniMax M2.7 (Anthropic API) | anthropic |
| `minimax-anthropic-highspeed` | MiniMax M2.7 高速版 (Anthropic API) | anthropic |
| `ollama` | Ollama 本地 | openai |
| `custom` | 自定义 | openai |

**火山方舟(Ark)端点配置说明**

火山方舟提供两种计费端点，模型池相同，但功能权限有差异：

| 端点 | 地址 | 计费 | response_format | 适用场景 |
|------|------|------|-----------------|----------|
| 标准端点 | `https://ark.cn-beijing.volces.com/api/v3` | 按量计费 | 支持 | 主 Agent、LightRAG |
| Coding Plan | `https://ark.cn-beijing.volces.com/api/coding/v3` | 包月计费 | **不支持**（网关拦截） | 主 Agent |

> **重要**：LightRAG 的 keyword_extraction 依赖 `response_format`，必须使用标准端点。如果误用 Coding Plan 端点，系统会自动 fallback 到纯 prompt JSON 返回（功能正常但多一次无效请求）。

**火山方舟深度思考模型 + 工具调用配置**（重要）：

火山方舟的 `doubao-seed` 系列模型（包括 `ark-code-latest`）默认启用深度思考模式。当 `litellm_kwargs` 中未显式传递 `thinking` 参数时，API 没有结构化工具调用保障——模型偶发将 tool_call 放入 `reasoning_content` 的 `<seed:tool_call>` XML 格式，而非标准的 `tool_calls` 字段，导致工具调用丢失。

**主 Agent 必须配置**：
```json
"llm": {
  ...
  "litellm_kwargs": {
    "thinking": {"type": "enabled"}
  }
}
```

| `thinking` 值 | 效果 | 适用场景 |
|---------------|------|----------|
| `{"type": "enabled"}` | 显式启用深度思考，API 网关保障 tool_call 走标准通道 | **主 Agent（必须）** |
| `{"type": "disabled"}` | 禁用深度思考 | LightRAG 入库（需配合 `response_format`） |
| `{"type": "auto"}` | 模型自行决定 | 不推荐（行为不确定） |
| 不传 | 模型默认启用思考，但无 tool_call 保障 | **危险：工具调用可能丢失** |

> **注意**：`thinking` 参数通过 `litellm_kwargs` 原样透传给 LiteLLM，再由 VolcEngine 适配器转为 API 请求的 `extra_body.thinking` 字段。代码不做任何厂商判断，所有厂商特有参数都走这个透传通道。

**模型名格式差异**：

两种端点的模型池相同，但模型名格式不同：

| 端点 | 模型名格式 | 示例 |
|------|-----------|------|
| 标准端点 | 带日期后缀的全名 | `doubao-seed-2-0-pro-260215` |
| Coding Plan | 简短别名 | `doubao-seed-2.0-pro` |

> **注意**：在标准端点上使用 Coding Plan 的简短别名会返回 404；反之，在 Coding Plan 端点上两种格式都能用。

**LiteLLM 路由配置**：

使用 LiteLLM 调用火山方舟模型时，必须通过 `provider: "volcengine"` 走 VolcEngine 适配器路由。这是 LiteLLM 的标准机制：

| 配置方式 | 路由 | 结果 |
|----------|------|------|
| `provider: "volcengine"` + `model: "doubao-seed-2.0-pro"` | VolcEngine 适配器 | 正确：`thinking` 参数自动处理，厂商特有参数通过 `litellm_kwargs` 透传 |
| `model: "volcengine/doubao-seed-2.0-pro"`（无 provider） | VolcEngine 适配器 | 也可：LiteLLM 通过前缀推断路由，但不如 `provider` 显式 |
| `model: "doubao-seed-2.0-pro"`（无 provider 无前缀） | OpenAI 适配器 | **错误**：`thinking` 等火山特有参数无法传递 |

> **注意**：`type: "openai"` 是系统内部参数（区分 Anthropic/OpenAI 格式转换），不是 LiteLLM 的 `custom_llm_provider`。两者独立：`type` 控制格式，`provider` 控制路由。

**可用模型**：doubao-seed-2.0-code、doubao-seed-2.0-pro、doubao-seed-2.0-lite、minimax-m2.7、minimax-m3、glm-5.1、kimi-k2.6、deepseek-v4-pro、deepseek-v4-flash、ark-code-latest

**LightRAG 知识图谱 LLM 配置**：`lightrag_llm` 段

LightRAG 入库（实体提取、关系构建）使用与主 Agent 独立的 LLM 配置。`model` 和 `reasoning_effort` 是两个独立的配置维度，互不依赖。

**`lightrag_llm` 字段说明**：

| 字段 | 说明 | 建议值 |
|------|------|--------|
| `presetId` | 预设 ID，对应 llm-presets.json 中的预设 | `doubao`（豆包，轻量快速） |
| `apiKey` | API Key。为空时自动继承 `llm` 段的 apiKey | 空（继承主配置） |
| `apiBase` | API 端点地址。为空时自动继承 `llm` 段的 apiBase | 空（继承主配置） |
| `model` | 模型名称。为空时使用主 Agent 同一模型 | `doubao-seed-2.0-pro`（Coding Plan 别名）或 `doubao-seed-2-0-pro-260215`（标准端点全名） |
| `type` | 类型：`openai` 或 `anthropic` | `openai` |
| `provider` | LiteLLM 路由参数，同 `llm` 段说明。火山引擎填 `"volcengine"` | `""` 或 `"volcengine"` |
| `reasoning_effort` | **思考链深度（核心配置）** | `"none"`（禁用，见下表） |
| `temperature` | LLM 采样温度。0.0 完全确定性，1.0 高随机度。为空时由系统兜底 0.2 | `0.2`（实体/关系抽取建议低温度避免 JSON 漂移） |
| `litellm_kwargs` | 厂商特有参数，同 `llm` 段说明。火山引擎知识图谱需传 `thinking` 和 `allowed_openai_params` | `{}` 或见配置示例 |

> **重要**：LightRAG 官方建议入库时不要使用带思考链的模型。思考链会导致实体提取超时（单次调用可达 198 秒）。`reasoning_effort` 默认 `"none"` 确保即使主 Agent 使用思考链模型，入库也不受影响。

> **温度值说明**：LightRAG 调 LLM 时如果不配 `temperature` 字段，系统兜底默认 `0.2`。这是为实体抽取、关系抽取等结构化任务优化的低温度值，避免高随机度导致 JSON 格式漂移、实体名不一致。如果需要更稳定的输出可设 `0.0`，需要更多创造性可设 `0.5~0.7`，但不建议超过 `1.0`。主 Agent / 子 Agent 的温度值在提示词文档里独立配置（主 Agent 0.6、子 Agent 多数 0.2），与此处的 `lightrag_llm.temperature` 完全独立，互不影响。

**reasoning_effort 参数说明**：

| 值 | 效果 | 建议场景 |
|----|------|----------|
| `none` | 完全禁用思考链 | 能力强的模型（自带足够推理能力） |
| `low` | 浅层推理 | **多数模型入库时的最优值**（实测推荐） |
| `medium` | 中等推理 | 非入库的图谱查询任务；部分模型入库也可用 |
| `high` | 深度推理 | 不建议用于入库（推理预算耗尽，输出质量反而下降） |
| `xhigh` | 最深推理 | 不建议用于入库 |

**reasoning_effort 配置与测试指南**：

`reasoning_effort` 的最优值与模型基础能力强相关，不存在通用最优值。实测数据（ark-code-latest 模型入库 SYSTEM_MANUAL.md）：

| 配置 | 实体类型准确率 | 空响应率 | 补充提取有效性 | 综合评分 |
|------|--------------|---------|--------------|---------|
| `none` | 67% | 60% | 无 | 5.5/10 |
| `low` | 95% | 40% | 有效 | **7.5/10** |
| `medium` | 14% | 40% | 部分 | 6.0/10 |
| `high` | 分裂 | 40% | 无 | 4.0/10 |

关键发现：
1. **推理越深不一定越好**：推理token被模型内部消耗，不转化为输出质量。过度推理反而导致实体类型误分类（如子Agent被归为organization而非technology）
2. **补充提取受影响**：高推理级别下模型过度自信，判断"无需补充"，导致遗漏无法弥补
3. **不同模型差异很大**：能力强的模型（如 Claude、GPT-4o）在 `none` 或 `low` 即可高质量提取；能力有限的模型可能需要 `medium`，但也可能适得其反

**换模型后必须实测**：更改模型或 reasoning_effort 后，用同一文档入库并检查日志验证效果。

**入库质量检查方法**：
1. 入库后检查日志目录 `logs/raw_http/YYYYMMDD/`，读取 `*_response.json` 文件
2. 检查空响应：响应体仅含 `<|COMPLETE|>` 或 content 为空，说明该轮提取失败
3. 检查实体类型：子Agent应为 technology 而非 person/organization；MCP服务器应为 technology 而非 organization
4. 检查违规节点：不应出现与 niu 根节点重名的实体
5. 检查补充提取：`*_response.json` 中补充提取应有实质内容（非仅 `<|COMPLETE|>`）
6. 如发现问题，调整 reasoning_effort 重新入库测试

**配置示例**：

主 Agent 用火山方舟 Coding Plan（完整配置）：
```json
"llm": {
  "presetId": "ark-code-latest",
  "apiKey": "你的API Key",
  "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
  "model": "ark-code-latest",
  "type": "openai",
  "provider": "volcengine",
  "reasoning_effort": "",
  "litellm_kwargs": {
    "thinking": {"type": "enabled"}
  }
}
```

场景一：主 Agent 用思考链模型，LightRAG 用轻量模型（推荐）：
```json
"lightrag_llm": {
  "presetId": "doubao",
  "apiKey": "",
  "apiBase": "https://ark.cn-beijing.volces.com/api/v3",
  "model": "doubao-seed-2-0-pro-260215",
  "type": "openai",
  "provider": "volcengine",
  "reasoning_effort": "low",
  "temperature": 0.2,
  "litellm_kwargs": {
    "thinking": {"type": "disabled"},
    "allowed_openai_params": ["response_format"]
  }
}
```

> **注意**：LightRAG 必须使用标准端点（apiBase 含 /api/v3），不能用 Coding Plan 端点（/api/coding/v3），因为 Coding Plan 网关拦截 response_format。model 必须用标准端点的全名格式（带日期后缀）。
> **LiteLLM 路由**：`provider: "volcengine"` 让 LiteLLM 走 VolcEngine 适配器。`litellm_kwargs` 中的 `thinking: {"type": "disabled"}` 关闭思考链（火山要求使用 response_format 时必须关闭），`allowed_openai_params: ["response_format"]` 让 VolcEngine 适配器透传 response_format 参数。

场景二：主 Agent 和 LightRAG 用同一模型，独立控制思考深度（零配置即生效）：
```json
"lightrag_llm": {
  "presetId": "",
  "apiKey": "",
  "apiBase": "",
  "model": "",
  "type": "openai",
  "provider": "",
  "reasoning_effort": "low",
  "temperature": 0.2,
  "litellm_kwargs": {}
}
```

场景三：能力强的模型，禁用思考链即可（如 Claude、GPT-4o）：
```json
"lightrag_llm": {
  "presetId": "",
  "apiKey": "",
  "apiBase": "",
  "model": "",
  "type": "openai",
  "provider": "",
  "reasoning_effort": "none",
  "temperature": 0.2,
  "litellm_kwargs": {}
}
```

**通过 MCP 工具动态修改**（无需重启）：
- 读取配置：调用 `get_lightrag_llm_config` 工具
- 修改配置：调用 `set_lightrag_llm_config` 工具，参数与字段名对应
- 清除模型（回退到主模型）：调用 `set_lightrag_llm_config(model="")`

**修改配置方式**：
- **方式一（推荐）**：通过设置窗口修改（首次启动自动弹出）
- **方式二**：关闭程序后，手动编辑 `config/user-config.json`
- **方式三**：通过 MCP 工具 `set_lightrag_llm_config` 动态修改（无需重启）

### 1.3 上下文配置

**配置文件**：`config/user-config.json` 中的 `context` 段

```json
{
  "context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "targetThreshold": 0.5,
    "sleepTriggerMinutes": 5
  }
}
```

**字段说明**：

| 字段 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `contextWindowSize` | 模型上下文窗口大小（tokens） | 200000 | 32000 ~ 2000000 |
| `warningThreshold` | 溢出警告阈值，上下文使用率超过此值触发压缩 | 0.8 | 0.0 ~ 1.0 |
| `targetThreshold` | 强制压缩目标，压缩后上下文使用率降至此值 | 0.5 | 0.0 ~ 1.0 |
| `sleepTriggerMinutes` | 空闲多久后触发睡眠整理（分钟） | 5 | > 0 |

**常见模型的 contextWindowSize**：

| 模型 | 上下文窗口 | 配置值 |
|------|-----------|--------|
| GPT-4o-mini | 128K | 128000 |
| GPT-4o | 128K | 128000 |
| Claude 3.5 Sonnet | 200K | 200000 |
| DeepSeek V3 (deepseek-chat) | 64K | 64000 |
| DeepSeek R1 (deepseek-reasoner) | 128K | 128000 |
| Qwen2.5-Turbo | 1M | 1000000 |
| 本地 Ollama 模型 | 取决于模型 | 按实际配置 |

> **注意**：`contextWindowSize` 与模型强相关，切换模型后需同步更新此值。设置窗口主界面可直接配置此值，高级选项中可配置溢出阈值等参数。

**修改配置方式**：
- **方式一（推荐）**：通过设置窗口修改（首次启动自动弹出，`contextWindowSize` 在主界面，阈值参数点"高级选项"展开）
- **方式二**：关闭程序后，手动编辑 `config/user-config.json`

### 1.4 知识图谱

知识图谱基于 LightRAG 引擎，支持文档入库后的自动实体提取和关系构建。

**查询方式**：
- 直接向大模型提问（如："XXX 和 YYY 有什么关系？"）
- 通过知识图谱可视化界面浏览实体关系（`/api/kg/snapshot`、`/api/kg/explore`）

**存储位置**：`~/.niu/lightrag_storage/`（LightRAG 固定存储路径，不随 workspace.path 变化）

**架构说明**：知识图谱和向量检索已统一由 `lightrag-server` 提供（23 个工具），取代了旧版独立的 `vector-store` 和 `kg-server`。旧的 `kg-server`（KuzuDB）和 `vector-store` 已禁用（`preload: false`）。

**注意**：文档入库时，LightRAG 自动完成实体提取、关系构建和向量索引，无需手动操作。

**注意**：并非所有文档格式都支持知识图谱入库，详见下方"1.5 支持的文件格式"。

**入库参数配置**：LightRAG 入库参数（并发数、分片大小、补充提取次数等）可在 `~/.niu/preferences.json` 的 `lightrag` 配置段调整，详见 [知识检索运维手册](manual-vector-store.md) 第 8.5 节。

**入库模型与思考链配置**：LightRAG 入库使用的模型和思考链深度在 `config/user-config.json` 的 `lightrag_llm` 配置段设置，详见上方 1.2 节"LightRAG 知识图谱 LLM 配置"。默认禁用思考链（`reasoning_effort: "none"`），防止深度推理导致入库超时。

### 1.5 支持的文件格式

Niu 有两种入库能力，格式支持范围不同：

**文件存储入库**：将文件复制到知识库目录，所有文档格式均支持。

**知识图谱入库**：将文件内容写入 LightRAG 知识图谱，仅支持以下格式。

#### 文件存储支持的格式

| 格式 | 扩展名 | 知识图谱入库 |
|------|--------|-------------|
| PDF | .pdf | 支持 |
| Word | .docx | 支持 |
| Excel | .xlsx | 支持 |
| PowerPoint | .pptx | 支持 |
| 纯文本 | .txt | 支持 |
| Markdown | .md | 支持 |
| HTML | .html | 支持 |

#### 不支持知识图谱入库的格式及原因

| 扩展名 | 原因 |
|--------|------|
| .doc | 旧版二进制格式（OLE2），无法可靠提取纯文本 |
| .xls | 旧版二进制格式（BIFF），无法可靠提取纯文本 |
| .ppt | 旧版二进制格式，无法可靠提取纯文本 |
| WPS 假 .docx | WPS 创建的 .docx 文件实际是 OLE2 格式（旧版 .doc），程序会自动检测并标记为不支持知识图谱入库 |

> **建议**：如果 .doc/.xls/.ppt 文件需要入库知识图谱，请先用 Office 或 WPS 另存为 .docx/.xlsx/.pptx 格式。

#### 照片入库支持的格式

| 格式 | 扩展名 | 说明 |
|------|--------|------|
| JPEG | .jpg / .jpeg | 支持存储 + 人脸识别 |
| PNG | .png | 支持存储 + 人脸识别 |
| GIF | .gif | 支持存储 + 人脸识别 |
| BMP | .bmp | 支持存储 + 人脸识别 |
| WebP | .webp | 支持存储 + 人脸识别 |
| HEIC | .heic / .heif | 支持存储 + 人脸识别 |

> 照片格式仅支持存储和人脸识别，不支持知识图谱入库。

详细格式说明和常见问题，请参阅 [文件格式支持手册](manual-file-formats.md)。

### 1.6 记忆管理

记忆系统分为两层：

**1. 用户长期记忆（memory.json，驻留系统提示词）**
- 存储路径：`~/.niu/memory.json` 的 `permanent` 数组
- 工具：`user_memory_remember`、`user_memory_forget`、`user_memory_list`
- 容量：最多 10 条（1 条工作便签 + 9 条长期记忆），每条不超过 200 token
- 便签（type=task）：新任务自动覆盖旧便签
- 特点：每轮对话自动注入系统提示词，大模型始终可见

**用户信息配置（memory.json 的 user 字段）**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `name` | 用户真实姓名 | 李磊 |
| `nickname` | 用户称呼/昵称，主Agent用此称呼用户 | 老板 |
| `occupation` | 用户职业，影响内容提取和日志编写的专业视角 | 软件工程师 |
| `organization` | 用户工作单位，影响内容提取和日志编写的专业视角 | 某科技公司 |

- 这些信息会自动注入到主Agent和子Agent的系统提示词中
- 缺失时主Agent会主动询问用户并写入
- 修改方式：告诉主Agent"我的职业是XXX"或"我在XXX工作"，主Agent会自动更新 memory.json

**2. 语义记忆（知识图谱 LightRAG）**
- 工具：`remember`、`recall`、`update_memory`、`get_memory_stats`、`cleanup_memories`、`link_memories`
- 特点：基于语义相似度检索 + 知识图谱关联，支持大量信息
- 用途：对话摘要、技术笔记、经验知识等

**操作示例**：
- 查看记忆：问 "你记得我的什么信息？"
- 添加记忆：说 "记住我的工作单位是 XXX"
- 删除记忆：说 "忘记我的工作单位信息"

### 1.7 首次使用（firstRun）

**触发条件**：`~/.niu/memory.json` 中 `firstRun` 为 `true`

**大模型处理流程**：

1. 在 system prompt 中看到"## 首次使用"段落
2. 主动询问用户工作目录
3. 用户回答路径（如：E:/我的知识库）
4. 大模型用 bash 工具完成设置：
   - 创建目录（如果不存在）
   - 写入 `~/.niu/memory.json`：设置 `workspace.path`，将 `firstRun` 设为 `false`

> 代码中实际将 `firstRun` 设为 `false`，而非删除该字段。

5. 大模型询问用户基本信息（真实姓名、称呼、职业、工作单位），用户回答后写入 memory.json 的 user 字段

6. 完成后，下次对话不再出现首次使用提示

**禁止事项**：
- 不要询问用户 API Key（由设置窗口处理）
- 只询问工作目录

### 1.8 常见问题

**Q: 数据存储在哪里？**
```
A: 数据分布在两个位置：

~/.niu/ 目录：
- 历史对话：~/.niu/messages.db
- 用户记忆：~/.niu/memory.json
- 知识图谱：~/.niu/lightrag_storage/
- 定时任务：~/.niu/scheduled_tasks.db
- 程序配置：config/ 目录

工作区目录（由 workspace.path 决定）：
- 定时任务：{workspace}/scheduled_tasks.db（优先路径）
- 入库文档：{workspace}/documents/
```

**Q: 可以离线使用吗？**
```
A: 可以！所有功能都支持离线，除了：
- 首次启动下载模型（需要网络）
- 云端 LLM API（需要网络）

本地 Ollama + 预下载模型 = 完全离线使用
```

**Q: 如何备份数据？**
```
A: 定期复制以下目录：
- ~/.niu/        (记忆、对话记录、知识图谱、定时任务、配置)
- {workspace}/documents/   (入库文档)

workspace 路径在 ~/.niu/memory.json 的 workspace.path 字段中。
```

**Q: 支持多用户吗？**
```
A: 当前版本为单用户设计，所有数据在本地。
多用户支持计划在未来版本中实现。
```

**Q: GPU 加速有什么要求？**
```
A: NVIDIA GPU：
- 显卡：GTX 1060 或更高
- CUDA：安装 CUDA Toolkit 12.x
- 安装：pip install onnxruntime-gpu

Windows + 任意 GPU：
- 安装：pip install onnxruntime-directml
- 无需 CUDA
```

**Q: 如何卸载？**
```
A: 1. 关闭程序
   2. 删除安装目录
   3. 删除用户数据（可选）：
      - C:\Users\用户名\.niu\
```

---

## 验证记录

2026-04-30 验证并修正以下内容：

| 位置 | 原文 | 修正后 |
|------|------|--------|
| 8.2 LLM 预设表 | `minimax` 对应模型名 "MiniMax" | 修正为 "MiniMax M2"（与 llm-presets.json 中 model 字段一致） |
| 8.2 LLM 预设表 | `minimax-anthropic-highspeed` 描述 "MiniMax M2.7 高速版" | 修正为 "MiniMax M2.7 高速版 (Anthropic API)"（与实际 preset description 一致） |
| 8.2 apiBase 说明 | "含 `/chat/completions` 后缀" | 补充说明：openai 类型含 `/chat/completions`，anthropic 类型含 `/v1/messages` |
| 8.3 存储位置 | "工作目录下的 LightRAG 数据文件（由 workspace.path 决定）" | 修正为 `~/.niu/lightrag_storage/`（LightRAG 固定路径，不随 workspace 变化） |
| 8.3 查询方式 | 仅提到"可视化界面浏览" | 补充具体 API 端点：`/api/kg/snapshot`、`/api/kg/explore` |
| 8.3 架构说明 | 无 | 新增：说明 lightrag-server 已统一取代旧版 vector-store 和 kg-server |
| 8.4 语义记忆工具 | 仅列出 `remember`、`recall` | 补充完整工具列表：`remember`、`recall`、`update_memory`、`get_memory_stats`、`cleanup_memories`、`link_memories` |
| 8.4 用户记忆便签 | 无 | 补充：便签（type=task）新任务自动覆盖旧便签 |
| 8.5 首次启动触发 | "memory.json 中存在 `firstRun` 字段" | 修正为 "memory.json 中 `firstRun` 为 `true`"（字段值判断而非字段存在判断） |
| 8.5 首次启动步骤 | 包含手动执行 `init_vector_db.py` 步骤 | 移除：向量库初始化已集成到启动流程，无需手动执行 |
| 8.6 数据存储 | 知识图谱路径 `{workspace}/lightrag/` | 修正为 `~/.niu/lightrag_storage/` |
| 8.6 数据存储 | 定时任务路径仅 `{workspace}/scheduled_tasks.db` | 补充 fallback 路径 `~/.niu/scheduled_tasks.db`，说明优先使用 workspace 路径 |
| 8.6 数据存储 | 历史对话路径 | 确认正确路径为 `~/.niu/messages.db`（单文件 SQLite，非目录） |
| 8.6 数据存储 | 列出 `vectors.db` 和 `{workspace}/lightrag/` | 移除旧向量索引引用，改为 `~/.niu/lightrag_storage/` |
| 8.6 备份 | 备份 `~/.niu/` + `{workspace}/` | 修正为 `~/.niu/` + `{workspace}/documents/`（LightRAG 数据已在 ~/.niu 下） |
