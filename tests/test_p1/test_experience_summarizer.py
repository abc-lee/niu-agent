"""P1-2: 测试 ExperienceSummarizer 集成"""
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="ExperienceSummarizer disabled — skill writing now handled by dream-evolver")

sys.path.insert(0, "E:/tools/ai-bot")

from agent.experience_summarizer import ExperienceContext, ExperienceSummarizer, ToolExecution


@pytest.mark.p1
class TestExperienceSummarizer:
    """测试 ExperienceSummarizer 功能"""

    @pytest.fixture
    def summarizer(self):
        """创建 ExperienceSummarizer 实例"""
        return ExperienceSummarizer()

    @pytest.fixture
    def temp_skills_dir(self):
        """创建临时 skills 目录"""
        temp_dir = Path(tempfile.mkdtemp())
        skills_dir = temp_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # 临时修改 SKILLS_DIR
        original_dir = ExperienceSummarizer.SKILLS_DIR
        ExperienceSummarizer.SKILLS_DIR = skills_dir

        yield skills_dir

        # 恢复
        ExperienceSummarizer.SKILLS_DIR = original_dir

        # 清理
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_experience_context_creation(self):
        """测试 ExperienceContext 创建"""
        context = ExperienceContext(
            user_input="Test task",
            turn_count=5
        )

        assert context.user_input == "Test task"
        assert context.turn_count == 5
        assert len(context.tool_executions) == 0

    def test_tool_execution_creation(self):
        """测试 ToolExecution 创建"""
        tool_exec = ToolExecution(
            tool_name="test_tool",
            args={"param": "value"},
            result="Success",
            success=True
        )

        assert tool_exec.tool_name == "test_tool"
        assert tool_exec.success is True
        assert tool_exec.timestamp > 0

    def test_should_summarize_high_turns(self, summarizer):
        """测试高轮次触发总结"""
        context = ExperienceContext(
            user_input="Complex task",
            turn_count=15,
            tool_executions=[
                ToolExecution("tool1", {}, "ok", True),
                ToolExecution("tool2", {}, "ok", True),
                ToolExecution("tool3", {}, "ok", True),
            ],
            final_result="Task completed successfully"
        )

        should, reason = summarizer.should_summarize(context)

        assert should is True, "Should summarize with high turns"
        assert "high_turn_count" in reason or "multi_tool" in reason

    def test_should_summarize_task_success(self, summarizer):
        """测试任务成功触发总结"""
        context = ExperienceContext(
            user_input="Simple task",
            turn_count=12,
            tool_executions=[
                ToolExecution("code_run", {}, "Success", True),
                ToolExecution("write", {}, "OK", True),
            ],
            final_result="Task done"
        )

        should, reason = summarizer.should_summarize(context)

        assert should is True, "Should summarize with task success"
        assert "task_success" in reason or "key_tool_success" in reason

    def test_should_not_summarize_low_complexity(self, summarizer):
        """测试低复杂度不触发总结"""
        context = ExperienceContext(
            user_input="Simple question",
            turn_count=2,
            tool_executions=[],  # 没有工具调用
            final_result="Answer provided"
        )

        should, reason = summarizer.should_summarize(context)

        assert should is False, "Should not summarize simple interaction with no tool calls"

    def test_summarize_and_write(self, summarizer, temp_skills_dir):
        """测试生成并写入 Skill"""
        context = ExperienceContext(
            user_input="Test task for skill generation",
            turn_count=12,
            tool_executions=[
                ToolExecution("code_run", {"code": "print('hello')"}, "hello", True),
                ToolExecution("write", {"path": "/tmp/test.txt"}, "Success", True),
            ],
            final_result="Task completed"
        )

        # 生成并写入 Skill
        skill_path = summarizer.summarize_and_write(context, "Test task summary")

        # 验证文件已创建
        assert skill_path is not None, "Should return skill path"
        assert skill_path.exists(), "Skill file should exist"

        # 验证文件内容
        content = skill_path.read_text(encoding="utf-8")
        assert "Test task" in content or "skill" in content.lower()

    def test_skill_file_naming(self, summarizer, temp_skills_dir):
        """测试 Skill 文件命名"""
        context = ExperienceContext(
            user_input="Task with specific name",
            turn_count=11,
            tool_executions=[
                ToolExecution("tool", {}, "ok", True),
            ]
        )

        skill_path = summarizer.summarize_and_write(context, "Specific task name")

        # 验证文件名包含日期或标识
        assert skill_path.name.endswith(".md")
        assert len(skill_path.stem) > 0  # 文件名不为空


@pytest.mark.p1
class TestExperienceSummarizerIntegration:
    """测试 ExperienceSummarizer 与 NiuHandler 集成"""

    def test_experience_summarizer_import_in_handler(self):
        """测试 handler.py 中已导入 ExperienceSummarizer"""
        try:
            from agent.handler import NiuHandler
            # 验证 NiuHandler 有 _experience_summarizer 属性
            # 这需要 mock mcp_client，简化测试只验证导入成功
            assert True
        except ImportError as e:
            pytest.fail(f"Failed to import ExperienceSummarizer in handler: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p1"])
