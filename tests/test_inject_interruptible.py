# tests/test_inject_interruptible.py
"""动态注入检索可中断化测试。

覆盖：stop 放弃返回空注入不崩 / 正常路径结果一致 / timeout=15 透传。
全 mock LightRAGAdapter + _decay_pool + _brain_injector，不调真实 LightRAG。
"""
import time

import pytest

from agent.generic.interruptible import run_interruptibly  # 执行器本体已被 T1 测


class _FakeAdapter:
    """可编程 adapter：正常返回 / 记录 timeout。"""

    def __init__(self, delay=0.0, results=None):
        self.delay = delay
        self.results = results or {"knowledge": [{"entity_name": "k1", "entity_type": "knowledge", "description": "k1 描述"}]}
        self.search_by_file_path_timeout = None
        self.search_multi_lightrag_timeout = None

    def search_by_file_path(self, query, file_path_contains, top_k=10, keywords=None, timeout=None):
        self.search_by_file_path_timeout = timeout
        if self.delay:
            time.sleep(self.delay)
        return []

    def search_multi_lightrag(self, query, mode="local", top_k=20, keywords=None, timeout=None):
        self.search_multi_lightrag_timeout = timeout
        if self.delay:
            time.sleep(self.delay)
        return self.results

    def _get_rag(self):
        # T2 P2 防御：真实 _get_brain_injector 创建路径会调 adapter._get_rag()，
        # 缺失会 AttributeError（被生产 except 吞掉）——返回 None 使该路径安全短路。
        return None


class _FakeBrainInjector:
    """最小 brain injector：activate_for_query 记录 timeout，格式段返回空。"""

    def __init__(self):
        self.last_timeout = None

    def activate_for_query(self, query_context, timeout):
        self.last_timeout = timeout

    def format_region_map_only(self):
        return ""


class _FakePool:
    """最小 decay pool：记录 inject 调用，get_top_by_category 返回已注入实体（格式段消费）。

    R1-B 修正：格式段用 get_top_by_category 取注入实体再格式化（e.entity_dict 属性访问）——
    恒空会导致"k1 in injection"断言必败；inject 存实体 + get_top_by_category 按 category 返回。
    """

    def __init__(self):
        self.entities = []

    def decay(self):
        pass

    def inject(self, entity_name=None, entity_dict=None, category=None, source=None, vector_score=None, **kw):
        self.entities.append({
            "entity_name": entity_name, "entity_dict": entity_dict or {},
            "category": category, "source": source,
        })

    def get_top_by_category(self, category, n):
        from types import SimpleNamespace
        matched = [e for e in self.entities if e["category"] == category][:n]
        return [SimpleNamespace(entity_name=e["entity_name"], entity_dict=e["entity_dict"]) for e in matched]

    def get_top_by_source(self, source, n):
        return []

    def get_entry(self, entity_name):
        return None

    def __len__(self):
        return len(self.entities)


def _make_runner(monkeypatch, adapter, pool):
    """构造最小 runner：patch 掉 adapter/pool/brain，返回可调 _inject_dynamic_resources 的实例。

    R2-B 修正：runner.py 无模块级 LightRAGAdapter（全函数内 import）——patch 目标必须是
    niu_api.internal.lightrag_adapter.LightRAGAdapter（模块级定义处）。
    """
    from agent import runner as runner_mod
    import niu_api.internal.lightrag_adapter as _la
    monkeypatch.setattr(_la.LightRAGAdapter, "__new__", lambda cls, *a, **k: adapter)
    runner = runner_mod.NiuRunner.__new__(runner_mod.NiuRunner)  # 绕过 __init__
    runner._brain_injector = None
    runner._decay_pool = pool
    runner._brain_adapter = None  # 触发 LightRAGAdapter() 新建路径（被 __new__ patch 接管）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    # R2-B P1-2：正常路径 all_hits 非空会触发真实 _traverse_from_hits → get_lightrag() 懒初始化
    # 真实 LightRAG（环境相关慢/挂）——测试必须覆盖为返回空（图遍历可中断正确性由生产代码保证）
    runner._traverse_from_hits = lambda hits: {}
    # T2 P2：覆盖 _get_brain_injector 返回 fake injector——真实创建路径在测试里
    # 会调 _FakeAdapter._get_rag()（现返回 None 走安全短路）且重，无法构造出可用
    # BrainContextInjector；此前缺该方法导致 AttributeError 被生产 except 吞掉，
    # activate_for_query(timeout=15) 透传分支从未执行。
    fake_injector = _FakeBrainInjector()
    runner._get_brain_injector = lambda: fake_injector
    runner._fake_injector = fake_injector
    return runner


def test_normal_injection_passes_timeout_15(monkeypatch):
    """正常路径：检索结果进入注入文本，timeout=15 透传。"""
    adapter = _FakeAdapter()
    pool = _FakePool()
    runner = _make_runner(monkeypatch, adapter, pool)
    injection, _ = runner._inject_dynamic_resources("测试上下文")
    assert adapter.search_multi_lightrag_timeout == 15
    assert adapter.search_by_file_path_timeout == 15
    assert runner._fake_injector.last_timeout == 15  # 脑区激活 timeout 透传（T2 P2）
    assert "k1" in injection  # knowledge 实体进入注入文本（经 FakePool 注入 + 格式段）


def test_stop_abandon_returns_empty(monkeypatch):
    """stop 置位（run_interruptibly 返回 False,None）：检索放弃，注入不抛错、不含检索结果。"""
    adapter = _FakeAdapter()
    pool = _FakePool()
    runner = _make_runner(monkeypatch, adapter, pool)
    # R1-B 修正：_inject_dynamic_resources 内 `from agent.generic.interruptible import run_interruptibly`
    # 是函数内 import——patch 模块属性 agent.generic.interruptible.run_interruptibly 才生效
    # （patch runner_mod.run_interruptibly 无效——该命名空间不存在该名字）
    monkeypatch.setattr("agent.generic.interruptible.run_interruptibly", lambda fn, sc, **kw: (False, None))
    injection, _ = runner._inject_dynamic_resources("测试上下文")
    assert isinstance(injection, str)  # 不抛错
    assert "k1" not in injection  # 放弃 → 无检索结果
    assert pool.entities == []  # 衰减池无注入


def test_slow_adapter_abandons_under_stop(monkeypatch):
    """真实 run_interruptibly + 慢 adapter + stop 置位：轮询放弃，不卡 120s。

    R3-B P0-1 修正：_inject_dynamic_resources 内 run_interruptibly 的 stop_check 是
    runner 模块级 is_stop_requested（同模块全局引用）——测试 stop_flag 必须 patch
    runner_mod.is_stop_requested 才连得上（否则 stop_flag 置位不影响轮询，测试必败）。
    R3-A P1-1 修正：断言放宽——1a/1b 两个串行 wrapper（含块间短路后收敛到单轮询）+
    余量，elapsed < 0.5。
    """
    import threading
    from agent import runner as runner_mod
    adapter = _FakeAdapter(delay=0.5)
    pool = _FakePool()
    runner = _make_runner(monkeypatch, adapter, pool)
    stop_flag = {"v": False}
    monkeypatch.setattr(runner_mod, "is_stop_requested", lambda: stop_flag["v"])
    threading.Timer(0.05, lambda: stop_flag.__setitem__("v", True)).start()
    started = time.monotonic()
    injection, _ = runner._inject_dynamic_resources("测试上下文")
    elapsed = time.monotonic() - started
    assert elapsed < 0.5  # 放弃（块间短路收敛到单轮询 ~0.2s + 余量）
    assert isinstance(injection, str)
