# 技术笔记：Agent 通讯机制与 Session 隔离

> 本文档记录 MiniMax M2.7 子Agent集成的关键设计决策，供后续开发参考。

## 一、核心理解

### Agent 系统的关键是通讯闭环

```
主Agent ──指令──> 子Agent
    ↑               │
    └──结果─────────┘
```

**指令和结果必须完整传递**，LLM 自己会处理格式、跨平台等细节问题。

**验证层过严反而阻碍通讯**——让 LLM 自己处理跨平台和格式问题。

### L0/L1/L2 思想应用于 Agent 通讯

> **L1 详细规范见**：[`docs/spec-L1-summary.md`](./spec-L1-summary.md)

| 层级 | 内容 | 用途 |
|------|------|------|
| L0 (~100 tokens) | 摘要 | 子Agent返回给主Agent |
| L1 (极简格式) | 概览 | 按需查询，向量检索为主 |
| L2 (无限) | 完整内容 | 按需加载，留在子Agent session |

**L1 格式**（节省 60% token）：
```
{标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
```

**设计原则**：
- 子Agent只返回L0摘要给主Agent
- 细节留在子Agent独立session
- 主Agent需要细节时，通过session_id查询

---

## 二、Session 隔离机制

### 问题

```
主Agent委托子Agent
    ↓
子Agent的所有操作写入主Agent session
    ↓
主Agent session膨胀
    ↓
白压缩了！
```

### 解决方案

```go
// pkg/tools/service.go:791-795
if _, ok := config.Agents[server]; ok {
    return s.sampleCall(ctx, server, args, SampleCallOptions{
        ProgressToken: opt.ProgressToken,
        NewThread:     true,  // 子Agent独立session
    })
}
```

**NewThread=true 的作用**：
- 子Agent在独立session运行
- 子Agent的操作不污染主Agent session
- 子Agent完成后返回摘要给主Agent

### 正确的设计

```
主Agent委托子Agent
    ↓
子Agent有独立session
    ↓
子Agent完成后返回摘要（L0）
    ↓
主Agent只记录摘要，不记录细节
    ↓
需要细节时，通过session_id查询（L1/L2）
```

**类比**：
- 我让你去买菜
- 你回来告诉我"买好了，花了50块"（摘要）
- 我不需要知道你去了哪个超市、挑了哪些菜（细节）

---

## 三、已修复的问题

### 3.1 子Agent调用返回undefined

**问题**：主Agent调用子Agent后，收到的tool_result是`undefined`

**根因**：
1. `InternalMessages` 中有ToolCallResult，但没有被提取
2. `ConsolidateTools` 没有正确合并ToolCall和ToolCallResult

**修复**：

```go
// pkg/agents/run.go:496-560
// 构建InternalMessages时，合并ToolCall和ToolCallResult
for i := range resp.InternalMessages {
    for j := range resp.InternalMessages[i].Content {
        if resp.InternalMessages[i].Content[j].Type == "tool_call" {
            // 查找对应的tool_result并合并
        }
    }
}
```

```go
// pkg/types/messages.go:3-50
// ConsolidateTools 合并逻辑
func ConsolidateTools(items []ContentItem) []ContentItem {
    // 合并相邻的 ToolCall + ToolCallResult
}
```

### 3.2 子Agent失败主Agent看不到

**问题**：子Agent执行失败时，主Agent收到的结果是`undefined`，无法知道失败原因

**修复**：

```go
// pkg/agents/toolcall.go:141-153
// 子Agent失败时，不设置Done=true
// 让主Agent能看到错误并决定是否重试
if err != nil {
    // 不设置 Done = true
    // 返回错误信息给主Agent
    return nil, err
}
```

### 3.3 附件URL格式验证过严

**问题**：文件路径格式验证过严，导致部分合法路径被拒绝

**修复**：

```go
// pkg/tools/service.go:1096-1170
// 支持多种格式：
// - file:///path/to/file
// - file://path/to/file
// - data:base64...
// - 纯路径（Windows/Unix）
```

**设计原则**：验证层不应过度限制，让LLM自己处理跨平台和格式问题

### 3.4 提示词优化

**之前**：7步详细流程，Agent机械执行

**之后**：两条核心规则 + 案例

```markdown
## 核心规则

1. 不要再调用其他Agent
2. 查看 ~/.niu/memory.json 了解用户对文件存储的偏好

## 文件结构

documents/{当前年份}/{文档类型}/

## L0/L1/L2 模式

- L0 (~100 tokens): 摘要，存入向量数据库
- L1 (~2k tokens): 概览，包含实体、关键词
- L2: 原文，按需加载
```

---

## 四、性能改善

| 指标 | 之前 | 之后 | 改善 |
|------|------|------|------|
| 处理时间 | 62秒 | 38秒 | -39% |
| 请求次数 | 12次 | 6次 | -50% |
| 实体数 | 9个 | 5个 | -44% |

---

## 五、修改的文件清单

### 核心代码修改

| 文件 | 行号 | 修改内容 |
|------|------|----------|
| `pkg/tools/service.go` | 785-795 | Agent调用路由，NewThread选项 |
| `pkg/tools/service.go` | 1096-1170 | 附件URL格式支持 |
| `pkg/tools/service.go` | 1199-1207 | SampleCallOptions添加NewThread |
| `pkg/agents/toolcall.go` | 141-153 | 子Agent错误处理，失败时不设置Done=true |
| `pkg/agents/run.go` | 496-560 | InternalMessages构建，ToolCallResult合并 |
| `pkg/types/messages.go` | 3-50 | ConsolidateTools合并逻辑 |
| `pkg/sampling/sampler.go` | 258-305 | 结果提取，检查合并的ToolCall+ToolCallResult |

### 提示词文件

| 文件 | 修改内容 |
|------|----------|
| `config/agents/file-processor.md` | 简化为两条核心规则 + L0/L1/L2模式 + 案例 |

### 设计文档

| 文件 | 内容 |
|------|------|
| `docs/feature-document-processing.md` | L0/L1/L2三级存储设计 |
| `docs/feature-file-management.md` | 文件管理设计 |

---

## 六、设计原则总结

1. **通讯闭环**：指令和结果必须完整传递
2. **验证层宽松**：让LLM自己处理格式和跨平台问题
3. **Session隔离**：子Agent独立session，只返回摘要
4. **提示词简洁**：核心规则 + 案例，避免过度约束
5. **L0/L1/L2模式**：摘要给主Agent，细节留在子Agent

---

## 七、相关归档

可通过 `mnemo-recall` 查看详细记录：

- `b101`: 子Agent性能优化
- `b102`: 子Agent验证与设计
- `b103`: Session隔离实现

---

## 八、验证成功的日志分析

### 最后一次测试 (2026-03-26 19:45)

**测试文件**: README.md (5.1 KB)

**执行流程**:

```
主Agent (niu)
    │  messages=1
    │  调用 chat-with-file-processor
    ↓
子Agent (file-processor) [独立session]
    │  messages=3 → 6 → 8 → 15 → 22
    │  read + mkdir (并行)
    │  copy_to_path
    │  create_document + create_entity x5 (并行)
    │  link_document_entity x5 + add_document (并行)
    │  返回 text 类型摘要
    ↓
主Agent (niu)
    │  messages=4 (原始输入 + tool_use + tool_result + 用户问题)
    │  回答"刚才子Agent给你干的活儿怎么样"
```

**关键日志**:

```
# 子Agent完成，返回摘要
sampler: output item index=0 type=content text="## ✅ 文件处理完成..."

# 主Agent收到结果
sampler: output item index=0 type=tool_call name=chat-with-file-processor

# 主Agent回答用户问题
Agent response: "子Agent 完成了基本任务，整体表现 **及格** ⭐⭐⭐"
```

### 验证结论

| 功能 | 状态 | 说明 |
|------|------|------|
| Session隔离 | ✅ | 主Agent messages=4，子Agent操作在独立session |
| 通讯闭环 | ✅ | 子Agent结果正确返回给主Agent |
| 摘要返回 | ✅ | 子Agent返回text类型摘要，不是undefined |
| 主Agent知情 | ✅ | 主Agent能回答"子Agent干了什么" |

### 性能数据

- **总时间**: ~38秒 (19:45:02 → 19:45:40)
- **子Agent请求**: 6次
- **工具调用**:
  - read + mkdir (并行)
  - copy_to_path
  - create_document + create_entity x5 (并行)
  - link_document_entity x5 + add_document (并行)

---

---

## 九、不同模型测试对比 (2026-03-26)

### 测试场景

用户拖入 README.md 文件，子Agent负责复制、解析、入库。

### 测试结果

| 模型 | 思考链 | 速度 | 通讯闭环 | 目录结构 | 版本管理 | 意外行为 |
|------|--------|------|----------|----------|----------|----------|
| MiniMax 2.7 | ✅ | 慢 | ✅ | 2026/其他/ | ❌ 覆盖 | 第三次跳过子Agent |
| GLM-5 | ✅ | 中 | ✅ | documents/ | ❌ 覆盖 | 没建年份目录 |
| 豆包 | ❌ | 快 | ✅ | 2026/文档/ | ❌ 删除旧文件 | 主动删除之前文件 |

### 结论

1. **通讯闭环已修复** - 所有模型都能正确返回结果给主Agent
2. **思考链对通讯无影响** - 豆包无思考链也能正常通讯
3. **业务逻辑不可靠** - 不同模型对规则理解不同，行为不可预测
4. **验证判断** - 复杂文件操作不适合让LLM做，需要独立程序

### 速度对比

- 豆包（无思考链）> GLM-5 > MiniMax 2.7

### 建议

对于文件归档等复杂业务逻辑，建议使用**独立归档程序**而非子Agent+MCP工具链。

---

*最后更新: 2026-03-26*
