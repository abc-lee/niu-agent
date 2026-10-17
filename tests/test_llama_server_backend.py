"""llama.cpp 编码后端（S2）。

禁真实 llama-server / 禁真实模型 / 禁图谱写入。以假 HTTP 层驱动适配器，
逐一核验现有 6 类消费方的调用契约不被破坏（这是本方案的最大回归面）。
"""

import io
import threading
import time
import urllib.error

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


@pytest.fixture(autouse=True)
def _reset_vector_activity(monkeypatch):
    """复位进程全局的向量活跃度状态（R1-A P2-3）：真实探针是全进程共享的，同进程里
    任何用例调过 encode_batch 都会污染它，使「初始 inf ∧ 不在途」类用例随执行顺序
    假红/假绿。本文件所有用例一律从「从未文本编码、无在途」的干净初态出发。"""
    monkeypatch.setattr(llama_server, "_last_text_encode_at", None)
    monkeypatch.setattr(llama_server, "_text_inflight", 0)


class TestVectorIdleGateAccounting:
    """向量闲置门打点侧（D-31）：encode_batch 计数在途+完成打点，encode_media* 两者皆无。

    媒体工作者的门（D-31）查的就是 vector_idle_seconds() / text_encode_busy()，
    打点侧语义必须逐条钉死：只记**文本**编码；媒体自身编码绝不打点——否则连编会把自己
    的下一项越推越晚，排空被卡成每项间隔 30s（用户口径①）。
    """

    def test_initial_state_idle_inf_and_not_busy(self):
        """新进程（从未文本编码）：闲置 inf ⇒ 零白等（AC-4），且不在途。"""
        assert llama_server.vector_idle_seconds() == float("inf")
        assert llama_server.text_encode_busy() is False

    def test_text_encode_marks_and_counts_inflight(self, monkeypatch):
        """文本编码：在途期间计数 1（**含等待 _encode_lock 的排队段**），完成后打点。
        桩内读 busy 断言的是「编码执行当下」，不是事后残值。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        seen_busy = []

        def fake_post(texts):
            seen_busy.append(llama_server.text_encode_busy())
            return [[1.0] * 768 for _ in texts]

        monkeypatch.setattr(llama_server, "_post_embeddings", fake_post)
        out = llama_server.encode_batch(["a", "b"])
        assert len(out) == 2 and all(len(v) == 768 for v in out)
        assert seen_busy == [True]              # 桩内（编码执行中）在途计数为真
        assert llama_server.text_encode_busy() is False
        assert llama_server.vector_idle_seconds() < 5.0   # 刚完成打点 ⇒ 闲置近 0

    def test_media_encode_never_marks_nor_counts(self, monkeypatch):
        """媒体自身编码不打点、不计在途（用户口径①）：连编完一项，闲置时钟原封不动。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        monkeypatch.setattr(llama_server, "_mmproj_loaded", True)
        seen_busy = []

        def fake_post_media(uri):
            seen_busy.append(llama_server.text_encode_busy())
            return [0.5] * 768

        monkeypatch.setattr(llama_server, "_post_media", fake_post_media)
        out = llama_server.encode_media_bytes(b"fake-jpeg", "image/jpeg")
        assert len(out) == 768
        assert seen_busy == [False]             # 桩内（媒体编码执行中）也不在途
        assert llama_server.vector_idle_seconds() == float("inf")   # 未打点 ⇒ 仍「从未文本编码」
        assert llama_server.text_encode_busy() is False

    def test_failure_path_no_mark_and_inflight_back_to_zero(self, monkeypatch):
        """失败路径（非 HTTPError 异常穿透）不打点，且在途计数 finally 归零。
        计数泄漏 = busy 永远为真 = 门永久关闭，是最保守也最危险的不变量。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)

        def fake_post(texts):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(llama_server, "_post_embeddings", fake_post)
        with pytest.raises(urllib.error.URLError):
            llama_server.encode_batch(["a"])
        assert llama_server.vector_idle_seconds() == float("inf")   # 失败不打点
        assert llama_server.text_encode_busy() is False             # 计数已归零

    def test_empty_input_no_mark_nor_inflight(self, monkeypatch):
        """空输入在计数/打点之前 return []（披露路径钉住，R1-A P3-2）：
        若空输入也打点，任何空探测都会把门误关 30s。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: False)   # 连健康检查都不该走到
        boom = []
        explode = lambda: boom.append(1)                                # noqa: E731
        monkeypatch.setattr(llama_server, "_begin_text_encode", explode)
        monkeypatch.setattr(llama_server, "_mark_text_encode", explode)
        assert llama_server.encode_batch([]) == []
        assert not boom                                          # 打点/计数函数从未被调
        assert llama_server.vector_idle_seconds() == float("inf")
        assert llama_server.text_encode_busy() is False

    def test_guardrail_split_path_inflight_balances(self, monkeypatch):
        """护栏（超限 → 拆批救回）路径：整批虽被服务端拒绝，但救回后正常返回 ⇒
        在途计数同样必须归零（计数泄漏 = 门永久关闭；审查 P3 增补）。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        body = b"input (99999 tokens) is too large to process. increase the physical batch size (current batch size: 8192)"

        def fake_post(texts):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:19878/v1/embeddings", 500, "too large", {}, io.BytesIO(body)
            )

        monkeypatch.setattr(llama_server, "_post_embeddings", fake_post)
        monkeypatch.setattr(llama_server, "_encode_one", lambda text: [2.0] * 768)
        out = llama_server.encode_batch(["a" * 8000, "short"])
        assert len(out) == 2 and len(out[0]) == 768     # 拆批救回真的发生了
        assert llama_server.text_encode_busy() is False   # 护栏路径的计数也归零

    def test_two_overlapping_text_batches_count_two_then_zero(self, monkeypatch):
        """两次 encode_batch 计数配平：并发在途时计数到达 2，全部结束后归 0
        （桩内读计数：两调用重叠在途 ⇒ 峰值 2；不配平 = 泄漏，门永久关闭）。"""
        monkeypatch.setattr(llama_server, "_health_ok", lambda: True)
        peak = []

        def fake_post(texts):
            time.sleep(0.3)   # 拉长编码段，让第二个调用也在途（排队等锁段同样计入）
            with llama_server._activity_lock:
                peak.append(llama_server._text_inflight)
            return [[1.0] * 768 for _ in texts]

        monkeypatch.setattr(llama_server, "_post_embeddings", fake_post)
        threads = [
            threading.Thread(target=llama_server.encode_batch, args=(["a"],)),
            threading.Thread(target=llama_server.encode_batch, args=(["b"],)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert peak and max(peak) == 2                 # 重叠在途时计数确实到 2
        assert llama_server.text_encode_busy() is False   # 全部结束后归 0


class TestPostMediaResponseShape:
    """``_post_media`` 响应结构解析（审查 P2-1）：200 响应但结构畸形时，必须同样归入
    ``MediaEncodeBackendError``（唯一可重试类别），不得让裸 AttributeError/KeyError/TypeError
    穿透到工作者端被误判「非后端」而一次处置——响应结构异常的重试预算本就应当保留。"""

    def test_malformed_response_missing_embedding_key_raises_backend_error(self, monkeypatch):
        """200 + 畸形 JSON（``{"data": [{}]}``——条目缺 ``embedding`` 键）⇒ ``MediaEncodeBackendError``。
        旧实现在 ``items[0]["embedding"]`` 处穿透 KeyError（红）。"""
        monkeypatch.setattr(llama_server, "_base_url", lambda: "http://127.0.0.1:19878")

        class _Resp:
            """与 ``urlopen`` 返回值同形的假响应（上下文管理器 + ``read()``）。"""

            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(llama_server.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp(b'{"data": [{}]}'))
        with pytest.raises(llama_server.MediaEncodeBackendError, match="响应结构异常"):
            llama_server._post_media("data:image/jpeg;base64,AAAA")

    def test_malformed_response_data_not_dict_raises_backend_error(self, monkeypatch):
        """200 + 畸形 JSON（顶层 ``data`` 不是 dict ⇒ ``.get`` 穿透 AttributeError）⇒
        同样归入后端故障类别（红）。"""
        monkeypatch.setattr(llama_server, "_base_url", lambda: "http://127.0.0.1:19878")

        class _Resp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(llama_server.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp(b'{"data": {"embedding": [1.0]}}'))
        # ``data.get("data")`` 是 dict（truthy）⇒ 直接当 items；单键 ⇒ len == 1 ⇒
        # 走 ``items[0]`` ⇒ dict 整数字典键 KeyError ⇒ 后端类别。
        with pytest.raises(llama_server.MediaEncodeBackendError):
            llama_server._post_media("data:image/jpeg;base64,AAAA")

    def test_malformed_response_non_numeric_embedding_raises_backend_error(self, monkeypatch):
        """200 + ``embedding`` 含不可转 float 的值（``"nan-string"``）⇒ ``MediaEncodeBackendError``。
        旧实现 ``list(map(float, ...))`` 穿透裸 ValueError（红）⇒ 工作者误判「非后端」而一次处置，
        吃掉本可用于响应结构异常的重试预算。"""
        monkeypatch.setattr(llama_server, "_base_url", lambda: "http://127.0.0.1:19878")

        class _Resp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(llama_server.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp(b'{"data": [{"embedding": ["nan-string", 0.1]}]}'))
        with pytest.raises(llama_server.MediaEncodeBackendError, match="响应结构异常"):
            llama_server._post_media("data:image/jpeg;base64,AAAA")

    def test_read_connection_reset_raises_backend_error(self, monkeypatch):
        """传输中断：``resp.read()`` 抛 ``OSError``（如 ``ConnectionResetError``；
        ``http.client.IncompleteRead`` 亦是其子类）⇒ 同归后端故障类别（唯一可重试）。
        旧 except 元组只有 ``HTTPError/URLError/TimeoutError/JSONDecodeError``，裸
        ``OSError`` 穿透 ⇒ 工作者误判「非后端」而一次处置，吃掉重试预算。"""
        monkeypatch.setattr(llama_server, "_base_url", lambda: "http://127.0.0.1:19878")

        class _Resp:
            def __init__(self):
                pass

            def read(self):
                raise OSError("connection reset")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(llama_server.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        with pytest.raises(llama_server.MediaEncodeBackendError,
                          match="媒体编码失败.*OSError"):
            llama_server._post_media("data:image/jpeg;base64,AAAA")

    def test_read_non_utf8_bytes_raises_backend_error(self, monkeypatch):
        """``read()`` 返回非 UTF-8 且无受认 BOM 的字节（``b'\\x80\\x81'``——无 BOM 时
        ``json.loads`` 走严格 UTF-8 解码）⇒ ``UnicodeDecodeError``（``ValueError`` 子类，
        **不是** ``JSONDecodeError``）⇒ 旧 except 元组漏接、裸穿透；现归后端故障类别。
        注：任务示例 ``b'\\xff\\xfe'`` 会被 ``detect_encoding`` 识为 UTF-16 BOM，实际抛
        ``JSONDecodeError``（旧元组已覆盖），钉不住本路径，故用裸非法字节。"""
        monkeypatch.setattr(llama_server, "_base_url", lambda: "http://127.0.0.1:19878")

        class _Resp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(llama_server.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp(b'\x80\x81'))
        with pytest.raises(llama_server.MediaEncodeBackendError,
                          match="媒体编码失败.*UnicodeDecodeError"):
            llama_server._post_media("data:image/jpeg;base64,AAAA")
