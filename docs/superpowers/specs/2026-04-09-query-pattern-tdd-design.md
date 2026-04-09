# Query Pattern TDD 流水线设计

> 版本：v1.0
> 日期：2026-04-09
> 目标：为 scheduler-server 生成 query patterns，验证递归检索方法论

---

## 1. Context

**问题**：向量递归检索依赖 query_pattern 记录引导用户查询到对应工具。当前仅有 8 条极简 patterns（如 "remind me in X minutes"），无法覆盖人类自然语言的多样性。

**实测效果**：测试 "remind me in 5 minutes to take medicine"：
- 直接检索 mcp_tool 相似度：~0.2（不达标）
- 通过 query_pattern → refined_query → mcp_tool：~0.5-0.9（达标）

**目标**：通过三角色 TDD 流水线，生成覆盖人类常用语言的 query patterns，让递归检索的命中率从 0% 提升到 80%+。

**约束**：
- 不修改现有 Skills/Knowledge 动态注入架构
- 只在 `query_pattern` 类型范围内工作
- 从 scheduler-server 开始，验证方法论后推广

---

## 2. 架构

### 2.1 三角色流水线

```
┌──────────────────────────────────────────────────────────────────┐
│                    Generator（Python 脚本）                        │
│  输入：MCP 工具描述（TOOL_SCHEMAS）                                 │
│  输出：candidates.jsonl（候选 patterns）                            │
│  策略：高温度（0.9），强制多样化，不同句式/语气/场景                   │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼ 写入 candidates.jsonl
┌──────────────────────────────────────────────────────────────────┐
│                    Writer（Python 脚本）                            │
│  输入：candidates.jsonl                                             │
│  输出：向量库（vectors.db）                                        │
│  逻辑：UPSERT，每条记录 doc_id 唯一，批量提交                      │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Tester（Python 脚本）                            │
│  输入：向量库中的 patterns                                          │
│  逻辑：对每个 pattern 执行递归检索，验证是否命中正确工具              │
│  通过：recursion_score ≥ 0.5                                      │
│  输出：verified_patterns.jsonl（通过）/ failed_patterns.jsonl（失败）│
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 文件结构

```
scripts/query_pattern/
├── GENERATOR.md          # Generator 的系统提示词（描述任务要求）
├── WRITER.md             # Writer 的系统提示词
├── TESTER.md             # Tester 的系统提示词
├── step1_generate.py     # Generator 入口
├── step2_write.py        # Writer 入口
├── step3_test.py         # Tester 入口
├── pipeline.py           # 主控脚本（串联三步 + 失败重试）
└── tools.py              # 共享工具函数
```

---

## 3. 数据结构

### 3.1 候选 Pattern（Generator 输出）

文件：`candidates.jsonl`，每行一条 JSON：

```json
{
  "target_tool": "scheduler-server/schedule_task",
  "content": "wake me up in 30 minutes",
  "variation_type": "time_relative",
  "generative_note": "使用 wake me up 而非 remind，语气更自然"
}
```

**variation_type**（强制多样性要求）：
- `time_relative`：相对时间（5分钟后、半小时）
- `time_absolute`：绝对时间（下午三点、明天上午10点）
- `action_verb`：不同动词（提醒、叫醒、通知、通知我）
- `context_embedded`：嵌入上下文的句子（我在开会，提醒我5分钟后接孩子）
- `informal`：口语化（赶紧叫我、别忘了哈）
- `question`：疑问句（能提醒我喝水吗）
- `negative`：反向表达（别忘了提醒我）

### 3.2 向量库记录（Writer 写入）

```python
{
    "id": "pattern:scheduler:schedule_task:003",
    "content": "wake me up in 30 minutes",
    "embedding": <L2 normalized>,
    "metadata": {
        "level": "l1",
        "category": "query_pattern",
        "language": "en",
        "type": "query_pattern",
        "is_recursive": True,
        "refined_query": "schedule task",
        "target_tool": "scheduler-server/schedule_task",
        "variation_type": "time_relative",
        "verified": False,  # Tester 之后更新
        "verified_score": None  # Tester 之后更新
    }
}
```

### 3.3 测试结果（Tester 输出）

通过：`verified_patterns.jsonl`
```json
{
    "pattern_id": "pattern:scheduler:schedule_task:003",
    "content": "wake me up in 30 minutes",
    "target_tool": "scheduler-server/schedule_task",
    "recursion_score": 0.72,
    "matched_tool": "scheduler-server/schedule_task",
    "passed": True
}
```

失败：`failed_patterns.jsonl`
```json
{
    "pattern_id": "pattern:scheduler:schedule_task:015",
    "content": "yo remind me bro",
    "target_tool": "scheduler-server/schedule_task",
    "recursion_score": 0.31,
    "matched_tool": "scheduler-server/schedule_task",
    "passed": False,
    "reason": "score below 0.5"
}
```

---

## 4. 核心组件

### 4.1 Generator（step1_generate.py）

**职责**：根据 MCP 工具描述，生成多样化的候选 query patterns

**输入**：
- MCP 工具的 description 字段
- 工具名称和所属服务器
- 目标数量指引（每个工具 5-20 条）

**输出**：`candidates.jsonl`

**生成策略**（强制多样化）：
1. **高频变化**：时间格式（X minutes/hours/days/weeks）
2. **动词替换**：remind → alert → notify → wake → tell → notify me
3. **句式变化**：祈使句、陈述句、疑问句、口语
4. **场景嵌入**：加入生活场景（开会、开车、运动、吃药）
5. **文化多样性**：中英文混用（中文用户的英文表达习惯）

**质量控制**：
- 每条 pattern 必须与目标工具有语义关联
- 避免生成与工具无关的噪声 pattern
- 记录 variation_type，确保覆盖多种类型

**实现**：Python 脚本读取 `GENERATOR.md` 中的提示词，调用 LLM API 生成 patterns

### 4.2 Writer（step2_write.py）

**职责**：将候选 patterns 批量写入向量库

**输入**：`candidates.jsonl`

**逻辑**：
1. 读取 candidates.jsonl
2. 对每条 pattern：
   - 生成 doc_id：`pattern:{server}:{tool}:{counter}`
   - 获取 embedding（L2 归一化）
   - 构建 metadata（包含 refined_query）
3. 批量 UPSERT 到 vectors.db
4. 更新 candidates.jsonl 中的 doc_id

**metadata.refined_query 规则**：
- schedule_task → "schedule task"
- cancel_task → "cancel scheduled task"
- update_task → "update scheduled task"
- list_scheduled_tasks → "list scheduled tasks"

### 4.3 Tester（step3_test.py）

**职责**：验证递归检索是否命中正确工具

**输入**：向量库中所有 `category="query_pattern"` 且 `verified=False` 的记录

**验证流程**：
```
for each pattern in candidates:
    1. 执行递归检索：VectorSearchAdapter.search(pattern["content"])
    2. 检查递归是否触发（is_recursive → refined_query → 第二轮检索）
    3. 验证第二轮检索的第一结果是否是目标工具
    4. 记录分数

    if recursion_score >= 0.5 AND matched_tool == target_tool:
        → passed=True，标记 verified=True, verified_score=recursion_score
    else:
        → passed=False，记录到 failed_patterns.jsonl
```

**分数阈值**：
- 递归检索分数 ≥ 0.5：PASS
- 递归检索分数 < 0.5：FAIL（需要 Generator 重新生成）

**输出**：
- `verified_patterns.jsonl`：通过的 patterns
- `failed_patterns.jsonl`：失败的 patterns

### 4.4 Pipeline（pipeline.py）

**职责**：串联三步，支持失败重试

**流程**：
```
for server in [scheduler-server, ...]:
    for tool in server.tools:
        # Step 1: 生成
        candidates = generate(tool)  # 10-20 条

        # Step 2: 写入
        write_to_db(candidates)

        # Step 3: 测试
        results = test_patterns(candidates)

        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]

        if failed:
            # 失败反馈给 Generator，重试（最多 3 次）
            for retry in range(3):
                new_candidates = generate(tool, failed_feedback=failed)
                write_and_test(new_candidates)

        # 汇总报告
        print(f"工具 {tool}: {len(passed)}/{len(candidates)} 通过")
```

---

## 5. TDD 循环细节

### 5.1 循环状态机

```
[Generator] → candidates.jsonl
                      ↓
                [Writer] → vectors.db
                              ↓
                         [Tester] → verified / failed
                              ↓
                         ┌─────────────────────────────────┐
                         │  失败patterns ≥ 1?               │
                         │    ↓ 是                         │
                         │  [Generator] 重试（附失败反馈）    │
                         │    ↓ 最多3次                    │
                         │  [Writer] 补充写入               │
                         │    ↓                            │
                         │  [Tester] 再次验证              │
                         │    ↓                            │
                         └───── 退出重试循环 ───────────────┘
                              ↓ 否
                         [下一个工具]
```

### 5.2 Generator 的失败反馈格式

```json
{
    "failed_patterns": [
        {
            "content": "yo remind me bro",
            "reason": "score 0.31 - too casual, LLM embedding doesn't match"
        }
    ],
    "target_tool": "scheduler-server/schedule_task",
    "instruction": "请生成更正式、embedding 语义更清晰的短句，避免过于口语化的表达"
}
```

### 5.3 停止条件

- 所有工具的 patterns 均验证通过（递归分数 ≥ 0.5）
- 或单个工具重试次数达到 3 次上限（记录失败，进入下一工具）

---

## 6. 扩展计划（验证 scheduler 后）

| 阶段 | 服务器 | 工具数 | 目标 patterns |
|------|--------|--------|--------------|
| Phase 1 | scheduler-server | 4 | 40-80 |
| Phase 2 | memory-server | 5 | 25-50 |
| Phase 3 | photo-server | 12 | 36-72 |
| Phase 4 | config-manager | 17 | 34-68 |
| Phase 5 | file-parser + kg-server | 14 | 28-56 |

---

## 7. 验证标准

### 7.1 功能验证

```bash
# 运行流水线
python scripts/query_pattern/pipeline.py

# 检查结果
python -c "
import json
verified = open('verified_patterns.jsonl').readlines()
failed = open('failed_patterns.jsonl').readlines()
print(f'通过: {len(verified)}, 失败: {len(failed)}')
"
```

### 7.2 手动测试

```python
# 测试一条 pattern 的递归检索
from agent.vector_search import get_vector_search
vs = get_vector_search()

result = vs.search("5分钟后提醒我吃药", limit=5, min_score=0.3)
# 期望：递归触发，命中 scheduler-server/schedule_task，分数 >= 0.5
```

### 7.3 质量指标

| 指标 | 目标 |
|------|------|
| 递归检索命中率 | ≥ 80% |
| 平均递归分数 | ≥ 0.6 |
| variation_type 覆盖率 | 每工具 ≥ 5 种类型 |

---

## 8. 关键文件

| 文件 | 修改/新增 | 说明 |
|------|----------|------|
| `scripts/query_pattern/GENERATOR.md` | 新增 | Generator 系统提示词 |
| `scripts/query_pattern/WRITER.md` | 新增 | Writer 系统提示词 |
| `scripts/query_pattern/TESTER.md` | 新增 | Tester 系统提示词 |
| `scripts/query_pattern/tools.py` | 新增 | 共享工具函数 |
| `scripts/query_pattern/step1_generate.py` | 新增 | Generator 入口 |
| `scripts/query_pattern/step2_write.py` | 新增 | Writer 入口 |
| `scripts/query_pattern/step3_test.py` | 新增 | Tester 入口 |
| `scripts/query_pattern/pipeline.py` | 新增 | 主控脚本 |
| `scripts/init_vector_db.py` | 修改 | 预留调用接口 |

---

## 9. 风险和缓解

| 风险 | 缓解 |
|------|------|
| Generator 生成质量不稳定 | 高温度 + 多样性约束 + 失败反馈重试 |
| 分数阈值 0.5 过严 | Phase 1 后根据实际数据调整 |
| patterns 数量爆炸 | variation_type 分类管理，定期去重合并 |
| 向量库写入冲突 | doc_id 带 counter，避免重复 |
