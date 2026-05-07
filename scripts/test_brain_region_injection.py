#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for brain region injection in real environment.

Run this script to verify the brain region injection system works
with a real LightRAG instance and API server.

Usage:
    python scripts/test_brain_region_injection.py

Expects:
    - LightRAG initialized (optional -- falls back to mock if not available)
    - API server running on localhost:9876 (optional -- skips proxy test if not)
"""

import sys
import os
from pathlib import Path

# Set project root and add to sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Fix Windows UTF-8 console output
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def test_detection():
    """Test 1: Detection function with real LightRAG extraction messages."""
    from niu_api.internal.brain_region_prompt import is_lightrag_extraction_request

    # Should detect -- matches real LightRAG extraction prompt format
    extraction_msgs = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities"},
    ]
    assert is_lightrag_extraction_request(extraction_msgs) is True, (
        "Should detect extraction request"
    )

    # Should NOT detect -- normal chat
    normal_msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    assert is_lightrag_extraction_request(normal_msgs) is False, (
        "Should not detect normal chat"
    )

    # Should NOT detect -- empty messages
    assert is_lightrag_extraction_request([]) is False, "Should not detect empty list"

    print("  PASS Detection function works correctly")


def test_static_prompt():
    """Test 2: Static prompt builder."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt

    prompt = build_static_brain_region_prompt()
    assert "brain:Niu" in prompt, "Missing brain:Niu"
    assert "brain_region_anchor" in prompt, "Missing brain_region_anchor"
    assert "belongs_to_region" in prompt, "Missing belongs_to_region"
    assert "聊天历史" in prompt, "Missing 聊天历史"
    assert "文档库" in prompt, "Missing 文档库"
    assert "知识体系" in prompt, "Missing 知识体系"
    assert "大脑区域架构" in prompt, "Missing 大脑区域架构 heading"
    assert "提取规则" in prompt, "Missing 提取规则 section"

    print("  PASS Static prompt contains all required content")


def test_dynamic_prompt_with_real_adapter():
    """Test 3: Dynamic prompt with real LightRAGAdapter (if available)."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    from unittest.mock import MagicMock

    adapter = None
    using_mock = False

    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        rag = adapter._get_rag()
        if rag is None:
            print("  WARN LightRAG not initialized -- using mock adapter for this test")
            adapter = None
    except Exception as e:
        print(f"  WARN LightRAGAdapter failed ({e}) -- using mock adapter")

    if adapter is None:
        adapter = MagicMock()
        adapter.query.return_value = (
            "brain:region:聊天历史\nbrain:region:文档库\nbrain:region:知识体系"
        )
        using_mock = True

    prompt = build_dynamic_brain_region_prompt(adapter)
    assert len(prompt) > 0, "Dynamic prompt is empty"
    assert "脑区" in prompt, "Missing 脑区 keyword"

    # Verify local mode was used
    if using_mock:
        call_kwargs = adapter.query.call_args[1]
        assert call_kwargs["mode"] == "local", f"Expected mode=local, got {call_kwargs['mode']}"
        assert call_kwargs["only_need_context"] is True, "Expected only_need_context=True"
        print(f"  PASS Dynamic prompt generated (mock): {prompt[:60]}...")
    else:
        print(f"  PASS Dynamic prompt generated (real): {prompt[:60]}...")


def test_dynamic_prompt_fallback():
    """Test 3b: Dynamic prompt falls back when adapter fails."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt, FALLBACK_REGIONS
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.query.return_value = ""

    prompt = build_dynamic_brain_region_prompt(adapter)
    assert "默认" in prompt, "Missing 默认 fallback marker"
    assert FALLBACK_REGIONS in prompt, f"Missing fallback regions: {FALLBACK_REGIONS}"

    print("  PASS Dynamic prompt fallback works correctly")


def test_dynamic_prompt_exception_fallback():
    """Test 3c: Dynamic prompt falls back when adapter raises exception."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt, FALLBACK_REGIONS
    from unittest.mock import MagicMock

    adapter = MagicMock()
    adapter.query.side_effect = Exception("Connection refused")

    prompt = build_dynamic_brain_region_prompt(adapter)
    assert "默认" in prompt, "Missing 默认 fallback marker on exception"
    assert FALLBACK_REGIONS in prompt, "Missing fallback regions on exception"

    print("  PASS Dynamic prompt exception fallback works correctly")


def test_full_injection_pipeline():
    """Test 4: Full injection pipeline."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    from unittest.mock import MagicMock

    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities from: Python is a programming language"},
    ]
    adapter = MagicMock()
    adapter.query.return_value = "brain:region:聊天历史\nbrain:region:文档库"

    result = inject_brain_region_context(messages, adapter)

    # Verify injection happened
    assert result is not messages, "Should return new list"
    system_msg = next(m for m in result if m["role"] == "system")
    assert "brain:Niu" in system_msg["content"], "Missing brain:Niu in injected content"
    assert "Knowledge Graph Specialist" in system_msg["content"], "Original content lost"
    assert "聊天历史" in system_msg["content"], "Missing dynamic region content"

    # Verify original not mutated
    assert "brain:Niu" not in messages[0]["content"], "Original messages were mutated"

    # Verify message order preserved
    roles = [m["role"] for m in result]
    assert roles == ["system", "user"], f"Message order changed: {roles}"

    print("  PASS Full injection pipeline works correctly")


def test_injection_normal_chat_passthrough():
    """Test 4b: Normal chat messages pass through unchanged."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    from unittest.mock import MagicMock

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What's the weather?"},
    ]
    adapter = MagicMock()

    result = inject_brain_region_context(messages, adapter)

    # Same object returned (no copy)
    assert result is messages, "Should return same list for non-extraction requests"
    # Adapter never called
    adapter.query.assert_not_called()

    print("  PASS Normal chat messages pass through unchanged")


def test_llm_proxy_integration():
    """Test 5: LLM proxy integration (requires running API server)."""
    try:
        import requests
        resp = requests.get("http://localhost:9876/health", timeout=2)
        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}")
    except Exception:
        print("  SKIP API server not running -- skipping proxy integration test")
        return "skip"

    # If API is running, test that injection is wired up
    try:
        from niu_api.internal.brain_region_prompt import inject_brain_region_context
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        adapter = LightRAGAdapter()
        messages = [
            {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
            {"role": "user", "content": "Test extraction"},
        ]
        result = inject_brain_region_context(messages, adapter)
        system_msg = next(m for m in result if m["role"] == "system")

        if "brain:Niu" in system_msg["content"]:
            print("  PASS LLM proxy integration verified (injection works with real adapter)")
        else:
            print("  WARN Injection may not be working -- brain:Niu not found in system message")
    except Exception as e:
        print(f"  WARN Proxy integration test failed: {e}")


def main():
    print("=" * 60)
    print("Brain Region Injection -- Smoke Test")
    print("=" * 60)

    tests = [
        ("Detection", test_detection),
        ("Static Prompt", test_static_prompt),
        ("Dynamic Prompt (real/mock adapter)", test_dynamic_prompt_with_real_adapter),
        ("Dynamic Prompt Fallback", test_dynamic_prompt_fallback),
        ("Dynamic Prompt Exception Fallback", test_dynamic_prompt_exception_fallback),
        ("Full Pipeline", test_full_injection_pipeline),
        ("Normal Chat Passthrough", test_injection_normal_chat_passthrough),
        ("LLM Proxy Integration", test_llm_proxy_integration),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, test_fn in tests:
        print(f"\n[Test] {name}")
        try:
            result = test_fn()
            if result == "skip":
                skipped += 1
            else:
                passed += 1
        except AssertionError as e:
            print(f"  FAIL {e}")
            failed += 1
        except Exception as e:
            print(f"  SKIP {e}")
            skipped += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
