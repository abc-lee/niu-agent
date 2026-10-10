"""llama.cpp 编码后端（S2）。

禁真实 llama-server / 禁真实模型 / 禁图谱写入。以假 HTTP 层驱动适配器，
逐一核验现有 6 类消费方的调用契约不被破坏（这是本方案的最大回归面）。
"""

import numpy as np
import pytest

from niu_api.internal import llama_server


class _FakeServer:
    """记录调用、返回确定性向量；可模拟各类失败。"""

    def __init__(self, dim: int = 768, fail: bool = False, wrong_count: bool = False):
        self.dim = dim
        self.fail = fail
        self.wrong_count = wrong_count
        self.calls: list[list[str]] = []
        self.stopped = 0

    def start(self):
        self.calls.append(["__start__"])

    def stop(self):
        self.stopped += 1

    def is_alive(self):
        return not self.fail

    def encode_batch(self, texts):
        if self.fail:
            raise RuntimeError("llama-server 未就绪（health 检查失败）")
        self.calls.append(list(texts))
        n = 1 if self.wrong_count else len(texts)
        return [[float(i + 1)] * self.dim for i in range(n)]


@pytest.fixture
def adapter(monkeypatch):
    """绕开真实 `__init__`（它只 import 模块并赋值 `self._server`，不启动子进程），
    但**必须给 `_server` 占位**：monkeypatch.setattr 默认 raising=True，
    目标属性不存在会 AttributeError（R2 两角实测：占位缺失时 TestEncodeContract 全 7 条报错）。"""
    from niu_api.internal.embedding import _LlamaCppAdapter

    obj = _LlamaCppAdapter.__new__(_LlamaCppAdapter)
    obj._server = None  # 占位，使后续 monkeypatch.setattr(instance, "_server", fake) 生效
    return obj


class TestEncodeContract:
    """现有调用点的契约——改任一一条都会炸，故逐条钉死。"""

    def test_convert_to_numpy_returns_ndarray_2d(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        out = adapter.encode(["a", "b"], convert_to_numpy=True, show_progress_bar=False)
        assert isinstance(out, np.ndarray) and out.shape == (2, 768)
        assert out.dtype == np.float32

    def test_single_str_returns_1d_and_tolist(self, adapter, monkeypatch):
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        out = adapter.encode("init", convert_to_numpy=True, show_progress_bar=False)
        assert isinstance(out, np.ndarray) and out.shape == (768,)
        assert isinstance(out.tolist(), list) and len(out.tolist()) == 768

    def test_no_kwargs_is_iterable_of_rows(self, adapter, monkeypatch):
        """lightrag_repair._embed_batch 不传 kwarg，逐行 map(float)。"""
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        vecs = adapter.encode(["x", "y"])
        rows = [list(map(float, v)) for v in vecs]
        assert len(rows) == 2 and len(rows[0]) == 768

    def test_indexable_for_similarity(self, adapter, monkeypatch):
        """embedding.similarity 取 embeddings[0]/[1]。"""
        fake = _FakeServer()
        monkeypatch.setattr(adapter, "_server", fake)
        out = adapter.encode(["t1", "t2"], convert_to_numpy=True)
        assert out[0].shape == (768,) and out[1].shape == (768,)

    def test_empty_input_rejected(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_server", _FakeServer())
        with pytest.raises(ValueError):
            adapter.encode([])

    def test_wrong_count_raises(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter, "_server", _FakeServer(wrong_count=True))
        with pytest.raises(RuntimeError):
            adapter.encode(["a", "b"])

    def test_server_failure_propagates(self, adapter, monkeypatch):
        """失败必须上抛，不得静默返回空列表（空列表=编码成功但无结果）。"""
        monkeypatch.setattr(adapter, "_server", _FakeServer(fail=True))
        with pytest.raises(RuntimeError):
            adapter.encode(["a"])


class TestModuleApiUnchanged:
    """embedding 模块公开函数的返回类型不得变（photo-server 等 6 类消费方依赖）。"""

    def test_encode_returns_list_float(self, monkeypatch):
        import niu_api.internal.embedding as emb

        monkeypatch.setattr(emb, "get_model", lambda: _FakeServerAdapter())
        out = emb.encode("hello")
        assert isinstance(out, list) and len(out) == 768

    def test_batch_encode_returns_list_of_list(self, monkeypatch):
        import niu_api.internal.embedding as emb

        monkeypatch.setattr(emb, "get_model", lambda: _FakeServerAdapter())
        out = emb.batch_encode(["a", "b", "c"])
        assert isinstance(out, list) and len(out) == 3 and len(out[0]) == 768

    def test_similarity_returns_float(self, monkeypatch):
        import niu_api.internal.embedding as emb

        monkeypatch.setattr(emb, "get_model", lambda: _FakeServerAdapter())
        out = emb.similarity("a", "b")
        assert isinstance(out, float)


class _FakeServerAdapter:
    """直接顶替 get_model() 的返回，带 SentenceTransformer 调用面。"""

    max_seq_length = 8192

    def encode(self, input, convert_to_numpy=True, show_progress_bar=False, **_):
        import numpy as np

        texts = [input] if isinstance(input, str) else list(input)
        arr = np.asarray([[1.0] * 768 for _ in texts], dtype=np.float32)
        if isinstance(input, str):
            arr = arr[0]
        return arr if convert_to_numpy else arr.tolist()


class TestMaxSeqLength:
    def test_llama_backend_not_silently_512(self, monkeypatch):
        """防回归：新后端不得静默拿到 512（长文本会被截断）。"""
        import niu_api.internal.embedding as emb

        monkeypatch.setattr(emb, "_model", None)
        monkeypatch.setattr(emb, "_get_embedding_model_name", lambda: "embeddinggemma-2")
        assert emb.get_embedding_max_seq_length() == 8192

    def test_unknown_model_returns_512_safe_default(self, monkeypatch):
        """未知模型名走既有保守回落 512（R1-④ 要求原样保留该链，故此处不得期望抛错）。"""
        import niu_api.internal.embedding as emb

        monkeypatch.setattr(emb, "_model", None)
        monkeypatch.setattr(emb, "_get_embedding_model_name", lambda: "no-such-model")
        assert emb.get_embedding_max_seq_length() == 512


class TestLifecycle:
    def test_stop_without_process_is_safe(self):
        """无进程时 stop() 必须安静返回（lifespan 停机与 atexit 兜底都会调）。"""
        llama_server.stop()
        llama_server.stop()  # 重复调用不得抛

    def test_stop_is_idempotent_after_fake_proc(self, monkeypatch):
        """有进程对象时重复 stop 不得抛，且第二次无动作（_FakeProc 为手写鸭子类型，
        故本用例不得 import subprocess —— R2 两角实测该 import 触发 F401）。"""

        class _FakeProc:
            def __init__(self):
                self.terminated = 0
                self.killed = 0

            def poll(self):
                return None  # 仍在跑

            def terminate(self):
                self.terminated += 1

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.killed += 1

        fake = _FakeProc()
        monkeypatch.setattr(llama_server, "_proc", fake)
        llama_server.stop()
        assert fake.terminated == 1
        assert fake.killed == 0
        # 第二次：_proc 已被取出置 None，不得再 terminate
        llama_server.stop()
        assert fake.terminated == 1

    def test_start_failure_path_does_not_call_stop(self):
        """防回归（R1-A P1）：start() 失败路径在持锁态，若调 stop() 会同线程自死锁
        （threading.Lock 非重入），导致 preload 永久挂起、契约 A 失效。
        此处钉死：失败分支必须调 _stop_locked()。"""
        import inspect

        src = inspect.getsource(llama_server.start)
        # 取失败分支（_wait_ready 判否之后到 raise 之前）
        tail = src.split("_wait_ready(")[-1]
        assert "_stop_locked()" in tail
        assert "stop()" not in tail.replace("_stop_locked()", "")

    def test_stop_and_stop_locked_both_exist(self):
        """锁内/锁外两个入口都必须存在，缺一即无法同时满足两条调用路径。"""
        assert callable(llama_server.stop)
        assert callable(llama_server._stop_locked)

    def test_encode_batch_empty_returns_empty(self, monkeypatch):
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        assert llama_server.encode_batch([]) == []
