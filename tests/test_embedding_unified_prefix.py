"""S4 —— EmbeddingGemma 2 统一前缀（query 任务前缀）。

全 mock：不启动真实 llama-server、不加载真实模型、禁图谱写入、禁写真实 ~/.niu/。
覆盖方案 §4.2 的五项强制断言：
  1. `EMBEDDING_PREFIX` 逐字等于 "task: search result | query: "；
  2. `_apply_unified_prefix` 幂等（已带前缀不重复加、空 list 返回 []、空前缀返回拷贝）；
  3. `encode` 发出的 texts 都带前缀（单条 str 与 list[str] 两种形态）；
  4. 形状校验仍生效（错宽度仍抛 RuntimeError）；
  5. `encode("")` 之类边界不炸，空输入仍 raise。
"""

import numpy as np
import pytest

from niu_api.internal import llama_server
from niu_api.internal.embedding import EMBEDDING_PREFIX, _apply_unified_prefix

PREFIX = "task: search result | query: "


class _FakeServer:
    """记录入参并返回确定性 768 维向量。"""

    def __init__(self, dim: int = 768, wrong_width: bool = False):
        self.dim = dim
        self.wrong_width = wrong_width
        self.calls: list[list[str]] = []

    def encode_batch(self, texts):
        self.calls.append(list(texts))
        if self.wrong_width:
            return [[0.0] * (self.dim - 1) for _ in texts]
        return [[float(i) for i in range(self.dim)] for _ in texts]


@pytest.fixture
def adapter():
    """绕开真实 `__init__`，但必须给 `_server` 占位（monkeypatch.setattr 默认 raising=True）。"""
    from niu_api.internal.embedding import _LlamaCppAdapter

    obj = _LlamaCppAdapter.__new__(_LlamaCppAdapter)
    obj._server = None
    return obj


# ============== 1. 前缀常量逐字钉死 ==============


class TestPrefixConstant:
    def test_verbatim_value(self):
        assert EMBEDDING_PREFIX == "task: search result | query: "
        assert EMBEDDING_PREFIX == PREFIX

    def test_trailing_space_preserved(self):
        """末尾空格是 prompt_name 逐字内容，被"顺手修正"掉就成了另一个前缀。"""
        assert EMBEDDING_PREFIX.endswith(" ")

    def test_no_second_literal_in_module(self):
        """前缀常量只允许有一处定义（方案 §5.1-1）。"""
        import inspect
        import re

        import niu_api.internal.embedding as emb

        src = inspect.getsource(emb)
        assert src.count('"task: search result | query: "') == 1
        assert re.search(r"EMBEDDING_PREFIX\s*=", src) is not None


# ============== 2. _apply_unified_prefix 幂等 ==============


class TestApplyUnifiedPrefix:
    def test_adds_prefix_when_missing(self):
        assert _apply_unified_prefix(["a", "b"]) == [PREFIX + "a", PREFIX + "b"]

    def test_idempotent_already_prefixed(self):
        assert _apply_unified_prefix([PREFIX + "a"]) == [PREFIX + "a"]

    def test_idempotent_across_repeated_calls(self):
        once = _apply_unified_prefix(["a", PREFIX + "b", ""])
        assert _apply_unified_prefix(once) == once

    def test_empty_list_returns_empty(self):
        assert _apply_unified_prefix([]) == []

    def test_empty_string_gets_prefix(self):
        assert _apply_unified_prefix([""]) == [PREFIX]

    def test_returns_new_list(self):
        """不得原地改调用方的 list（encode 里的 texts 可能来自外部入参）。"""
        texts = ["a"]
        out = _apply_unified_prefix(texts)
        assert out is not texts
        assert texts == ["a"]

    def test_empty_prefix_returns_copy(self, monkeypatch):
        monkeypatch.setattr("niu_api.internal.embedding.EMBEDDING_PREFIX", "")
        texts = ["a", "b"]
        out = _apply_unified_prefix(texts)
        assert out == ["a", "b"]
        assert out is not texts


# ============== 3. encode 发出的 texts 都带前缀 ==============


class TestEncodeAppliesPrefix:
    def test_single_str_sends_prefixed(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        adapter.encode("你好", convert_to_numpy=True)
        assert len(fake.calls) == 1
        assert fake.calls[0] == [PREFIX + "你好"]

    def test_list_sends_all_prefixed(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        adapter.encode(["a", "b", "c"], convert_to_numpy=True)
        assert fake.calls[0] == [PREFIX + "a", PREFIX + "b", PREFIX + "c"]

    def test_already_prefixed_input_not_doubled(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        adapter.encode([PREFIX + "a"], convert_to_numpy=True)
        assert fake.calls[0] == [PREFIX + "a"]
        assert not fake.calls[0][0].startswith(PREFIX + PREFIX)

    def test_module_function_paths_also_prefixed(self, monkeypatch):
        """sentinel：经模块公开函数（embedding.encode / batch_encode）同样是本注入点。"""
        import niu_api.internal.embedding as emb
        from niu_api.internal.embedding import _LlamaCppAdapter

        fake = _FakeServer()
        obj = _LlamaCppAdapter.__new__(_LlamaCppAdapter)
        obj._server = llama_server
        monkeypatch.setattr(llama_server, "encode_batch", fake.encode_batch)
        monkeypatch.setattr(emb, "get_model", lambda: obj)
        emb.encode("x")
        emb.batch_encode(["y", "z"])
        assert fake.calls == [[PREFIX + "x"], [PREFIX + "y", PREFIX + "z"]]


# ============== 4. 形状校验仍然生效 ==============


class TestShapeGuardStillWorks:
    def test_wrong_width_raises_runtime_error(self, adapter, monkeypatch):
        fake = _FakeServer(wrong_width=True)
        monkeypatch.setattr(adapter, "_server", fake)
        with pytest.raises(RuntimeError):
            adapter.encode(["a", "b"], convert_to_numpy=True)

    def test_correct_shape_still_passes(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        out = adapter.encode(["a", "b"], convert_to_numpy=True)
        assert isinstance(out, np.ndarray) and out.shape == (2, 768)
        single = adapter.encode("a", convert_to_numpy=True)
        assert isinstance(single, np.ndarray) and single.shape == (768,)


# ============== 5. 边界输入 ==============


class TestBoundaryInputs:
    def test_empty_string_does_not_blow_up(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        out = adapter.encode("", convert_to_numpy=True)
        assert out.shape == (768,)
        assert fake.calls[0] == [PREFIX]

    def test_empty_list_still_raises(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_server", _FakeServer())
        with pytest.raises(ValueError):
            adapter.encode([])

    def test_empty_list_does_not_reach_server(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        with pytest.raises(ValueError):
            adapter.encode([])
        assert fake.calls == []

    def test_no_kwarg_returns_iterable_of_rows(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        rows = [list(map(float, v)) for v in adapter.encode(["a", "b"])]
        assert len(rows) == 2 and len(rows[0]) == 768
