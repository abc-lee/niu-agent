"""End-to-end verification tests for brain region injection pipeline.

Tests the complete flow: LightRAG extraction request -> detection ->
prompt building -> injection -> LLM receives enhanced messages.

All tests use mocks — no running LightRAG instance required.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestE2EBrainRegionInjection:
    """E2E tests for the complete brain region injection pipeline."""

    def test_full_pipeline_extraction_request(self):
        """Complete pipeline: detection -> static -> dynamic -> injection."""
        from niu_api.internal.brain_region_prompt import (
            is_lightrag_extraction_request,
            build_static_brain_region_prompt,
            build_dynamic_brain_region_prompt,
            inject_brain_region_context,
        )

        # Step 1: Simulate LightRAG extraction messages
        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities from: Python is a programming language"},
        ]

        # Step 2: Detect
        assert is_lightrag_extraction_request(messages) is True

        # Step 3: Build static prompt
        static = build_static_brain_region_prompt()
        assert "brain:Niu" in static
        assert "brain_region_anchor" in static

        # Step 4: Build dynamic prompt (mock adapter)
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:聊天历史\nbrain:region:文档库"
        dynamic = build_dynamic_brain_region_prompt(adapter)
        assert "聊天历史" in dynamic

        # Step 5: Inject
        result = inject_brain_region_context(messages, adapter)
        system_msg = next(m for m in result if m["role"] == "system")
        assert "brain:Niu" in system_msg["content"]
        assert "聊天历史" in system_msg["content"]
        # Original content preserved
        assert "Knowledge Graph Specialist" in system_msg["content"]

    def test_full_pipeline_normal_chat_unchanged(self):
        """Normal chat messages pass through completely unchanged."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the weather?"},
        ]
        adapter = MagicMock()

        result = inject_brain_region_context(messages, adapter)

        # Same object returned (no copy)
        assert result is messages
        # Adapter never called
        adapter.query.assert_not_called()

    def test_full_pipeline_adapter_failure_graceful(self):
        """When adapter fails, injection still works with fallback."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.side_effect = Exception("LightRAG not initialized")

        result = inject_brain_region_context(messages, adapter)
        system_msg = next(m for m in result if m["role"] == "system")

        # Static prompt still injected
        assert "大脑区域架构" in system_msg["content"]
        # Dynamic fallback present
        assert "当前图谱中的脑区" in system_msg["content"]
        assert "默认" in system_msg["content"]

    def test_full_pipeline_empty_graph(self):
        """When graph has no regions, fallback is used."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.return_value = ""

        result = inject_brain_region_context(messages, adapter)
        system_msg = next(m for m in result if m["role"] == "system")

        assert "大脑区域架构" in system_msg["content"]
        # Dynamic fallback present
        assert "当前图谱中的脑区" in system_msg["content"]
        assert "默认" in system_msg["content"]

    def test_injection_uses_local_mode_no_llm(self):
        """Dynamic query uses local mode (0 LLM calls) to prevent infinite loops."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        inject_brain_region_context(messages, adapter)

        # Verify local mode was used
        call_kwargs = adapter.query.call_args[1]
        assert call_kwargs["mode"] == "local"
        assert call_kwargs["only_need_context"] is True

    def test_original_messages_not_mutated(self):
        """Injection never mutates the original message list."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities..."},
        ]
        original_content = messages[0]["content"]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        result = inject_brain_region_context(messages, adapter)

        # Original unchanged
        assert messages[0]["content"] == original_content
        # Result is different list
        assert result is not messages
        # Result system message has different content
        assert result[0]["content"] != original_content

    def test_multiple_system_messages_only_injects_marker_system(self):
        """Only the system message containing the marker gets injected."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "system", "content": "Additional instructions"},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        result = inject_brain_region_context(messages, adapter)

        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) == 2
        # Only the first system message (with marker) should be injected
        assert "brain:Niu" in system_msgs[0]["content"]
        # The second system message should be unchanged
        assert system_msgs[1]["content"] == "Additional instructions"

    def test_full_pipeline_preserves_non_system_messages(self):
        """User and assistant messages pass through unchanged."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities from: some text"},
            {"role": "assistant", "content": "Previous response"},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        result = inject_brain_region_context(messages, adapter)

        user_msg = next(m for m in result if m["role"] == "user")
        assert user_msg["content"] == "Extract entities from: some text"
        assistant_msg = next(m for m in result if m["role"] == "assistant")
        assert assistant_msg["content"] == "Previous response"

    def test_full_pipeline_message_order_preserved(self):
        """Message order is preserved after injection."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "First user message"},
            {"role": "assistant", "content": "First assistant response"},
            {"role": "user", "content": "Second user message"},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        result = inject_brain_region_context(messages, adapter)

        roles = [m["role"] for m in result]
        assert roles == ["system", "user", "assistant", "user"]

    def test_full_pipeline_dynamic_and_static_both_present(self):
        """Both static architecture and dynamic region list appear in output."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:自定义区域"

        result = inject_brain_region_context(messages, adapter)
        content = next(m for m in result if m["role"] == "system")["content"]

        # Static part present
        assert "大脑区域架构" in content
        assert "brain:Niu" in content
        assert "brain_region_anchor" in content
        assert "belongs_to_region" in content
        # Dynamic part present (with format marker distinguishing it from static)
        assert "当前图谱中的脑区" in content
        assert "自定义区域" in content
        # Original content still at the beginning
        assert content.startswith("---Role---")

    def test_full_pipeline_injection_appended_not_prepended(self):
        """Brain region info is appended after original system content, not prepended."""
        from niu_api.internal.brain_region_prompt import inject_brain_region_context

        original_content = "---Role---\nYou are a Knowledge Graph Specialist..."
        messages = [
            {"role": "system", "content": original_content},
            {"role": "user", "content": "Extract entities..."},
        ]
        adapter = MagicMock()
        adapter.query.return_value = "brain:region:测试"

        result = inject_brain_region_context(messages, adapter)
        content = next(m for m in result if m["role"] == "system")["content"]

        # Original content comes first
        assert content.index("---Role---") < content.index("brain:Niu")
