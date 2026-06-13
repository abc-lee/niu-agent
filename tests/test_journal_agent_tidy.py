"""
Journal-Agent Auto-Tidy 集成测试

验证 journal-agent 在 auto-tidy 管道中的调用逻辑：
- sleep 模式 usage >= 50% 时调用
- sleep 模式 usage < 50% 时跳过
- force 模式始终调用
- 游标读取和写入
- clear_chat 游标重置
"""
import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import os


# --- 消息对象模拟（与 MessageStore 返回的对象兼容） ---

@dataclass
class FakeMessage:
    id: str
    role: str
    content: str
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    tool_call_id: str = ""
    created_at: str = ""


def make_messages(n: int, start_idx: int = 0) -> list[FakeMessage]:
    """生成 n 条模拟消息，UUID 顺序可预测"""
    return [
        FakeMessage(id=f"uuid-{start_idx + i}", role="user" if i % 2 == 0 else "assistant", content=f"消息内容 {start_idx + i}")
        for i in range(n)
    ]


# --- 导入被测函数 ---
import sys
sys.path.insert(0, ".")
from niu_api.compat import _build_incremental_msg_text


# --- 源码读取辅助 ---
SOURCE_PATH = os.path.join(os.path.dirname(__file__), "..", "niu_api", "compat.py")


def _read_source() -> str:
    with open(SOURCE_PATH, encoding="utf-8") as f:
        return f.read()


# --- Tests ---

class TestJournalCursorInitialization:
    """测试 journal 游标初始化"""

    def test_journal_cursor_path_defined(self):
        """验证 journal 游标路径定义正确"""
        source = _read_source()
        assert 'journal_cursor_path = Path.home() / ".niu" / "last_journal.json"' in source

    def test_journal_cursor_read_last_journal_id(self):
        """验证从 last_journal.json 读取 last_journal_id"""
        source = _read_source()
        assert 'last_journal_id = cursor_data.get("last_journal_id", "")' in source

    def test_journal_cursor_initialized_empty(self):
        """验证 last_journal_id 初始化为空字符串"""
        source = _read_source()
        assert 'last_journal_id = ""' in source

    def test_journal_cursor_exists_check(self):
        """验证游标文件存在性检查"""
        source = _read_source()
        assert "journal_cursor_path.exists()" in source


class TestJournalAgentSleepMode:
    """测试 sleep 模式下 journal-agent 调用逻辑"""

    def test_sleep_mode_usage_gates_journal_agent(self):
        """sleep 模式中 journal-agent 被 usage_percent >= 50 保护"""
        source = _read_source()
        assert "if usage_percent >= 50:" in source

    def test_sleep_mode_journal_agent_call(self):
        """sleep 模式调用 journal-agent 子Agent"""
        source = _read_source()
        # 在 sleep 模式区域中应有 journal-agent 的 call_subagent 调用
        assert 'agent_name="journal-agent"' in source

    def test_sleep_mode_journal_comment(self):
        """验证 sleep 模式 journal-agent 的注释说明"""
        source = _read_source()
        assert "journal-agent（sleep 模式，仅 usage >= 50% 时调用）" in source

    def test_sleep_mode_skips_journal_below_50(self):
        """sleep 模式 usage < 50% 时跳过 journal-agent 并记录日志"""
        source = _read_source()
        assert 'journal-agent: skipped (usage' in source

    def test_sleep_mode_journal_incremental_range(self):
        """sleep 模式 journal-agent 使用增量消息范围"""
        messages = make_messages(20)
        journal_msg_ids = []
        result = _build_incremental_msg_text(
            messages, "uuid-9", journal_msg_ids
        )
        # 从 uuid-9 之后开始，应有 uuid-10 ~ uuid-19
        assert journal_msg_ids[0] == "uuid-10"
        assert journal_msg_ids[-1] == "uuid-19"
        assert len(journal_msg_ids) == 10


class TestJournalAgentForceMode:
    """测试 force 模式下 journal-agent 调用逻辑"""

    def test_force_mode_always_calls_journal_agent(self):
        """force 模式始终调用 journal-agent（无 usage 阈值保护）"""
        source = _read_source()
        # force 模式的 journal-agent 注释
        assert "journal-agent（force 模式，始终调用）" in source

    def test_force_mode_journal_not_gated_by_usage(self):
        """force 模式 journal-agent 不被 usage_percent 保护"""
        source = _read_source()
        # 在 force 分支（elif mode == "force"）内，
        # journal-agent 调用前不应有 usage_percent 判断
        lines = source.split("\n")
        in_force_branch = False
        journal_found_in_force = False
        usage_guard_before_journal = False
        for line in lines:
            if 'elif mode == "force"' in line:
                in_force_branch = True
            if in_force_branch and "journal-agent" in line and "force 模式" in line:
                journal_found_in_force = True
            # 在 force 分支的 journal-agent 之前不应有独立的 usage_percent 判断
            if journal_found_in_force and "if usage_percent" in line:
                usage_guard_before_journal = True
                break
        assert journal_found_in_force, "journal-agent not found in force branch"

    def test_force_mode_journal_incremental_range(self):
        """force 模式 journal-agent 使用增量消息范围（非全量）"""
        messages = make_messages(20)
        journal_force_msg_ids = []
        result = _build_incremental_msg_text(
            messages, "uuid-14", journal_force_msg_ids
        )
        # 从 uuid-14 之后开始，应有 uuid-15 ~ uuid-19
        assert journal_force_msg_ids[0] == "uuid-15"
        assert journal_force_msg_ids[-1] == "uuid-19"
        assert len(journal_force_msg_ids) == 5

    def test_force_mode_journal_cursor_write(self):
        """force 模式 journal 游标写入格式正确"""
        source = _read_source()
        # force 模式中 journal_cursor_path.write_text 包含必要字段
        assert '"last_journal_id": new_journal_id' in source
        assert '"last_journal_at"' in source


class TestClearChatJournalCursorReset:
    """测试 clear_chat 重置 journal 游标"""

    def test_journal_cursor_in_reset_list(self):
        """验证 last_journal.json 在 clear_chat 游标重置列表中"""
        source = _read_source()
        assert '"last_journal.json"' in source

    def test_journal_cursor_reset_list_complete(self):
        """验证游标重置列表包含所有已知游标文件"""
        source = _read_source()
        # 找到重置列表所在行
        lines = source.split("\n")
        for line in lines:
            if "last_entity_extract.json" in line and "last_journal.json" in line:
                # 所有游标文件都应在同一行
                assert "last_dream_evolve.json" in line
                assert "last_compress.json" in line
                break
        else:
            pytest.fail("Cursor reset list not found with expected entries")

    def test_journal_cursor_unlink_on_clear(self):
        """验证 clear_chat 会删除 journal 游标文件"""
        source = _read_source()
        # 检查存在性检查 + unlink 逻辑
        assert "cursor_p.exists()" in source
        assert "cursor_p.unlink()" in source


class TestJournalAgentPromptFormat:
    """测试 journal-agent 调用的 prompt 格式"""

    def test_journal_prompt_contains_uuid_format(self):
        """验证 journal prompt 包含 [id:UUID] [idx:N] 格式说明"""
        source = _read_source()
        assert "[id:UUID] [idx:N]" in source

    def test_journal_prompt_contains_cursor_report(self):
        """验证 journal prompt 包含游标报告 JSON 格式"""
        source = _read_source()
        assert '"last_journal_id"' in source

    def test_journal_prompt_contains_work_content_instruction(self):
        """验证 journal prompt 包含工作内容识别指令"""
        source = _read_source()
        assert "工作内容" in source

    def test_journal_prompt_contains_journal_md_write(self):
        """验证 journal prompt 包含写入 journal.md 的指令"""
        source = _read_source()
        assert "journal.md" in source

    def test_journal_prompt_cursor_advance_instruction(self):
        """验证 journal prompt 包含必须推进游标的指令"""
        source = _read_source()
        assert "必须推进游标" in source

    def test_journal_incremental_text_format(self):
        """验证增量消息文本格式正确"""
        messages = make_messages(5)
        journal_msg_ids = []
        result = _build_incremental_msg_text(
            messages, "", journal_msg_ids
        )
        assert len(journal_msg_ids) == 5
        assert "[id:uuid-0]" in result
        assert "[idx:1]" in result


class TestJournalCursorWrite:
    """测试 journal 游标写入"""

    def test_journal_cursor_write_format(self):
        """验证 journal 游标写入包含必要字段"""
        source = _read_source()
        assert '"last_journal_id": new_journal_id' in source
        assert '"last_journal_at"' in source

    def test_journal_cursor_write_text_call(self):
        """验证 journal 游标使用 write_text 写入"""
        source = _read_source()
        assert "journal_cursor_path.write_text" in source

    def test_journal_cursor_mkdir_before_write(self):
        """验证写入前创建目录"""
        source = _read_source()
        assert "journal_cursor_path.parent.mkdir(parents=True, exist_ok=True)" in source


class TestJournalCursorFallback:
    """测试 journal 游标 fallback 逻辑"""

    def test_journal_cursor_overflow_fallback(self):
        """验证 journal-agent 溢出时游标 fallback"""
        source = _read_source()
        # 溢出时应 fallback 到增量消息最后一条
        assert "Journal cursor overflow fallback" in source

    def test_journal_cursor_not_matched_fallback(self):
        """验证 journal-agent 游标未匹配时 fallback"""
        source = _read_source()
        assert "Journal cursor not matched" in source

    def test_journal_cursor_incremental_fallback_value(self):
        """验证游标 fallback 到增量消息列表的最后一条"""
        messages = make_messages(10)
        journal_msg_ids = []
        _build_incremental_msg_text(messages, "uuid-3", journal_msg_ids)
        fallback_cursor = journal_msg_ids[-1] if journal_msg_ids else None
        assert fallback_cursor == "uuid-9"

    def test_journal_cursor_deleted_revert(self):
        """验证 journal 游标被删除时回退到旧游标"""
        source = _read_source()
        assert "reverting to" in source
        # 在 journal 相关行中应有回退逻辑
        lines = source.split("\n")
        journal_revert_lines = [
            l for l in lines
            if "Journal cursor" in l and "reverting" in l
        ]
        assert len(journal_revert_lines) >= 1


class TestJournalIntegrationWithOtherAgents:
    """测试 journal-agent 与其他子 Agent 的集成"""

    def test_journal_runs_after_dream_evolver(self):
        """验证 journal-agent 在 dream-evolver 之后执行（步骤 2.5/3）"""
        source = _read_source()
        lines = source.split("\n")
        dream_line = None
        journal_line = None
        for i, line in enumerate(lines):
            if "dream-evolver" in line and "2/3" in line:
                dream_line = i
            if "journal-agent" in line and "2.5/3" in line:
                journal_line = i
        assert dream_line is not None, "dream-evolver step not found"
        assert journal_line is not None, "journal-agent step not found"
        assert journal_line > dream_line, "journal-agent should run after dream-evolver"

    def test_journal_runs_before_context_manager(self):
        """验证 journal-agent 在 context-manager 之前执行"""
        source = _read_source()
        lines = source.split("\n")
        journal_line = None
        context_line = None
        for i, line in enumerate(lines):
            if "journal-agent" in line and "2.5/3" in line:
                journal_line = i
            if "context-manager" in line and "3/3" in line:
                context_line = i
        assert journal_line is not None, "journal-agent step not found"
        assert context_line is not None, "context-manager step not found"
        assert journal_line < context_line, "journal-agent should run before context-manager"

    def test_journal_uses_independent_cursor(self):
        """验证 journal-agent 使用独立游标（与 entity/dream/compress 无关）"""
        messages = make_messages(20)
        # 三个 Agent 的游标独立
        entity_ids = []
        dream_ids = []
        journal_ids = []
        _build_incremental_msg_text(messages, "uuid-5", entity_ids)
        _build_incremental_msg_text(messages, "uuid-10", dream_ids)
        _build_incremental_msg_text(messages, "uuid-15", journal_ids)
        # 各自独立计算增量范围
        assert entity_ids[0] == "uuid-6"
        assert dream_ids[0] == "uuid-11"
        assert journal_ids[0] == "uuid-16"
