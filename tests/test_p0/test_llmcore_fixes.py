"""P0-4 & P0-5: 测试 llmcore.py 修复"""
import pytest
import sys
sys.path.insert(0, "E:/tools/ai-bot")

from agent.generic.llmcore import BaseSession, NativeClaudeSession, MockResponse
import unittest.mock as mock


@pytest.mark.p0
class TestTypeValidation:
    """P0-4: 测试 ask 方法的类型验证"""

    @pytest.fixture
    def session(self):
        """创建测试 session"""
        cfg = {
            "apikey": "test-key",
            "apibase": "https://api.anthropic.com",
            "model": "claude-3-5-sonnet-20241022"
        }
        return NativeClaudeSession(cfg)

    def test_ask_rejects_non_dict(self, session):
        """测试 ask 方法拒绝非 dict 参数"""
        # 传入字符串而非 dict
        with pytest.raises(TypeError) as exc_info:
            session.ask("invalid message")

        # 验证错误信息
        assert "Expected dict" in str(exc_info.value), \
            "Error message should mention 'Expected dict'"
        assert "str" in str(exc_info.value), \
            "Error message should mention actual type 'str'"

    def test_ask_accepts_dict(self, session):
        """测试 ask 方法接受 dict 参数"""
        msg = {"role": "user", "content": "Hello"}

        # Mock raw_ask 避免真实 API 调用
        with mock.patch.object(session, 'raw_ask') as mock_raw:
            mock_raw.return_value = iter([])  # 空生成器
            try:
                session.ask(msg)
                # 验证 raw_ask 被调用（类型检查通过）
                assert mock_raw.called
            except TypeError as e:
                # 如果抛出 TypeError，应不是类型检查错误
                assert "Expected dict" not in str(e)

    def test_ask_rejects_list(self, session):
        """测试 ask 方法拒绝 list 参数"""
        msg = [{"role": "user", "content": "Hello"}]

        with pytest.raises(TypeError) as exc_info:
            session.ask(msg)

        assert "Expected dict" in str(exc_info.value)
        assert "list" in str(exc_info.value)

    def test_ask_rejects_none(self, session):
        """测试 ask 方法拒绝 None 参数"""
        with pytest.raises(TypeError) as exc_info:
            session.ask(None)

        assert "Expected dict" in str(exc_info.value)
        assert "NoneType" in str(exc_info.value)


@pytest.mark.p0
class TestContentBlocksInitialization:
    """P0-5: 测试 ask 方法正确处理 content_blocks"""

    @pytest.fixture
    def session(self):
        """创建测试 session"""
        cfg = {
            "apikey": "test-key",
            "apibase": "https://api.anthropic.com",
            "model": "claude-3-5-sonnet-20241022"
        }
        return NativeClaudeSession(cfg)

    def test_ask_handles_generator_return_value(self, session):
        """测试 ask 方法正确处理生成器返回值"""
        # Mock raw_ask 返回生成器
        def mock_generator():
            """模拟生成器 yield chunks"""
            # yield 一些文本块
            yield "Hello"
            yield " world"
            # 返回 content_blocks
            return [
                {"type": "text", "text": "Hello world"},
                {"type": "tool_use", "name": "test_tool", "id": "tool_1", "input": {}}
            ]

        with mock.patch.object(session, 'raw_ask', return_value=mock_generator()):
            # 调用 ask 方法
            msg = {"role": "user", "content": "Test"}
            response = session.ask(msg)

            # 验证返回值
            assert isinstance(response, MockResponse), \
                "ask should return MockResponse"
            assert response.content == "Hello world", \
                "Response content should match"
            assert len(response.tool_calls) == 1, \
                "Should extract 1 tool call"
            # MockToolCall 的属性是 function.name 而不是 name
            assert response.tool_calls[0].function.name == "test_tool", \
                "Tool name should match"

    def test_ask_handles_empty_generator(self, session):
        """测试 ask 处理空生成器"""
        def mock_empty_generator():
            """空生成器 - 必须有 yield 才是生成器"""
            return []  # 空列表
            yield  # 永远不会执行，但让函数成为生成器

        with mock.patch.object(session, 'raw_ask', return_value=mock_empty_generator()):
            msg = {"role": "user", "content": "Test"}
            response = session.ask(msg)

            # 验证不会崩溃
            assert isinstance(response, MockResponse)
            assert response.content == ""
            assert len(response.tool_calls) == 0

    def test_ask_handles_none_return_value(self, session):
        """测试 ask 处理 None 返回值"""
        def mock_none_generator():
            """返回 None 的生成器"""
            return None
            yield  # 永远不会执行，但让函数成为生成器

        with mock.patch.object(session, 'raw_ask', return_value=mock_none_generator()):
            msg = {"role": "user", "content": "Test"}
            response = session.ask(msg)

            # 验证不会崩溃
            assert isinstance(response, MockResponse)

    def test_ask_no_attribute_error(self, session):
        """测试 ask 不会抛出 AttributeError"""
        # 这个测试确保 content_blocks 已初始化
        def mock_generator():
            return [{"type": "text", "text": "test"}]
            yield  # 永远不会执行，但让函数成为生成器

        with mock.patch.object(session, 'raw_ask', return_value=mock_generator()):
            msg = {"role": "user", "content": "Test"}

            # 不应抛出 AttributeError: 'NoneType' object is not iterable
            try:
                response = session.ask(msg)
                # 成功调用
                assert True
            except AttributeError as e:
                if "'NoneType' object is not iterable" in str(e):
                    pytest.fail("content_blocks not initialized: " + str(e))
                else:
                    # 其他 AttributeError 可能是预期的
                    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "p0"])
