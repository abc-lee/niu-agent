# LLM 交互流程分析报告

**日志文件**: `E:\tools\ai-bot\logs\llm_interaction_20260407.log`
**分析日期**: 2026-04-07
**日志时间**: 13:18:44 - 13:22:23
**总行数**: 1287 行

---

## 一、交互流程概览

### 1.1 会话统计

```
总用户消息数: 18 条
总助手响应: 18 次
总 LLM 调用: 4 次
工具调用次数: 1 次 (chat-with-file-processor)
```

### 1.2 会话时间线

```
[13:18:44] 第1次 LLM 调用 - 用户问好
[13:18:44] 第2次 LLM 调用 - 用户询问功能
[13:18:44] 第3次 LLM 调用 - 用户说早上好
[13:18:44] 第4次 LLM 调用 - 用户说测试
[13:22:06] ❌ API 错误 - 用户再次询问
[13:22:10] 第5次 LLM 调用 - 用户拖入照片
[13:22:10] 第6次 LLM 调用 - 子 Agent 处理照片
[13:22:23] 第7次 LLM 调用 - 主 Agent 汇报结果
```

---

## 二、问题分析

### ❌ 问题 1: API 调用失败（严重）

**位置**: 第 231 行
**时间**: 13:22:06 (未记录，推断)
**错误类型**: HTTP 400 - InvalidParameter

**错误信息**:
```json
{
  "error": {
    "code": "InvalidParameter",
    "message": "The parameter `messages` specified in the request are not valid:
                expected a list of object, but got `\"# Role: 妞妞...\"`"
  }
}
```

**根本原因**:
- `LLMSession.raw_ask()` 接收字符串 prompt
- 直接传递给 `_openai_stream()`
- `_openai_stream()` 将字符串作为 `payload["messages"]` 发送
- API 期望 `messages` 是对象列表，而非字符串

**已修复**: ✅
- 提交: `fd37a2d` - fix: 修复 LLMSession.raw_ask 类型不匹配
- 在 `LLMSession.raw_ask()` 中添加类型判断
- 字符串 prompt 自动包装为 `messages` 列表

---

### ⚠️ 问题 2: 重复的系统提示（冗余）

**现象**: 每次对话都发送完整的 35K+ 字符 system prompt

**分析**:
```
第1次调用: 32481 字符
第2次调用: 32481 字符
第3次调用: 32481 字符
...
第5次调用: 35719 字符 (增加了动态注入的 3238 字符)
第6次调用: 17260 字符 (子 Agent)
第7次调用: 36774 字符
```

**问题点**:

1. **System prompt 过长** (32K+ 字符):
   - 核心系统提示: ~5000 字符
   - 工具 Schema JSON: ~20000 字符 (75 个工具)
   - 动态注入内容: ~3000-6000 字符

2. **重复发送**:
   - 每次对话都发送完整的工具 Schema
   - 每次都发送完整的身份设定和行为准则
   - 动态注入的内容重复计算

**影响**:
- Token 消耗大（每次 ~30K tokens 仅 system prompt）
- 成本增加
- 响应速度变慢

**建议优化**:
1. 使用 Claude 的缓存机制 (`cache_control`)
2. 工具 Schema 只发送一次（后续用"同上"引用）
3. 动态注入内容按需加载

---

### ⚠️ 问题 3: 动态注入效率低

**现象**: 每次都进行向量搜索，即使没有相关内容

**日志证据**:
```
[Debug] Dynamic injection - Skills: 0 results
[Debug] Dynamic injection - MCP tools: 0 results
[Debug] Dynamic injection - Knowledge: 0 results
```

**分析**:
- 用户简单问候也触发动态注入
- 搜索结果为空时仍占用时间
- 没有缓存机制

**建议**:
1. 添加意图分类，简单对话跳过动态注入
2. 缓存常见查询的动态注入结果
3. 设置阈值，结果太少时跳过

---

### ✅ 优点 1: 工具调用正确

**示例**: 照片入库流程

```json
{
  "name": "chat-with-file-processor",
  "arguments": {
    "task": "处理照片入库：E:/tmp/2009.6.4西柏坡/DSC_3348.jpg，处理方式为复制..."
  }
}
```

**正确性**:
- ✅ 使用了子 Agent 委托
- ✅ 参数格式正确
- ✅ 符合提示词要求

---

### ✅ 优点 2: 子 Agent 提示词清晰

**文件处理器提示词**:
- ✅ 明确限制只使用 photo-server 工具
- ✅ 详细的文档入库两步流程说明
- ✅ L1 摘要格式示例
- ✅ 批量处理建议

---

### ✅ 优点 3: 会话历史管理正确

**证据**:
- 第6次调用包含了之前的工具返回结果
- `<tool_result>` 正确传递
- 工作记忆机制正常

---

## 三、Token 使用分析

### 3.1 Token 消耗统计

| 调用次数 | Prompt 长度 | 估算 Tokens | 主要内容 |
|---------|------------|-------------|---------|
| 1-4 | ~32K 字符 | ~25K | 系统 prompt + 工具 Schema |
| 5 | 35.7K 字符 | ~28K | 系统 prompt + 动态注入 |
| 6 | 17.3K 字符 | ~13K | 子 Agent prompt |
| 7 | 36.8K 字符 | ~29K | 系统 prompt + 工具结果 |

**总消耗**: ~150K tokens (仅输入)

### 3.2 成本估算

**假设使用 GPT-4o**:
- 输入: $5/1M tokens
- 输出: $15/1M tokens

**单次会话成本**:
- 输入: 150K × $5/1M = $0.75
- 输出: ~20K × $15/1M = $0.30
- **总计**: ~$1.05 / 会话

**优化后预期**:
- 缓存 system prompt: 减少 70% 输入 token
- 成本降至: ~$0.35 / 会话

---

## 四、准确性评估

### 4.1 ✅ 准确的部分

1. **工具调用逻辑**:
   - 正确使用子 Agent
   - 参数格式符合 Schema
   - 遵守提示词约束

2. **会话管理**:
   - 历史消息正确传递
   - 工具结果正确处理
   - 工作记忆机制正常

3. **子 Agent 交互**:
   - 主 Agent → 子 Agent 委托正确
   - 子 Agent → 主 Agent 返回格式正确
   - 结果展示清晰

### 4.2 ❌ 不准确的部分

1. **API 调用失败**:
   - `LLMSession.raw_ask` 类型不匹配
   - 已修复 ✅

2. **Token 浪费**:
   - System prompt 重复发送
   - 工具 Schema 重复发送
   - 需要优化 ⚠️

---

## 五、完整性评估

### 5.1 ✅ 完整的部分

1. **照片入库流程**:
   - ✅ 文件路径解析
   - ✅ 人脸识别执行
   - ✅ EXIF 信息提取
   - ✅ 知识图谱存储
   - ✅ 向量化存储
   - ✅ 结果反馈

2. **错误处理**:
   - ✅ API 错误被捕获
   - ✅ 用户收到错误提示
   - ✅ 不影响后续使用

### 5.2 ⚠️ 不完整的部分

1. **动态注入逻辑**:
   - ⚠️ 没有意图分类
   - ⚠️ 简单对话也触发注入
   - ⚠️ 结果为空时仍占用资源

2. **性能监控**:
   - ⚠️ 缺少 Token 统计日志
   - ⚠️ 缺少响应时间记录
   - ⚠️ 缺少成本追踪

---

## 六、冗余性评估

### 6.1 ⚠️ 存在冗余

1. **System Prompt 冗余** (严重):
   ```
   每次调用重复发送：
   - 完整身份设定 (~2000 字符)
   - 核心能力说明 (~500 字符)
   - 行为准则 (~1000 字符)
   - 错误处理规则 (~300 字符)
   - 安全原则 (~200 字符)
   ```

   **累计冗余**: ~4000 字符 × 7 次 = 28000 字符

2. **工具 Schema 冗余** (严重):
   ```
   每次调用发送 75 个工具的完整 Schema
   ```

   **累计冗余**: ~20000 字符 × 7 次 = 140000 字符

3. **动态注入冗余** (中等):
   ```
   简单问候也触发向量搜索
   ```

   **影响**: 每次搜索耗时 0.5-1s，结果为空时浪费资源

### 6.2 ✅ 无冗余的部分

1. **用户消息**:
   - 每次消息都是必要的
   - 没有重复内容

2. **工具调用**:
   - 只有1次工具调用（照片入库）
   - 调用是必要的

---

## 七、优化建议

### 7.1 高优先级优化

#### 1. 实现 Prompt 缓存（减少 70% token）

**方法**: 使用 Claude 的 `cache_control` 特性

**实现**:
```python
# agent/generic/llmcore.py
def make_messages(self, raw_list):
    msgs = [{"role": m["role"], "content": list(m["content"])} for m in raw_list]
    # 最后一个消息添加缓存控制
    msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return msgs
```

**预期效果**:
- System prompt 只发送一次
- 后续调用命中缓存
- Token 消耗减少 70%

#### 2. 优化工具 Schema 传递

**方法**: 工具 Schema 只在首次发送

**实现**:
```python
# agent/handler.py
if not hasattr(self, '_tools_schema_sent'):
    tools_schema = self.base_tools_schema.copy()
    self._tools_schema_sent = True
else:
    # 后续只发送新增工具
    tools_schema = []
```

**预期效果**:
- 减少 20000 字符/次
- 节省 30% token

#### 3. 添加意图分类

**方法**: 简单对话跳过动态注入

**实现**:
```python
# agent/runner.py
def _should_inject(self, user_input: str) -> bool:
    # 简单问候、确认等跳过
    simple_patterns = ['你好', '早上好', '谢谢', '好的', '没事']
    if any(p in user_input for p in simple_patterns):
        return False
    return True
```

**预期效果**:
- 减少 30-50% 动态注入调用
- 提升 10-20% 响应速度

---

### 7.2 中优先级优化

#### 4. 添加 Token 统计

**方法**: 记录每次调用的 token 消耗

**实现**:
```python
# agent/generic/llmcore.py
def chat(self, messages, tools=None):
    # ... 调用 LLM ...
    usage = response.usage
    logger.info(f"Token usage: input={usage.input_tokens}, output={usage.output_tokens}")
```

**预期效果**:
- 监控成本
- 发现异常消耗

#### 5. 缓存动态注入结果

**方法**: 缓存向量搜索结果

**实现**:
```python
# agent/runner.py
from functools import lru_cache

@lru_cache(maxsize=100)
def _search_dynamic_resources(self, query: str) -> dict:
    # 缓存查询结果
    ...
```

**预期效果**:
- 减少向量搜索调用
- 提升响应速度

---

## 八、总结

### 8.1 问题汇总

| 问题 | 严重程度 | 状态 | 影响 |
|------|---------|------|------|
| API 调用失败 | ❌ Critical | ✅ 已修复 | 阻塞使用 |
| System prompt 冗余 | ⚠️ High | ⏳ 待优化 | Token 浪费 |
| 工具 Schema 冗余 | ⚠️ High | ⏳ 待优化 | Token 浪费 |
| 动态注入效率低 | ⚠️ Medium | ⏳ 待优化 | 响应变慢 |

### 8.2 优化效果预估

| 优化项 | Token 减少 | 成本减少 | 速度提升 |
|--------|-----------|---------|---------|
| Prompt 缓存 | -70% | -$0.52/会话 | +30% |
| 工具 Schema 优化 | -30% | -$0.22/会话 | +10% |
| 意图分类 | -5% | -$0.04/会话 | +20% |
| **总计** | **-85%** | **-$0.78/会话** | **+60%** |

### 8.3 当前评分

**准确性**: ⭐⭐⭐⭐ (4/5)
- 工具调用逻辑正确
- 会话管理完整
- 有1个 Critical Bug（已修复）

**完整性**: ⭐⭐⭐⭐⭐ (5/5)
- 照片入库流程完整
- 错误处理完善
- 子 Agent 交互正确

**效率**: ⭐⭐ (2/5)
- Token 消耗高
- 动态注入效率低
- 需要优化

**总体评分**: ⭐⭐⭐⭐ (4/5)

---

## 九、行动计划

### 短期（本周）

1. ✅ **修复 API 错误**（已完成）
2. ⏳ **实现 Prompt 缓存**（优先级最高）
3. ⏳ **添加意图分类**（快速见效）

### 中期（下周）

4. ⏳ **优化工具 Schema 传递**
5. ⏳ **添加 Token 统计**
6. ⏳ **缓存动态注入结果**

### 长期（持续）

7. ⏳ **监控和告警**
8. ⏳ **性能基准测试**
9. ⏳ **成本追踪系统**

---

**分析完成日期**: 2026-04-07
**分析人**: Claude Sonnet 4.6
**日志状态**: 已归档
