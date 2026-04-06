# Niu 记忆系统设计

## 概述

Niu 需要一个智能记忆系统，帮助 Agent 记住用户偏好、工作目录、重要事件等信息。利用向量数据库实现语义记忆，支持自动注入和智能检索。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    Memory System                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  短期记忆 (Session)                                       │
│  ├── 当前对话上下文                                       │
│  └── 会话级变量                                          │
│                                                          │
│  长期记忆 (Vector Store)                                  │
│  ├── 核心配置 (工作目录、初始化状态)                       │
│  ├── 身份设定 (名字、性别、性格)                          │
│  ├── 用户偏好 (回答风格、语言、习惯)                       │
│  ├── 重要事件 (用户告诉的重要信息)                         │
│  ├── 文件处理历史 (解析过的文件上下文)                     │
│  └── 学习到的模式 (用户习惯、常用操作)                     │
│                                                          │
│  自动注入机制                                             │
│  ├── 启动时：加载核心记忆到 System Prompt                  │
│  └── 对话时：语义检索相关记忆注入上下文                    │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## 记忆类型

### 1. 核心记忆 (Core Memory)

存储在 `~/.niu/memory.json`，启动时必读：

```json
{
  "version": 1,
  "identity": {
    "name": "妞妞",
    "gender": "female",
    "personality": ["温暖", "专业", "简洁", "主动"],
    "greetingStyle": "友好问候，简洁明了"
  },
  "workspace": {
    "path": "D:\\我的知识库",
    "createdAt": "2026-03-24T20:00:00Z"
  },
  "user": {
    "name": "用户昵称",
    "preferences": ["简洁回答", "使用中文"],
    "communicationStyle": "直接高效"
  },
  "firstRun": false,
  "lastActiveAt": "2026-03-24T20:00:00Z"
}
```

### 2. 身份设定 (Identity)

用户可以随时修改的身份属性：

| 属性 | 默认值 | 说明 |
|------|--------|------|
| name | 妞妞 | 助手名称，用户可以改名 |
| gender | female | 性别，影响称呼和语气 |
| personality | ["温暖", "专业", "简洁"] | 性格特质数组 |
| greetingStyle | 友好问候 | 问候风格描述 |
| avatar | default | 头像/精灵样式 |

**修改方式**：
```
用户: "我想给你改个名字叫小美"
助手: "好的！从现在起我就是小美啦！"
      → 调用 update_identity(name="小美")
      
用户: "你说话太啰嗦了，简洁一点"
助手: "明白了！我会更简洁直接。"
      → 调用 remember("用户偏好简洁回答", type="preference")
      → 更新 personality 增加"简洁"
```

### 3. 语义记忆 (Semantic Memory)

存储在向量数据库，支持语义检索：

| 类型 | 说明 | 示例 |
|------|------|------|
| identity | 身份设定变更 | "2026-03-24 用户给我改名为小美" |
| preference | 用户偏好 | "用户喜欢简洁的回答" |
| event | 重要事件 | "2026-03-20 用户提到了项目X很重要" |
| context | 文件上下文 | "合同.pdf 是张三的购房合同" |
| pattern | 行为模式 | "用户每周一会整理上周文档" |

---

## L0/L1/L2 三层记忆架构

> **L1 详细规范见**：[`docs/spec-L1-summary.md`](./spec-L1-summary.md)

### 概念

| 层级 | Token 限制 | 来源 | 说明 |
|------|-----------|------|------|
| L0 | < 100 | 对话核心摘要 | Agent 精简后的一句话核心信息 |
| L1 | 50-80字 | 从 L2 生成 | 极简格式摘要，用于向量检索 |
| L2 | 无限制 | 执行结果 | 完整内容（工具输出、长篇粘贴） |

### L1 格式规范

L1 采用极简格式，详见 [`docs/spec-L1-summary.md`](./spec-L1-summary.md)：

```
{标题}|{关键词}|{摘要}|{实体}|{类型}|{指针}
```

**示例**：
```
Redis分布式缓存设计|缓存,Redis,架构|基于Redis的分布式缓存系统实现方案|Redis,缓存|技术文档|/docs/cache.md
```

**Token 效率**：相比 JSON 格式节省 60% token。

### 存储结构

所有层级存储在向量数据库同一张表，用 `type` 字段区分：

```
documents 表
├── id: TEXT (主键)
├── content: TEXT
├── embedding: BLOB
└── metadata: JSON
    ├── type: "l0" | "l1" | "l2"
    ├── l1_id: TEXT (L0 指向 L1)
    ├── l2_id: TEXT (L1 指向 L2)
    └── ... 其他元数据
```

### 指针关系

```
L0 (< 100 tokens)
 │
 └─► l1_id ──► L1 (极简格式摘要)
                  │
                  └─► l2_id ──► L2 (完整内容)
```

### 生成流程

```
用户命令执行
    │
    ├─► 简短输出 (< 100 tokens) → 直接存为 L0
    │
    └─► 长输出 (>= 100 tokens) → 存为 L2
                                      │
                                      ▼
                              闲置整理时
                              子Agent 读取 L2
                              生成 L1 摘要（极简格式）
                              存入向量库（带 l2_id 指针）
```

### 检索流程

```
用户发送消息
    │
    ▼
向量相似度搜索 (L1 的 embedding)
    │
    ▼
稀疏关键词匹配 (L1 的关键词/实体)
    │
    ▼
返回 L1 记录（极简格式）
    │
    └─► 如需完整内容，用指针查询 L2
```

---

## MCP Tools

### memory-server (新建)

```python
# ========== 身份管理 ==========
get_identity() -> Identity
  # 获取当前身份设定

update_identity(name: str = None, gender: str = None, 
                personality: list = None, greeting_style: str = None) -> Identity
  # 更新身份设定

# ========== 核心记忆 ==========
get_core_memory() -> CoreMemory
  # 读取核心配置

update_core_memory(updates: dict) -> CoreMemory
  # 更新核心配置

# ========== 语义记忆 ==========
remember(content: str, type: str, metadata: dict = None) -> str
  # 存储一条记忆到向量库
  # type: identity | preference | event | context | pattern

recall(query: str, limit: int = 5, memory_type: str = None) -> list[Memory]
  # 语义搜索相关记忆

# ========== 初始化 ==========
is_first_run() -> bool
  # 判断是否需要引导用户设置

complete_setup(workspace_path: str, user_name: str = None,
               assistant_name: str = "妞妞") -> None
  # 完成初始化设置
```

## 自动注入机制

### 启动时注入

Agent 启动时，自动将核心记忆注入到 System Prompt：

```go
func buildSystemPrompt(baseInstructions string, coreMemory CoreMemory, relatedMemories []Memory) string {
    var sb strings.Builder
    
    // 基础指令
    sb.WriteString(baseInstructions)
    sb.WriteString("\n\n")
    
    // 身份设定
    sb.WriteString("# 我的身份\n\n")
    sb.WriteString(fmt.Sprintf("我的名字是 %s。\n", coreMemory.Identity.Name))
    sb.WriteString(fmt.Sprintf("我的性格是：%s。\n", strings.Join(coreMemory.Identity.Personality, "、")))
    sb.WriteString(fmt.Sprintf("问候风格：%s\n\n", coreMemory.Identity.GreetingStyle))
    
    // 工作环境
    sb.WriteString("# 我的工作环境\n\n")
    sb.WriteString(fmt.Sprintf("工作目录：%s\n\n", coreMemory.Workspace.Path))
    
    // 用户信息
    if coreMemory.User.Name != "" {
        sb.WriteString("# 用户信息\n\n")
        sb.WriteString(fmt.Sprintf("用户称呼：%s\n", coreMemory.User.Name))
        if len(coreMemory.User.Preferences) > 0 {
            sb.WriteString(fmt.Sprintf("用户偏好：%s\n\n", strings.Join(coreMemory.User.Preferences, "、")))
        }
    }
    
    // 相关记忆
    if len(relatedMemories) > 0 {
        sb.WriteString("# 相关记忆\n\n")
        for _, m := range relatedMemories {
            sb.WriteString(fmt.Sprintf("- %s\n", m.Content))
        }
    }
    
    return sb.String()
}
```

### 对话时注入

每次用户发送消息时，自动检索相关记忆：

```go
func (a *Agent) Run(ctx context.Context, req Request) (*Response, error) {
    // 检索与当前消息相关的记忆
    query := req.Messages[len(req.Messages)-1].Content
    memories := recallMemories(query, limit=5)
    
    // 如果有相关记忆，注入到上下文
    if len(memories) > 0 {
        memoryContext := formatMemories(memories)
        // 作为系统提示注入
    }
    
    // 继续正常流程
    // ...
}
```

## 初始化流程

```
┌──────────────┐
│  用户启动 Niu │
└──────┬───────┘
       │
       ▼
┌──────────────────┐     是首次运行
│ is_first_run()?  │────────────────┐
└──────┬───────────┘                │
       │ 否                          │
       ▼                             ▼
┌──────────────────┐      ┌──────────────────────┐
│ 加载核心记忆      │      │ Agent: "你好！我是妞妞│
│ 构建身份设定      │      │ 请给我一个工作目录..."│
│ 注入到 System    │      └──────────┬───────────┘
│ Prompt          │                 │
└──────┬──────────┘                 ▼
       │                  ┌──────────────────────┐
       │                  │ 用户: "放D:\\知识库"  │
       │                  │ complete_setup()     │
       │                  └──────────┬───────────┘
       │                             │
       │                             ▼
       │                  ┌──────────────────────┐
       │                  │ Agent: "好的！还需要  │
       │                  │ 给我起个名字吗？"     │
       │                  └──────────┬───────────┘
       │                             │
       ▼                             ▼
┌──────────────────────────────────────────────┐
│              正常运行                         │
│  用户随时可以修改身份设定                      │
└──────────────────────────────────────────────┘
```

## 身份修改场景

```
用户: "我想给你改名叫小美"
→ update_identity(name="小美")
→ remember("用户给我改名为小美", type="identity")
→ Agent: "好的！从现在起我就是小美！"

用户: "你性格能不能活泼一点？"
→ update_identity(personality=["温暖", "活泼", "专业"])
→ Agent: "没问题！我会更活泼一些！😄"

用户: "我是张三"
→ update_core_memory(user.name="张三")
→ Agent: "好的张三，我会这样称呼你！"
```

## 实现计划

1. **Phase 1**: 核心 memory.json + 身份设定 + 首次运行引导
2. **Phase 2**: memory-server MCP 实现
3. **Phase 3**: 语义记忆 + 自动注入
4. **Phase 4**: 学习用户模式

## 文件位置

| 文件 | 位置 | 说明 |
|------|------|------|
| memory.json | ~/.niu/memory.json | 核心记忆 + 身份设定 |
| 向量数据库 | ~/.niu/vectors.db | 语义记忆 |
| 日志 | ~/.niu/logs/ | 操作日志 |
