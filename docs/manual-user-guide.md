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

**配置逻辑总览**（先读这段，理解整体机制再动手）：

Niu 的模型配置以**设置窗口**为权威入口（`/setup` 命令或托盘图标打开），整套机制由三层构成：

1. **基础字段**（预设 / API Key / 地址 / 模型 / 类型 / 输出上限 max_tokens）：决定"用哪个模型"。模型名**大小写敏感**（如 zen/go 包月端点 `MiMo-V2.5` 大写 → 401，须全小写 `mimo-v2.5`）。`max_tokens` 缺省不传（服务端默认），长回复被截断时调大——见字段说明表。
2. **能力探测档案**（`~/.niu/model_capabilities.json`）：决定"这个模型支持哪些推理深度档位"。设置窗口"探测能力"按钮按**生产同参**（当前场景的 thinking 配置）实测模型真实支持的值域，写入档案后驱动推理深度下拉框——**只显示模型实际支持的档位**。档案键 = `apiBase|model|llm` / `apiBase|model|lightrag`；换模型/换服务商后旧档案不适用，**必须重新探测**。
3. **测试连接并保存**（testAndSave）：最后把关。先测连通性，再自动探测 response_format 能力（3 档递进），全程真实 LLM + 生产同参；参数组合无效（如推理深度档位与思考链状态不兼容）报"参数组合无效"**阻断保存**。全通过才写入配置文件。

参数送达通道：`reasoning_effort` / `thinking` 必须走 `extra_body` 注入（litellm 白名单会静默丢弃顶层参数——"测试通过但请求体没带参数"的假象根因，见《故障排查手册》1.7.2）。

LightRAG 继承：`lightrag_llm.model` 为空 = 继承主 llm（设置页只显示一个探测按钮，探测主 llm 自动填充入库段档位）。

**Agent 应引导用户在设置窗口完成配置**（见下方"Agent 引导用户配置指南"），不要直接替用户修改配置文件——手动改文件绕过能力探测档案与参数组合校验，是"配置了但参数无效/不生效"的主要来源。

**配置文件**：`~/.niu/config/user-config.json`

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
    "read_timeout": 300,
    "litellm_kwargs": {}
  },
  "lightrag_llm": {
    "presetId": "",
    "apiKey": "",
    "apiBase": "",
    "model": "",
    "type": "openai",
    "provider": "",
    "reasoning_effort": "",
    "read_timeout": 300,
    "litellm_kwargs": {}
  }
}
```

**缺省值已显式列出**：系统默认 `read_timeout: 300`（秒）。首次运行时按上述示例写入即可；后续需要调整（如模型响应慢调大、卡顿调小），建议通过设置窗口操作（改后主对话/子 Agent 链路**重启 Niu** 生效；知识图谱 LLM 调用**下次操作即时生效**）。

**字段说明**：

| 字段 | 说明 |
|------|------|
| `presetId` | 预设 ID，对应 llm-presets.json 中的预设 |
| `apiKey` | 你的 API Key |
| `apiBase` | API 端点基础地址（不含路径后缀，LiteLLM 会自动追加：openai 类型追加 `/chat/completions`，anthropic 类型追加 `/v1/messages`） |
| `model` | 模型名称 |
| `type` | 系统内部参数，用于区分 API 格式转换和认证方式：`openai`（OpenAI 兼容格式）或 `anthropic`（Anthropic 原生格式）。**不是** LiteLLM 的 custom_llm_provider |
| `provider` | LiteLLM 路由参数，映射为 `custom_llm_provider`。常见值：`""`（空，默认由 type 决定）、`"volcengine"`（火山引擎）。填写后模型名无需加厂商前缀 |
| `reasoning_effort` | 推理深度档位：`""`（空，由模型默认决定）、`"none"`（禁用）、`"low"`、`"medium"`、`"high"`、`"xhigh"` 等（取值以模型能力探测档案为准）。主 Agent 默认空；LightRAG 默认由探测档案驱动——设置窗口"探测能力"按钮（lightrag 段）探测后，下拉框只显示该模型实际支持的档位，选择即写入。**注意**：该参数的实际效果与模型基础能力强相关，不同模型的最优值差异很大，换模型/换服务商后必须重新探测（详见下方"reasoning_effort 配置与测试指南"）。`reasoning_effort` 只控制推理深度，**不控制**思考链返回——思考链返回由 `litellm_kwargs.thinking` 独立控制 |
| `litellm_kwargs` | 厂商特有参数，JSON 对象格式，原样透传给 LiteLLM。用于传递各厂商 SDK 要求的额外参数（如火山引擎的 `thinking`、`allowed_openai_params` 等）。代码不做任何厂商判断，只负责透传 |
| `read_timeout` | LLM 流式响应读取超时（秒），默认 `300`。模型首响应/流式 chunk 间隔超过该值判定超时并触发重试。**调小场景**：对话卡顿久等（如 `60`）；**调大场景**：知识图谱入库/大文档分析（分块分析可能数分钟，见下方 LightRAG 段）。`llm` 段控制主对话/子 Agent；知识图谱 LLM 调用默认继承 `llm` 段的 `read_timeout`，若 `lightrag_llm` 段配置了独立 `model`，则以 `lightrag_llm` 段的 `read_timeout` 为准（两段同默认 `300`） |
| `max_tokens` | 单次回复最大输出 token 数（输出上限）。**缺省不传**（用服务端默认）。长报告/长回复被截断时调大（如 `8192`/`16384`）。**注意**：模型对 max_tokens 无感知——它是服务端硬截断线，到上限即 `finish_reason=length` 截断；思考链（thinking）与正文**共享**该预算，思考链模型尤其要调大。设置窗口"测试连接并保存"会顺带校验该值合法性（非法值服务端 400 阻断保存）。只作用于会话对话链路（主 Agent/子 Agent/知识图谱），压缩与能力探测保持程序内部固定值 |

**自定义 HTTP 请求头（`litellm_kwargs.extra_headers`）**：

`litellm_kwargs` 是通用透传通道——除 `thinking`（程序拦截走专用通道）外，任何键都原样展开进 LiteLLM 调用。LiteLLM 一等参数 `extra_headers`（自定义 HTTP 请求头）在此通道内可用，适用于服务商标明要求附加请求头的场景，例如：OpenRouter 要求的 `HTTP-Referer`/`X-Title`、企业网关要求的鉴权/溯源头。配置示例（`llm` 段或 `lightrag_llm` 段均同）：

```json
"litellm_kwargs": {
  "extra_headers": {
    "HTTP-Referer": "https://example.com",
    "X-Title": "Niu"
  }
}
```

要点：
- **没有变量替换机制**：配置里的值是**字面量**——写什么字符串就原样发送什么，程序不会自动填充或替换任何值。适合静态请求头（固定的 Referer、产品名、固定鉴权头）。
- **全链路覆盖**：主对话/子 Agent（`llm` 段）与知识图谱入库/查询（`lightrag_llm` 段）的出网 LLM 调用统一走 `LiteLLMSession`，`litellm_kwargs` 同一机制透传，`extra_headers` 两段都生效。
- **与 drop_params 兼容**：`litellm_kwargs` 非空时程序自动开启 `drop_params`（厂商不支持的参数自动丢弃），实测 `extra_headers` 不受其影响——LiteLLM 的 openai 兼容路径参数白名单原生包含它。
- **设置窗口不提供该字段的可视化编辑**，需手动编辑 `~/.niu/config/user-config.json`（关闭程序后编辑；改后回设置窗口"测试连接并保存"验证连通）。保存时已有键会保留（仅 thinking 被增删），`extra_headers` 不会被设置窗口保存覆盖。
- **边界——每会话动态 id 不支持**：部分服务商要求的头是**每会话动态 id**（如 OpenCode Go 的 `x-opencode-session`，语义是 stable-id-per-conversation）。配置层写死一个值虽然请求不会报错，但所有会话共用同一个 id，与该头的语义不符（服务端会把所有会话当成一个）。此类需求需要程序层按会话注入（`LiteLLMSession` 调用层动态合并 `{**静态头, "x-opencode-session": 会话id}`），**当前程序未实现**——真接入该服务商时由开发补上，配置层届时无需改动。
- **Agent 引导指引**：用户要求"给模型配置加某个请求头"时，若该头是静态值（Referer、鉴权头、固定追踪头），直接引导走本节 `litellm_kwargs.extra_headers`；若是每会话动态值（会话 id 类），如实告知当前仅支持静态值、动态注入需程序层支持，不要用写死的值硬凑语义。

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
| Coding Plan | `https://ark.cn-beijing.volces.com/api/coding/v3` | 包月计费 | **不支持**（网关 400 拒绝） | 主 Agent |

**格式化输出（response_format）能力自动探测**

设置窗口"测试连接并保存"按钮在测试通过后，会自动追加一次格式化输出能力探测，按 3 档递进调用真实 LLM，找出当前配置支持的最强档位：

| 档位 | response_format.type | 约束 | 典型支持厂商 |
|------|---------------------|------|--------------|
| Tier 1（最强） | `json_schema` strict | 输出严格匹配 schema | OpenAI 真正支持 |
| Tier 2（中等） | `json_object` | 输出合法 JSON，不约束字段 | OpenAI、DeepSeek |
| Tier 3（最弱） | 无 response_format | prompt + `json_repair` 客户端容错 | 所有厂商兜底 |

**探测流程**：从 Tier 1 开始测，失败则降级测 Tier 2，再失败则定为 Tier 3。每档失败条件：
- `param_conflict`：LiteLLM 抛 400 且错误文案含 `reasoning_effort`/`combination`（参数组合无效，如推理深度档位与思考链状态不兼容）——探测立即失败并**阻断保存**，提示"参数组合无效"，请调整思考链/推理深度档位后重试
- `model_rejected`：LiteLLM 抛 `BadRequestError`/`UnsupportedParamsError`（模型/网关 4xx 拒绝，如豆包 Coding Plan）
- `gateway_blocked`：网关返回 200 但响应非合法 JSON（网关接受参数但模型输出漂移，如 GLM xopglm5）

探测采用三次采样 + 冲突式设计：
- 三次采样全过才升档，任何一次失败立即降级——防 flaky 网关（豆包 Coding
  Plan 2026-07-21 实测：同一请求 5 次采样 2 次执行、3 次静默忽略）碰巧
  命中执行窗口期误判支持
- 冲突式设计：schema 强制要求 `{"verdict": "SCHEMA_ENFORCED"}`，prompt
  却要求模型写普通英文句子且禁止输出 JSON——只有 schema 战胜 prompt
  （输出被强制为 schema JSON）才判定真支持，模型跟随 prompt 输出普通
  文本即判定网关静默忽略
- 限流单独处理：RateLimitError 不计失败，sleep 后重试本次采样（指数
  退避 5s→10s→20s→40s→80s，最多 5 次），直到返回非限流结果才判定

**写入配置**：
- `lightrag_llm.litellm_kwargs.response_format_mode`：`"json_schema"` / `"json_object"` / `"prompt_only"`
- `lightrag_llm.litellm_kwargs.allowed_openai_params`：前两档 `["response_format"]`，prompt_only 档 `[]`（双写兼容旧逻辑）

**典型场景**（2026-07-19 实测）：
- 豆包 Coding Plan 端点（`/api/coding/v3`，model=`ark-code-latest`）：
  网关行为非确定性（flaky），同一请求多次采样结果不稳定（有时执行
  json_schema strict、有时静默忽略），三次采样必然 ≥1 次静默忽略 →
  探测结果稳定 `prompt_only`
- GLM 端点（`maas-coding-api.cn-huabei-1.xf-yun.com/v2`，model=`xopglm5`）：网关接受但模型输出漂移，探测结果 `prompt_only`
- OpenAI 官方：探测结果 `json_schema`

**升级后自动探测**：旧版本用户配置无 `response_format_mode` 字段。程序启动后若检测到该字段缺失，后台自动触发一次探测写入配置，不阻塞启动。

**手动覆盖**：关闭程序后手动编辑 `~/.niu/config/user-config.json` 的 `lightrag_llm.litellm_kwargs.response_format_mode`。下次设置窗口测试保存会覆盖手动值。

**注意**：
- 探测调用会消耗约 100-200 token（最坏情况测到 Tier 2）。仅"测试连接并保存"按钮触发，不引入后台定时探测（升级后首次启动除外）。
- 探测独立于启动时的 LLM 连通性测试（`/api/test-llm`），不影响启动速度。

> **重要**：LightRAG 的 keyword_extraction 依赖 `response_format`，必须使用标准端点。如果误用 Coding Plan 端点，本次升级后的"格式化输出能力自动探测"会识别为 `prompt_only` 档位并写入配置，运行时 LightRAG 直接走 prompt-only 路径（不再每次发无效请求触发 400 后 fallback）。详见上方"格式化输出（response_format）能力自动探测"小节。

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
| `reasoning_effort` | **推理深度档位（核心配置）** | 探测后从下拉选择模型实际支持的档位；未探测时留空（模型默认） |
| `temperature` | LLM 采样温度。0.0 完全确定性，1.0 高随机度。为空时由系统兜底 0.2 | `0.2`（实体/关系抽取建议低温度避免 JSON 漂移） |
| `litellm_kwargs` | 厂商特有参数，同 `llm` 段说明。火山引擎知识图谱需传 `thinking` 和 `allowed_openai_params` | `{}` 或见配置示例 |
| `max_tokens` | 单次回复最大输出 token 数。**缺省不传**（用服务端默认）；为空时自动继承 `llm` 段的 max_tokens。入库调用一般不需要调大——结构化输出被截断（日志见 `finish_reason=length`）时再设 | 空 |

> **重要**：LightRAG 官方建议入库时不要使用带思考链的模型。思考链会导致实体提取超时（单次调用可达 198 秒）。是否返回思考链由 `litellm_kwargs.thinking` 独立控制（与 `reasoning_effort` 是两个不同参数，互不替代）——火山方舟入库场景配置 `thinking: {"type": "disabled"}` 关闭思考链返回。`reasoning_effort` 只控制推理深度档位，默认由模型能力探测档案驱动（不再预设固定档位，见下方"reasoning_effort 配置与测试指南"），即使主 Agent 使用思考链模型，只要入库段 thinking disabled + 探测出的合理档位，入库也不受影响。

> **温度值说明**：LightRAG 调 LLM 时如果不配 `temperature` 字段，系统兜底默认 `0.2`。这是为实体抽取、关系抽取等结构化任务优化的低温度值，避免高随机度导致 JSON 格式漂移、实体名不一致。如果需要更稳定的输出可设 `0.0`，需要更多创造性可设 `0.5~0.7`，但不建议超过 `1.0`。主 Agent / 子 Agent 的温度值在提示词文档里独立配置（主 Agent 0.6、子 Agent 多数 0.2），与此处的 `lightrag_llm.temperature` 完全独立，互不影响。

**reasoning_effort 参数说明**：

| 值 | 效果 | 建议场景 |
|----|------|----------|
| `none` | 完全禁用 | 建议初始使用时，根据模型情况自己测试后确定 |
| `low` | 浅层推理 | 模型推理速度极慢时，可选用 |
| `medium` | 中等推理 | 非入库的图谱查询任务；部分模型入库也可用 |
| `high` | 深度推理 | 建议用于入库（推理预算耗尽，输出质量高） |
| `xhigh` | 最深推理 | 不建议用于入库 |

**reasoning_effort 配置与测试指南**：

`reasoning_effort` 的最优值与模型基础能力强相关，不存在通用最优值，且**不同厂商/模型对取值范围的接受度不同**（部分服务端对不支持的取值直接 400，部分静默忽略）。系统通过**模型能力探测器**按"生产同参"实测每个模型真实支持的值域，写入能力档案后驱动配置页动态档位：

- **LightRAG 段默认档位由探测档案驱动**（2026-08-18 起，不再预设固定档位——旧版曾兜底 `"high"`）：设置窗口"探测能力"按钮（lightrag 段）探测后，推理深度下拉只显示该模型**实际支持**的档位（厂商原生档位名原样），选择即写入 `lightrag_llm.reasoning_effort`；未探测时留空（由模型默认决定）。
- **探测按场景同参**：值域扫描 `[minimal, low, medium, high, xhigh, none, max]`，请求携带当前场景的 thinking 配置（lightrag 场景恒 disabled、llm 场景按用户配置）——**值域结论只对当前场景 thinking 成立**，不得外推到其他场景（如豆包 `disabled` 场景只接受 minimal/none，`high` + disabled 直接 400）。
- **档案位置**：`~/.niu/model_capabilities.json`，键 = `apiBase|model|llm` / `apiBase|model|lightrag`——换模型/换服务商后旧档案不适用，**必须重新探测**。

实测数据（ark-code-latest 模型入库 SYSTEM_MANUAL.md，仅供参考——不同模型差异很大）：

| 配置 | 实体类型准确率 | 空响应率 | 补充提取有效性 | 综合评分 |
|------|--------------|---------|--------------|---------|
| `none` | 67% | 60% | 无 | 5.5/10 |
| `low` | 95% | 40% | 有效 | **7.5/10** |
| `medium` | 14% | 40% | 部分 | 6.0/10 |
| `high` | 分裂 | 40% | 无 | 4.0/10 |

关键发现：
1. **推理越深不一定越好**：推理token被模型内部消耗，不转化为输出质量。过度推理反而导致实体类型误分类（如子Agent被归为organization而非technology）
2. **补充提取受影响**：高推理级别下模型过度自信，判断"无需补充"，导致遗漏无法弥补
3. **不同模型差异很大**：能力强的模型（如 Claude、GPT-4o）在 `none` 或 `low` 即可高质量提取；能力有限的模型可能需要 `high`，但也可能适得其反

**换模型/换服务商后必须重新探测**（2026-08-18 起）：
- **设置窗口"探测能力"按钮**：llm 段与 lightrag 段各一个按钮，探测成功后自动刷新推理深度下拉（档案 supported 档位）
- **CLI**（主 Agent 可直接调用）：
  ```bash
  python/bin/python3 scripts/model_capability_probe.py --api-base URL --model MODEL [--api-type anthropic] [--lightrag] [--api-key KEY]
  ```
  - `--lightrag` 探测 lightrag_llm 场景（档案键后缀 `|lightrag`，apiKey 缺省从 lightrag_llm 段读）
  - 退出码 `0` = 档案已更新；`1` = 探测失败未覆盖旧档（保持旧档案，检查配置后重试）
  - 探测预算：单场景 ≈11 次极小请求（单次 ≤10s），值域候选超时重试最坏 ≈140s——CLI 建议 `timeout=150`；双场景（llm + lightrag）建议 `timeout=300` 或分两次调用

探测通过后，用同一文档入库并检查日志验证实际效果（见下方"入库质量检查方法"）。

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

> **注意**：LightRAG 必须使用标准端点（apiBase 含 /api/v3），不能用 Coding Plan 端点（/api/coding/v3），因为 Coding Plan 网关 400 拒绝 response_format。设置窗口"测试连接并保存"会自动探测并写入 `response_format_mode=prompt_only`（豆包 Coding Plan 场景）。model 必须用标准端点的全名格式（带日期后缀）。
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

场景三：能力强的模型，禁用思考链返回即可（如 Claude、GPT-4o）——注意"关思考链"用 `thinking` 参数，`reasoning_effort` 留空（模型默认）或选探测支持的低档位：
```json
"lightrag_llm": {
  "presetId": "",
  "apiKey": "",
  "apiBase": "",
  "model": "",
  "type": "openai",
  "provider": "",
  "reasoning_effort": "",
  "temperature": 0.2,
  "litellm_kwargs": {
    "thinking": {"type": "disabled"}
  }
}
```

**通过 MCP 工具动态修改**（无需重启）：
- 读取配置：调用 `get_lightrag_llm_config` 工具
- 修改配置：调用 `set_lightrag_llm_config` 工具，参数与字段名对应
- 清除模型（回退到主模型）：调用 `set_lightrag_llm_config(model="")`

**修改配置方式**（按推荐顺序）：

| 方式 | 说明 | 适用 |
|------|------|------|
| 设置窗口 | `/setup` 命令或托盘"设置"打开。完整流程：填基础字段 → 探测能力 → 选档位 → 测试连接并保存 | **推荐**——唯一带能力探测与参数组合校验的入口 |
| MCP 工具 | `set_lightrag_llm_config` / `get_lightrag_llm_config`（运行中修改，无需重启） | 仅调整入库段字段时的快捷方式 |
| 手动编辑 `~/.niu/config/user-config.json` | 关闭程序后编辑 | **最后手段**——绕过探测档案驱动与 testAndSave 参数组合校验，可能写入无效组合（如 `high` + disabled → 400）；改后必须回设置窗口"测试连接并保存"验证 |

**Agent 引导用户配置指南**（最佳效果）：

当用户要求配置模型/更换模型/调整参数时，按以下流程引导用户在设置窗口操作——**不要替用户手改配置文件**：

1. **打开设置窗口**：告诉用户输入 `/setup` 命令，或点托盘图标 → 设置。
2. **基础配置**：引导选择预设，或填入 API Key / 地址 / 模型 / 类型。提醒：模型名**大小写敏感**，从服务商控制台复制原样粘贴；不确定模型名时查该服务商文档或先探测验证。
3. **探测能力**：点"探测能力（对话模型）"按钮——探测完成后推理深度下拉只显示该模型**实际支持**的档位。入库段若继承主模型（`lightrag_llm.model` 为空，默认），探测主 llm 会自动填充入库段，无需单独探测。
4. **选档位**：llm 段按对话需求选（深度思考选 high，速度优先选 low/medium）；入库段按知识图谱质量选——见上文"reasoning_effort 配置与测试指南"实测数据（能力强的模型 none/low 即可高质量提取，**不要盲目 high**；不同模型差异大，以探测档案为准）。
4.5 **输出上限（可选）**：长回复被截断（回复不完整/报告断尾，或日志 `finish_reason=length`）时，引导用户在"输出上限 (max_tokens)"填入更大值（如 `8192`/`16384`），再点"测试连接并保存"——测试会顺带校验该值合法性（非法值服务端 400 阻断）。思考链模型尤其注意：thinking 与正文共享该预算。
5. **测试连接并保存**：点按钮——程序自动测连通性 + response_format 能力 + 参数组合校验 + max_tokens 合法性，全部通过才保存。若报"参数组合无效"，回到第 3-4 步调整档位/思考链后重试。
6. **验证效果**：入库一个文档，按上文"入库质量检查方法"检查日志确认提取质量；对话侧直接发消息观察回复。

> **边界**：仅当用户明确要求"帮我直接改配置文件"、或设置窗口不可用（如后端异常）时，才允许 Agent 手动修改 `user-config.json`——修改后必须告知用户回设置窗口"测试连接并保存"做一次校验。

**配置独立入库模型（第二个模型）**：

设置窗口的入库卡片**只支持继承场景**（`lightrag_llm.model` 为空 = 用主模型，只显示一个探测按钮）——它没有暴露入库段的 API Key/地址/模型输入框。当用户需要**独立的入库模型**（如主对话用思考链模型、入库用轻量模型）时，必须由 Agent 协助：

1. **写入配置**：调用 MCP 工具 `set_lightrag_llm_config`（config-manager 服务器）：
   - 按预设：`set_lightrag_llm_config(preset_id="doubao")`（自动填 apiBase/model/type）
   - 完全自定义：`set_lightrag_llm_config(api_key="…", api_base="https://ark.cn-beijing.volces.com/api/v3", model="doubao-seed-2-0-pro-260215", llm_type="openai")`
   - 输出上限：`set_lightrag_llm_config(max_tokens=8192)`；`max_tokens=0` 清除（回退不传）；读回确认用 `get_lightrag_llm_config`
   - 回退继承：`set_lightrag_llm_config(model="")`（清除独立模型，reasoning_effort 和 max_tokens 保留——均独立维度）
2. **探测档位**：入库模型配好后，让用户在设置窗口点"探测能力（入库模型）"按钮（此刻非继承，按钮显示）——探测写入 `|lightrag` 档案并刷新档位下拉。
3. **校验保存**：引导用户选档位后点"测试连接并保存"——程序按入库段配置真实探测 + 参数组合校验，通过才落盘。

> 独立入库模型建议：轻量快速（如豆包标准端点 `doubao-seed-2-0-pro-260215`），`thinking: {"type": "disabled"}` 关闭思考链（入库建议），`temperature` 建议 0.2 或更低；若用火山方舟，必须用**标准端点**（含 `/api/v3`）——Coding Plan 端点 400 拒绝 response_format，入库无法工作。

**Agent 自主评测新模型能力**：

Agent 可以自己启动评测代码评估一个新模型，无需用户手动操作。评测工具 = `scripts/model_capability_probe.py`（真实 LLM 调用，按生产同参扫描模型支持的推理深度值域）：

```bash
python/bin/python3 scripts/model_capability_probe.py \
  --api-base https://api.example.com/v1 \
  --model 模型名 \
  [--api-type anthropic]   # 默认 openai；Anthropic 原生格式才需要
  [--api-key KEY]          # 缺省从配置文件 llm 段读
  [--lightrag]             # 评测入库场景（档案键后缀 |lightrag，值域按入库 thinking 配置）
```

**何时评测**：用户换模型/换服务商/不确定模型支持哪些档位时；或用户要求"帮我看看这个模型能不能用/支持什么档位"。

**评测流程与结果**：
1. 先向用户确认模型名（**大小写敏感**——从服务商控制台复制原样粘贴；zen/go 包月端点模型名全小写）与 API Key 归属（避免把用户 key 用于未知端点）。
2. 运行 CLI（llm 场景）或加 `--lightrag`（入库场景）。**评测要真实连接模型，需在目标机执行**；若 Niu 运行中且目标模型就是当前配置，也可引导用户直接用设置页"探测能力"按钮（同核心、免命令行）。
3. 退出码 `0` = 档案已更新（`~/.niu/model_capabilities.json`，键 `apiBase|model|llm` / `|lightrag`）；`1` = 探测失败**保持旧档案**（检查 429 限流/401 认证/404 模型名后重试）。
4. 评测后：告诉用户该模型支持的档位清单，引导在设置窗口选档位 → "测试连接并保存"完成配置；或直接 `set_lightrag_llm_config(reasoning_effort="low")` 写入（但仍建议走一次设置页校验）。
5. 预算提示：单场景 ≈11 次极小请求，值域超时重试最坏 ≈140s——CLI 建议超时 `timeout=150`；双场景 ≈280s 建议 `timeout=300` 或分两次。

> **评测纪律**：①评测消耗用户 API 配额（11 次极小请求），先征得用户同意；②评测值域只对**当前场景 thinking 配置**成立（lightrag 恒 disabled、llm 按用户配置）——不要把一个场景测出的档位外推到另一个场景；③评测不是测试对话质量——档位支持 ≠ 输出质量，质量评估按上文"入库质量检查方法"或对话实测。

### 1.3 上下文配置

**配置文件**：`~/.niu/config/user-config.json` 中的 `context` 段

```json
{
  "context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "sleepTriggerMinutes": 5
  }
}
```

**字段说明**：

| 字段 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `contextWindowSize` | 模型上下文窗口大小（tokens） | 200000 | 32000 ~ 2000000 |
| `warningThreshold` | 溢出警告阈值，校准后上下文使用率超过此值触发批量压实（滞回：<78% 复位） | 0.8 | 0.0 ~ 1.0 |
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
- **方式二**：关闭程序后，手动编辑 `~/.niu/config/user-config.json`

**上下文相关新增字段（2026-08-26 上下文组装器起）**：

| 字段 | 说明 | 默认值 |
|------|------|--------|

| `keepRecentTurns` | 压实后保留的最近会话轮数 | 3 |
| `journalScheduledEnabled` | 每日 18 点定时整理工作日志到 journal.md | true |

#### 你需要知道的行为变化

- **对话历史不会丢失**：早期对话超出原文窗口后，会被归档为带编号的「历史块」。AI 看到的是一份历史索引（每块一行摘要），需要时 AI 会自动调用取回工具读取任意块的**逐字原文**——你也可以直接说"把第 3 块的内容调出来看看"。
- **/compact 按钮秒级完成**：手动压实是纯机械操作（不调用大模型），点击后立即生效；所有被收起的早期内容都还在，随时可取回。
- **上下文圆环跳变属正常**：使用率圆环基于服务端返回的真实 token 数与本地估算校准，压实发生后圆环会明显回落、偶发小幅跳变——这是校准机制在正常工作，不代表异常。
- **/new 与 /clear 清空彻底但保留日记**：清空全部对话及派生数据（归档块、缓存等）；journal.md 工作日志作为长期资产**不会被清空**。
- **每日自动整理工作日志**：每天 18 点系统自动把当天新增对话整理进 journal.md（活跃对话时会自动避让等待），无需手动触发。

### 1.4 知识图谱

知识图谱基于 LightRAG 引擎，支持文档入库后的自动实体提取和关系构建。

**查询方式**：
- 直接向大模型提问（如："XXX 和 YYY 有什么关系？"）
- 通过知识图谱可视化界面浏览实体关系（`/api/kg/snapshot`、`/api/kg/explore`）

**存储位置**：`~/.niu/lightrag_storage/`（LightRAG 固定存储路径，不随 workspace.path 变化）

**架构说明**：知识图谱和向量检索已统一由 `lightrag-server` 提供（23 个工具），取代了旧版独立的 `vector-store` 和 `kg-server`。旧的 `kg-server`（KuzuDB）和 `vector-store` 已禁用（`preload: false`）。

**注意**：文档入库时，LightRAG 自动完成实体提取、关系构建和向量索引，无需手动操作。

**注意**：并非所有文档格式都支持知识图谱入库，详见下方"1.5 支持的文件格式"。

**关系方向**：图谱关系为无向存储——查询返回的 source/target 顺序不代表方向，方向语义在关系描述文本中（如"李磊 属于 人际关系脑区"）。系统已在工具描述中说明此契约，Agent 会按描述文本解读方向。

**时间链**：会话实体按日期以 `followed_by` 相连（如 `2026-08-08会话 → 2026-08-09会话`），由系统自动补全维护。询问"之前/后来/某天发生了什么"时，可让 Agent 沿时间链查询（timeline_query）或定位对应日期的会话实体展开当天内容。

**入库参数配置**：LightRAG 入库参数（并发数、分片大小、补充提取次数等）可在 `~/.niu/preferences.json` 的 `lightrag` 配置段调整，详见 [知识检索运维手册](manual-vector-store.md) 第 8.5 节。

**入库模型与思考链配置**：LightRAG 入库使用的模型和推理深度档位在 `~/.niu/config/user-config.json` 的 `lightrag_llm` 配置段设置，详见上方 1.2 节"LightRAG 知识图谱 LLM 配置"。思考链返回由 `litellm_kwargs.thinking` 独立控制（火山方舟入库配置 `thinking: {"type": "disabled"}` 关闭，防止深度思考导致入库超时）；`reasoning_effort`（推理深度档位）由模型能力探测档案驱动，默认不预设固定档位。

**LightRAG 操作超时**（`~/.niu/preferences.json` 的 `lightrag` 段，缺省值已显式列出，首次运行按此写入）：

| 键 | 默认 | 说明 |
|---|---|---|
| `insert_timeout` | 600 | 文档/照片入库超时（秒）。大文档分块分析可能数分钟，模型慢时调大 |
| `query_timeout` | 120 | 知识图谱语义查询/图谱快照超时（秒） |
| `delete_timeout` | 300 | 文档/实体删除超时（秒） |
| `status_timeout` | 30 | 处理状态/图谱标签等轻量查询超时（秒） |
| `merge_timeout` | 300 | 实体合并超时（秒） |

配置示例：

```json
{
  "lightrag": {
    "embedding_model": "bge-base-zh-v1.5",
    "reranker_model": "none",
    "insert_timeout": 600,
    "query_timeout": 120,
    "delete_timeout": 300,
    "status_timeout": 30,
    "merge_timeout": 300
  }
}
```

> 生效方式：主对话/子 Agent 的 `read_timeout`（user-config.json 的 llm 段）修改后需**重启 Niu** 生效；知识图谱 LLM 调用的 `read_timeout`（lightrag_llm 段或继承的 llm 段）与 LightRAG 操作超时（preferences.json 的 lightrag 段）每次操作实时读取配置，**修改后即时生效**。这些高级设置建议通过设置窗口操作；确需运行中调整时可调用 MCP 工具（如 `set_lightrag_llm_config`）即时生效。

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

**2. 话题暂存（park/recall，暂存告一段落的话题）**
- 用户说「这事先告一段落/回头再说/先放着」时，主 Agent 自动把该话题暂存（`~/.niu/memory.json` 的 `parked` 数组，最多 10 条）
- 暂存后每轮对话的上下文都会出现一行 `[暂存事项]` 提醒（摘要一句话，随时可召回）
- 用户提起之前暂存的话题时，主 Agent 按提醒行序号召回，恢复该话题的上下文后继续处理；**召回即从暂存列表移除**
- 与工作便签的区分：便签=进行中任务进度（1 条自动覆盖）；暂存=被叫停的多个话题（多条并存）

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

6. 完成后，下一轮对话起不再出现首次使用提示（memory.json 每轮重读，写入后立即生效）

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

### 1.9 日志配置

**配置文件**：`~/.niu/config/user-config.json`（首次启动从 bundle 内 `config/user-config.json` 模板复制）

**配置字段**：

````json
{
  "logging": {
    "enabled": false,
    "level": "INFO"
  }
}
````

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 日志总开关。`false` 时所有日志输出关闭（见下表），`true` 时按 `level` 输出 |
| `level` | string | `"INFO"` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

**关闭日志时（`enabled=false`，缺省）受控的日志源**：

| 日志源 | 文件路径 | 控制方式 |
|--------|----------|----------|
| Python loguru sink | stderr | `logger.disable("")` 全局禁用 |
| Python stdlib logging | stderr | `logging.disable(CRITICAL)` 禁用 10+ 处散落 logger |
| uvicorn 访问日志 | stderr | `log_level="critical"` + `access_log=False` |
| raw_http transport 层日志 | `~/.niu/logs/raw_http/YYYYMMDD/NNNNNN.json` | `install_http_logger()` 不 patch HTTP client + 幂等守卫 |
| raw_http 应用层日志 | `~/.niu/logs/raw_http/YYYYMMDD/NNNNNN_request.json` + `_response.json` | `_write_raw_log()` 静默跳过 |
| LLM interaction 可读日志 | `~/.niu/logs/llm_interaction_YYYYMMDD.log` | `_write_interaction_log()` 静默跳过 |
| 飞书 adapter stderr | `~/.niu/logs/im_adapter_stderr.log` | `subprocess.DEVNULL` 代替文件重定向 |
| /http-log/ HTTP 日志查看服务 | http://localhost:9876/http-log/ | router 不挂载（返回 404） |
| Rust tracing | stderr | `tracing_subscriber` 不 init（tracing 调用静默丢弃） |

**不受日志开关控制的诊断日志**（关键诊断必须保留）：

| 日志源 | 文件路径 | 用途 |
|--------|----------|------|
| launcher 致命错误 | `~/.niu/logs/launcher_error.log` | 启动失败诊断（API 未运行、Electron 启动失败等），用 `time` crate 格式化时间戳 |
| gateway 致命错误 | `~/.niu/logs/gateway_error.log` | 飞书 adapter 启动失败诊断（app_id 配错、端口占用、credentials 缺失） |


**日志目录**：

所有运行时日志统一写入 `~/.niu/logs/`（macOS/Linux）或 `%USERPROFILE%\.niu\logs\`（Windows），不在 bundle 内（macOS Gatekeeper 禁止运行时改 bundle 内文件）。

**Windows 控制台窗口**：

Windows release build 下，niu.exe 编译为 GUI 子系统（`#![cfg_attr(all(target_os="windows", not(debug_assertions)), windows_subsystem="windows")]`），双击不弹 cmd 窗口。debug build 保留 console 方便调试。

**macOS 控制台窗口**：

macOS 下构造 `niu.app` bundle（`Info.plist` 含 `LSUIElement=true`），Finder 双击不弹 Terminal。命令行 `./niu` 裸二进制仍保留供开发调试。

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
