"""上下文窗口实时读取回归锁（ContextManager 窗口冻结 bug）。

实测 bug：ContextManager 在构造期把 context_windowSize 读进 self.max_tokens，
而实例是进程级单例——用户运行中把窗口从 262144 改成 1000000 后，
页面首回合 usage 65.2%、次回合 17.1%（比值恒 3.815 = 1e6/262144），
分母停留在旧窗口。

本文件锁两件事：
1. 病根：窗口不冻结（构造参数 <=0）——组装每次现读配置，usage 分母跟随最新窗口；
2. 覆盖语义不回归：构造参数 max_tokens>0 仍是显式覆盖（测试/调用方专用）。

基建沿用 tests/test_fold_view_refresh.py 同款：FakeStore 替身 +
assemble_view_sync 入口 + 确定性 token 计数；校准倍率钉 1.0，
禁真实 LLM / 禁碰 ~/.niu（blocks DB 走 tmp 注入）。
"""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.context_assembler.calibration as calibration
import agent.context_manager as cm_mod
import agent.session as session_mod
from agent.context_manager import ContextManager
from agent.session import Message


# ---------------------------------------------------------------------------
# 同款基建（复制自 tests/test_fold_view_refresh.py / tests/test_calibration.py）
# ---------------------------------------------------------------------------

class FakeStore:
    """mock MessageStore——只实现 get_messages。"""

    def __init__(self, messages: list[Message]):
        self.messages = messages

    async def get_messages(self, limit=None):
        return list(self.messages) if limit is None else list(self.messages)[-limit:]


def _fake_count_tokens(messages):
    """确定性计数：每条消息 = len(content) + 8 结构开销。"""
    return sum(len(m.get("content", "")) + 8 for m in messages)


def _conversation():
    """一段纯 user/assistant 对话（无 tool → 与 fold 列无关）。"""
    return [
        Message(id="m0", rowid=1, role="user", content="Q1" + "x" * 100,
                tool_calls=[], tool_call_id="", created_at="2026-10-05T10:00:00"),
        Message(id="m1", rowid=2, role="assistant", content="A1" + "y" * 400,
                tool_calls=[], tool_call_id="", created_at="2026-10-05T10:00:01"),
        Message(id="m2", rowid=3, role="user", content="Q2",
                tool_calls=[], tool_call_id="", created_at="2026-10-05T10:00:02"),
    ]


@pytest.fixture
def window_env(monkeypatch, tmp_path):
    """校准倍率 1.0 + fold 列标志 True + 确定性计数；user-config 重定向 tmp。"""
    from niu_api import config as cfg_mod
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))

    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    monkeypatch.setattr(session_mod, "_fold_columns_available", True)
    monkeypatch.setattr(ContextManager, "count_tokens_simple",
                        staticmethod(_fake_count_tokens))
    yield tmp_path
    calibration._cached_ratio = old_ratio


# ---------------------------------------------------------------------------
# 1. 病根：窗口不冻结——usage 分母跟随配置最新值
# ---------------------------------------------------------------------------

class TestWindowLiveRead:
    def test_usage_follows_window_change_after_construction(self, window_env, monkeypatch):
        """构造时窗口 262144 → 组装 → 配置改 1000000 → 再组装，
        第二次 usage ≈ 第一次 ÷ 3.815（分母跟随 1M，不再停在构造期读到的 262144）。"""
        monkeypatch.setattr(cm_mod, "_read_context_window_tokens", lambda: 262144)
        msgs = _conversation()
        cm = ContextManager(FakeStore(msgs), blocks_db_path=window_env / "blocks.db")

        cm.assemble_view_sync(msgs, exclude_last=False)
        usage_first = cm._fold_stats["usage"]

        # 用户在设置页把 context_windowSize 改大 → 同实例再次组装
        monkeypatch.setattr(cm_mod, "_read_context_window_tokens", lambda: 1_000_000)
        cm.assemble_view_sync(msgs, exclude_last=False)
        usage_second = cm._fold_stats["usage"]

        assert usage_second == pytest.approx(usage_first / (1_000_000 / 262144), rel=1e-6), (
            f"usage 分母应跟随最新窗口（÷{1_000_000 / 262144:.3f}），"
            f"实际 {usage_first:.6f} → {usage_second:.6f}"
        )


# ---------------------------------------------------------------------------
# 2. 覆盖语义不回归：构造参数 >0 仍是显式覆盖
# ---------------------------------------------------------------------------

class TestExplicitOverride:
    def test_max_tokens_argument_overrides_config(self, window_env, monkeypatch):
        """max_tokens=1000 构造后，即使配置读出 10M，usage 仍按 1000 计算。"""
        monkeypatch.setattr(cm_mod, "_read_context_window_tokens", lambda: 10_000_000)
        msgs = _conversation()
        cm = ContextManager(FakeStore(msgs), max_tokens=1000,
                            blocks_db_path=window_env / "blocks.db")

        view = cm.assemble_view_sync(msgs, exclude_last=False)

        assert cm._fold_stats["usage"] == pytest.approx(_fake_count_tokens(view) / 1000)
