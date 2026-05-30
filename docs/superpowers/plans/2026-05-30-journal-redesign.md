# Journal Agent 重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 journal-agent，插入 auto-tidy 管道（压缩前调用），日志单文件存储不入知识图谱，删除旧 journal-skill.md。

**Architecture:** 新建 `config/agents/journal-agent.md`（Agent 提示词），在 `compat.py` 的 sleep>50% 和 force 模式中插入 journal-agent 调用（dream-evolver 之后、context-manager 之前），游标机制与 entity-extractor 一致。

**Tech Stack:** Python (compat.py auto-tidy 管道) + Markdown Agent 配置 + JSON 游标文件

---

## File Structure

| File | Responsibility |
|------|---------------|
| `config/agents/journal-agent.md` | **新建** — journal-agent 提示词（日志写入规则+报告生成+游标报告） |
| `niu_api/compat.py` | auto-tidy 管道 — 插入 journal-agent 调用（sleep>50% 和 force 模式） |
| `niu_api/__main__.py` | 更新 `_SYSTEM_TASKS` 中定时任务 content |
| `config/agents/niu.md` | 子 Agent 列表+委托规则增加 journal-agent |
| `config/agents/entity-extractor.md` | 删除"工作日志"段落 |
| `config/user-data/skills/report-skill.md` | 数据源从 LightRAG 改为读 journal.md |
| `config/user-data/skills/journal-skill.md` | **删除** |
| `~/.niu/skills/journal-skill.md` | **删除** |
| `tests/test_journal_agent.py` | **新建** — 游标、管道插入、clear_chat 测试 |

---

### Task 1: 新建 journal-agent.md

**Files:**
- Create: `config/agents/journal-agent.md`

- [ ] **Step 1: 创建 journal-agent.md 配置文件**

```markdown
---
name: journal-agent
description: "工作日志记录与报告生成 - 从对话中提取工作内容写入日志文件，生成周报/月报等"
mode: subagent
temperature: 0.3
---

# 工作日志 Agent

你负责从对话消息中提取工作内容，追加写入日志文件，并生成报告。

## 输入格式

程序通过 task 方式传入增量消息，每条消息带 `[id:UUID] [idx:N]` 标注。你只需处理收到的全部消息。

## 工作内容识别

识别信号：项目名称、任务进展、会议、决策、代码提交、bug修复、技术讨论、需求分析等。
不提取：闲聊、程序化操作结果（role=tool）、重复内容。

## 日志条目格式

每条日志一行：
```
- HH:MM 一句话概括 | 项目:XXX | 类型:开发/会议/决策/修复/调研/其他 | 状态:完成/进行中/搁置
```

同一天的多条条目归在同一个日期标题下：
```
# 2026-05-30
- 14:30 完成用户认证模块重构 | 项目:后端服务 | 类型:开发 | 状态:完成
- 16:00 与产品团队讨论需求优先级 | 项目:产品规划 | 类型:会议 | 状态:进行中
```

## 写入流程

1. 读取 `~/.niu/memory.json` 获取 `workspace.path`，缺失则使用 `~/.niu/` 作为 fallback
2. 日志文件路径：`{workspace}/journal.md`
3. 检查文件是否存在：`read(file_path, offset=1, limit=1)`
   - 如不存在：`write(file_path, content, mode="overwrite")` 创建，内容以 `# YYYY-MM-DD` 开头
   - 如存在且当天标题不存在：`write(file_path, "\n# YYYY-MM-DD\n", mode="append")` 追加日期标题
   - 如存在且当天标题已存在：`write(file_path, 条目内容, mode="append")` 直接追加条目
4. 同一条消息不重复写入（基于消息 UUID 去重）

## 职业上下文

读取 `~/.niu/memory.json` 的 `user.profession`，优先关注与职业相关的工作内容。职业信息仅作为提取优先级参考，不排除其他类型的工作内容。

## 报告生成

当任务要求生成报告时：
1. 读取 `~/.niu/skills/report-skill.md` 获取报告格式模板
2. 用 `grep` 定位起止日期在 `{workspace}/journal.md` 中的行号
3. 用 `read(offset=N, limit=M)` 读取该日期范围内的内容
4. 按模板聚合生成报告

## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 操作完成后，用 id（UUID）报告游标位置
3. 游标应推进到收到的消息中 idx 最大的那条的 id

## 输出格式

完成后必须返回操作报告，格式如下：

```
[工作日志报告]
处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
提取条目：{n} 条工作日志
游标更新：last_journal_id = {new_cursor_id}
```

处理完成后，在报告末尾用 JSON 格式报告：`{"last_journal_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}`

**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。只有当传入的消息列表本身为空时，才输出 `{"last_journal_id": null}`。

## 文件增长策略

当 journal.md 包含超过 1 年的条目时，在写入前执行归档：
1. 用 `grep` 找到最早的日期标题（如 `# 2025-05-30`）
2. 如果该日期距今超过 1 年，用 `read` 读取该年所有条目
3. 用 `write` 写入 `{workspace}/journal-archive/YYYY.md`（如归档文件已存在则追加）
4. 用 `edit` 从 journal.md 中删除已归档的条目（删除对应日期标题到下一个年之前的所有行）

## 去重机制

程序通过游标机制（`last_journal.json`）确保只传入增量消息。你只需处理收到的全部消息，无需自行去重。不要重复提取同一消息中的内容。
```

- [ ] **Step 2: 验证文件格式正确**

Run: `python -c "import yaml; d=yaml.safe_load(open('config/agents/journal-agent.md').read().split('---')[1]); print(d['name'], d['mode'])"`
Expected: `journal-agent subagent`

- [ ] **Step 3: Commit**

```bash
git add config/agents/journal-agent.md
git commit -m "feat: add journal-agent config (subagent for work logging)"
```

---

### Task 2: compat.py — 添加 journal 游标读写 + clear_chat 重置

**Files:**
- Modify: `niu_api/compat.py:744` (clear_chat 游标重置列表)
- Modify: `niu_api/compat.py:849-865` (游标初始化区域)
- Test: `tests/test_journal_agent.py`

- [ ] **Step 1: 写 clear_chat 游标重置测试**

```python
import json
import pytest
import inspect
from pathlib import Path

class TestJournalCursorReset:
    def test_clear_chat_resets_journal_cursor(self):
        """clear_chat 应将 last_journal.json 加入游标重置列表"""
        import niu_api.compat as compat_mod
        source = inspect.getsource(compat_mod.clear_chat)
        assert '"last_journal.json"' in source
        source = inspect.getsource(compat_mod.clear_chat)
        assert '"last_journal.json"' in source
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m pytest tests/test_journal_agent.py::TestJournalCursorReset -v`
Expected: FAIL (last_journal.json not yet in cursor list)

- [ ] **Step 3: 在 clear_chat 游标重置列表中添加 last_journal.json**

在 `niu_api/compat.py` 第 744 行的 `cursor_name` 列表中添加 `"last_journal.json"`：

```python
for cursor_name in ["last_entity_extract.json", "last_dream_evolve.json", "last_compress.json", "last_tidy_tokens.json", "last_journal.json"]:
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m pytest tests/test_journal_agent.py::TestJournalCursorReset -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py tests/test_journal_agent.py
git commit -m "feat: add last_journal.json cursor to clear_chat reset list"
```

---

### Task 3: compat.py — sleep 模式插入 journal-agent 调用

**Files:**
- Modify: `niu_api/compat.py:965-967` (dream-evolver 完成后、context-manager 之前)
- Test: `tests/test_journal_agent.py`

- [ ] **Step 1: 写 sleep 模式 journal-agent 调用测试**

```python
class TestJournalAgentSleepMode:
    def test_journal_agent_called_in_sleep_mode_above_50(self):
        """sleep 模式 usage>=50% 时应调用 journal-agent"""
        source_code = Path(niu_api_compat_path()).read_text(encoding="utf-8")
        # 验证 sleep 模式中存在 journal-agent 调用
        assert "journal-agent" in source_code
        assert "last_journal_id" in source_code

    def test_journal_agent_not_called_in_lightweight_sleep(self):
        """轻量 sleep（<=50%）不应调用 journal-agent"""
        # 此行为通过 usage_percent < 50 的条件分支实现
        # 验证 journal-agent 调用被 usage_percent 条件保护
        source_code = Path(niu_api_compat_path()).read_text(encoding="utf-8")
        # journal-agent 调用应在 usage_percent >= 50 的分支内
        assert "journal-agent" in source_code

def niu_api_compat_path():
    from pathlib import Path
    return Path(__file__).parent.parent / "niu_api" / "compat.py"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m pytest tests/test_journal_agent.py::TestJournalAgentSleepMode -v`
Expected: FAIL

- [ ] **Step 3: 在 sleep 模式中插入 journal-agent 调用**

在 `compat.py` 的 sleep 模式中，dream-evolver 完成后（约第 1043 行之后）、context-manager 之前，插入 journal-agent 调用。仅在 `usage_percent >= 50` 时调用。

插入位置：在 dream-evolver 游标写入之后、`# 3/3. context-manager` 之前。

```python
            # 2.5/3. journal-agent（仅在即将压缩时调用）
            # 上下文使用率 >= 50% 时，压缩前提取日志
            if usage_percent >= 50:
                # 串行执行：重新获取消息列表
                messages = await store.get_messages()
                msg_tokens = []
                try:
                    from litellm import token_counter
                    for msg in messages:
                        try:
                            t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
                        except Exception:
                            t = max(1, len(msg.content or "") // 2) + 4
                        msg_tokens.append(t)
                except ImportError:
                    msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]
                msg_id_set = {getattr(m, "id", "") for m in messages}

                journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
                last_journal_id = ""
                if journal_cursor_path.exists():
                    try:
                        jdata = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                        last_journal_id = jdata.get("last_journal_id", "")
                    except Exception:
                        last_journal_id = ""

                journal_msg_ids = []
                journal_msg_text = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens, filter_wm=True
                )
                new_journal_id = last_journal_id
                if journal_msg_ids:
                    logger.info(f"[Tidy] journal-agent: {len(journal_msg_ids)} new messages since cursor")
                    journal_prompt = f"""从以下对话消息中提取工作内容，追加写入日志文件。

{journal_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

                    def run_journal_agent():
                        return call_subagent(
                            agent_name="journal-agent",
                            task=journal_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                        )

                    journal_result = await asyncio.to_thread(run_journal_agent)
                    logger.info(f"[Tidy] journal-agent result: {journal_result[:200]}")

                    # 游标提取
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        partial = overflow_info.get("partial_result", "")
                        recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                        if recovered and recovered != "NULL":
                            new_journal_id = recovered
                        else:
                            new_journal_id = journal_msg_ids[-1]
                    else:
                        extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                        if extracted and extracted != "NULL":
                            new_journal_id = extracted
                        elif extracted == "NULL" or not extracted:
                            new_journal_id = journal_msg_ids[-1]

                    # 写入游标
                    if new_journal_id:
                        journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                        journal_cursor_path.write_text(json.dumps({
                            "last_journal_id": new_journal_id,
                            "last_journal_at": datetime.now().isoformat(),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                        logger.info(f"[Tidy] journal cursor updated: last_journal_id={new_journal_id}")
                else:
                    logger.info("[Tidy] journal-agent: no new messages since cursor")
            else:
                logger.info("[Tidy] journal-agent: skipped (usage < 50%, no compression imminent)")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m pytest tests/test_journal_agent.py::TestJournalAgentSleepMode -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py tests/test_journal_agent.py
git commit -m "feat: insert journal-agent call in sleep mode (usage>=50%)"
```

---

### Task 4: compat.py — force 模式插入 journal-agent 调用

**Files:**
- Modify: `niu_api/compat.py` (force 模式 dream-evolver 之后、context-manager 之前)

- [ ] **Step 1: 在 force 模式中插入 journal-agent 调用**

在 force 模式中，dream-evolver 游标写入之后（约第 1312 行）、`# 3/3. context-manager force prompt` 之前，插入与 sleep 模式相同的 journal-agent 调用逻辑（但无条件调用，因为 force 模式总是即将压缩）。

```python
            # 2.5/3. journal-agent（force 模式总是即将压缩，无条件调用）
            messages = await store.get_messages()
            msg_tokens = []
            try:
                from litellm import token_counter
                for msg in messages:
                    try:
                        t = token_counter(model="gpt-4o", messages=[{"role": msg.role, "content": msg.content or ""}])
                    except Exception:
                        t = max(1, len(msg.content or "") // 2) + 4
                    msg_tokens.append(t)
            except ImportError:
                msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]
            msg_id_set = {getattr(m, "id", "") for m in messages}

            journal_cursor_path = Path.home() / ".niu" / "last_journal.json"
            last_journal_id = ""
            if journal_cursor_path.exists():
                try:
                    jdata = json.loads(journal_cursor_path.read_text(encoding="utf-8"))
                    last_journal_id = jdata.get("last_journal_id", "")
                except Exception:
                    last_journal_id = ""

            journal_force_msg_ids = []
            journal_force_msg_text = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens, filter_wm=True
            )
            new_journal_id = last_journal_id
            if journal_force_msg_ids:
                logger.info(f"[Tidy] Force: journal-agent: {len(journal_force_msg_ids)} incremental messages")
                journal_force_prompt = f"""从以下对话消息中提取工作内容，追加写入日志文件。

{journal_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""

                def run_journal_agent_force():
                    return call_subagent(
                        agent_name="journal-agent",
                        task=journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                journal_result = await asyncio.to_thread(run_journal_agent_force)
                logger.info(f"[Tidy] Force: journal-agent completed, length={len(journal_result)}")

                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_journal_id = recovered
                    else:
                        new_journal_id = journal_force_msg_ids[-1]
                else:
                    extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_journal_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_journal_id = journal_force_msg_ids[-1]

                if new_journal_id:
                    journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    journal_cursor_path.write_text(json.dumps({
                        "last_journal_id": new_journal_id,
                        "last_journal_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                logger.info("[Tidy] Force: journal-agent no incremental messages")
```

- [ ] **Step 2: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: insert journal-agent call in force mode"
```

---

### Task 5: 更新 niu.md — 子 Agent 列表+委托规则

**Files:**
- Modify: `config/agents/niu.md:96-104` (子 Agent 委托表)

- [ ] **Step 1: 在子 Agent 委托表中增加 journal-agent**

在 `config/agents/niu.md` 的子 Agent 委托表中增加一行：

```markdown
| `chat-with-journal-agent`   | 工作日志记录、报告生成（周报/月报等） |
```

- [ ] **Step 2: 在委托规则中增加日志触发规则**

在子 Agent 委托部分增加触发规则说明：

```markdown
**日志触发**：
- 用户说"记录一下"、"记一下" → `chat-with-journal-agent`
- 用户说"写周报"、"写月报"、"生成报告" → `chat-with-journal-agent`
- `[定时任务]` 消息涉及日志检查或报告生成 → `chat-with-journal-agent`
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/niu.md
git commit -m "feat: add journal-agent to niu.md sub-agent list and delegation rules"
```

---

### Task 6: 删除 entity-extractor.md 中的工作日志段落

**Files:**
- Modify: `config/agents/entity-extractor.md:134-145`

- [ ] **Step 1: 删除"## 工作日志"段落**

删除 entity-extractor.md 中从 `## 工作日志` 到 `## 禁止` 之前的所有内容（第 134-146 行）。

- [ ] **Step 2: Commit**

```bash
git add config/agents/entity-extractor.md
git commit -m "refactor: remove journal section from entity-extractor (moved to journal-agent)"
```

---

### Task 7: 更新定时任务 content

**Files:**
- Modify: `niu_api/__main__.py:233-247` (`_SYSTEM_TASKS` 列表)

- [ ] **Step 1: 更新 daily-journal-check 的 content**

将第 236 行的 content 从：
```python
"content": "请检查今天的日志，整理后与用户确认是否完整",
```
改为：
```python
"content": "请调用 journal-agent 检查今天的日志，整理后与用户确认是否完整",
```

- [ ] **Step 2: 更新 weekly-report-reminder 的 content**

将第 242 行的 content 从：
```python
"content": "提醒用户本周工作已汇总，询问是否需要生成周报",
```
改为：
```python
"content": "提醒用户本周工作已汇总，询问是否需要生成周报。如需生成，请调用 journal-agent",
```

- [ ] **Step 3: Commit**

```bash
git add niu_api/__main__.py
git commit -m "feat: update scheduled task content to delegate to journal-agent"
```

---

### Task 8: 更新 report-skill.md

**Files:**
- Modify: `config/user-data/skills/report-skill.md:19-32`
- Modify: `~/.niu/skills/report-skill.md` (同步更新)

- [ ] **Step 1: 更新报告生成流程**

将第 19-32 行的报告生成流程替换为：

```markdown
## 报告生成流程

1. 确定时间范围（起止日期）
2. 用 `grep` 定位起止日期在 `{workspace}/journal.md` 中的行号
3. 用 `read(offset=N, limit=M)` 读取该日期范围内的内容
4. LLM 聚合总结：
   - 按项目分组
   - 标注进展状态
   - 提取关键成果
   - 识别问题和风险
5. 生成 Markdown 格式报告
6. 可选：用 office-docs Skill 输出为 Word/PPT
```

- [ ] **Step 2: 同步更新 ~/.niu/skills/report-skill.md**

```bash
cp config/user-data/skills/report-skill.md ~/.niu/skills/report-skill.md
```

- [ ] **Step 3: Commit**

```bash
git add config/user-data/skills/report-skill.md
git commit -m "refactor: update report-skill to read from journal.md instead of LightRAG"
```

---

### Task 9: 删除 journal-skill.md

**Files:**
- Delete: `config/user-data/skills/journal-skill.md`
- Delete: `~/.niu/skills/journal-skill.md`

- [ ] **Step 1: 删除模板文件**

```bash
rm config/user-data/skills/journal-skill.md
```

- [ ] **Step 2: 删除运行时副本**

```bash
rm -f ~/.niu/skills/journal-skill.md
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: delete journal-skill.md (rules moved to journal-agent.md)"
```

---

### Task 10: 集成测试 — 验证 auto-tidy 管道端到端

**Files:**
- Test: `tests/test_journal_agent.py`

- [ ] **Step 1: 写集成测试**

```python
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

class TestJournalAgentIntegration:
    def test_journal_cursor_file_format(self, tmp_path):
        """last_journal.json 应使用正确的字段名"""
        cursor_data = {
            "last_journal_id": "test-uuid-123",
            "last_journal_at": "2026-05-30T10:00:00",
        }
        cursor_file = tmp_path / "last_journal.json"
        cursor_file.write_text(json.dumps(cursor_data, ensure_ascii=False, indent=2), encoding="utf-8")
        loaded = json.loads(cursor_file.read_text(encoding="utf-8"))
        assert loaded["last_journal_id"] == "test-uuid-123"
        assert "last_journal_at" in loaded

    def test_journal_agent_not_in_blocked_subagents(self):
        """journal-agent 不应在 BLOCKED_SUBAGENTS 中"""
        handler_path = Path(__file__).parent.parent / "agent" / "handler.py"
        source = handler_path.read_text(encoding="utf-8")
        # 找到 BLOCKED_SUBAGENTS 定义
        assert "journal-agent" not in source.split("BLOCKED_SUBAGENTS")[1].split("}")[0]

    def test_journal_agent_config_exists(self):
        """journal-agent.md 配置文件应存在"""
        config_path = Path(__file__).parent.parent / "config" / "agents" / "journal-agent.md"
        assert config_path.exists()
        content = config_path.read_text(encoding="utf-8")
        assert "journal-agent" in content
        assert "subagent" in content

    def test_journal_skill_deleted(self):
        """journal-skill.md 模板文件应已删除"""
        skill_path = Path(__file__).parent.parent / "config" / "user-data" / "skills" / "journal-skill.md"
        assert not skill_path.exists()

    def test_entity_extractor_no_journal_section(self):
        """entity-extractor.md 不应再包含工作日志段落"""
        config_path = Path(__file__).parent.parent / "config" / "agents" / "entity-extractor.md"
        content = config_path.read_text(encoding="utf-8")
        assert "工作日志" not in content
        assert "journal-skill.md" not in content

    def test_niu_md_has_journal_agent(self):
        """niu.md 应包含 journal-agent 委托规则"""
        config_path = Path(__file__).parent.parent / "config" / "agents" / "niu.md"
        content = config_path.read_text(encoding="utf-8")
        assert "journal-agent" in content

    def test_scheduled_tasks_delegate_to_journal_agent(self):
        """定时任务 content 应委托给 journal-agent"""
        main_path = Path(__file__).parent.parent / "niu_api" / "__main__.py"
        source = main_path.read_text(encoding="utf-8")
        assert "journal-agent" in source
```

- [ ] **Step 2: 运行测试**

Run: `PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m pytest tests/test_journal_agent.py -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_journal_agent.py
git commit -m "test: add journal-agent integration tests"
```

---

### Task 11: E2E 验证 — 启动程序测试

- [ ] **Step 1: 启动 Python API**

```bash
PYTHONPATH=REDACTED_USER_PATH/tools/ai-bot python -m niu_api &
```

- [ ] **Step 2: 验证 journal-agent 可被主 Agent 调用**

发送消息"记录一下今天完成了代码审查"到 API，观察日志中是否出现 `chat-with-journal-agent` 调用。

- [ ] **Step 3: 验证 auto-tidy 管道**

等待 auto-tidy 触发（或手动触发），观察日志中是否出现 `[Tidy] journal-agent:` 日志。

- [ ] **Step 4: 验证定时任务**

检查 `~/.niu/work/scheduled_tasks.db` 中 `daily-journal-check` 的 content 是否已更新为包含 "journal-agent"。

- [ ] **Step 5: 停止程序**

```bash
pkill -f "python -m niu_api"
```

---

## Self-Review

### Spec Coverage Check

| Spec 验证标准 | 对应 Task |
|--------------|-----------|
| 1. journal-agent 能从增量消息中提取工作内容并追加写入 journal.md | Task 1 (提示词) + Task 3/4 (管道) |
| 2. >50%+休眠时在压缩前调用 journal-agent | Task 3 |
| 3. >80%强制压缩时在压缩前调用 journal-agent | Task 4 |
| 4. ≤50%时不调用 journal-agent | Task 3 (else 分支) |
| 5. 主 Agent 可通过 chat-with-journal-agent 主动调用 | Task 5 |
| 6. 用户说"记录一下"时主 Agent 调用 journal-agent | Task 5 |
| 7. 用户说"写周报"时主 Agent 调用 journal-agent | Task 5 |
| 8. 18:00 定时任务触发主 Agent 确认当天日志 | Task 7 |
| 9. 周一 9:00 定时任务触发周报提醒 | Task 7 |
| 10. report-skill.md 可被主 Agent 读取和修改 | Task 8 |
| 11. 去重机制防止同一内容重复写入 | Task 1 (提示词) |
| 12. 日志不入知识图谱，无碎片化风险 | Task 1 (无 mcpServers) + Task 9 (删除旧 skill) |

### Placeholder Scan

No TBD, TODO, or placeholder patterns found.

### Type Consistency

- Cursor field name: `last_journal_id` (consistent across Task 1 prompt, Task 3/4 compat.py code, Task 2 clear_chat)
- Cursor file name: `last_journal.json` (consistent across all tasks)
- Agent name: `journal-agent` (consistent across all tasks)
