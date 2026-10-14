"""S4b：媒体工作者状态机的 tick 接缝直调行为套件（AC-7/AC-8/AC-12 可编程半）。

tick 接缝（D-22）：生产由 ``Scheduler._run_loop`` 每轮直调 ``tick()``，测试直调**同一个函数**
（本文件不构造第二个副本）——临时队列文件 + ``TickEnv`` 逐项注入桩：``switch``/``ready_check``/
``encoder``/``guard``/``store_add``/``submit``。

fake submit 按 plan 钉死的**捕获异常版**：同步执行回调、``BaseException`` 捕进假 future
（``FakeFuture`` 提供 ``done()``/``result()``），与生产「异常进 future、由收敛归类」同构——
按字面写「同步执行 + 无条件 set_result」会让异常直接穿出 tick、与生产不同构（第二轮 P1 修正）。
另有「返回未完成假 future」版，模拟编码真实在途（池 worker 尚未跑完）。

两拍语义：同步 fake 下每行占两拍——提交拍（tick ⑥：``store_add`` 已收到该行、行**未删**、
状态 ``encoding``）+ 收敛拍（下一轮 tick ②：成功 ⇒ 删行 + idle / 失败 ⇒ 保留行 + 计数 + stalled）。

用例（plan S4b 步骤 2；用例⑨ ``on_stop`` 归 S4c 审查面、由 S4c 追加进本文件）：
① FIFO（多行顺序 + 至少一次的两拍语义）；② 至少一次（写库失败有界重试 → 达上限弃单）；
③ 在途不取新行；④ 在途超时与排队上报（判定只计「编码已开始」时段 / 排队段报可辨状态 /
  三拍断言：开跑后改回 encoding）；⑤ 像素护栏拒收（encoder 打桩成 AssertionError 证明永不被发送）；
⑥ 队列空短路（不探活，v3.2 条件求值顺序）；⑦ 开关热翻转（开→关：在途收敛后 off、不再取新行）；
⑧ 坏行弃除（醒目告警 + 合法行继续处理）；
⑨ on_stop 有界退出（R20，S4c 追加：有在途 ⇒ 主动中断编码〔桩 llama_server.stop 记调用〕、在途复位、状态 off、
⑩ 护栏 total（截断图头 / 桩抛异常都不穿透 tick）；
⑪ 读队列 total（非 UTF-8 ⇒ stalled + queue_depth=-1 哨兵；PermissionError 同族；坏行删不掉停取）；
⑫ 池不可提交（submit 抛 RuntimeError ⇒ 兜底 stalled、在途未发布、下轮同样兜住）；
⑬ ①/④ 逃逸兜底（switch 抛异常 ⇒ 先收敛在途再报可辨 stalled；ready_check 抛异常 ⇒ stalled 且队首行保留）；
⑭ 成功路径删行失败（第四轮 P2-1：stalled + 保留在途 + 下轮重试收敛，绝不静默永停 encoding）。
"""
import json
import time

import pytest

import niu_api.internal.media_worker as mw
from agent.md_mirror import append_record
from niu_api.internal.media_worker import TickEnv


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class FakeFuture:
    """与 concurrent.futures.Future 同形的假 future：``done()``/``result()``，
    异常经 ``set_exception`` 存入、``result()`` 重抛——与生产「异常进 future、由收敛归类」同构。"""

    def __init__(self):
        self._done = False
        self._exception = None
        self._result = None

    def set_result(self, value):
        self._result = value
        self._done = True

    def set_exception(self, exc):
        self._exception = exc
        self._done = True

    def done(self):
        return self._done

    def result(self):
        if not self._done:
            raise RuntimeError("result() 被调用在 future 未完成上（生产只在 done() 为真后调用）")
        if self._exception is not None:
            raise self._exception
        return self._result


def make_submit_sync():
    """同步执行 + 异常捕进假 future 的 fake submit（与生产同构），返回 (submit, futures)。"""
    futures = []

    def submit(fn):
        f = FakeFuture()
        futures.append(f)
        try:
            fn()
        except BaseException as e:
            f.set_exception(e)
        else:
            f.set_result(None)
        return f

    return submit, futures


def make_submit_pending():
    """返回未完成假 future 的 fake submit（回调不执行）——模拟编码真实在途。"""
    futures = []

    def submit(fn):
        f = FakeFuture()
        futures.append(f)
        return f

    return submit, futures


def rec(entity="a/img-0001.jpg", path="/tmp/fake/img.jpg", ts="2026-10-13T00:00:00"):
    """一条合法的 F4 队列行（D-24 三键）。"""
    return {"entity_name": entity, "file_path": path, "ts": ts}


def write_queue(queue_file, records):
    """经 S3 唯一队列写入路径（append_record，排它锁 + O_APPEND 持锁写）逐行写入。"""
    for r in records:
        assert append_record(json.dumps(r, ensure_ascii=False) + "\n", md_path=str(queue_file))


def queue_lines(queue_file):
    """队列文件实际行数（非空行）。"""
    if not queue_file.exists():
        return 0
    return sum(1 for ln in queue_file.read_text(encoding="utf-8").splitlines() if ln.strip())


@pytest.fixture(autouse=True)
def _reset_worker_state(monkeypatch, tmp_path):
    """每用例前复位模块状态：在途记录 / 失败计数 / 状态机 / 媒体库接缝（绝不写真 ~/.niu）。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_store.json"))
    monkeypatch.setattr(mw, "_inflight", None)
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0)
    yield
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0)


# ---------------------------------------------------------------------------
# ① FIFO：多行顺序消费，两拍语义（AC-7）
# ---------------------------------------------------------------------------

def test_fifo_two_lines_submit_then_converge(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/img-0001.jpg", str(tmp_path / "1.jpg")),
                    rec("a/img-0002.jpg", str(tmp_path / "2.jpg"))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # tick#1 提交拍：行 1 的 store_add 已收到、行未删、状态 encoding
    assert calls == ["a/img-0001.jpg"]
    assert queue_lines(q) == 2
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # tick#2 收敛拍：成功删行 1
    assert queue_lines(q) == 1
    assert mw.get_state()["status"] == "idle"
    assert mw._inflight is None
    mw.tick(env)  # tick#3 提交行 2
    assert queue_lines(q) == 1
    mw.tick(env)  # tick#4 收敛清空队列
    assert queue_lines(q) == 0
    assert mw.get_state()["status"] == "idle"
    assert calls == ["a/img-0001.jpg", "a/img-0002.jpg"]  # 调用顺序 = 行序


# ---------------------------------------------------------------------------
# ② 至少一次：写库失败保留行、有界重试，达上限弃单（AC-7）
# ---------------------------------------------------------------------------

def test_store_add_failure_bounded_retry_then_drop(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/broken.jpg", "/tmp/fake/broken.jpg")])
    events = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom_store_add(e, m, v):
        raise RuntimeError("媒体库写入失败")

    def submit(fn):
        f = FakeFuture()
        try:
            fn()
        except BaseException as e:
            f.set_exception(e)
        else:
            f.set_result(None)
        return f

    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=boom_store_add, submit=submit)

    mw.tick(env)  # tick#1 提交（store_add 抛错 → 异常进 future），行未删、状态 encoding
    assert queue_lines(q) == 1
    assert mw.get_state()["status"] == "encoding"

    mw.tick(env)  # tick#2 收敛判失败：行仍在、stalled、reason 含失败原因、计数 1
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "媒体库写入失败" in st["reason"]
    assert mw._fail_counts.get("a/broken.jpg") == 1
    assert queue_lines(q) == 1

    mw.tick(env)  # tick#3 重新提交同一行
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # tick#4 收敛计数 2
    assert mw._fail_counts.get("a/broken.jpg") == 2
    mw.tick(env)  # tick#5 再提交
    mw.tick(env)  # tick#6 收敛达上限 ⇒ 弃单：行删除、广播 ok=False、状态回 idle

    st = mw.get_state()
    assert st["status"] == "idle"
    assert queue_lines(q) == 0
    assert "a/broken.jpg" not in mw._fail_counts
    drops = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert drops and "弃单" in drops[-1]["message"]


# ---------------------------------------------------------------------------
# ③ 在途不取新行：同一时刻至多一条在途（AC-12 可编程半）
# ---------------------------------------------------------------------------

def test_inflight_blocks_new_head_until_converged(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/img-0001.jpg", str(tmp_path / "1.jpg")),
                    rec("a/img-0002.jpg", str(tmp_path / "2.jpg"))])
    calls = []
    submit, futures = make_submit_pending()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)

    mw.tick(env)  # tick#1 提交行 1（未完成 future）
    assert mw._inflight is not None
    assert mw.get_state()["status"] == "encoding"
    assert calls == []  # worker 未跑 ⇒ store_add 未被调用
    mw.tick(env)  # tick#2 只收敛不提交新行（store_add 调用数不变）
    assert mw._inflight is not None
    assert mw.get_state()["status"] == "encoding"
    assert calls == []
    assert queue_lines(q) == 2

    futures[0].set_result(None)  # future 完成
    mw.tick(env)  # tick#3 收敛删行 1
    assert queue_lines(q) == 1
    assert mw.get_state()["status"] == "idle"
    assert mw._inflight is None
    mw.tick(env)  # tick#4 才取行 2（提交与收敛是两拍，不得合并断言）
    assert queue_lines(q) == 1
    assert mw.get_state()["status"] == "encoding"
    assert len(futures) == 2


# ---------------------------------------------------------------------------
# ④ 在途超时与排队上报：判定只计「编码已开始」时段（R19），排队段报可辨状态（R14/D-27）
# ---------------------------------------------------------------------------

def test_inflight_timeout_and_pool_saturation_reporting(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "INFLIGHT_TIMEOUT", 0.01)

    # (a) started=True + 回拨 started_at：编码已实际开始却永不完成 ⇒ 判失败，允许取下一条
    qa = tmp_path / "queue.md"
    write_queue(qa, [rec("a/t1.jpg", "/tmp/fake/t1.jpg")])
    submit_a, _ = make_submit_pending()
    env_a = TickEnv(queue_path=str(qa), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_a)
    mw.tick(env_a)
    assert mw._inflight is not None
    mw._inflight.started = True
    mw._inflight.started_at = time.time() - 5.0  # 模拟「编码已开始、超时仍未完成」
    mw.tick(env_a)  # 收敛：在途超时判失败
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "在途超时" in st["reason"]
    assert mw._inflight is None
    assert queue_lines(qa) == 1  # 放弃 ≠ 删行（重放语义不变，R15）
    mw.tick(env_a)  # 下一轮能重新取行（不冻结）
    assert mw.get_state()["status"] == "encoding"

    # (b) started=False + 回拨量在阈值内：排队段不计入超时判定（R19）
    mw._inflight = None
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0)
    qb = tmp_path / "queue_b.md"
    write_queue(qb, [rec("a/t2.jpg", "/tmp/fake/t2.jpg")])
    submit_b, futures_b = make_submit_pending()
    env_b = TickEnv(queue_path=str(qb), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_b)
    mw.tick(env_b)  # 提交（started_at = 提交时刻）
    assert mw._inflight is not None
    mw._inflight.started_at = time.time() - 0.005  # 回拨量 < 阈值 0.01
    mw.tick(env_b)  # 不触发任何超时判定
    assert mw.get_state()["status"] == "encoding"  # 状态保持 encoding（不是 stalled）
    assert mw._inflight is not None

    # (c) started=False 且回拨超阈值：池饱和、编码从未开始 ⇒ stalled「编码排队中」，
    # 不判失败、不删行、不清计数，记录保持在途（第二轮 P2-1 回归）
    mw._inflight.started_at = time.time() - 0.02
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "编码排队中" in st["reason"]
    assert mw._inflight is not None
    assert mw._fail_counts == {}
    assert queue_lines(qb) == 1

    # (d) 三拍断言（第三轮 P3-1 回归）：记录转 started=True 后改回 encoding；future 完成收敛照常
    mw._inflight.started = True
    mw._inflight.started_at = time.time()
    mw.tick(env_b)
    assert mw.get_state()["status"] == "encoding"  # 不再是「编码排队中」的旧状态
    futures_b[0].set_result(None)
    mw.tick(env_b)
    assert mw.get_state()["status"] == "idle"
    assert mw._inflight is None
    assert queue_lines(qb) == 0


# ---------------------------------------------------------------------------
# ⑤ 像素护栏拒收：encoder 永不被发送（AC-8），原因可辨
# ---------------------------------------------------------------------------

def test_guard_reject_drops_line_without_encoding(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/big.jpg", str(tmp_path / "big.jpg"))])
    events = []
    notices = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)
    calls = []
    never_encoded = []

    def never_encode(p):
        # 具名编码器探针（旧版语义）：先记录、再抛——「拒收误提交池」回归下，抛出的异常会被
        # make_submit_sync 吞进假 future，但记录吞不掉 ⇒ 零调用断言独立证明「真的没发」
        never_encoded.append(p)
        raise AssertionError("拒收不得编码")

    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=never_encode,
                  guard=lambda p: ("reject", "文件 31MB 超体积预算 30MB", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert queue_lines(q) == 0  # 行已弃除
    assert never_encoded == []  # 编码器入口从未被调用（具名探针的独立信号）
    assert calls == []  # store_add 零调用 ⇒ 未进写库路径
    rejects = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert rejects and "超体积预算" in rejects[0]["message"]
    assert notices and "超体积预算" in notices[-1]   # 拒收原因口径随 I-6 更新


def test_guard_reject_drop_failure_stalls_and_stays_silent(tmp_path, monkeypatch):
    """拒收 + 删行失败（drop 返 0 / 抛异常）：行仍在队首、报可辨 stalled 原因、**不**发
    「拒收」通知——每轮重复提示会成 10s 级通知风暴；下一轮重试该行（再拒收、仍无通知）。
    回归方向：stalled 分支退回发提示 / 清零失败计数 / 报 idle ⇒ 下列断言各自转红
    （tick ⑤ reject 分支 else 分支）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/big.jpg", str(tmp_path / "big.jpg"))])
    events = []
    notices = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)
    drop_calls = []
    monkeypatch.setattr(mw, "f4_drop_prefix",
                       lambda n=1, queue_path=None: (drop_calls.append(n), 0)[1])  # 删行恒失败（锁/IO/撕裂尾行形态）
    guard_calls = []

    def rejecting_guard(p):
        guard_calls.append(p)
        return ("reject", "文件 31MB 超体积预算 30MB", None)

    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: (_ for _ in ()).throw(AssertionError("拒收不得编码")),
                  guard=rejecting_guard,
                  store_add=lambda e, m, v: None, submit=submit)
    mw._fail_counts["a/big.jpg"] = 1  # 预置失败计数：删行失败路径必须保留（退回清零 ⇒ 红）
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "拒收但删行失败" in st["reason"]  # reason 可辨
    assert st["entity"] == "a/big.jpg"
    assert st["queue_depth"] == 1  # 行仍在队首
    assert queue_lines(q) == 1
    assert drop_calls == [1]  # 尝试过删行，失败
    assert events == [] and notices == []  # 不发「拒收」类通知（不重复提示）
    assert mw._fail_counts["a/big.jpg"] == 1  # 失败计数保留
    mw.tick(env)  # 下一轮：重试该行
    st = mw.get_state()
    assert st["status"] == "stalled" and "拒收但删行失败" in st["reason"]
    assert guard_calls == [str(tmp_path / "big.jpg")] * 2  # 两轮都取到该行（重试行为）
    assert drop_calls == [1, 1]
    assert events == [] and notices == []  # 每轮仍不重复提示
    assert queue_lines(q) == 1

# ---------------------------------------------------------------------------
# ⑥ 队列空短路：不探活、不判就绪（v3.2 条件求值顺序）
# ---------------------------------------------------------------------------

def test_empty_queue_never_probes(tmp_path):
    q = tmp_path / "queue.md"
    q.write_text("", encoding="utf-8")

    def boom():
        raise AssertionError("队列空时绝不应调 ready_check（不探活，v3.2 条件求值顺序）")

    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=boom,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)  # 不得抛异常
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["queue_depth"] == 0


# ---------------------------------------------------------------------------
# ⑦ 开关热翻转：开→关 时在途条收敛处理掉、不再取新行（AC-12）
# ---------------------------------------------------------------------------

def test_switch_hot_flip_keeps_remaining_lines(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/img-0001.jpg", str(tmp_path / "1.jpg")),
                    rec("a/img-0002.jpg", str(tmp_path / "2.jpg"))])
    calls = []
    submit, futures = make_submit_pending()
    base = dict(queue_path=str(q), ready_check=lambda: True,
                encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                store_add=lambda e, m, v: calls.append(e), submit=submit)
    env_on = TickEnv(switch=lambda: True, **base)
    mw.tick(env_on)  # 开关开：取行 1（在途未完成）
    assert mw.get_state()["status"] == "encoding"
    assert len(calls) == 0
    futures[0].set_result(None)  # 在途收敛完成
    env_off = TickEnv(switch=lambda: False, **base)
    mw.tick(env_off)  # 开关关：先收敛在途（删行 1），然后状态 off、不再取新行
    st = mw.get_state()
    assert st["status"] == "off"
    assert "开关关" in st["reason"]
    assert queue_lines(q) == 1  # 行 2 原样保留（下次开开关重放）
    assert len(futures) == 1  # 行 2 从未提交（pending 夹具下 worker 回调不执行，store_add 零调用）
    assert calls == []
    assert mw._inflight is None


# ---------------------------------------------------------------------------
# ⑧ 坏行弃除：队首坏行被醒目告警弃掉，合法行继续处理（D-24）
# ---------------------------------------------------------------------------

def test_bad_head_line_swept_good_line_processed(tmp_path):
    q = tmp_path / "queue.md"
    q.write_text("not-json\n" + json.dumps(rec("a/img-0001.jpg", str(tmp_path / "g.jpg")),
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 坏行弃除 + 合法行取走（同步 fake 已执行编码/写库桩）
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # 收敛删行
    assert queue_lines(q) == 0
    assert calls == ["a/img-0001.jpg"]
    assert mw.get_state()["status"] == "idle"


# ---------------------------------------------------------------------------
# ⑩ 护栏 total（第一轮 P1 回归）：截断图头 / 桩抛异常都不穿透 tick
# ---------------------------------------------------------------------------

def test_guard_total_truncated_header_passes_without_exception(tmp_path):
    from niu_api.internal import pixel_guard  # 生产真值护栏
    img = tmp_path / "trunc.png"
    # 16 字节：合法 PNG 签名（8B）+ IHDR 段长与段名，宽高字节缺失（截断图头）
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0d" + b"IHDR")
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/trunc.jpg", str(img))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=pixel_guard.classify,
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 不得抛异常（护栏按契约「图头读不出 ⇒ 放行 + WARNING」）
    assert calls == ["a/trunc.jpg"]  # encoder 被调用 ⇒ 异常不再穿透 tick
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # 收敛删行，同一行不会每轮崩死
    assert mw.get_state()["status"] == "idle"
    assert queue_lines(q) == 0


def test_guard_stub_exception_treated_as_pass(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", str(tmp_path / "x.jpg"))])

    def bad_guard(p):
        raise AssertionError("护栏桩抛异常（契约漂移模拟）")

    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=bad_guard,
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 第二道防线：按放行处理、绝不崩本轮 tick
    assert calls == ["a/x.jpg"]
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)
    assert mw.get_state()["status"] == "idle"


# ---------------------------------------------------------------------------
# T2 新增：downscale 动作 / guard 缺省路径（P0 回归）
# ---------------------------------------------------------------------------

def test_downscale_action_submits_like_pass(tmp_path):
    """护栏判 downscale（超门槛、非病态）⇒ 不弃行、正常提交编码（缩放由分派器完成，I-2）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec()])
    calls = []
    submit, _futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768,
                  guard=lambda p: ("downscale", "4000×4000 超直送门槛 8MP", (4000, 4000)),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)
    mw.tick(env)  # 收敛拍
    assert calls == ["a/img-0001.jpg"] and queue_lines(q) == 0  # 行被消费，未被拒收


def test_guard_default_resolves_to_classify(tmp_path, monkeypatch):
    """TickEnv 不注入 guard ⇒ tick 前言的缺省绑定必须解析到 `pixel_guard.classify`
    （`check` 移除后 tick 不得抛 AttributeError——否则媒体队列永不被消费；存量用例全部
    注入 guard=，此路径此前无覆盖）。spy `classify` 钉死缺省绑定「确实解析到 classify 且被
    调用」的方向（误删 `or` 子句 ⇒ guard=None ⇒ 保护性 try 吞成 pass ⇒ 下列断言转红）。
    队列行的 file_path 指向不存在的文件 ⇒ classify 按契约放行（文件缺失 ⇒ pass，total
    函数），随后照常提交注入的 encoder。"""
    from niu_api.internal import pixel_guard

    q = tmp_path / "queue.md"
    write_queue(q, [rec()])  # rec() 缺省 file_path=/tmp/fake/img.jpg 不存在 ⇒ classify 放行
    guard_calls = []
    real_classify = pixel_guard.classify

    def spy_classify(p):
        guard_calls.append(p)
        return real_classify(p)  # 委托生产真值护栏，spy 只记录不改行为

    monkeypatch.setattr(pixel_guard, "classify", spy_classify)
    calls = []
    submit, _futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768,  # 注入 encoder 旁路生产分派器
                  store_add=lambda e, m, v: calls.append(e), submit=submit)  # guard 刻意不注入
    mw.tick(env)  # 不得抛 AttributeError（缺省绑定 = pixel_guard.classify，三动作解构成立）
    assert guard_calls == ["/tmp/fake/img.jpg"]  # guard 确实解析到 classify 且被调用（误删 `or` 子句 ⇒ guard=None ⇒ pass ⇒ 红）
    assert calls == ["a/img-0001.jpg"]  # encoder 被调用 ⇒ 行按 pass 分支照常提交
    mw.tick(env)
    assert mw.get_state()["status"] == "idle" and queue_lines(q) == 0


# ---------------------------------------------------------------------------
# ⑪ 读队列 total（第二轮 P1-1 回归）：任何读异常绝不外抛崩 tick，绝不永久冻结
# ---------------------------------------------------------------------------

def test_unreadable_queue_non_utf8_stalls_with_depth_sentinel(tmp_path):
    q = tmp_path / "queue.md"
    q.write_bytes(b"\xff\xfe" + "photos/img-0001.jpg".encode("utf-16-le"))  # 非 UTF-8 字节
    before = q.read_bytes()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)  # 不抛异常
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "队列文件读不出" in st["reason"]
    assert st["queue_depth"] == -1  # -1 哨兵：行数未知，绝不误报 0 行
    assert q.read_bytes() == before  # 队列行未删
    mw.tick(env)  # 再跑一次：同样不抛、状态不变（不会每轮崩死、不永久冻结）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert st2["queue_depth"] == -1


def test_unreadable_queue_permission_error(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def deny(path=None):
        raise PermissionError("permission denied")

    monkeypatch.setattr(mw, "f4_take_head", deny)
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "PermissionError" in st["reason"]
    assert queue_lines(q) == 1  # 行未删，下轮可重试


def test_bad_head_undroppable_stalls_without_burning_sweep_budget(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text("not-json\n" + json.dumps(rec("a/x.jpg", "/tmp/fake/x.jpg"),
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    takes, drops = [], []
    real_take = mw.f4_take_head

    def counting_take(path=None):
        takes.append(path)
        return real_take(path)

    monkeypatch.setattr(mw, "f4_take_head", counting_take)
    monkeypatch.setattr(mw, "f4_drop_prefix", lambda n=1, queue_path=None: (drops.append(n), 0)[1])
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "坏行删不掉" in st["reason"]
    assert len(drops) <= 1 and len(takes) <= 1  # 不烧穿单轮弃行上限（32）
    assert queue_lines(q) == 2  # 坏行仍在队首，下轮重试


# ---------------------------------------------------------------------------
# ⑫ 池不可提交（第三轮 P2 回归）：submit 抛 RuntimeError ⇒ 兜底 stalled、下轮同样兜住
# ---------------------------------------------------------------------------

def test_submit_rejected_stalls_and_stays_stable(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def dead_submit(fn):
        raise RuntimeError("池已 shutdown")

    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None, submit=dead_submit)
    mw.tick(env)  # 不抛异常
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "媒体线程池不可提交" in st["reason"]
    assert mw._inflight is None  # 在途记录未发布
    assert queue_lines(q) == 1   # 队首行未删
    mw.tick(env)  # 再跑一次：同一行在同一处同样被兜住（不崩死、状态稳定）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert "媒体线程池不可提交" in st2["reason"]
    assert mw._inflight is None


# ---------------------------------------------------------------------------
# ⑬ ①/④ 逃逸兜底（第三轮 P2 回归）：switch/ready_check 抛异常也不外泄
# ---------------------------------------------------------------------------

def test_switch_exception_converges_inflight_first(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, futures = make_submit_pending()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None, submit=submit)
    mw.tick(env)  # 预置一条在途记录（未完成）
    assert mw._inflight is not None
    futures[0].set_result(None)  # 在途已完成（可收敛删行）

    def broken_switch():
        raise ValueError("配置读取失败")

    env_bad = TickEnv(queue_path=str(q), switch=broken_switch, ready_check=lambda: True,
                      guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                      store_add=lambda e, m, v: None, submit=submit)
    mw.tick(env_bad)  # 不抛异常：先收敛在途（行删掉），再报可辨 stalled
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "开关读取异常" in st["reason"]
    assert mw._inflight is None  # 在途已被收敛
    assert queue_lines(q) == 0


def test_ready_check_exception_stalls_keeps_head(tmp_path):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def boom_ready():
        raise AssertionError("就绪检查桩抛异常")

    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=boom_ready,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)  # 不抛异常
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "就绪检查异常" in st["reason"]
    assert queue_lines(q) == 1  # 队首行未删（下轮重取）


# ---------------------------------------------------------------------------
# ⑭ 成功路径删行失败（第四轮 P2-1 回归）：stalled + 保留在途 + 下轮重试收敛，绝不静默永停 encoding
# ---------------------------------------------------------------------------

def test_success_path_drop_failure_stalls_then_recovers(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 提交成功：store_add 已收到该行、_inflight 非空、状态 encoding
    assert mw._inflight is not None
    assert calls == ["a/x.jpg"]
    assert mw.get_state()["status"] == "encoding"

    real_drop = mw.f4_drop_prefix
    monkeypatch.setattr(mw, "f4_drop_prefix", lambda n=1, queue_path=None: 0)  # 恒返 0（删行失败）；函数域 monkeypatch，中途断言失败也不把 stub 泄漏进模块（级联假红）
    mw.tick(env)  # 收敛：删行失败 ⇒ stalled + 保留在途，不抛异常
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "编码完成但删行失败" in st["reason"]
    assert mw._inflight is not None  # 保留（下轮重试删行、再收敛）
    assert queue_lines(q) == 1
    mw.tick(env)  # 再跑一次：状态稳定同为 stalled（每轮在 ② 短路重发同一可辨状态、不是假进度）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert "编码完成但删行失败" in st2["reason"]
    monkeypatch.setattr(mw, "f4_drop_prefix", real_drop)  # 恢复真值
    mw.tick(env)  # 收敛删行成功
    st3 = mw.get_state()
    assert st3["status"] == "idle"
    assert mw._inflight is None
    assert queue_lines(q) == 0


# ---------------------------------------------------------------------------
# ⑨ on_stop 有界退出（R20，S4c 审查面）：有在途编码 ⇒ 主动中断（终止编码子进程 ⇒ 在途 urlopen
# 即刻失败 ⇒ 池 worker 即刻返回 ⇒ 退出有界——线程池 join 先于普通 atexit，2026-10-13 本机探针实证），
# 在途复位、状态 off、行未删（重放语义，R15）；对照：无在途 ⇒ llama_server.stop 不被调用。
# 桩 llama_server.stop 记录调用，避免测试里真终止子进程。
# ---------------------------------------------------------------------------

def test_on_stop_with_inflight_interrupts_encoding(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, _ = make_submit_pending()  # 未完成假 future：编码真实在途（回调永不执行）
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit)
    mw.tick(env)  # 提交拍：在途记录已发布、状态 encoding、行未删
    assert mw._inflight is not None
    assert mw.get_state()["status"] == "encoding"
    stop_calls = []
    monkeypatch.setattr(mw.llama_server, "stop", lambda: stop_calls.append(1))  # 桩：不真终止子进程
    mw.on_stop()
    assert len(stop_calls) == 1  # 有在途 ⇒ 主动中断编码（R20 有界退出的真中断手段，不是只记日志）
    assert mw._inflight is None  # 在途记录复位
    assert mw.get_state()["status"] == "off"
    assert queue_lines(q) == 1  # 行未删 ⇒ 下次启动重放（R15 至少一次语义不变）


def test_on_stop_without_inflight_does_not_interrupt(tmp_path, monkeypatch):
    stop_calls = []
    monkeypatch.setattr(mw.llama_server, "stop", lambda: stop_calls.append(1))
    mw.on_stop()
    assert stop_calls == []  # 无在途 ⇒ 不碰编码子进程
    assert mw.get_state()["status"] == "off"
    assert mw._inflight is None
