"""get_or_create_runner 配置变更重建判据单测（V9c：capabilities 变更触发重建）。

全 mock（不建真实 NiuRunner、不触网、不读真实配置）：
- chat.get_runner → 返回预置 llm_config 的假 runner（两次调用均非 None，
  短路 init_runner 创建路径——本测试只验证"是否清 _runner 触发重建"判据）
- chat._load_llm_config → 返回当前配置 dict（每次读盘语义由 mock 固定值替代）

覆盖：
① capabilities 变更（探测写盘 ["text"]→["text","image"]）→ 重建触发（_runner 清空）
② capabilities 未变且其余字段全同 → 不重建（_runner 保留）
③ 两侧均无 capabilities 键（未探测模型）→ .get() 双 None 不误判重建
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.runner as runner_module  # noqa: E402
from niu_api import chat  # noqa: E402


@pytest.fixture
def restore_runner():
    """保存/恢复 agent.runner._runner（本测试直接改写模块全局）。"""
    saved = runner_module._runner
    yield
    runner_module._runner = saved


def _cfg(**over):
    """完整 llm 配置 dict（get_or_create_runner 判据涉及的全部键）。"""
    cfg = {
        "apikey": "k",
        "model": "m1",
        "apibase": "https://api.example.com/v1",
        "type": "openai",
        "read_timeout": 300,
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}},
    }
    cfg.update(over)
    return cfg


def _run_case(monkeypatch, existing_llm_config, current_config):
    """预置假 runner + mock 配置读取，调 get_or_create_runner，返回调用后 _runner。"""
    existing = SimpleNamespace(llm_config=existing_llm_config)
    monkeypatch.setattr(chat, "get_runner", lambda *a, **kw: existing)
    monkeypatch.setattr(chat, "_load_llm_config", lambda: current_config)
    runner_module._runner = existing
    chat.get_or_create_runner()
    return runner_module._runner


def test_capabilities_change_triggers_rebuild(monkeypatch, restore_runner):
    """① capabilities 变更（探测写盘）→ 重建触发：_runner 被清空待 init_runner 重建。"""
    existing = _cfg(capabilities={"model": "m1", "input": ["text"], "probed_at": "2026-09-01T00:00:00"})
    current = _cfg(capabilities={"model": "m1", "input": ["text", "image"], "probed_at": "2026-09-10T00:00:00"})
    assert _run_case(monkeypatch, existing, current) is None


def test_unchanged_capabilities_no_rebuild(monkeypatch, restore_runner):
    """② capabilities 未变且其余字段全同 → 不重建（_runner 保留，避免无谓重建）。"""
    cfg = _cfg(capabilities={"model": "m1", "input": ["text"], "probed_at": "2026-09-01T00:00:00"})
    existing, current = dict(cfg), dict(cfg)  # 内容相等但非同一对象
    assert _run_case(monkeypatch, existing, current) is not None


def test_capabilities_absent_both_sides_no_false_rebuild(monkeypatch, restore_runner):
    """③ 两侧均无 capabilities 键（未探测模型）→ .get() 双 None，不误判重建。"""
    cfg = _cfg()
    assert _run_case(monkeypatch, dict(cfg), dict(cfg)) is not None
