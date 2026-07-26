# 子 Agent 提示词清理 Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理同步异步子 Agent 调用改造（阶段一/二/三/四）遗留的提示词问题，分三部分独立推进：(A) 删除 `agent/subagent.py:84` 的有害补丁句（所有子 Agent 共用守则）；(B) 提高"用 `@end` 退出"指令在守则里的醒目度，让子 Agent 不要忘记用 `@end` 结束（不改黑名单、不改拦截器）；(C) 清理 `config/agents/entity-extractor.md` / `dream-evolver.md` / `journal-agent.md` 三个静态提示词里残留的实现细节/补丁句/重复段。三部分互不依赖，可独立按顺序实施。

**Architecture:** 三个落点：(A) `agent/subagent.py` L84 单行删除；(B) `agent/subagent.py` L69-85 的 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 重写——首句命令式强提醒 + 重组结构（退出在前、询问在后）+ 结尾命令式总结，marker `<!-- NIU_SUBAGENT_GUIDE_v1 -->` 升级为 `v2` 避免与已有正文里的 marker 冲突判定失误（实际只是文本变化，但升级 marker 让"已有 v1 不重复注入"的判定自动失效，强制走新模板）；(C) 三个 .md 文件局部 Edit，机械改动。**不动** `agent/generic/agent_loop.py` 的 `_intercept_at_prefix_content` 拦截器、**不动** `agent/subagent.py` 的 `agent_name != "context-manager"` 注入绕过判断、**不动** compat.py / runner.py 的 task prompt 构造（force/sleep 压缩子 Agent 仍由守则注入受益，不需要在 task 里重复 `@end` 提示——见 Context §3 评估）。

**Tech Stack:** Python 3.11+，pytest，subagent 同步调用架构

---

## Context

### 三个问题（用户已确认修复方向）

**问题 A：守则末尾的补丁句有害**

`agent/subagent.py:84` 的 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 末尾：
> 注：你不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。

这是之前修复旧版错误提示词的补丁。子 Agent 原本就没有加标识符的习惯，说"不需要包含"反而让它困惑"我到底要不要加"。所有子 Agent 都受影响（守则对所有非 context-manager 子 Agent 注入）。

**问题 B：子 Agent 总忘记用 `@end` 退出**

拦截器对所有子 Agent（除 context-manager 外）保留。用户原话精简：
- 拦截器有双向作用：好处是子 Agent 想问主 Agent 时被拦 + AUTO_ANSWER 回复"无法解答，请 @end 或继续"，子 Agent 知道"问也没用"最终用正确方式结束；坏处是子 Agent 想正常结束（不调工具、正常回答）时被拦（因为没 `@end` 前缀），强制多跑一轮，浪费 token
- 真正的问题不是"该不该拦截"，而是"子 Agent 总忘记用 `@end` 退出"
- **修复重点**：提高"用 `@end` 退出"这个指令在守则里的醒目度/强度

当前守则（L69-85）问题诊断（从子 Agent 视角）：
1. 开头第一句是"你是子 Agent..."的身份定义——子 Agent 读完第一句还在"建立身份"，没看到"如何退出"
2. "如何退出"被埋在中间（L74-78），跟"如何询问"混在一起，且询问在前退出在后
3. 最后还跟一句补丁句（L84）解释"不需要包含标识符"——跟退出完全无关，是干扰项
4. LLM 读 prompt 是顺序扫读，遇到"如何询问"会先建模"我要问问题"——但实际大部分子 Agent 任务是"做完就退出"，问问题反而是少数情况。当前措辞让"少数情况"显得比"主要情况"还突出

**问题 C：三个 .md 静态提示词残留实现细节/补丁句/重复段**

基于之前的审查报告：
- `config/agents/entity-extractor.md` L82：含"查映射找到对应 UUID 写入游标文件"实现细节 + "你无需报告游标位置"补丁句
- `config/agents/dream-evolver.md` L476：含"游标之后的新消息"解释，应改为"你收到的消息即为本次需要处理的全量消息"
- `config/agents/dream-evolver.md` L316：含 SkillSync/watchdog/FileMovedEvent 等开发者视角实现细节
- `config/agents/dream-evolver.md` L468：get_messages 工具说明含"通常不需要调用"补丁句，应改为"禁止调用"或删除
- `config/agents/journal-agent.md` L115：含"游标机制（`last_journal.json`）"文件名细节 + "不要重复提取"重复句
- `config/agents/journal-agent.md` L26-31 vs L73-82：输入格式段和游标机制段重复，合并

**journal-agent 双重身份特别注意**：它既是程序触发（force/sleep 压缩流水线调用，task 是 `_build_journal_task()` 构造的纯指令，走 `call_subagent_with_auto_answer`），又被主 Agent 调用（`chat-with-journal-agent` 工具，记日志/写周报）。两种场景系统提示词都是同一份 `journal-agent.md`。清理时两种场景的提示词都要通顺：
- 程序触发场景：task 含"从消息中识别工作内容..."指令，history 含消息，走日志记录流程
- 主 Agent 调用场景：task 是用户原话（如"帮我记一条日志：完成了XXX"或"生成本周周报"），history 可能为空，走日志记录或报告生成流程

清理时不能让任何一种场景的提示词失效。

### 改造前基线（已确认）

- **基线 SHA**：`c5cab64a fix(context-manager): 绕过 @niu-agent/@end 守则注入和拦截层` — context-manager 绕过修复已合入，本次在其基础上继续
- 守则模板当前内容（`agent/subagent.py` L69-85）：
  ```
  <!-- NIU_SUBAGENT_GUIDE_v1 -->
  ## 子 Agent 与主 Agent 对话规则

  你是子 Agent，工作未完成时遇到必须澄清的问题，必须用 `@niu-agent ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

  只有以下情况才能直接返回：
  1. 任务已完成，用 `@end ` 前缀返回最终结果。
  2. 任务确实无法继续（如缺权限、缺资源），用 `@end ` 前缀汇报情况让主 Agent 决策。

  其他任何"需要更多信息才能继续"的情况，一律用 `@niu-agent ` 前缀询问。

  格式示例：
  - 询问：`@niu-agent 我应该选择哪个选项？`
  - 结束：`@end 任务已完成，结果：...`

  注：你不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
  ```
- 三个 .md 文件相关段落原文见 File Structure §各 Task 的 Step

### 关键代码位置（HEAD = c5cab64a）

**守则模板** `agent/subagent.py` L62-87：
```python
_BOUNDARY_SECTION_TEMPLATE = """## 职责边界

你的职责范围由上方系统提示词界定的功能描述决定。
不要猜测含义，无法完全确认属于自己的职责范围的，就要直接退出，回复主 Agent。"""


_SUBAGENT_ASK_GUIDE_TEMPLATE = """<!-- NIU_SUBAGENT_GUIDE_v1 -->
## 子 Agent 与主 Agent 对话规则

你是子 Agent，工作未完成时遇到必须澄清的问题，必须用 `@niu-agent ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

只有以下情况才能直接返回：
1. 任务已完成，用 `@end ` 前缀返回最终结果。
2. 任务确实无法继续（如缺权限、缺资源），用 `@end ` 前缀汇报情况让主 Agent 决策。

其他任何"需要更多信息才能继续"的情况，一律用 `@niu-agent ` 前缀询问。

格式示例：
- 询问：`@niu-agent 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`

注：你不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v1 -->"
```

**注入点** `agent/subagent.py` L501-506（context-manager 绕过已存在，本次不动）：
```python
    # 4. 强制注入 @niu-agent/@end 守则
    # context-manager 例外：它原设计是直接输出 keep=/update=/cursor= 让程序写数据库，
    # 不走 @niu-agent/@end 交互通道。注入守则会污染它的输出格式，导致压缩失败。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    if agent_name != "context-manager" and _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

### 第二部分改进方案设计（从子 Agent 视角）

**核心思路**：利用 LLM 处理 system prompt 时的 primacy/recency effect（首尾位置 attention 权重最高），把"用 `@end` 退出"这个高频指令放在首尾各一次强信号位置，中间用清晰结构说明两种情况。

**三条改动**（都不动黑名单、不动拦截器）：

1. **首句命令式强提醒**：守则第一句改成 "任务完成时必须用 `@end ` 前缀输出最终结果，否则会被程序拦截重跑浪费 token。" —— 子 Agent 第一眼就建立"做完要 @end"的肌肉记忆
2. **重组结构**：先讲退出（多数情况）→ 再讲询问（少数情况）。当前是先讲询问再讲退出，反了。子 Agent 任务多数是"做完就退出"，问问题是少数，应该让退出规则占据主导位置
3. **结尾命令式总结**：最后一行改成简短命令式 "记住：完成用 `@end`，提问用 `@niu-agent`，二选一。" —— 收尾强化，子 Agent 扫到结尾时再被提醒一次

**为什么这样改子 Agent 更不容易忘**（子 Agent 视角论证）：
- LLM 生成回复时，system prompt 的首尾内容对生成方向影响最大（primacy/recency）。当前退出规则在中间，被询问规则和补丁句夹击，attention 权重低
- 首句强提醒让子 Agent 在"建立身份"阶段就同时建立"退出方式"——身份和退出方式是绑定的，不会出现"我知道我是子 Agent 但不知道怎么退出"的状态
- 结尾总结是最后一道防线，子 Agent 即使中间内容忘了，结尾的命令式总结会再次触发"用 @end 退出"的生成方向
- 重组结构让退出规则占据视觉上的主导位置（先讲退出 = 退出是默认行为，询问是例外），符合子 Agent 任务的实际分布（多数任务做完就退出）

**marker 升级 v1 → v2 的理由**：
- `build_subagent_system_segments` L505 的 `if _SUBAGENT_ASK_GUIDE_MARKER not in static_system` 判定，如果某个子 Agent 的 .md 正文里恰好含 `<!-- NIU_SUBAGENT_GUIDE_v1 -->` 字样（理论上不会，但保险起见），升级 marker 让旧 marker 失效，强制走新模板注入
- 升级 marker 也让"已有守则不重复注入"的判定自动失效（因为新 marker 在旧正文里不存在），强制注入新模板——这正是我们要的
- marker 升级本身不影响功能，只是注释标记，但让"所有非 context-manager 子 Agent 都拿到新守则"这件事更有保障

**为什么不在 task prompt 里也重复 `@end` 提示**（评估结论）：
- force/sleep 压缩流水线的子 Agent（entity-extractor/dream-evolver/journal-agent）通过 `call_subagent_with_auto_answer` 调用，会注入守则（context-manager 例外，其他都注入）
- 守则改了，这些子 Agent 也会受益，不需要在 task prompt 里重复
- task prompt（compat.py L942/L2658/L2743/L2826 + runner.py L1217）当前完全不含 `@end` 提示，加进去会让 task prompt 变长且与守则重复，维护成本高
- 主 Agent 调用子 Agent（`chat-with-xxx` 工具）的路径，子 Agent 也走 `call_subagent`，同样注入守则——守则改了所有路径都受益
- 结论：**不在 task prompt 里重复 `@end` 提示**，只改守则模板

### 关键约束（用户铁律）

- **修改前必须先做临时提交备份**（铁律 #3）— Task 0 做备份
- **禁止 `git reset --hard` / force push**（铁律 #9）
- **测试必须用真实数据 + 真实 LLM**（铁律 #5）— Task 4 端到端用真实定时任务触发，不 mock
- **Python 编辑后立即语法检查**（用户记忆 Edit Safety Rules）
- **Edit 前必须 Read 确认 old_string**（用户记忆 Edit Safety Rules）
- **派出去的子 Agent 必须遵守所有铁律**

---

## File Structure

```
ai-bot/                              # 项目根
├── agent/
│   └── subagent.py                  # Task A 删 L84 补丁句；Task B 重写 L69-85 守则 + 升级 marker
├── config/agents/
│   ├── entity-extractor.md          # Task C 改 L82
│   ├── dream-evolver.md             # Task C 改 L316 / L468 / L476
│   └── journal-agent.md             # Task C 合并 L26-31+L73-82 / 改 L115
├── tests/
│   ├── test_at_prefix_interception.py        # 已有，回归验证（不改）
│   ├── test_context_manager_bypass_at_prefix.py  # 已有，回归验证（不改）
│   ├── test_general_subagent.py             # 已有，回归验证；Task B 改守则后需检查 L425/L444 测试是否仍通过
│   ├── test_call_subagent_with_auto_answer.py # 已有，回归验证（不改）
│   └── test_subagent_prompt_cleanup.py      # 新增，Task B 的 TDD 测试
└── niu_api/compat.py                # 不改（task prompt 不动）
```

---

## Tasks

### Task 0: 修改前临时备份提交

- [ ] **Step 0.1**：检查工作区干净（除本次新计划文件外）
```bash
cd <repo_root>
git status
```
**预期**：只有 `docs/superpowers/plans/2026-07-09-subagent-prompt-cleanup.md` 是新文件，其他干净。如果有其他未提交改动，**停下来报告用户**，不要盲目备份。

- [ ] **Step 0.2**：临时备份提交（标注问题名+节点类型+基线 hash）
```bash
cd <repo_root>
git add docs/superpowers/plans/2026-07-09-subagent-prompt-cleanup.md
git commit -m "backup: 子 Agent 提示词清理改造前临时备份 (baseline c5cab64a)

问题：守则末尾补丁句有害 + 子 Agent 总忘记用 @end 退出 + 三个 .md 残留实现细节/补丁句/重复段

准备改三个点（三部分独立）：
A. agent/subagent.py L84 删补丁句（所有子 Agent 共用守则）
B. agent/subagent.py L69-85 重写守则：首句命令式 + 重组结构 + 结尾总结 + marker v1→v2
C. config/agents/{entity-extractor,dream-evolver,journal-agent}.md 清理实现细节/补丁句/重复段

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task A: 删守则末尾补丁句（最简单）

**目标**：删除 `agent/subagent.py:84` 的补丁句 "注：你不需要在输出里包含自己的标识符..."。这一行是 Task B 重写守则的前置步骤——先删掉，Task B 再重写整段。**但 Task A 和 Task B 不能合并**：Task A 是独立可回滚的最小改动，Task B 是大重写。如果 Task B 出问题，Task A 的删除仍然有价值（补丁句有害，删了就是对的）。

**注意**：Task A 删完后，守则末尾会变成空行 + 换行。Task B 会整段重写，所以 Task A 不需要管末尾空行是否优雅，只要把那一行删掉即可。

- [ ] **Step A.1**：Read `agent/subagent.py` L69-85 确认当前守则内容（Edit 前必须 Read）
```bash
cd <repo_root>
# 用 Read 工具读 agent/subagent.py L69-85
```

- [ ] **Step A.2**：Edit 删除 L84 补丁句

old_string（精确匹配 L82-85）：
```
格式示例：
- 询问：`@niu-agent 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`

注：你不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
"""
```

new_string（删掉补丁句，保留三引号闭合）：
```
格式示例：
- 询问：`@niu-agent 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`
"""
```

- [ ] **Step A.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import agent.subagent; print('OK')"
```
**预期**：输出 `OK`。如果报错，立即用 Edit 撤销（把删掉的那行加回去），**不要继续**。

- [ ] **Step A.4**：跑现有守则注入测试，确认不破坏
```bash
cd <repo_root>
python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_injects_guide_for_all_subagents tests/test_general_subagent.py::test_build_subagent_system_segments_no_duplicate_injection -v 2>&1 | tail -20
```
**预期**：两个测试通过（它们断言 marker + `@niu-agent` + `@end` 在 static_system 里，删补丁句不影响这些断言）。

- [ ] **Step A.5**：跑 at_prefix 拦截层测试，确认不破坏
```bash
cd <repo_root>
python -m pytest tests/test_at_prefix_interception.py tests/test_context_manager_bypass_at_prefix.py -v 2>&1 | tail -30
```
**预期**：全部通过。

---

### Task B: 提高退出醒目度（重写守则 + TDD）

**目标**：重写 `_SUBAGENT_ASK_GUIDE_TEMPLATE`，让"用 `@end` 退出"在首尾各出现一次强信号，中间用清晰结构说明两种情况。marker 从 v1 升级到 v2。

**TDD 流程**：先写失败测试（断言新守则的措辞和位置），再改守则让测试通过。

- [ ] **Step B.1**：创建测试文件 `tests/test_subagent_prompt_cleanup.py`

```python
"""子 Agent 守则清理后的措辞和位置验证。

验证三件事：
1. 补丁句"你不需要在输出里包含自己的标识符"已删除（Task A 的回归保护）
2. 新守则首句是命令式强提醒"任务完成时必须用 @end"（Task B 的首句强提醒）
3. 新守则结尾是命令式总结"记住：完成用 @end"（Task B 的结尾总结）
4. marker 升级为 v2（强制走新模板）
5. context-manager 仍不被注入守则（回归保护）
6. 其他子 Agent（如 file-processor）仍被注入新守则（回归保护）
"""


def test_patch_sentence_removed():
    """补丁句"你不需要在输出里包含自己的标识符"已从守则删除"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert "你不需要在输出里包含自己的标识符" not in _SUBAGENT_ASK_GUIDE_TEMPLATE
    assert "程序会自动在你的问题前加上唯一标识" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_guide_first_line_is_command_style_exit_reminder():
    """新守则首句是命令式强提醒，含"任务完成"和"@end"

    首句强提醒：让子 Agent 第一眼就建立"做完要 @end"的肌肉记忆。
    利用 primacy effect（LLM 处理 system prompt 时首句 attention 权重最高）。
    """
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    # 去掉 marker 行和空行后，第一句实际内容应含"任务完成"和"@end"
    lines = [
        line for line in _SUBAGENT_ASK_GUIDE_TEMPLATE.splitlines()
        if line.strip() and not line.strip().startswith("<!--")
    ]
    first_content_line = lines[0] if lines else ""
    # 首句应含"任务完成"和"@end"两个关键词（命令式强提醒）
    assert "任务完成" in first_content_line, f"首句应含'任务完成'，实际: {first_content_line}"
    assert "@end" in first_content_line, f"首句应含'@end'，实际: {first_content_line}"


def test_guide_last_line_is_command_style_summary():
    """新守则结尾是命令式总结"记住：完成用 @end"

    结尾总结：子 Agent 扫到结尾时再被提醒一次。
    利用 recency effect（结尾 attention 权重高）。
    """
    from agent.subagent import _SUBAGENT_ASK_GUIDE_TEMPLATE

    lines = [
        line for line in _SUBAGENT_ASK_GUIDE_TEMPLATE.splitlines()
        if line.strip() and not line.strip().startswith("<!--")
    ]
    last_content_line = lines[-1] if lines else ""
    # 结尾应含"记住"和"@end"（命令式总结）
    assert "记住" in last_content_line, f"结尾应含'记住'，实际: {last_content_line}"
    assert "@end" in last_content_line, f"结尾应含'@end'，实际: {last_content_line}"
    # 结尾应同时提到 @niu-agent（二选一）
    assert "@niu-agent" in last_content_line, f"结尾应含'@niu-agent'，实际: {last_content_line}"


def test_guide_marker_upgraded_to_v2():
    """marker 从 v1 升级到 v2，强制走新模板注入"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, _SUBAGENT_ASK_GUIDE_TEMPLATE

    assert _SUBAGENT_ASK_GUIDE_MARKER == "<!-- NIU_SUBAGENT_GUIDE_v2 -->"
    assert "<!-- NIU_SUBAGENT_GUIDE_v2 -->" in _SUBAGENT_ASK_GUIDE_TEMPLATE
    # 旧 marker 不应出现在新模板里
    assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" not in _SUBAGENT_ASK_GUIDE_TEMPLATE


def test_context_manager_still_not_injected():
    """context-manager 仍不被注入守则（回归保护）"""
    from agent.subagent import build_subagent_system_segments, _SUBAGENT_ASK_GUIDE_MARKER

    static_system, _ = build_subagent_system_segments("context-manager")
    assert _SUBAGENT_ASK_GUIDE_MARKER not in static_system


def test_other_subagents_still_injected_with_new_guide(tmp_path, monkeypatch):
    """其他子 Agent（如 my-agent）仍被注入新守则（回归保护）"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text("---\ndescription: my agent\n---\nYou are my agent.")

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, _ = subagent.build_subagent_system_segments("my-agent")
    assert subagent._SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@end" in static_system
    assert "@niu-agent" in static_system
```

- [ ] **Step B.2**：跑测试确认失败（守则还没改）
```bash
cd <repo_root>
python -m pytest tests/test_subagent_prompt_cleanup.py -v 2>&1 | tail -30
```
**预期失败**：
- `test_guide_first_line_is_command_style_exit_reminder` 失败（当前首句是"你是子 Agent..."）
- `test_guide_last_line_is_command_style_summary` 失败（当前结尾是格式示例或已删的补丁句位置）
- `test_guide_marker_upgraded_to_v2` 失败（当前是 v1）
**预期通过**：
- `test_patch_sentence_removed` 通过（Task A 已删）
- `test_context_manager_still_not_injected` 通过（context-manager 绕过未动）
- `test_other_subagents_still_injected_with_new_guide` 通过（注入逻辑未动，只是模板内容会变，但 marker 和 @end/@niu-agent 仍在）

- [ ] **Step B.3**：Read `agent/subagent.py` L62-87 确认当前守则模板和 marker（Edit 前必须 Read）

**注意**：此时 Task A 已删了 L84 补丁句，守则末尾是 "格式示例...结束：..." + 三引号闭合，**不含补丁句**。Step B.4 的 old_string 匹配的是这个状态。如果 Read 发现守则仍含补丁句，说明 Task A 没做或被回滚，**停下来报告用户**，不要盲目继续。

- [ ] **Step B.4**：Edit 替换 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 和 `_SUBAGENT_ASK_GUIDE_MARKER`

old_string（L69-87，含 marker 常量行）：
```python
_SUBAGENT_ASK_GUIDE_TEMPLATE = """<!-- NIU_SUBAGENT_GUIDE_v1 -->
## 子 Agent 与主 Agent 对话规则

你是子 Agent，工作未完成时遇到必须澄清的问题，必须用 `@niu-agent ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

只有以下情况才能直接返回：
1. 任务已完成，用 `@end ` 前缀返回最终结果。
2. 任务确实无法继续（如缺权限、缺资源），用 `@end ` 前缀汇报情况让主 Agent 决策。

其他任何"需要更多信息才能继续"的情况，一律用 `@niu-agent ` 前缀询问。

格式示例：
- 询问：`@niu-agent 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v1 -->"
```

new_string（重写后的守则 + 升级 marker）：
```python
_SUBAGENT_ASK_GUIDE_TEMPLATE = """<!-- NIU_SUBAGENT_GUIDE_v2 -->
## 子 Agent 与主 Agent 对话规则

任务完成时必须用 `@end ` 前缀输出最终结果，否则会被程序拦截重跑浪费 token。

### 退出（默认行为，任务做完就走）

以下两种情况都用 `@end ` 前缀返回：
1. 任务已完成——返回最终结果。
2. 任务确实无法继续（如缺权限、缺资源）——汇报情况让主 Agent 决策。

### 询问（少数情况，必须澄清才能继续）

工作未完成时遇到必须澄清的问题，必须用 `@niu-agent ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

### 格式示例

- 退出：`@end 任务已完成，结果：...`
- 询问：`@niu-agent 我应该选择哪个选项？`

记住：完成用 `@end`，提问用 `@niu-agent`，二选一。
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v2 -->"
```

**设计说明**（从子 Agent 视角）：
- 首句 "任务完成时必须用 `@end ` 前缀输出最终结果，否则会被程序拦截重跑浪费 token。" —— 命令式 + 说明后果（浪费 token），子 Agent 第一眼就建立退出肌肉记忆
- 重组为"退出（默认行为）→ 询问（少数情况）"——退出占据主导位置，询问是例外，符合子 Agent 任务实际分布
- 结尾 "记住：完成用 `@end`，提问用 `@niu-agent`，二选一。" —— 命令式总结，首尾各一次强信号
- marker v1→v2：强制走新模板注入（旧 marker 在任何子 Agent 正文里都不存在，但升级让"已有守则不重复注入"判定自动失效，保险起见）

- [ ] **Step B.5**：Python 语法检查
```bash
cd <repo_root>
python -c "import agent.subagent; print('OK')"
```
**预期**：输出 `OK`。

- [ ] **Step B.6**：跑 Task B 的测试，确认全部通过
```bash
cd <repo_root>
python -m pytest tests/test_subagent_prompt_cleanup.py -v 2>&1 | tail -20
```
**预期**：6 个测试全部通过。

- [ ] **Step B.7**：跑现有守则注入测试，确认哪些破坏
```bash
cd <repo_root>
python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_injects_guide_for_all_subagents tests/test_general_subagent.py::test_build_subagent_system_segments_no_duplicate_injection -v 2>&1 | tail -20
```
**预期**：两个测试都会**失败**，因为它们都硬编码了 v1 marker 字符串，而 Task B 把 marker 升级成了 v2：
- `test_build_subagent_system_segments_injects_guide_for_all_subagents` L439：`assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" in static_system` —— 新守则注入的是 v2，断言 v1 in static_system 会失败
- `test_build_subagent_system_segments_no_duplicate_injection` L451 fixture 正文写 `<!-- NIU_SUBAGENT_GUIDE_v1 -->` + L461 断言 `static_system.count("<!-- NIU_SUBAGENT_GUIDE_v1 -->") == 1` —— 新代码用 v2 判定，fixture 的 v1 不匹配，仍注入 v2 守则；此时 v1 count == 1（仅来自 fixture）、v2 count == 1（来自注入），L461 断言 v1 count == 1 表面通过但**违背测试意图**（测试意图是"已含守则时不重复注入"，但新代码确实注入了 v2，因为 fixture 是 v1 不匹配 v2）

**注意**：这两个测试是测试代码不是生产代码，更新它们的硬编码 v1 字符串是允许的小改动。Step B.8 统一处理。

- [ ] **Step B.8**：更新 `tests/test_general_subagent.py` 中所有硬编码 v1 的断言（推荐改用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量，更健壮）

Step B.7 的两个测试都会失败，必须更新。**推荐方案**：把硬编码的 v1 字符串改成引用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量，这样以后 marker 再升级（v2→v3）测试也不会失效。

**改动 1**：L439 `test_build_subagent_system_segments_injects_guide_for_all_subagents` 的断言

先 Read 确认（Step B.8.a）：
```bash
cd <repo_root>
# 用 Read 工具读 tests/test_general_subagent.py L425-442
```

Edit（Step B.8.b）：

old_string（L438-441）：
```python
    static_system, dynamic_system = subagent.build_subagent_system_segments("my-agent")
    assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" in static_system
    assert "@niu-agent" in static_system
    assert "@end" in static_system
```

new_string（改用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量，与 marker 升级解耦）：
```python
    static_system, dynamic_system = subagent.build_subagent_system_segments("my-agent")
    assert subagent._SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@niu-agent" in static_system
    assert "@end" in static_system
```

**改动 2**：L451 fixture + L461 断言 `test_build_subagent_system_segments_no_duplicate_injection`

先 Read 确认（Step B.8.c）：
```bash
cd <repo_root>
# 用 Read 工具读 tests/test_general_subagent.py L444-462
```

Edit（Step B.8.d）—— fixture 改用常量：

old_string（L450-452）：
```python
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent.\n\n<!-- NIU_SUBAGENT_GUIDE_v1 -->\n已有守则"
    )
```

new_string（用 `_SUBAGENT_ASK_GUIDE_MARKER` 拼接，与 marker 升级解耦）：
```python
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent.\n\n"
        + subagent._SUBAGENT_ASK_GUIDE_MARKER + "\n已有守则"
    )
```

Edit（Step B.8.e）—— 断言改用常量：

old_string（L460-461）：
```python
    # 守则只出现一次（marker 计数 == 1）
    assert static_system.count("<!-- NIU_SUBAGENT_GUIDE_v1 -->") == 1
```

new_string（用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量计数）：
```python
    # 守则只出现一次（marker 计数 == 1）
    assert static_system.count(subagent._SUBAGENT_ASK_GUIDE_MARKER) == 1
```

Step B.8.f：重跑两个测试确认通过
```bash
cd <repo_root>
python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_injects_guide_for_all_subagents tests/test_general_subagent.py::test_build_subagent_system_segments_no_duplicate_injection -v 2>&1 | tail -20
```
**预期**：两个测试都通过。

**为什么不直接把 v1 改成 v2 字符串**：硬编码 v2 字符串虽然能让测试通过，但下次 marker 再升级（v2→v3）又要改一次测试。改用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量断言，测试与 marker 值解耦，更健壮，符合"测试断言行为不断言实现细节"原则。

**为什么不删掉这两个测试**：它们验证的行为（"所有非 context-manager 子 Agent 都注入守则" + "正文已含 marker 时不重复注入"）是有价值的回归保护，不能删。改用常量断言后，它们与 Task B.1 的新测试互补——Task B.1 测试守则的措辞和位置，这两个测试测试注入逻辑（是否注入 + 是否重复注入）。

- [ ] **Step B.9**：跑 at_prefix 拦截层测试，确认不破坏
```bash
cd <repo_root>
python -m pytest tests/test_at_prefix_interception.py tests/test_context_manager_bypass_at_prefix.py tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -40
```
**预期**：全部通过。拦截器的 FORMAT_ERROR 提示词里含 `@niu-agent` 和 `@end`（来自 `_intercept_at_prefix_content` 内部，不是守则模板），改守则不影响。

---

### Task C: 清理三个 .md 静态提示词（机械改动）

**目标**：清理 `entity-extractor.md` / `dream-evolver.md` / `journal-agent.md` 里残留的实现细节/补丁句/重复段。每个文件独立改动，互不影响。

**通用约束**：
- Edit 前必须 Read 确认 old_string（用户记忆 Edit Safety Rules）
- 改完用 `python -c` 或 grep 确认文件仍可读（.md 文件不需要语法检查，但确认改动生效）

#### Task C1: entity-extractor.md L82

- [ ] **Step C1.1**：Read `config/agents/entity-extractor.md` L78-83 确认当前内容

- [ ] **Step C1.2**：Edit 删除"查映射找到对应 UUID 写入游标文件"实现细节 + "你无需报告游标位置"补丁句

old_string（L78-82）：
```
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- history 列表中的消息即为本次需要处理的全量消息，不含已处理过的旧消息；每条 content 前缀 `[N]` 极简编号（1-based）
- 游标由程序根据你输出的 `processed_up_to=N` 推进（查映射找到对应 UUID 写入游标文件），你无需报告游标位置，但必须输出 `processed_up_to=N`
```

new_string（删实现细节 + 补丁句，保留必要信息）：
```
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- history 列表中的消息即为本次需要处理的全量消息，不含已处理过的旧消息；每条 content 前缀 `[N]` 极简编号（1-based）
- 你必须输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标
```

**改动说明**：
- 删 "（查映射找到对应 UUID 写入游标文件）"——这是开发者视角的实现细节，子 Agent 不需要知道 UUID 和游标文件
- 删 "你无需报告游标位置"——补丁句，子 Agent 原本就不会报告游标位置，说"无需"反而困惑
- 改 "但必须输出 `processed_up_to=N`" 为 "你必须输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标"——命令式 + 明确 N 的含义，子 Agent 视角更清晰

- [ ] **Step C1.3**：grep 确认改动生效
```bash
cd <repo_root>
grep -n "查映射找到对应 UUID\|你无需报告游标位置" config/agents/entity-extractor.md
```
**预期**：无输出（两处都已删除）。

#### Task C2: dream-evolver.md 三处清理

- [ ] **Step C2.1**：Read `config/agents/dream-evolver.md` L313-317 确认 L316 上下文

- [ ] **Step C2.2**：Edit 删除 L316 的 SkillSync/watchdog/FileMovedEvent 开发者视角实现细节

old_string（L316 整行）：
```
3. **LightRAG 实体清理**：文件移动后，由 SkillSync 清理 LightRAG 实体。注意 watchdog 的 `on_deleted` 在 macOS 上对 `mv` 到子目录**不触发**（产生 FileMovedEvent 而非 FileDeletedEvent），所以清理依赖 `scan_and_sync` 的60秒定时扫描（检测到磁盘文件消失后调 `_delete_skill_from_lightrag`）。最长延迟约60秒，期间主Agent可能仍检索到该 skill 的残留实体——这是可接受的（文件已不在，主Agent即使检索到也读取不到内容）。
```

new_string（从子 Agent 视角重写，只保留它需要知道的）：
```
3. **LightRAG 实体清理**：文件移动后，程序会在后台清理 LightRAG 实体（最长延迟约60秒）。期间主 Agent 可能仍检索到该 skill 的残留实体——这是可接受的（文件已不在，主 Agent 即使检索到也读取不到内容）。
```

**改动说明**：删 SkillSync/watchdog/FileMovedEvent/on_deleted/scan_and_sync/_delete_skill_from_lightrag 这些开发者视角的实现细节，子 Agent 只需要知道"程序会后台清理，有60秒延迟，期间残留可接受"。

- [ ] **Step C2.3**：Read `config/agents/dream-evolver.md` L467-472 确认 L468 上下文

- [ ] **Step C2.4**：Edit 把 L468 的"通常不需要调用"补丁句改为"禁止调用"

old_string（L467-472）：
```
其他工具：
- `get_messages(session_id)` — session_id 传 `"default"`（但消息已在 prompt 中提供，通常不需要调用）
- `edit(file_path, old_string, new_string)` — Skill 修改（含 frontmatter status/issue_count 字段修改）
- `write(file_path, content)` — Skill 创建
- `read(file_path)` — Skill 读取
- `bash(command)` — 执行 shell 命令，仅用于步骤 C5 删除 skill（mv 到 .trash/）
```

new_string（"通常不需要"改为"禁止"，更明确）：
```
其他工具：
- `get_messages(session_id)` — **禁止调用**（消息已在 prompt 中提供）
- `edit(file_path, old_string, new_string)` — Skill 修改（含 frontmatter status/issue_count 字段修改）
- `write(file_path, content)` — Skill 创建
- `read(file_path)` — Skill 读取
- `bash(command)` — 执行 shell 命令，仅用于步骤 C5 删除 skill（mv 到 .trash/）
```

**改动说明**："通常不需要调用"是软约束，子 Agent 可能仍调用；改为"禁止调用"是硬约束，子 Agent 不会调用。减少不必要的工具调用浪费 token。

- [ ] **Step C2.5**：Read `config/agents/dream-evolver.md` L474-487 确认 L476 上下文

- [ ] **Step C2.6**：Edit 把 L476 "游标之后的新消息"解释改为"你收到的消息即为本次需要处理的全量消息"

old_string（L474-476）：
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息，不需要自行过滤范围。
```

new_string（从子 Agent 视角重写，删"游标之后"开发者视角措辞）：
```
## 游标机制

你收到的消息即为本次需要处理的全量消息，直接处理全部，不需要自行过滤范围。
```

**改动说明**：删"程序只传入增量消息（游标之后的新消息）"——"游标之后"是开发者视角，子 Agent 不需要知道游标概念。改为"你收到的消息即为本次需要处理的全量消息"——子 Agent 视角，直接告诉它"收到啥处理啥"。

- [ ] **Step C2.7**：grep 确认三处改动都生效
```bash
cd <repo_root>
grep -n "SkillSync\|watchdog\|FileMovedEvent\|scan_and_sync\|_delete_skill_from_lightrag" config/agents/dream-evolver.md
grep -n "通常不需要调用" config/agents/dream-evolver.md
grep -n "游标之后的新消息" config/agents/dream-evolver.md
```
**预期**：三条 grep 都无输出（都已删除）。

#### Task C3: journal-agent.md 合并重复段 + 删文件名细节

**journal-agent 双重身份注意**：既是程序触发（force/sleep 压缩，task 是 `_build_journal_task()` 纯指令，history 含消息），又被主 Agent 调用（`chat-with-journal-agent`，task 是用户原话，history 可能为空）。清理时两种场景的提示词都要通顺。

- [ ] **Step C3.1**：Read `config/agents/journal-agent.md` 全文（L1-120）确认当前内容

- [ ] **Step C3.2**：Edit 合并 L24-31 输入格式段和 L73-82 游标机制段（重复部分合并）

当前 L24-31（输入格式段）：
```
## 输入格式

task 是纯指令，消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based）。两种场景：

1. **日志记录**（默认）：task 是"从消息中识别工作内容..."指令，history 含增量对话消息，从中提取工作内容写入日志。这是最常见的场景。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`，程序据此推进游标。
2. **报告生成**（task 明确要求"生成周报/月报/季报/年报"）：history 为空或不含相关消息，按指令执行报告生成操作。此时无需输出 `processed_up_to=`。

如果 history 含消息，按日志记录流程处理。如果 history 为空或 task 明确要求生成报告，按指令内容执行。
```

当前 L73-82（游标机制段）：
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based），每条含 `role` 和 `content` 字段，assistant 消息可能含 `tool_calls`，tool 消息含 `tool_call_id`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）
```

合并方案：保留 L24-31 的输入格式段（含两种场景说明），把 L73-82 游标机制段里**不重复的**信息（assistant 含 tool_calls、tool 含 tool_call_id、未输出 processed_up_to 的兜底说明）合并进 L24-31，然后删除 L73-82 整段。

old_string（L24-31，整段替换为合并版）：
```
## 输入格式

task 是纯指令，消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based）。两种场景：

1. **日志记录**（默认）：task 是"从消息中识别工作内容..."指令，history 含增量对话消息，从中提取工作内容写入日志。这是最常见的场景。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`，程序据此推进游标。
2. **报告生成**（task 明确要求"生成周报/月报/季报/年报"）：history 为空或不含相关消息，按指令执行报告生成操作。此时无需输出 `processed_up_to=`。

如果 history 含消息，按日志记录流程处理。如果 history 为空或 task 明确要求生成报告，按指令内容执行。
```

new_string（合并版，吸收游标机制段的不重复信息）：
```
## 输入格式

task 是纯指令，消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based），每条含 `role` 和 `content` 字段，assistant 消息可能含 `tool_calls`，tool 消息含 `tool_call_id`。两种场景：

1. **日志记录**（默认）：task 是"从消息中识别工作内容..."指令，history 含增量对话消息，从中提取工作内容写入日志。这是最常见的场景。你收到的消息即为本次需要处理的全量消息，直接处理全部。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）。
2. **报告生成**（task 明确要求"生成周报/月报/季报/年报"）：history 为空或不含相关消息，按指令执行报告生成操作。此时无需输出 `processed_up_to=`。

如果 history 含消息，按日志记录流程处理。如果 history 为空或 task 明确要求生成报告，按指令内容执行。
```

- [ ] **Step C3.3**：Edit 删除 L73-82 游标机制段（信息已合并到 L24-31）

old_string（L73-82 整段，含标题）：
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based），每条含 `role` 和 `content` 字段，assistant 消息可能含 `tool_calls`，tool 消息含 `tool_call_id`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）

## 输出格式
```

new_string（删除整段，保留下一个标题）：
```
## 输出格式
```

**注意**：old_string 末尾含 `## 输出格式` 是为了让 new_string 保留这个标题（删段不删下一个标题）。Read 时确认 L83 是 `## 输出格式`。

- [ ] **Step C3.4**：Read `config/agents/journal-agent.md` L113-116 确认 L115 上下文

- [ ] **Step C3.5**：Edit 删除 L115 的"游标机制（`last_journal.json`）"文件名细节 + "不要重复提取同一消息中的内容"重复句

old_string（L113-116）：
```
## 去重机制

程序通过游标机制（`last_journal.json`）确保只传入增量消息。你只需处理收到的全部消息，无需自行去重。不要重复提取同一消息中的内容。
```

new_string（删文件名细节 + 重复句）：
```
## 去重机制

程序确保只传入增量消息，你只需处理收到的全部消息，无需自行去重。
```

**改动说明**：
- 删 "（`last_journal.json`）"——文件名是开发者视角的实现细节，子 Agent 不需要知道游标文件叫什么
- 删 "不要重复提取同一消息中的内容"——这句跟"无需自行去重"重复，且"重复提取同一消息"语义模糊（是指同一条消息里提取多个条目？还是同一条消息处理两次？），删掉避免困惑
- "程序通过游标机制确保" 改为 "程序确保"——"游标机制"也是开发者视角，子 Agent 不需要知道游标概念

- [ ] **Step C3.6**：grep 确认 journal-agent.md 三处改动都生效
```bash
cd <repo_root>
grep -n "last_journal.json" config/agents/journal-agent.md
grep -n "不要重复提取同一消息中的内容" config/agents/journal-agent.md
grep -n "## 游标机制" config/agents/journal-agent.md
```
**预期**：三条 grep 都无输出（都已删除/合并）。
**注意**：`## 游标机制` 标题应该消失（合并到输入格式段了）。如果 grep 到 `游标` 字样（如"程序据此推进游标"），那是正常的——删的是"游标机制"这个标题段和"游标之后的新消息"措辞，不是删所有"游标"字样。

- [ ] **Step C3.7**：通读 journal-agent.md 全文，确认双重身份场景都通顺
```bash
cd <repo_root>
# 用 Read 工具读 config/agents/journal-agent.md 全文
```
**检查点**（双重身份）：
- 程序触发场景（task 是 `_build_journal_task()` 纯指令"从消息中识别工作内容..."，history 含消息）：输入格式段说"task 是纯指令，消息以 history 形式逐条传入"——通顺
- 主 Agent 调用场景（task 是用户原话如"帮我记一条日志：完成了XXX"，history 可能为空）：
  - 如果 task 是"记日志"且 history 为空：输入格式段说"如果 history 为空或 task 明确要求生成报告，按指令内容执行"——但"记日志"不是"生成报告"，这句会让子 Agent 困惑。**检查这句是否需要调整**
  - 实际上"记日志"且 history 为空是合法场景（用户直接说"帮我记一条日志：完成了XXX"，不需要从对话消息提取）——此时子 Agent 应该直接按 task 内容写日志，不需要 processed_up_to
  - 当前输入格式段 L31 "如果 history 为空或 task 明确要求生成报告，按指令内容执行" —— 这句覆盖了"history 为空"的情况，"按指令内容执行"包括"记日志"和"生成报告"两种。**通顺**，不需要调整
- 报告生成场景（task 含"周报/月报/季报/年报"，history 为空）：输入格式段场景2明确覆盖——通顺

**如果发现任何场景不通顺**，用 Edit 微调措辞，但**不要扩大改动范围**（只改不通顺的那一句）。

---

### Task D: 回归测试 — 现有测试不破坏

**目标**：跑现有相关测试，确认三部分改动不破坏其他子 Agent 行为。

- [ ] **Step D.1**：跑 subagent 相关全套测试
```bash
cd <repo_root>
python -m pytest tests/test_general_subagent.py tests/test_at_prefix_interception.py tests/test_context_manager_bypass_at_prefix.py tests/test_call_subagent_with_auto_answer.py tests/test_subagent_prompt_cleanup.py tests/test_subagent_overflow.py tests/test_subagent_registry.py tests/test_subagent_supplement.py tests/test_subagent_supplement_integration.py tests/test_subagent_msg_role.py tests/test_call_subagent_memory_hook.py tests/test_sync_subagent_interaction.py tests/test_async_subagent_dispatch.py tests/test_subagent_interaction_integration.py -v 2>&1 | tail -60
```
**预期**：全部通过。
**如果失败**：
- 如果是 `test_build_subagent_system_segments_injects_guide_for_all_subagents` 或 `test_build_subagent_system_segments_no_duplicate_injection` 失败（硬编码 v1 字符串问题），说明 Step B.8 没做或被回滚——回去补做 Step B.8（改用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量断言）
- 其他失败立即停下，用 Edit 撤销相关改动恢复原状（铁律 #5 调试无效马上撤销），**不要继续**

- [ ] **Step D.2**：跑 journal-agent 相关测试（验证 .md 清理不破坏）
```bash
cd <repo_root>
python -m pytest tests/test_journal_agent_tidy.py tests/test_journal_unified_paths.py -v 2>&1 | tail -30
```
**预期**：全部通过。这两个测试如果断言了被删的措辞（如"游标机制"标题、"last_journal.json"字样），需要更新测试断言——**这是允许的小改动**，因为测试断言的是实现细节而非行为。

- [ ] **Step D.3**：如果 Step D.2 失败，检查失败原因
```bash
cd <repo_root>
# 用 Read 工具读失败的测试文件，看断言了什么
# 如果断言的是被删的措辞（如"游标机制"标题），用 Edit 更新测试断言
# 如果断言的是行为（如 processed_up_to 解析），不要改测试，撤销 .md 改动
```

---

### Task E: 真实端到端验证（真实定时任务 + 真实 LLM）

**目标**：启动 ./niu 触发压缩，看子 Agent 是否更少忘记用 @end 退出。验证三部分改动的实际效果。

**铁律 #5 要求**：测试必须用真实数据 + 真实 LLM，不 mock。

- [ ] **Step E.1**：清理测试环境
```bash
cd <repo_root>
# 杀掉所有 niu 进程（铁律 #7 必须优雅退出，禁止 pkill -f niu）
ps aux | grep -E "niu|launcher" | grep -v grep
# 用 kill -TERM <pid> 逐个优雅退出
```

- [ ] **Step E.2**：检查 messages.db 状态
```bash
sqlite3 ~/.niu/messages.db "SELECT role, COUNT(*) FROM messages GROUP BY role"
```
**预期**：不含 `subagent_msg` 行（只有 assistant/system/user）。如果含，按上一份计划 Task 5 Step 5.2 的恢复分支处理（从 `~/.niu/messages.db_副本` 恢复）。

- [ ] **Step E.3**：启动程序
```bash
cd <repo_root>
./niu &
# 等待启动完成，看到 "LightRAG initialized" 和 API ready 日志
```

- [ ] **Step E.4**：触发定时任务让主 Agent 上下文涨到 84% 触发强制压缩
- 方式一：跑一段长任务直到日志出现 `Proactive compress: ... (84% > 80%)`
- 方式二：临时调小 `~/.niu/preferences.json` 的 `contextWindowSize` 或 `warningThreshold`

```bash
# 监控日志
tail -f logs/api_stderr.log | grep -E "Proactive compress|context-manager|keep=|FORMAT_ERROR|No keep= line|@end|@niu-agent"
```

- [ ] **Step E.5**：验证压缩一次性成功（回归 context-manager 修复）
**预期日志序列**（按顺序）：
1. `[Context] Proactive compress: NNNNN/NNNNNN tokens (84% > 80%)` — 触发压缩回调
2. `[Tidy] Force: context-manager completed, length=NNN` — context-manager 一次返回
3. `[Runner] Force: Parsed from content: keep=K, delete=D, update=U, cursor_idx=C` — 解析成功
4. 没有 `[AtPrefix]` 拦截日志（context-manager 绕过）
5. 没有 `[对话格式错误]` 提示
6. 没有 `No keep= line found in sub-agent reply` 异常

- [ ] **Step E.6**：验证 entity-extractor / dream-evolver / journal-agent 用 @end 退出（Task B 效果）
**预期**：这三个子 Agent 在 force/sleep 压缩流水线里，正常完成时应该输出 `@end` 前缀的回复，不被拦截器拦下重跑。
**检查日志**：
- 不应出现大量 `[AtPrefix] FORMAT_ERROR` 针对 entity-extractor / dream-evolver / journal-agent
- 这三个子 Agent 的 result 应该以 `@end` 开头（或被 `call_subagent_with_auto_answer` 的 `_strip_at_prefix` 处理后是纯结果）
**如果仍出现 FORMAT_ERROR**：说明守则改进效果不够，记录到报告里，但**不在这个计划里继续改**（超范围）

- [ ] **Step E.7**：测试完彻底杀进程（铁律 #7）
```bash
# 用 kill -TERM 优雅退出，禁止 pkill -f niu
ps aux | grep -E "niu|launcher" | grep -v grep | awk '{print $2}' | xargs -I {} kill -TERM {}
sleep 5
ps aux | grep -E "niu|launcher" | grep -v grep  # 应为空
```

---

### Task F: 提交修复

- [ ] **Step F.1**：检查改动范围
```bash
cd <repo_root>
git status
git diff agent/subagent.py config/agents/entity-extractor.md config/agents/dream-evolver.md config/agents/journal-agent.md
```
**预期**：只有四个文件改动 + 一个新测试文件 + 这个计划文件 + `tests/test_general_subagent.py`（Step B.8 把 L439/L451/L461 三处硬编码 v1 改成 `_SUBAGENT_ASK_GUIDE_MARKER` 常量断言）。

- [ ] **Step F.2**：提交修复
```bash
cd <repo_root>
git add agent/subagent.py config/agents/entity-extractor.md config/agents/dream-evolver.md config/agents/journal-agent.md tests/test_subagent_prompt_cleanup.py tests/test_general_subagent.py docs/superpowers/plans/2026-07-09-subagent-prompt-cleanup.md
git commit -m "$(cat <<'EOF'
refactor(subagent): 清理子 Agent 提示词三部分

A. agent/subagent.py: 删守则末尾补丁句"你不需要在输出里包含自己的标识符"
   — 子 Agent 原本就没有加标识符的习惯，说"不需要"反而困惑

B. agent/subagent.py: 重写守则提高 @end 退出醒目度
   - 首句命令式强提醒"任务完成时必须用 @end"
   - 重组结构：退出（默认）→ 询问（少数情况）
   - 结尾命令式总结"记住：完成用 @end，提问用 @niu-agent"
   - marker v1→v2 强制走新模板
   不改黑名单、不改拦截器，只改守则措辞和位置
   利用 LLM primacy/recency effect 让退出规则在首尾各一次强信号

C. config/agents/{entity-extractor,dream-evolver,journal-agent}.md:
   - entity-extractor L82: 删"查映射找到对应 UUID 写入游标文件"实现细节
     + "你无需报告游标位置"补丁句
   - dream-evolver L316: 删 SkillSync/watchdog/FileMovedEvent 开发者视角细节
   - dream-evolver L468: get_messages "通常不需要调用"→"禁止调用"
   - dream-evolver L476: "游标之后的新消息"→"你收到的消息即为全量消息"
   - journal-agent L26-31+L73-82: 输入格式段和游标机制段重复，合并
   - journal-agent L115: 删"游标机制（last_journal.json）"文件名细节
     + "不要重复提取同一消息中的内容"重复句

新增测试 tests/test_subagent_prompt_cleanup.py 验证新守则措辞和位置。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step F.3**：git 操作后修复文件权限（铁律 #7）
```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null
```

- [ ] **Step F.4**：验证提交成功
```bash
cd <repo_root>
git log --oneline -3
git status
```

---

## Self-Review

### 三部分独立性检查

- [x] **Task A 删补丁句**：只改 `agent/subagent.py` L84 一行，与 Task B/C 互不干扰
- [x] **Task B 重写守则**：改 `agent/subagent.py` L69-85 + marker 常量，与 Task A 的删除有交集（Task A 删的行在 Task B 重写的范围内）——**但 Task A 先做，Task B 重写时 Task A 的删除已经生效，Task B 的 old_string 是 Task A 删完后的状态**。执行顺序：Task A → Task B，Task B 的 old_string 不含补丁句（因为 Task A 已删）
- [x] **Task C 清理 .md**：改三个 .md 文件，与 Task A/B 完全独立（不同文件）
- [x] **三部分可独立回滚**：Task A 失败只回滚 L84 删除；Task B 失败只回滚守则重写；Task C 失败只回滚对应 .md 文件

### 第二部分改进方案视角检查

- [x] **从子 Agent 视角设计**：首句"任务完成时必须用 @end"是子 Agent 第一眼看到的信息；重组结构"退出（默认）→ 询问（少数）"符合子 Agent 任务实际分布；结尾"记住：完成用 @end"是子 Agent 最后看到的提醒
- [x] **不是开发者视角**：没有提"拦截器""AUTO_ANSWER""FORMAT_ERROR"等开发者概念，只说"否则会被程序拦截重跑浪费 token"（子 Agent 视角的后果）
- [x] **利用 LLM attention 机制**：primacy（首句）+ recency（结尾）effect 是 LLM 处理 prompt 的已知特性，把高频指令放首尾是符合该特性的设计

### journal-agent 双重身份检查

- [x] **程序触发场景**（task 是 `_build_journal_task()` 纯指令，history 含消息）：合并后的输入格式段说"task 是纯指令，消息以 history 形式逐条传入"+"两种场景：1.日志记录（默认）..."——通顺
- [x] **主 Agent 调用场景 - 记日志**（task 是用户原话"帮我记一条日志：完成了XXX"，history 为空）：输入格式段 L31 "如果 history 为空或 task 明确要求生成报告，按指令内容执行"——"按指令内容执行"覆盖"记日志"，通顺
- [x] **主 Agent 调用场景 - 生成报告**（task 含"周报/月报/季报/年报"，history 为空）：输入格式段场景2明确覆盖——通顺
- [x] **删除游标机制段不影响**：游标机制段的信息（assistant 含 tool_calls、tool 含 tool_call_id、未输出 processed_up_to 的兜底）已合并到输入格式段，两种场景都能看到

### 引入新 bug 的风险

- [x] **风险一：marker v1→v2 导致 `tests/test_general_subagent.py` 三个硬编码 v1 位置失效**
  - 评估：`tests/test_general_subagent.py` 有三处硬编码 v1 字符串——L439 断言（`test_build_subagent_system_segments_injects_guide_for_all_subagents`）、L451 fixture 正文（`test_build_subagent_system_segments_no_duplicate_injection`）、L461 断言（同上）。Task B 把 marker 从 v1 升级到 v2 后：L439 断言 v1 in static_system 会失败（注入的是 v2）；L451 fixture 用 v1 不匹配新 v2 判定，新代码仍注入 v2，L461 断言 v1 count == 1 表面通过但**违背测试意图**（测试意图是"已含守则不重复注入"，但新代码确实注入了 v2）
  - 结论：Step B.7 预期两个测试失败，Step B.8 统一把三处硬编码 v1 改成引用 `_SUBAGENT_ASK_GUIDE_MARKER` 常量（与 marker 值解耦，以后 marker 再升级测试也不失效）。Step B.8 是**必做步骤**，不是"失败才改"的条件步骤
- [x] **风险二：新守则措辞影响其他子 Agent 行为**
  - 评估：新守则仍含 `@niu-agent` 和 `@end` 两个前缀，拦截器逻辑不变，子 Agent 输出 `@end` 仍走 EXIT、输出 `@niu-agent` 仍走 INTERCEPTED/INTERCEPTED_SYNC、无前缀仍走 FORMAT_ERROR。**行为不变**
  - 结论：低风险，回归测试 Task D 验证
- [x] **风险三：.md 清理影响子 Agent 输出格式**
  - 评估：entity-extractor/dream-evolver/journal-agent 的 `processed_up_to=N` 输出要求保留（只是删了实现细节，没删输出要求）。compat.py / runner.py 的 `_parse_processed_up_to` 解析逻辑不变
  - 结论：低风险，Task D.2 跑 journal-agent 测试验证
- [x] **风险四：journal-agent 合并段落导致信息丢失**
  - 评估：合并时明确吸收了游标机制段的不重复信息（assistant 含 tool_calls、tool 含 tool_call_id、未输出 processed_up_to 的兜底）。重复信息（"history 形式逐条传入""processed_up_to=N"）只保留一份
  - 结论：低风险，Step C3.7 通读全文确认双重身份场景都通顺
- [x] **风险五：dream-evolver L468 "禁止调用" get_messages 导致子 Agent 无法获取消息**
  - 评估：dream-evolver 的消息已在 prompt 中提供（task prompt L2743 / runner.py 构造的 history），不需要调 get_messages。原措辞"通常不需要调用"已经是软禁止，改为硬禁止"禁止调用"只是强化
  - 结论：低风险

### 测试覆盖检查

- [x] **Task A 回归**：Step A.4/A.5 跑守则注入测试 + at_prefix 测试
- [x] **Task B TDD**：Step B.1 写 6 个测试，Step B.6 全部通过
- [x] **Task B 回归**：Step B.7/B.9 跑守则注入测试 + at_prefix + call_subagent 测试
- [x] **Task C 回归**：Step D.2 跑 journal-agent 测试
- [x] **全套回归**：Step D.1 跑 14 个 subagent 相关测试文件
- [x] **真实端到端**：Task E 用真实定时任务 + 真实 LLM 触发 84% 上下文压缩

---

## Execution Handoff

执行顺序（**严格按 Task 0 → A → B → C → D → E → F 顺序**）：

1. **Task 0**：临时备份提交（铁律 #3）
2. **Task A**：删守则末尾补丁句（最小改动，独立可回滚）
3. **Task B**：TDD 写失败测试 → 重写守则 + 升级 marker → 跑测试通过 → 回归测试
4. **Task C**：清理三个 .md（C1 entity-extractor → C2 dream-evolver → C3 journal-agent，每个独立）
5. **Task D**：跑现有测试套件回归验证（不破坏其他子 Agent）
6. **Task E**：真实端到端验证（真实定时任务 + 真实 LLM，铁律 #5）
7. **Task F**：提交修复 + 修复文件权限（铁律 #7）

**关键约束**：
- 每个 Step 都要打勾 `- [ ]` → `- [x]`
- 任何 Step 失败立即停下，不要继续
- 调试无效立即撤销改动恢复原状（铁律 #5）
- Edit 前必须 Read 确认 old_string（用户记忆 Edit Safety Rules）
- Python 编辑后立即语法检查（用户记忆 Edit Safety Rules）
- 派出去的子 Agent 必须遵守所有铁律（特别是 #3 备份、#5 真实测试、#7 修权限、#8 不 pkill）

**执行顺序的设计理由**：
- Task A 在 Task B 前：Task A 是最小改动，先做让守则末尾干净，Task B 重写时 old_string 不含补丁句
- Task B 在 Task C 前：Task B 改的是代码（subagent.py），Task C 改的是配置（.md），代码先改先验证，配置后改
- Task D 在 Task E 前：先跑单元测试回归，再跑真实端到端，避免端到端失败时不知道是代码问题还是配置问题
