# Page-Agent 代理使用指南

## 🎯 功能说明

为 Page-Agent 浏览器插件提供本地代理服务，使其能够使用你配置的 LLM API。

## 📋 工作原理

```
Page-Agent 插件
    ↓ HTTP POST /proxy/v1/chat/completions
Niu API 代理
    ↓ LiteLLMSession
你的 LLM API（MiniMax/OpenAI/DeepSeek等）
```

## 🚀 使用方法

### 1. 确保 Niu API 正在运行

```bash
# 启动 API（默认端口 9876）
python -m niu_api
```

### 2. 在 Page-Agent 插件中配置

打开浏览器，找到 Page-Agent 扩展：

**配置参数：**
- **Base URL**: `http://localhost:9876/proxy/v1`
- **Model**: 随便填（比如 `default`）
- **API Key**: 留空或随意填写

**配置方法：**
1. 点击插件图标打开 Hub Tab
2. 点击右上角的"设置"图标
3. 在配置面板中填写上述参数
4. 点击保存

### 3. 测试连接

访问健康检查端点：
```bash
curl http://localhost:9876/proxy/v1/health
```

预期返回：
```json
{
  "status": "ok",
  "model": "MiniMax-M2-highspeed",
  "api_base": "https://api.minimaxi.com/..."
}
```

## 📊 API 端点

### POST /proxy/v1/chat/completions

OpenAI 兼容的聊天补全端点。

**请求格式：**
```json
{
  "model": "any",
  "messages": [
    {
      "role": "user",
      "content": "打开百度搜索 page-agent"
    }
  ],
  "temperature": 1.0,
  "tools": [...]
}
```

**响应格式：**
```json
{
  "id": "chatcmpl-xxxxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "MiniMax-M2-highspeed",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150
  }
}
```

### GET /proxy/v1/models

列出可用模型。

### GET /proxy/v1/health

健康检查。

## 🔧 技术细节

### 独立 Session

代理为每个请求创建独立的 `LiteLLMSession`，不会污染主聊天会话。

### 完整 SDK 路径

```
代理请求 → LiteLLMSession → litellm → 你的 LLM API
```

全路径通过你的 SDK 和配置，确保兼容性。

### 配置读取

从 `config/user-config.json` 读取 LLM 配置，与主聊天使用相同配置。

## ⚙️ 配置示例

`config/user-config.json`:
```json
{
  "llm": {
    "type": "openai",
    "apiKey": "your-api-key",
    "apiBase": "https://api.minimaxi.com/anthropic",
    "model": "MiniMax-M2-highspeed"
  }
}
```

## 🐛 故障排查

### 问题 1：连接被拒绝

**症状：** 插件显示 "Hub is not connected"

**解决：**
1. 确认 Niu API 正在运行：`curl http://localhost:9876/health`
2. 检查端口是否正确（默认 9876）
3. 检查防火墙设置

### 问题 2：LLM 未配置

**症状：** 返回 "LLM not configured"

**解决：**
1. 检查 `config/user-config.json` 是否存在
2. 确认 `llm.apiKey` 字段已填写
3. 重启 Niu API

### 问题 3：API 调用失败

**症状：** 返回 500 错误

**解决：**
1. 查看 Niu API 日志：`logs/api_stderr.log`
2. 确认 LLM API Base 和 Key 正确
3. 测试 LLM API 是否可用（通过主聊天）

## 📝 注意事项

1. **端口配置**：如果 Niu API 使用非默认端口，记得修改插件配置中的 Base URL
2. **模型名称**：代理会忽略插件中的 model 参数，使用配置文件中的模型
3. **API Key**：代理会忽略插件中的 API Key，使用配置文件中的 Key
4. **独立运行**：代理是独立模块，不影响主聊天功能

## 🔗 相关文档

- Page-Agent 官方文档：https://alibaba.github.io/page-agent/
- LiteLLM 文档：https://docs.litellm.ai/
- Niu Agent 文档：见 `CLAUDE.md`
