# tests/test_clear_brain_state.py
"""会话边界清理测试（T3-5）。

覆盖：/new 与 /clear 走同一端点 clear_chat——清空衰减池的同处必须同时清空
脑区注入缓存（_recent_region_entities，激活管理器跨会话单例、缓存无会话
生命周期）——否则新会话前 ~11-15 轮持续注入上一会话缓存实体（R24-A P2）。

行为断言：真实 injector 预填缓存 → clear_chat → 缓存空（红相——未接线缓存保留）。
"""
from unittest.mock import MagicMock

import niu_api.chat as chat_module
import niu_api.compat as compat
from niu_api.internal.region_injector import BrainContextInjector


def _make_injector_with_cache() -> BrainContextInjector:
    """真实 BrainContextInjector + 预填最近命中缓存（会话边界清理目标）。"""
    injector = BrainContextInjector(
        adapter=MagicMock(),
        activation_mgr=MagicMock(),
        region_mgr=MagicMock(),
    )
    injector._recent_region_entities = {
        "工作事务脑区": [
            {"entity_name": "雄安分行", "entity_type": "org", "description": "雄安分行 描述"},
        ],
        "知识体系脑区": [
            {"entity_name": "银企直连平台", "entity_type": "system", "description": "银企直连 描述"},
        ],
    }
    return injector


async def test_clear_resets_brain_state(monkeypatch):
    """T3-5：clear_chat 清空脑区注入缓存（行为断言，非调用断言）。

    mock 清单（R29-A P3）：request_stop / clear_stop / drain_supplements /
    get_message_store / niu_api.chat.get_or_create_runner（源模块 patch——
    test_compress_history L202 先例）/ cleanup_all_tmp。
    get_message_store 是 compat 模块级绑定名——
    patch "niu_api.compat.<name>"（源模块 patch 对模块级 import 无效）。
    """
    injector = _make_injector_with_cache()

    class FakeStore:
        async def clear_messages(self):
            return 2

    class FakeRunner:
        def __init__(self):
            self.handler = MagicMock()
            self._decay_pool = MagicMock()
            self._brain_injector = injector

    class FakeRequest:
        async def json(self):
            return {}

    async def fake_get_message_store():
        return FakeStore()

    monkeypatch.setattr("agent.runner.request_stop", lambda: None)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: None)
    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: FakeRunner())
    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", lambda: 0)
    # Task 8：/new 清理面派生状态复位——函数级 import，patch 包命名空间防真实删 ~/.niu 文件
    monkeypatch.setattr("agent.context_assembler.reset_derived_state", lambda *a, **k: None)

    result = await compat.clear_chat(FakeRequest())

    assert result["success"] is True
    # 行为断言：脑区注入缓存已被清空（红相——未接线时缓存保留）
    assert injector._recent_region_entities == {}


async def test_clear_runner_none_is_noop(monkeypatch):
    """_reset_runner_brain_state 对 None runner 无副作用（getattr 读取，不触发懒初始化）。"""
    from agent.decay_pool import DecayPool

    pool = DecayPool()

    class FakeStore:
        async def clear_messages(self):
            return 0

    class FakeRunner:
        def __init__(self):
            self.handler = MagicMock()
            self._decay_pool = pool
            # 无 _brain_injector 属性（等价于 _get_brain_injector 从未被调用过）

    class FakeRequest:
        async def json(self):
            return {}

    async def fake_get_message_store():
        return FakeStore()

    monkeypatch.setattr("agent.runner.request_stop", lambda: None)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplements", lambda: None)
    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", lambda: FakeRunner())
    monkeypatch.setattr("agent.tmp_dir.cleanup_all_tmp", lambda: 0)
    # Task 8：/new 清理面派生状态复位——函数级 import，patch 包命名空间防真实删 ~/.niu 文件
    monkeypatch.setattr("agent.context_assembler.reset_derived_state", lambda *a, **k: None)

    result = await compat.clear_chat(FakeRequest())

    assert result["success"] is True  # 无异常
    assert len(pool) == 0  # 衰减池正常清空
