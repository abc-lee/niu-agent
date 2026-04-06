# 自我进化系统使用指南

## 概述

Niu Agent 现已支持**自我进化能力**，能够自动保存、检索和利用长期记忆，实现持续学习和改进。

## 核心功能

### 1. 自动记忆保存

系统会在以下情况自动建议保存记忆：

- **重要环境信息**：探测到 GPU、内存等硬件配置
- **用户偏好**：发现用户的使用习惯和偏好
- **成功经验**：完成复杂操作（15+ 轮）后的经验总结
- **失败教训**：遇到问题并解决后的经验记录

**触发方式**：
```
用户: [进行复杂操作]
系统: [检测到值得记忆的信息] 建议: start_long_term_update
```

### 2. 手动记忆保存

**对话中直接说**：
```
请记住：我喜欢使用深色主题，字体大小是14px
```

系统会自动：
1. 推断记忆类型（environment/preferences/skills/experiences/facts）
2. 计算重要性评分（0-1）
3. 生成 L0/L1/L2 三层记录
4. 保存到向量库

### 3. 记忆检索

**自然语言查询**：
```
用户: 我之前说过我喜欢什么主题？
系统: [检索记忆] 您喜欢深色主题
```

**自动注入**：
- 每 5 轮对话自动注入相关记忆
- 无需手动查询

### 4. 记忆统计

**查询统计**：
```
用户: 帮我查看保存了多少条记忆
系统: [调用 memory-server/get_memory_stats]
```

## L0/L1/L2 三层存储

### 架构说明

```
┌─────────────────────────────────────────┐
│  L0: 极简索引（≤50字符）               │
│  - 快速判断是否相关                     │
│  - 提取第一句话                         │
│  - l1_pointer → L1                      │
└─────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────┐
│  L1: 结构化摘要                        │
│  - 格式: {title}|{keywords}|{summary}   │
│  - 提取关键信息                         │
│  - l2_pointer → L2                      │
└─────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────┐
│  L2: 完整内容                          │
│  - 原始记忆内容                         │
│  - 包含所有细节                         │
└─────────────────────────────────────────┘
```

### 查询流程

1. **快速判断（L0）**：搜索 L0，判断是否有相关知识
2. **语义匹配（L1）**：搜索 L1，获取摘要列表
3. **深度阅读（L2）**：根据 L1 的 l2_pointer 读取完整内容

## 记忆类型

| 类型 | 说明 | 默认重要性 | 示例 |
|------|------|-----------|------|
| environment | 环境信息 | 0.9 | GPU: RTX 4090, CUDA 可用 |
| preferences | 用户偏好 | 0.85 | 喜欢深色主题，字体14px |
| skills | 学到的技能 | 0.8 | 学会使用 Python 批量处理文件 |
| experiences | 经验教训 | 0.7 | 内存泄漏问题需注意及时关闭连接 |
| facts | 事实信息 | 0.75 | 项目使用 Python 3.11 |

## MCP 工具

### 可用工具

```python
# 1. 保存记忆
memory-server/remember
参数: {
    "content": "记忆内容",
    "memory_type": "preferences",  # 可选
    "title": "标题",               # 可选
    "importance": 0.8              # 可选
}

# 2. 检索记忆
memory-server/recall
参数: {
    "query": "搜索查询",
    "limit": 5,                    # 可选
    "memory_type": "preferences",  # 可选
    "level": "l1"                  # 可选: l0/l1/l2
}

# 3. 更新记忆
memory-server/update_memory
参数: {
    "memory_id": "记忆ID",
    "content": "新内容"
}

# 4. 获取统计
memory-server/get_memory_stats

# 5. 清理过期
memory-server/cleanup_memories
参数: {
    "days": 30  # 清理多少天前的记忆
}

# 6. 关联记忆
memory-server/link_memories
参数: {
    "memory_id_1": "ID1",
    "memory_id_2": "ID2",
    "relation": "关系描述"
}
```

## 测试方法

### 基础测试

```bash
# 1. 测试向量库 L0/L1/L2 功能
python scripts/test_l0l1l2.py

# 2. 测试 Memory Server
python scripts/test_memory_server.py

# 3. 测试 Agent 层
python scripts/test_agent_evolution.py
```

### 对话测试

**测试 1：保存记忆**
```
用户: 请记住：我的开发环境使用 Windows 11，GPU 是 RTX 4090
```

**测试 2：检索记忆**
```
用户: 我之前说过我的开发环境是什么？
```

**测试 3：自动建议**
```
用户: [进行 15+ 轮复杂操作]
观察: 是否出现 "[SYSTEM TIP] 检测到值得长期记忆的信息"
```

## 工作原理

### 自我进化闭环

```
┌──────────────────────────────────────────────┐
│  1. 任务执行                                 │
│     ↓                                        │
│  2. 工作记忆更新 (tool_after_callback)       │
│     ↓                                        │
│  3. 智能判断 (_should_remember)              │
│     ↓                                        │
│  4. 长期记忆提炼 (start_long_term_update)    │
│     ↓                                        │
│  5. L0/L1/L2 存储                            │
│     ↓                                        │
│  6. 下次对话自动注入 (next_prompt_patcher)   │
│     ↓                                        │
│  7. 改进执行 → 循环                          │
└──────────────────────────────────────────────┘
```

### 关键机制

**1. 工作记忆增强**
```python
# agent/handler.py
def tool_after_callback(self, tool_name, args, response, ret):
    # 记录摘要
    # 判断是否值得记忆
    if self._should_remember(tool_name, args, ret):
        self.working["suggest_remember"] = True
        self.working["remember_reason"] = "成功执行了复杂代码"
```

**2. 记忆注入**
```python
# agent/handler.py
def next_prompt_patcher(self, next_prompt, outcome, turn):
    # 每 5 轮注入相关记忆
    if turn % 5 == 0:
        memories = self._recall_relevant_memories(next_prompt)
        next_prompt += f"\n\n### [相关长期记忆]\n{memories}"
```

**3. L0/L1/L2 生成**
```python
# memory-server/storage.py
def store_memory(self, content, memory_type):
    # L2: 完整内容
    l2_content = content

    # L1: 提取摘要
    l1_content = self._generate_l1_summary(content, memory_type)
    # 格式: {title}|{keywords}|{summary}|{entities}|{type}

    # L0: 极简索引
    l0_content = self._generate_l0_index(content, memory_type)
    # 提取第一句话
```

## 性能优化

### 数据库索引

自动创建以下索引：
- `idx_level` - 层级过滤
- `idx_category` - 分类过滤
- `idx_memory_type` - 记忆类型过滤
- `idx_server` - MCP 服务器过滤

### 缓存机制

- 模型单例加载（避免重复加载）
- 向量缓存（避免重复计算）
- 连接池（减少数据库连接开销）

## 故障排查

### 记忆无法保存

**检查**：
```bash
# 1. 检查向量库
python -c "
from agent.vector_search import get_vector_search
vs = get_vector_search()
stats = vs.search('测试', limit=1)
print(f'向量库记录数: {len(stats)}')
"

# 2. 检查 memory-server
python scripts/test_memory_server.py
```

### 记忆无法检索

**检查**：
```bash
# 查看日志
tail -f logs/api_stderr.log | grep "记忆|MEMORY"
```

### 性能问题

**优化建议**：
1. 定期清理过期记忆：`memory-server/cleanup_memories`
2. 检查数据库大小：`memory-server/get_memory_stats`
3. 重建索引：删除 `vectors.db` 后重新初始化

## 设计文档

详细设计文档：
- `docs/design-self-evolution-system.md` - 完整设计规范
- `docs/analysis-genericagent-evolution.md` - GenericAgent 分析

## 更新日志

### v2.0.0 (2026-04-06)

**新增功能**：
- ✅ L0/L1/L2 三层存储架构
- ✅ 6 个记忆管理 MCP 工具
- ✅ 自动记忆建议机制
- ✅ 记忆自动注入
- ✅ 自我进化闭环

**修复问题**：
- ✅ ONNX Runtime stdout 污染 MCP stdio
- ✅ 动态注入 MCP 工具名称显示错误
- ✅ 工具描述误导触发

**性能优化**：
- ✅ 数据库索引自动创建
- ✅ 向量缓存机制
- ✅ 模型单例加载

---

**下一步计划**：
- [ ] LLM 驱动的摘要生成（替代关键词提取）
- [ ] 记忆重要性动态调整
- [ ] 记忆关联图可视化
- [ ] 主动学习机制
