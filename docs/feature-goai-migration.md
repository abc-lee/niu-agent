# GoAI SDK 统一 LLM 层迁移文档

## 概述

本次迁移将原本碎片化的 LLM 通讯层统一到 GoAI SDK，实现了统一的 Token 使用量统计。

## 迁移原因

### 原有架构问题

```
原架构（碎片化）：
pkg/llm/
├── anthropic/    → 独立 HTTP client
├── genai/       → maruel/genai SDK（仅思维链模型）
├── completions/  → 独立 HTTP client
└── responses/   → 独立 HTTP client

问题：
- 各 Provider Usage 字段名不统一（prompt_tokens vs input_tokens）
- CompletionResponse 没有 Usage 字段
- 无法统一计算 Token 使用量
```

### 新架构

```
新架构（统一）：
pkg/llm/
├── client.go      → 路由逻辑
└── goai/          → GoAI adapter（统一接口）

优势：
- 统一的 Usage 格式
- Token 使用量可直接获取
- 支持思维链模型
- 支持 22+ Provider
```

## 新增文件

### `pkg/llm/goai/client.go`

核心功能：
- 实现 `types.Completer` 接口
- 根据 `apiType` 路由到正确的 Provider
- 统一处理流式输出
- 自动提取思维链标签
- 返回 Token 使用量

```go
// 使用示例
result, err := goai.NewClient(
    model,
    apiKey,
    baseURL,
    apiType,  // "openai" 或 "anthropic"
).Complete(ctx, req)
```

## 配置说明

### 配置文件格式

```json
{
  "llm": {
    "presetId": "模型名称",
    "apiKey": "API密钥",
    "apiBase": "API端点URL",
    "model": "模型ID",
    "type": "openai 或 anthropic"
  }
}
```

### 支持的 Provider

| Provider | type 配置 | apiBase 示例 |
|----------|----------|--------------|
| 火山引擎 | `openai` | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| MiniMax OpenAI | `openai` | `https://api.minimaxi.com/v1` |
| MiniMax Anthropic | `anthropic` | `https://api.minimaxi.com/anthropic/v1` |
| OpenAI | `openai` | `https://api.openai.com/v1` |
| DeepSeek | `openai` | `https://api.deepseek.com` |
| Anthropic | `anthropic` | `https://api.anthropic.com` |

> **注意**：MiniMax 有两种端点：
> - **OpenAI 兼容** (`minimaxi.com/v1`)：使用 `type: "openai"`
> - **Anthropic 兼容** (`minimaxi.com/anthropic/v1`)：使用 `type: "anthropic"`

## Token 使用量获取

### 日志输出

每次 LLM 调用后会输出 Token 使用量：

```
level=INFO msg="Token usage" input=3526 output=101 total=3627 reasoning=66 cacheRead=0 cacheWrite=0
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `input` | 输入 Token 数 |
| `output` | 输出 Token 数 |
| `total` | 总 Token 数 |
| `reasoning` | 思维链 Token 数（思维链模型） |
| `cacheRead` | 缓存命中 Token 数 |
| `cacheWrite` | 缓存写入 Token 数 |

### 程序内获取

Token 使用量存储在 `CompletionResponse.InternalMessages` 中：

```go
// 返回的 response 中包含 Usage 数据
response, err := client.Complete(ctx, req)

// Usage 数据在 InternalMessages 里
// 格式：JSON 字符串
// {
//   "inputTokens": 3526,
//   "outputTokens": 101,
//   "totalTokens": 3627,
//   "reasoningTokens": 66,
//   "cacheReadTokens": 0,
//   "cacheWriteTokens": 0
// }
```

### 应用层实现建议

1. **创建 Token 使用量显示组件**

```go
// 示例：从 InternalMessages 提取 Usage
func extractUsage(response *types.CompletionResponse) map[string]int {
    for _, msg := range response.InternalMessages {
        for _, item := range msg.Items {
            if item.Content != nil && item.Content.Type == "text" {
                var usage map[string]int
                if json.Unmarshal([]byte(item.Content.Text), &usage) == nil {
                    return usage
                }
            }
        }
    }
    return nil
}
```

2. **上下文窗口溢出判断**

参考 OpenCode 的实现：

```go
func isOverflow(tokens map[string]int, modelContextLimit int, maxOutputTokens int) bool {
    reserved := 20000 // 为输出预留的空间
    usable := modelContextLimit - reserved
    return tokens["totalTokens"] >= usable
}
```

3. **模型上下文窗口配置**

| 模型 | 上下文窗口 |
|------|-----------|
| MiniMax-M2.7 | 1M tokens |
| DeepSeek-R1 | 64K tokens |
| GPT-4o | 128K tokens |
| Claude-4 | 200K tokens |

## 路由逻辑

```
请求进入 goai.NewClient()
    ↓
配置路由（pkg/llm/client.go）：
    ├─ apiType == "anthropic" → 使用 Anthropic.APIKey + Anthropic.BaseURL
    └─ apiType == "openai"    → 使用 Responses.APIKey + Responses.BaseURL
    ↓
模型选择（pkg/llm/goai/client.go）：
    ├─ MiniMax + Anthropic 端点 → 特殊处理（strip /v1, Bearer auth）
    ├─ apiType == "anthropic"   → Anthropic provider
    ├─ apiType == "openai"      → OpenAI provider
    └─ default                  → 按模型名启发式选择
```

### MiniMax Anthropic 特殊处理

MiniMax Anthropic 兼容端点需要特殊处理：

1. **URL 路径处理**：GoAI anthropic provider 会追加 `/v1/messages`，需要 strip 配置中的 `/v1` 后缀
2. **认证方式**：使用 `Authorization: Bearer` 而非 `x-api-key`

```go
// pkg/llm/goai/client.go
isMiniMaxAnthropic := (strings.HasPrefix(model, "MiniMax") || strings.HasPrefix(model, "minimax")) &&
    strings.Contains(c.baseURL, "/anthropic")

if isMiniMaxAnthropic {
    // Strip /v1 suffix
    baseURL := strings.TrimSuffix(c.baseURL, "/v1")
    // Use Bearer auth
    opts := []anthropic.Option{
        anthropic.WithAPIKey(c.apiKey),
        anthropic.WithHeaders(map[string]string{
            "Authorization": "Bearer " + c.apiKey,
        }),
        anthropic.WithBaseURL(baseURL),
    }
    return anthropic.Chat(model, opts...), nil
}
```

## 思维链处理

### 当前状态

| Provider | 思维链处理 |
|----------|-----------|
| DeepSeek-R1 | ✅ API `reasoning_content` 字段，自动分离 |
| QwQ | ✅ API `reasoning_content` 字段，自动分离 |
| MiniMax OpenAI | ⚠️ `

` 标签在 content 里，部分过滤 |
| MiniMax Anthropic | ✅ API `thinking` block，自动分离 |

### 思维链标签过滤

对于 MiniMax OpenAI 端点，实现了基础的标签过滤：

```go
// 根据模型类型检测标签格式
func getReasoningTagName(model string) string {
    // MiniMax M2.x → "think" 标签
    if strings.Contains(model, "minimax") && strings.Contains(model, "m2") {
        return "think"
    }
    return ""
}
```

## 已知限制

1. **思维链过滤**
   - 流式输出中可能仍显示思维链标签
   - 新模型可能需要更新标签检测逻辑

2. **Prompt Caching**
   - `cacheRead` 和 `cacheWrite` 数值已正确获取
   - 应用层需要根据模型判断是否支持 caching

## 依赖更新

```go
// go.mod 新增
require github.com/zendev-sh/goai v0.5.4
```

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `pkg/llm/goai/client.go` | 新增 | GoAI adapter 实现 |
| `pkg/llm/goai/client.go` | 修复 | MiniMax Anthropic 特殊处理（strip /v1, Bearer auth） |
| `pkg/llm/goai/client.go` | 修复 | 通用 Anthropic 端点 strip /v1 后缀 |
| `pkg/llm/client.go` | 修改 | 路由到 GoAI |
| `pkg/llm/client.go` | 修复 | 根据 apiType 选择正确的配置 |
| `go.mod` | 修改 | 添加 GoAI 依赖 |

## 回滚方案

如需回滚，恢复 `pkg/llm/client.go` 的路由逻辑：

```go
// 恢复旧的路由逻辑
if c.cfg.APIType == "anthropic" {
    return anthropic.NewClient(dynamic.Anthropic).Complete(ctx, req, opts...)
}
if genai.IsThinkingModel(req.Model) {
    return genai.NewClient(...).Complete(ctx, req, opts...)
}
// ... 其他旧逻辑
```

## 参考资料

- GoAI 官方文档: https://goai.sh
- GoAI GitHub: https://github.com/zendev-sh/goai
- Vercel AI SDK: https://ai-sdk.dev

## 常见问题排查

### 问题：工具循环失败 - "tool call result does not follow tool call"

**原因**：消息格式不正确，Tool result 没有放在正确的 `role: "tool"` 消息里。

**解决方案**：

GoAI 要求 Tool result 必须在单独的 `RoleTool` 消息中：

```go
// 正确格式
provider.Message{
    Role: provider.RoleTool,
    Content: []provider.Part{
        {
            Type:       provider.PartToolResult,
            ToolCallID: "call_xxx",
            ToolOutput: "工具执行结果",
        },
    },
}
```

代码已实现在 `convertMessages` 中自动处理。

### 问题：工具循环失败 - 子 Agent 使用 system role

**现象**：
```
Agent 响应: 还是报错。问题确认是子 Agent 在构造消息时使用了 system role，但后端不支持。
```

**原因**：子 Agent（如 file-processor）在对话历史中插入了 `role: "system"` 消息。但 MiniMax OpenAI 端点不支持 `system` 消息在对话中间——只支持第一条消息是 system。

**解决方案**：

在消息转换时跳过 `system` role 消息：

```go
// pkg/llm/goai/client.go
func convertMessages(input []types.Message) []provider.Message {
    for _, msg := range input {
        // Skip system role messages - they should go through SystemPrompt field
        // MiniMax OpenAI endpoint doesn't support system messages in the middle of conversation
        if msg.Role == "system" {
            continue
        }
        // ... 处理其他消息
    }
}
```

**正确做法**：System prompt 应该通过 `CompletionRequest.SystemPrompt` 字段传递，GoAI 会把它作为第一条消息发送，这是 OpenAI API 允许的格式。

### 问题：MiniMax Anthropic 返回 "Request not allowed"

**现象**：
```
LLM error: stream error: Request not allowed
```

**原因**：配置路由逻辑错误，`pkg/llm/client.go` 中无论 `apiType` 是什么都使用 OpenAI 的配置，导致 Anthropic 类型的 `baseURL` 为空。

**解决方案**：

根据 `apiType` 选择正确的配置：

```go
// pkg/llm/client.go
var apiKey, baseURL string
if c.cfg.APIType == "anthropic" {
    apiKey = dynamic.Anthropic.APIKey
    baseURL = dynamic.Anthropic.BaseURL
} else {
    apiKey = dynamic.Responses.APIKey
    baseURL = dynamic.Responses.BaseURL
}

return goai.NewClient(req.Model, apiKey, baseURL, c.cfg.APIType).Complete(ctx, req, opts...)
```

### 问题：MiniMax Anthropic 返回 404 Not Found

**现象**：
```
status=404 message="404 Not Found"
```

**原因**：GoAI anthropic provider 会追加 `/v1/messages`，而配置的 URL 已经包含 `/v1`，导致路径重复：
```
配置: https://api.minimaxi.com/anthropic/v1
GoAI 追加: /v1/messages
最终: https://api.minimaxi.com/anthropic/v1/v1/messages  ← 404
```

**解决方案**：

在创建模型时 strip `/v1` 后缀：

```go
// pkg/llm/goai/client.go
baseURL := c.baseURL
if strings.HasSuffix(baseURL, "/v1") {
    baseURL = strings.TrimSuffix(baseURL, "/v1")
}
```

此修复已应用于所有 Anthropic 兼容端点，确保向后兼容。

---

## 更新日志

### 2026-04-01

#### 修复：MiniMax Anthropic 端点不可用

**问题 1**：配置路由错误
- 现象：`Request not allowed`
- 原因：`pkg/llm/client.go` 无视 `apiType`，总是用 OpenAI 配置
- 修复：根据 `apiType` 选择 `Anthropic` 或 `Responses` 配置

**问题 2**：URL 路径重复
- 现象：`404 Not Found`
- 原因：GoAI 追加 `/v1/messages`，配置已含 `/v1`，导致路径重复
- 修复：创建模型前 strip `/v1` 后缀

**问题 3**：认证方式错误
- 原因：MiniMax Anthropic 端点需要 `Authorization: Bearer` 而非 `x-api-key`
- 修复：MiniMax Anthropic 特殊处理，添加 Bearer header
