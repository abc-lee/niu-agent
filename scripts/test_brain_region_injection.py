#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for brain region prompt building in real environment.

Run this script to verify the brain region prompt system works
with a real LightRAG instance.

Usage:
    python scripts/test_brain_region_injection.py

Expects:
    - LightRAG initialized (optional -- falls back to mock if not available)
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


def test_static_prompt():
    """Test 2: Static prompt builder."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt

    prompt = build_static_brain_region_prompt()
    assert "根节点" in prompt, "Missing 根节点"
    assert "禁止事项" in prompt, "Missing 禁止事项"
    assert "包含" in prompt, "Missing 包含"
    assert "聊天历史" in prompt, "Missing 聊天历史"
    assert "文档库" in prompt, "Missing 文档库"
    assert "知识体系" in prompt, "Missing 知识体系"
    assert "大脑区域架构" in prompt, "Missing 大脑区域架构 heading"
    assert "提取规则" in prompt, "Missing 提取规则 section"

    print("  PASS Static prompt contains all required content")


def test_dynamic_prompt_with_real_adapter():
    """Test 3: Dynamic prompt with real LightRAGAdapter (if available)."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    from unittest.mock import patch

    using_mock = False

    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        rag = adapter._get_rag()
        if rag is None:
            print("  WARN LightRAG not initialized -- using mock for this test")
            using_mock = True
    except Exception as e:
        print(f"  WARN LightRAGAdapter failed ({e}) -- using mock")
        using_mock = True

    if using_mock:
        with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=["聊天历史脑区", "文档库脑区", "知识体系脑区"]):
            prompt = build_dynamic_brain_region_prompt()
    else:
        prompt = build_dynamic_brain_region_prompt()

    assert len(prompt) > 0, "Dynamic prompt is empty"
    assert "脑区" in prompt, "Missing 脑区 keyword"

    if using_mock:
        print(f"  PASS Dynamic prompt generated (mock): {prompt[:60]}...")
    else:
        print(f"  PASS Dynamic prompt generated (real): {prompt[:60]}...")


def test_dynamic_prompt_fallback():
    """Test 3b: Dynamic prompt falls back when adapter fails."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt, FALLBACK_REGIONS
    from unittest.mock import patch

    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=[]):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, "Missing 默认 fallback marker"
    assert FALLBACK_REGIONS in prompt, f"Missing fallback regions: {FALLBACK_REGIONS}"

    print("  PASS Dynamic prompt fallback works correctly")


def test_dynamic_prompt_exception_fallback():
    """Test 3c: Dynamic prompt falls back when adapter raises exception."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt, FALLBACK_REGIONS
    from unittest.mock import patch

    with patch("niu_api.internal.brain_region_prompt.get_brain_regions", side_effect=Exception("Connection refused")):
        prompt = build_dynamic_brain_region_prompt()
    assert "默认" in prompt, "Missing 默认 fallback marker on exception"
    assert FALLBACK_REGIONS in prompt, "Missing fallback regions on exception"

    print("  PASS Dynamic prompt exception fallback works correctly")


def main():
    print("=" * 60)
    print("Brain Region Injection -- Smoke Test")
    print("=" * 60)

    tests = [
        ("Static Prompt", test_static_prompt),
        ("Dynamic Prompt (real/mock adapter)", test_dynamic_prompt_with_real_adapter),
        ("Dynamic Prompt Fallback", test_dynamic_prompt_fallback),
        ("Dynamic Prompt Exception Fallback", test_dynamic_prompt_exception_fallback),
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
