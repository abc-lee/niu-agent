"""v3.5：媒体工作者状态机换代（新原语接线 + 真进度状态面）的 tick 接缝直调行为套件。

tick 接缝（D-22）：生产由 ``Scheduler._run_loop`` 每轮直调 ``tick()``，测试直调**同一个函数**
（本文件不构造第二个副本）——临时队列文件 + ``TickEnv`` 逐项注入桩：``switch``/``ready_check``/
``encoder``/``guard``/``store_add``/``submit``。v3.5 新增接线（I-2/I-4/I-5/I-6/口径②③④/乙/丁）：
- 取件 = **第一条未完成行**（``f4_take_first_pending`` 返回行号 + 记录，跳过已完成行与空行）；
- 三条「处置完成」路径——成功 / 有界重试用尽弃单 / 护栏拒收——都**标注完成不删行**
  （``f4_mark_line_done`` 按行号改写为「原键保留 + done: true」）⇒ 完成行 + 未完成行并存，
  百分比 = 已完成/总数 = 真进度；**删文件的唯一触发方 = Chat 页面**（工作者永不删文件，I-3）；
- 标注返回值语义：``missing``（页面已删文件；成功 / 弃单 / 拒收三条路径都按收敛处理）⇒ 不误报
  stalled；``failed`` ⇒ stalled + 保留在途/失败计数，下轮重试。

tick 夹具纪律：夹具**必须**打桩 ``mw.F4_DEFAULT_QUEUE`` 别名——``on_stop`` 的 ``**_counts(None)``
经它解析默认队列路径，不打桩会读用户真实 ``~/.niu/md/media_vector_queue.md``、用例红绿取决于
机器上真实队列内容（v1.3 R3 A-P3-1）。fake submit 按 plan 钉死的**捕获异常版**：同步执行回调、
``BaseException`` 捕进假 future（``FakeFuture`` 提供 ``done()``/``result()``），与生产「异常进
future、由收敛归类」同构。每个状态断言**同时断言三个计数字段**（queue_depth/queue_total/
queue_done，防漏字段：I-8）。撕裂尾行（文件尾无换行、写了一半）**不是完整记录**（与
``md_mirror._f4_split_records`` 同边界）⇒ 三计数不数它，防状态面「idle 且 depth>0」自相矛盾（第二轮 P2-2）。

用例（plan §2 T2 测试清单，22 用例形状迁移 + 新增）：
① 第一条未完成取件（完成行跳过、成功后该行标注、完成行原样不动；旧格式三键行兼容 + 写回「原三键
   + done」；成功 ⇒ 行仍含该行、其余行逐字节不变）；
② 先成功后标注的竞态/失败形态（标注时文件已被页面删 ⇒ idle 收敛；弃单标注 missing ⇒ 按弃单收敛
   （计数清零、不误报「弃单失败」）；标注失败 ⇒ stalled + 可辨 reason、下轮重试标注成功 ⇒ idle）；
③ 失败终局（连败 3 次 ⇒ 弃单 = 标完成 + 临时提示被调、计数清零；弃单时标注失败 ⇒ 计数保留 +
   stalled、下轮重新编码再次弃单）；
④ 护栏拒收（标完成、编码器零调用、临时提示被调；拒收但标注失败 ⇒ stalled 下轮重试）；
⑤ 在途语义（在途不取新行；在途超时与池饱和上报：判定只计「编码已开始」时段、三拍断言）；
⑥ 无未完成行短路（空文件 / 仅完成行 / **撕裂尾行不数进三计数** ⇒ idle、零探活、无「idle 且 depth>0」自相矛盾）；
⑦ 开关热翻转（开→关：在途条标注收敛、不再取新行）；
⑧ 坏行弃除（**任意位置**：夹在完成行与未完成行之间，按行号弃除后继续处理未完成行；单轮弃坏行
   达上限（32）⇒ 本轮停取报 idle、下轮继续弃至清空）；
⑨ 读队列 total（非 UTF-8 ⇒ stalled + 三字段 -1 哨兵；PermissionError 同族；坏行弃不掉停取不烧
   单轮弃行上限）；
⑩ 池不可提交（submit 抛 RuntimeError ⇒ 兜底 stalled、下轮同样兜住）；
⑪ ①/④ 逃逸兜底（switch 抛异常 ⇒ 先收敛在途再报可辨 stalled；ready_check 抛异常 ⇒ stalled 且
   第一条未完成行保留）；
⑫ 护栏 total / downscale / 缺省解析（截断图头 / 桩抛异常 / downscale 动作 / 缺省绑定
   pixel_guard.classify）；
⑬ on_stop 有界退出（R20：有在途 ⇒ 主动中断、状态 off、行未标注 ⇒ 重放；无在途 ⇒ 不碰编码子进程）。
⑭ 向量闲置门（D-31，让路）：关门拍（就绪检查仍先执行、不 submit、idle + 让路 reason + 计数如实）/
   后端未就绪不被等待态掩盖（stalled）/ 开门拍与既有路径逐字同效 / 边界恰 30.0 放行、29.999 等待 /
   探针异常与非 bool 保守等待（不外抛）/ 交互在途等待 / 逐项之间重判 / 自家编码不自我推迟 / 缺省接线
   llama_server / 关门拍仍弃坏行 / ①② 与空队列短都在门之前 / 双条件同时不满足仍等待）。
⑮ 文件已不存在 ⇒ 不可重试（F-4/D-1）：显式 FileNotFoundError ⇒ 一次处置（``_fail_counts`` 不增长、
   标注恰一次、状态回 idle + 三计数如实、文案含「文件不存在」「无需重试」且**不含**「将自动重试」/
   「连续 n 次失败」，日志 WARNING 非 ERROR）；TOCTOU（encoder 抛 RuntimeError 但收敛时文件已不在
   ⇒ 仍不重试，复合判据钉死）；预置旧计数被 pop；标注返 "failed" ⇒ stalled + 可辨 reason（不冻结在
   encoding、计数仍不写）；对照：**非后端** RuntimeError 且文件存在 ⇒ 同样一次处置（可重试类别仅
   ``MediaEncodeBackendError``，走 1/3→弃单）。
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


def rec(entity="a/img-0001.jpg", path="/tmp/fake/img.jpg", ts="2026-10-13T00:00:00", done=False):
    """一条合法的 F4 队列行（D-24 三键；``done`` 键由工作者标注写入——手写行不带，v3.5 I-7）。"""
    r = {"entity_name": entity, "file_path": path, "ts": ts}
    if done:
        r["done"] = True
    return r


def write_queue(queue_file, records):
    """经 S3 唯一队列写入路径（append_record，排它锁 + O_APPEND 持锁写）逐行写入。"""
    for r in records:
        assert append_record(json.dumps(r, ensure_ascii=False) + "\n", md_path=str(queue_file))


def queue_lines(queue_file):
    """队列文件实际行数（非空行）——v3.5 标注不删行 ⇒ 收敛后行数不减，完成行留在文件里。"""
    if not queue_file.exists():
        return 0
    return sum(1 for ln in queue_file.read_text(encoding="utf-8").splitlines() if ln.strip())


def queue_records(queue_file):
    """队列文件实际行（跳空行）逐行解析为记录；坏行保留原字符串。"""
    if not queue_file.exists():
        return []
    out = []
    for ln in queue_file.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append(ln)
    return out


@pytest.fixture(autouse=True)
def _reset_worker_state(monkeypatch, tmp_path):
    """每用例前复位模块状态：在途记录 / 失败计数 / 状态机（三计数字段）/ 媒体库接缝 / **默认队列
    路径别名**（``F4_DEFAULT_QUEUE`` 指向本用例 tmp 的标准队列文件名——``on_stop`` 的
    ``**_counts(None)`` 经此别名解析默认路径，不打桩会读用户真实 ``~/.niu`` 队列文件、计数断言
    红绿取决于机器上真实队列内容，v1.3 R3 A-P3-1；绝不写真 ~/.niu）。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_store.json"))
    monkeypatch.setattr(mw, "F4_DEFAULT_QUEUE", str(tmp_path / "queue.md"))
    monkeypatch.setattr(mw, "_inflight", None)
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None,
                  queue_depth=0, queue_total=0, queue_done=0)
    yield
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None,
                  queue_depth=0, queue_total=0, queue_done=0)


@pytest.fixture(autouse=True)
def _neutralize_vector_gate(monkeypatch):
    """缺省探针固定「长期闲置 ∧ 不在途」（= 新进程的真实初值）。真实探针是**进程全局**状态：
    同进程里其他测试文件调用过 encode_batch 就会污染它，本文件用例会随执行顺序假红/假绿。
    要测门本身的用例显式注入 TickEnv.vector_idle_seconds / text_encode_busy。"""
    monkeypatch.setattr(mw.llama_server, "vector_idle_seconds", lambda: float("inf"), raising=False)
    monkeypatch.setattr(mw.llama_server, "text_encode_busy", lambda: False, raising=False)


# ---------------------------------------------------------------------------
# ① 第一条未完成取件：完成行跳过、成功后该行标注、完成行原样不动（I-2/I-4/I-7）
# ---------------------------------------------------------------------------

def test_first_pending_skips_done_lines_and_marks_after_success(tmp_path):
    """前两行已完成（done）、第三行未完成 ⇒ 取**第三行**、成功后**该行**被标注（done 写入、原键
    保留），完成行原样不动；计数 = (未完成 0, 总数 3, 已完成 3)。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/d1.jpg", str(tmp_path / "1.jpg"), done=True),
                    rec("a/d2.jpg", str(tmp_path / "2.jpg"), done=True),
                    rec("a/img-0003.jpg", str(tmp_path / "3.jpg"))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 提交拍：跳过两条完成行，取第一条未完成（行 3）
    assert calls == ["a/img-0003.jpg"]  # 完成行 d1/d2 绝不再被编码（I-4）
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 3, 2)
    mw.tick(env)  # 收敛拍：该行标注完成（不删行）
    assert calls == ["a/img-0003.jpg"]
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 3, 3)
    recs = queue_records(q)
    assert len(recs) == 3  # 零行被删：完成行 + 新标注行并存
    assert recs[0]["entity_name"] == "a/d1.jpg" and recs[0].get("done") is True  # 完成行原样不动
    assert recs[1]["entity_name"] == "a/d2.jpg" and recs[1].get("done") is True
    assert recs[2]["entity_name"] == "a/img-0003.jpg" and recs[2]["done"] is True
    assert recs[2]["file_path"] == str(tmp_path / "3.jpg")  # 原键逐字保留
    assert recs[2]["ts"] == "2026-10-13T00:00:00"


def test_old_format_line_without_done_is_consumed_then_marked_with_done_key(tmp_path):
    """I-7 兼容：旧格式三键行（无 done 键）天然未完成 ⇒ 正常消费；完成后该行写回 = 原三键 + done: true
    （键集全为四键，无其它增删）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/old.jpg", str(tmp_path / "old.jpg"))])  # 三键，无 done
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)
    assert calls == ["a/old.jpg"]
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    recs = queue_records(q)
    assert len(recs) == 1  # 行仍在文件里（标注不删行）
    assert set(recs[0]) == {"entity_name", "file_path", "ts", "done"}  # 升级写回格式 = 原三键 + done
    assert recs[0]["done"] is True
    assert recs[0]["entity_name"] == "a/old.jpg" and recs[0]["ts"] == "2026-10-13T00:00:00"


def test_success_marks_line_not_deleted(tmp_path):
    """I-2：成功 ⇒ 文件仍含该行、行内容带 done、**其余行逐字节不变**（标注 = 按行号改写，不动他行）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/done.jpg", str(tmp_path / "0.jpg"), done=True),
                    rec("a/next.jpg", str(tmp_path / "1.jpg")),
                    rec("a/after.jpg", str(tmp_path / "2.jpg"))])
    before = q.read_text(encoding="utf-8").splitlines()
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 取行 2（第一条未完成）
    assert calls == ["a/next.jpg"]
    mw.tick(env)  # 行 2 标注完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 3, 2)
    after = q.read_text(encoding="utf-8").splitlines()
    assert len(after) == 3  # 零行被删
    assert after[0] == before[0]  # 完成行逐字节不变
    assert after[2] == before[2]  # 未处理行逐字节不变
    marked = json.loads(after[1])
    assert marked["done"] is True and marked["entity_name"] == "a/next.jpg"


# ---------------------------------------------------------------------------
# ② 先成功后标注：竞态收敛与标注失败形态（I-3/I-5）
# ---------------------------------------------------------------------------

def test_mark_missing_file_converges_without_stall(tmp_path):
    """I-3/口径丁 竞态形态：编码成功、标注时文件已被 Chat 页面删（标注返 "missing"）⇒ 该行随文件
    一起消失，按收敛处理（idle、计数归零），**不误报 stalled**。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", str(tmp_path / "x.jpg"))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 提交拍：向量已写库
    assert mw.get_state()["status"] == "encoding"
    q.unlink()  # Chat 页面观察到全部完成并删了队列文件
    mw.tick(env)  # 收敛拍：标注返回 "missing" ⇒ idle 收敛
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["reason"] == ""  # 无 stalled
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)  # 文件已缺失
    assert mw._inflight is None
    assert calls == ["a/x.jpg"]  # 意图不受影响：向量已入库


def test_mark_failure_stalls_then_recovers(tmp_path, monkeypatch):
    """第四轮 P2-1 同型：编码完成但标注失败 ⇒ stalled + 可辨 reason + 保留在途（绝不静默永停
    encoding）；下一轮重试标注成功 ⇒ idle 收敛。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", str(tmp_path / "x.jpg"))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 提交成功：store_add 已收到该行、状态 encoding
    assert mw._inflight is not None
    assert calls == ["a/x.jpg"]
    assert mw.get_state()["status"] == "encoding"

    real_mark = mw.f4_mark_line_done
    monkeypatch.setattr(mw, "f4_mark_line_done",
                        lambda *a, **kw: "failed")  # 标注恒失败（锁/IO/护栏不符形态）
    mw.tick(env)  # 收敛：标注失败 ⇒ stalled + 保留在途，不抛异常
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "编码完成但标注失败" in st["reason"]
    assert mw._inflight is not None  # 保留（下轮重试标注、再收敛）
    assert queue_lines(q) == 1  # 行未标注，留在文件里
    mw.tick(env)  # 再跑一次：状态稳定同为 stalled（每轮在 ② 短路重发同一可辨状态、不是假进度）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert "编码完成但标注失败" in st2["reason"]
    monkeypatch.setattr(mw, "f4_mark_line_done", real_mark)  # 恢复真值
    mw.tick(env)  # 收敛：标注成功 ⇒ idle
    st3 = mw.get_state()
    assert st3["status"] == "idle"
    assert (st3["queue_depth"], st3["queue_total"], st3["queue_done"]) == (0, 1, 1)
    assert mw._inflight is None
    assert queue_lines(q) == 1  # 行被标注、留在文件里（页面负责删）


# ---------------------------------------------------------------------------
# ③ 失败终局：有界重试 → 弃单 = 标完成（I-6/口径乙）
# ---------------------------------------------------------------------------

def test_fail_counts_retry_then_drop_marks_done(tmp_path, monkeypatch):
    """口径乙：连败 3 次 ⇒ 弃单 = **标完成** + 醒目 ERROR + SSE 失败事件 + 临时提示被调；行带 done
    留在文件里、计数清零、状态回 idle（失败不卡进度，完成比能走到 100%）。重试进行中（n<3）的行
    保持未完成、不标注（百分比不动 = 如实反映）。文件用**真实存在的 tmp 文件**：F-4 复合判据下
    「异常 + 文件已不在」改判不可重试，本用例钉的是「后端异常（``MediaEncodeBackendError``）+ 文件存在 ⇒ 仍重试」（2026-10-17 定稿）。"""
    q = tmp_path / "queue.md"
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\x00garbage")  # 真实存在的文件（F-4 复合判据：exists 为真 ⇒ 不改判）
    write_queue(q, [rec("a/broken.jpg", str(broken))])
    events = []
    notices = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)

    def boom_store_add(e, m, v):
        raise mw.llama_server.MediaEncodeBackendError("媒体库写入失败")

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

    mw.tick(env)  # tick#1 提交（store_add 抛错 → 异常进 future）
    assert mw.get_state()["status"] == "encoding"
    assert queue_lines(q) == 1
    mw.tick(env)  # tick#2 收敛判失败 1/3：行仍未标注、stalled、计数 1
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "失败重试中（1/3）" in st["reason"] and "媒体库写入失败" in st["reason"]
    assert mw._fail_counts.get("a/broken.jpg") == 1
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)  # 重试期完成比不动
    assert queue_records(q)[0].get("done") is not True  # 行未被标注

    mw.tick(env); mw.tick(env)  # tick#3/#4 重新提交、收敛计数 2
    assert mw._fail_counts.get("a/broken.jpg") == 2
    mw.tick(env); mw.tick(env)  # tick#5/#6 再提交、收敛达上限 ⇒ 弃单 = 标完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)  # 弃单计入完成比
    assert "a/broken.jpg" not in mw._fail_counts  # 计数清零（不重放）
    recs = queue_records(q)
    assert len(recs) == 1 and recs[0]["done"] is True  # 弃单行标完成、留在文件里（页面删文件）
    assert notices  # 临时提示被调（失败信息只作临时提示，不落文件当状态）
    drops = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert drops and "弃单" in drops[-1]["message"]


def test_drop_mark_failure_keeps_count_and_stalls(tmp_path, monkeypatch):
    """弃单时标注失败（返 "failed"）：保留失败计数 + 报 stalled（下轮重新编码并再次弃单）——防「标注
    失败但计数被清零 ⇒ 再凑 3 次失败 ⇒ 再弃单又没标注」的无限循环。文件用**真实存在的 tmp 文件**
    （F-4 复合判据：异常 + 文件已不在 ⇒ 改判不可重试；本用例钉的是后端异常 + 文件仍在时的既有重试/弃单口径）。"""
    q = tmp_path / "queue.md"
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\x00garbage")  # 真实存在的文件（F-4 复合判据：exists 为真 ⇒ 不改判）
    write_queue(q, [rec("a/broken.jpg", str(broken))])
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom_store_add(e, m, v):
        raise mw.llama_server.MediaEncodeBackendError("媒体库写入失败")

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
    mw.tick(env); mw.tick(env)  # 失败 1/3
    mw.tick(env); mw.tick(env)  # 失败 2/3
    assert mw._fail_counts.get("a/broken.jpg") == 2
    real_mark = mw.f4_mark_line_done
    monkeypatch.setattr(mw, "f4_mark_line_done", lambda *a, **kw: "failed")  # 弃单时的标注恒失败
    mw.tick(env); mw.tick(env)  # 失败 3/3 达上限 ⇒ 弃单尝试 ⇒ 标注失败 ⇒ stalled + 计数保留
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "弃单失败" in st["reason"]
    assert mw._fail_counts.get("a/broken.jpg") == 3  # 计数保留（退回清零 ⇒ 红）
    assert queue_lines(q) == 1  # 行仍未标注
    monkeypatch.setattr(mw, "f4_mark_line_done", real_mark)  # 恢复真值
    mw.tick(env); mw.tick(env)  # 重新编码并再次弃单：这次标注成功 ⇒ idle
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert "a/broken.jpg" not in mw._fail_counts
    assert queue_records(q)[0]["done"] is True


def test_drop_mark_missing_converges_without_stall(tmp_path, monkeypatch):
    """弃单路径的 missing 支（P2 审查覆盖空洞）：弃单标注窗口里 Chat 页面恰删了队列文件
    （``f4_mark_line_done`` 返 "missing"）⇒ 按弃单收敛（idle、reason 空、计数清零、SSE 失败
    事件照发），**不误报**「弃单失败」stalled（那会让计数保留、下轮重新编码再弃单，循环往复）。
    图片文件用**真实存在的 tmp 文件**（F-4 复合判据：exists 为真 ⇒ 三次后端异常失败仍走既有弃单口径）。"""
    q = tmp_path / "queue.md"
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\x00garbage")  # 真实存在的文件（F-4 复合判据：exists 为真 ⇒ 不改判）
    write_queue(q, [rec("a/broken.jpg", str(broken))])
    events = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom_store_add(e, m, v):
        raise mw.llama_server.MediaEncodeBackendError("媒体库写入失败")

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
    mw.tick(env); mw.tick(env)  # 失败 1/3（行保持未完成）
    mw.tick(env); mw.tick(env)  # 失败 2/3
    assert mw._fail_counts.get("a/broken.jpg") == 2
    mw.tick(env)  # 重新提交（文件仍在：第三次在途记录，store_add 抛错进 future）
    assert mw._inflight is not None
    q.unlink()  # Chat 页面恰在此刻删了队列文件
    mw.tick(env)  # 收敛：第 3 次失败达上限 ⇒ 弃单标注返 "missing" ⇒ 按弃单收敛
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["reason"] == ""  # missing 被并入 failed 分支（误报「弃单失败」）⇒ 这里红
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)  # 文件已随页面删除消失
    assert "a/broken.jpg" not in mw._fail_counts  # 计数清零（按弃单收敛，不重放）
    assert mw._inflight is None
    drops = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert drops and "弃单" in drops[-1]["message"]


# ---------------------------------------------------------------------------
# ④ 护栏拒收：标完成、编码器零调用（I-6/口径乙）
# ---------------------------------------------------------------------------

def test_guard_reject_marks_done_without_encoding(tmp_path, monkeypatch):
    """口径乙：护栏拒收（病态：体积超预算）= 确定性失败 ⇒ **标完成** + 醒目 WARNING + 临时提示被调；
    编码器入口零调用（永不被发送）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/big.jpg", str(tmp_path / "big.jpg"))])
    events = []
    notices = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)
    calls = []
    never_encoded = []

    def never_encode(p):
        # 具名编码器探针：先记录、再抛——「拒收误提交池」回归下，抛出的异常会被 make_submit_sync
        # 吞进假 future，但记录吞不掉 ⇒ 零调用断言独立证明「真的没发」
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
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)  # 拒收计入完成比
    assert never_encoded == []  # 编码器入口从未被调用（具名探针的独立信号）
    assert calls == []  # store_add 零调用 ⇒ 未进写库路径
    assert queue_records(q)[0]["done"] is True  # 行标完成、留在文件里
    rejects = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert rejects and "超体积预算" in rejects[0]["message"]
    assert notices and "超体积预算" in notices[-1]  # 临时提示被调（不落文件当状态）


def test_guard_reject_mark_failure_stalls(tmp_path, monkeypatch):
    """拒收但标注失败（返 "failed"）：行仍未标注——报可辨 stalled、保留失败计数（如有），下轮重试
    该行（再拒收、仍无通知）；退回「清零计数 + 报 idle」⇒ 同一行每轮重复触发拒收 + 提示 = 10s 级
    通知风暴。回归方向：tick ⑥ reject 分支 else 分支。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/big.jpg", str(tmp_path / "big.jpg"))])
    events = []
    notices = []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)
    mark_calls = []
    monkeypatch.setattr(mw, "f4_mark_line_done",
                        lambda *a, **kw: (mark_calls.append(a), "failed")[1])  # 标注恒失败
    guard_calls = []

    def rejecting_guard(p):
        guard_calls.append(p)
        return ("reject", "文件 31MB 超体积预算 30MB", None)

    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: (_ for _ in ()).throw(AssertionError("拒收不得编码")),
                  guard=rejecting_guard,
                  store_add=lambda e, m, v: None, submit=submit)
    mw._fail_counts["a/big.jpg"] = 1  # 预置失败计数：标注失败路径必须保留（退回清零 ⇒ 红）
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "拒收但标注失败" in st["reason"]  # reason 可辨
    assert st["entity"] == "a/big.jpg"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)  # 行仍未标注
    assert queue_lines(q) == 1
    assert mark_calls  # 尝试过标注，失败
    assert events == [] and notices == []  # 不发「拒收」类通知（不重复提示）
    assert mw._fail_counts["a/big.jpg"] == 1  # 失败计数保留
    mw.tick(env)  # 下一轮：重试该行
    st = mw.get_state()
    assert st["status"] == "stalled" and "拒收但标注失败" in st["reason"]
    assert guard_calls == [str(tmp_path / "big.jpg")] * 2  # 两轮都取到该行（重试行为）
    assert events == [] and notices == []  # 每轮仍不重复提示
    assert queue_lines(q) == 1


# ---------------------------------------------------------------------------
# ⑤ 在途语义：至多一条在途 / 在途超时与池饱和上报（D-22/R19/R14）
# ---------------------------------------------------------------------------

def test_inflight_blocks_new_pending_until_converged(tmp_path):
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
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (2, 2, 0)
    assert calls == []  # worker 未跑 ⇒ store_add 未被调用
    mw.tick(env)  # tick#2 只收敛不提交新行（store_add 调用数不变）
    assert mw._inflight is not None
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert calls == []
    assert queue_lines(q) == 2

    futures[0].set_result(None)  # future 完成
    mw.tick(env)  # tick#3 收敛：行 1 标注完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 2, 1)
    assert queue_lines(q) == 2  # 标注不删行
    assert mw._inflight is None
    mw.tick(env)  # tick#4 才取行 2（提交与收敛是两拍，不得合并断言）
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert len(futures) == 2


def test_inflight_timeout_and_pool_saturation_reporting(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "INFLIGHT_TIMEOUT", 0.01)

    # (a) started=True + 回拨 started_at：编码已实际开始却永不完成 ⇒ 判失败（重试计数 +1），
    # 行保持未完成 ⇒ 下一轮能重新取行（不冻结）
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
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)  # 行未标注
    assert mw._inflight is None
    assert queue_lines(qa) == 1  # 失败不删行（行未标注 ⇒ 重放，I-5）
    mw.tick(env_a)  # 下一轮能重新取行（不冻结）
    assert mw.get_state()["status"] == "encoding"

    # (b) started=False + 回拨量在阈值内：排队段不计入超时判定（R19）
    mw._inflight = None
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None,
                  queue_depth=0, queue_total=0, queue_done=0)
    qb = tmp_path / "queue_b.md"
    write_queue(qb, [rec("a/t2.jpg", "/tmp/fake/t2.jpg")])
    submit_b, futures_b = make_submit_pending()
    env_b = TickEnv(queue_path=str(qb), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_b)
    mw.tick(env_b)  # 提交（started_at = 提交时刻）
    assert mw._inflight is not None
    mw._inflight.started_at = time.time() - 0.001  # 回拨量 < 阈值 0.01（余量 9ms；tick 内含文件读 + 计数读，负载下 >5ms 抖动会把 (b) 误判成 (c)）
    mw.tick(env_b)  # 不触发任何超时判定
    st = mw.get_state()
    assert st["status"] == "encoding"  # 状态保持 encoding（不是 stalled）
    assert mw._inflight is not None

    # (c) started=False 且回拨超阈值：池饱和、编码从未开始 ⇒ stalled「编码排队中」，
    # 不判失败、不标注、不清计数，记录保持在途（第二轮 P2-1 回归）
    mw._inflight.started_at = time.time() - 0.02
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "编码排队中" in st["reason"]
    assert mw._inflight is not None
    assert mw._fail_counts == {}
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)

    # (d) 三拍断言（第三轮 P3-1 回归）：记录转 started=True 后改回 encoding；future 完成收敛照常
    mw._inflight.started = True
    mw._inflight.started_at = time.time()
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "encoding"  # 不再是「编码排队中」的旧状态
    futures_b[0].set_result(None)
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert mw._inflight is None
    assert queue_lines(qb) == 1  # 行被标注、留在文件里


# ---------------------------------------------------------------------------
# ⑥ 无未完成行短路：空文件 / 仅完成行 ⇒ idle、零探活（v3.2 条件求值顺序，v3.5 同型）
# ---------------------------------------------------------------------------

def test_empty_queue_never_probes(tmp_path):
    def boom():
        raise AssertionError("无未完成行时绝不应调 ready_check（不探活，v3.2 条件求值顺序在 v3.5 保留）")

    # (a) 空文件
    qa = tmp_path / "empty.md"
    qa.write_text("", encoding="utf-8")
    env_a = TickEnv(queue_path=str(qa), switch=lambda: True, ready_check=boom,
                    guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                    store_add=lambda e, m, v: None)
    mw.tick(env_a)  # 不得抛异常
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)

    # (b) 只剩完成行（口径丁「文件里只剩已完成行」形态 = 无活可干 ⇒ idle，完成行不是「活」）
    qb = tmp_path / "done_only.md"
    write_queue(qb, [rec("a/d1.jpg", "/tmp/fake/d1.jpg", done=True),
                     rec("a/d2.jpg", "/tmp/fake/d2.jpg", done=True)])
    env_b = TickEnv(queue_path=str(qb), switch=lambda: True, ready_check=boom,
                    guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                    store_add=lambda e, m, v: None)
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 2, 2)  # 完成行计入总数


def test_torn_tail_line_not_counted_as_pending_record(tmp_path):
    """第二轮 P2-2（真缺陷）：撕裂尾行（文件尾无换行、O_APPEND 写了一半、崩溃残留）不是完整记录
    （与 ``md_mirror._f4_split_records`` 同边界：写入方可能还在写）⇒ 三计数只统计完整记录行 ⇒
    收敛后状态面是 idle 且 depth==0，不再出现「idle 且 queue_depth>0」自相矛盾、真进度不永停
    (N-1)/N；撕裂尾行本身原样不动（绝不被计数、也不被工作者处置）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg", done=True),
                    rec("a/y.jpg", "/tmp/fake/y.jpg", done=True)])
    # 写入方在 O_APPEND 写半之后崩溃：追加一条无尾换行的半行
    with open(q, "a", encoding="utf-8") as f:
        f.write('{"entity_name": "a/half.jpg", "file_path": "/tmp/fake/half.jpg", "ts": "2026-10-1')
    before = q.read_bytes()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)  # 只有完成行 + 撕裂尾行 ⇒ 无活可干（取件同样按 _f4_split_records 边界，撕裂行不扫）
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["reason"] == ""
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 2, 2)  # 撕裂行不数 ⇒ 无 depth>0 自相矛盾
    assert mw._queue_counts(str(q)) == (0, 2, 2)  # 直查计数原语同边界
    assert q.read_bytes() == before  # 撕裂尾行原样不动


# ---------------------------------------------------------------------------
# ⑦ 开关热翻转：开→关 时在途条标注收敛掉、不再取新行（AC-12）
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
    futures[0].set_result(None)  # 在途完成（可收敛）
    env_off = TickEnv(switch=lambda: False, **base)
    mw.tick(env_off)  # 开关关：先收敛在途（行 1 标注完成），然后状态 off、不再取新行
    st = mw.get_state()
    assert st["status"] == "off"
    assert "开关关" in st["reason"]
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 2, 1)
    assert queue_lines(q) == 2  # 文件保留两行：行 1 已标注、行 2 未完成原样保留（下次开开关重放）
    recs = queue_records(q)
    assert recs[0]["entity_name"] == "a/img-0001.jpg" and recs[0]["done"] is True
    assert recs[1]["entity_name"] == "a/img-0002.jpg" and recs[1].get("done") is not True
    assert len(futures) == 1  # 行 2 从未提交（pending 夹具下 worker 回调不执行，store_add 零调用）
    assert calls == []
    assert mw._inflight is None


# ---------------------------------------------------------------------------
# ⑧ 坏行弃除：任意位置（I-4/v3.5）按行号弃除，未完成行继续处理
# ---------------------------------------------------------------------------

def test_bad_line_at_middle_position_swept_then_pending_processed(tmp_path):
    """I-4：v3.5 坏行可在文件**任意位置**（完成行留文件后，坏行可能夹在已完成行与未完成行之间）
    ——醒目告警、按行号弃除（与位置无关），随后的未完成行照常取件处理。"""
    q = tmp_path / "queue.md"
    q.write_text(
        json.dumps(rec("a/done.jpg", "/tmp/fake/done.jpg", done=True), ensure_ascii=False) + "\n"
        + "not-json\n"
        + json.dumps(rec("a/img-0001.jpg", str(tmp_path / "g.jpg")), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit)
    mw.tick(env)  # 行 1 完成行跳过、行 2 坏行弃除、行 3 取走（同步 fake 已执行编码/写库桩）
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # 收敛：行 3 标注完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 2, 2)
    assert calls == ["a/img-0001.jpg"]
    recs = queue_records(q)
    assert [r["entity_name"] for r in recs] == ["a/done.jpg", "a/img-0001.jpg"]  # 坏行消失、完成行原样
    assert recs[1]["done"] is True


def test_bad_line_sweep_cap_stops_this_round_then_next_round_finishes(tmp_path):
    """swept 形态（P2 审查覆盖空洞）：单轮 tick 连弃坏行达 32 上限 ⇒ 本轮停取报 idle（醒目 ERROR
    已在 ``_read_first_pending_or_sweep`` 内打出，刻意取舍 = v3.0 原条款），不把病态队列文件烧掉循环
    线程；下一轮 swept 计数归零、继续弃，直到队列清干净 ⇒ idle（0,0,0）且不再报「停取」。"""
    q = tmp_path / "queue.md"
    n = mw._MAX_BADLINE_SWEEPS + 8  # 40 条坏行：本轮烧满 32 停取，余 8 条下轮清完
    with open(q, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(f"not-json-{i}\n")
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)  # 第一轮：弃满 32 条后停取（swept）⇒ 报 idle、不判失败不烧穿上限
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["reason"] == ""  # swept 的醒目告警走日志，状态面报 idle（v3.0 原条款）
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (8, 8, 0)  # 剩余 8 条坏行仍未处置
    assert queue_lines(q) == 8
    mw.tick(env)  # 第二轮：swept 计数归零、继续弃剩余 8 条，弃完无活可干 ⇒ idle（0,0,0）
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)
    assert queue_lines(q) == 0


# ---------------------------------------------------------------------------
# ⑨ 护栏 total / downscale / 缺省解析（第一轮 P1 回归 + 缺省绑定钉死）
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
    mw.tick(env)  # 收敛：行标注完成（同一行不会每轮崩死）
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert queue_records(q)[0]["done"] is True


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
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)


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
    mw.tick(env)  # 收敛拍：行标注完成
    assert calls == ["a/img-0001.jpg"]  # 行被消费，未被拒收
    st = mw.get_state()
    assert st["status"] == "idle"
    assert queue_records(q)[0]["done"] is True


def test_guard_default_resolves_to_classify(tmp_path, monkeypatch):
    """TickEnv 不注入 guard ⇒ tick 前言的缺省绑定必须解析到 `pixel_guard.classify`
    （误删 `or` 子句 ⇒ guard=None ⇒ 保护性 try 吞成 pass ⇒ 下列断言转红）。
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
    assert guard_calls == ["/tmp/fake/img.jpg"]  # guard 确实解析到 classify 且被调用
    assert calls == ["a/img-0001.jpg"]  # encoder 被调用 ⇒ 行按 pass 分支照常提交
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle" and queue_records(q)[0]["done"] is True


# ---------------------------------------------------------------------------
# ⑩ 读队列 total（第二轮 P1-1 回归）：任何读异常绝不外抛崩 tick，绝不永久冻结
# ---------------------------------------------------------------------------

def test_unreadable_queue_non_utf8_stalls_with_counts_sentinel(tmp_path):
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
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (-1, -1, -1)  # 三字段同哨兵
    assert q.read_bytes() == before  # 队列行未动
    mw.tick(env)  # 再跑一次：同样不抛、状态不变（不会每轮崩死、不永久冻结）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert (st2["queue_depth"], st2["queue_total"], st2["queue_done"]) == (-1, -1, -1)


def test_unreadable_queue_permission_error(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def deny(path=None):
        raise PermissionError("permission denied")

    monkeypatch.setattr(mw, "f4_take_first_pending", deny)
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "PermissionError" in st["reason"]
    assert queue_lines(q) == 1  # 行未动，下轮可重试


def test_bad_line_undroppable_stalls_without_burning_sweep_budget(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text("not-json\n" + json.dumps(rec("a/x.jpg", "/tmp/fake/x.jpg"),
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    takes, drops = [], []
    real_take = mw.f4_take_first_pending

    def counting_take(queue_path=None):
        takes.append(queue_path)
        return real_take(queue_path)

    monkeypatch.setattr(mw, "f4_take_first_pending", counting_take)
    monkeypatch.setattr(mw, "f4_drop_line", lambda line_index, queue_path=None: (drops.append(line_index), "failed")[1])
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                  store_add=lambda e, m, v: None)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "弃不掉" in st["reason"] and "坏行" in st["reason"]
    assert len(drops) <= 1 and len(takes) <= 1  # 不烧穿单轮弃行上限（32）
    assert queue_lines(q) == 2  # 坏行仍在（按行号弃不掉），下轮重试


# ---------------------------------------------------------------------------
# ⑪ 池不可提交（第三轮 P2 回归）：submit 抛 RuntimeError ⇒ 兜底 stalled、下轮同样兜住
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
    assert queue_lines(q) == 1   # 第一条未完成行未动
    mw.tick(env)  # 再跑一次：同一行在同一处同样被兜住（不崩死、状态稳定）
    st2 = mw.get_state()
    assert st2["status"] == "stalled"
    assert "媒体线程池不可提交" in st2["reason"]
    assert mw._inflight is None


# ---------------------------------------------------------------------------
# ⑫ ①/④ 逃逸兜底（第三轮 P2 回归）：switch/ready_check 抛异常也不外泄
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
    futures[0].set_result(None)  # 在途已完成（可收敛标注）

    def broken_switch():
        raise ValueError("配置读取失败")

    env_bad = TickEnv(queue_path=str(q), switch=broken_switch, ready_check=lambda: True,
                      guard=lambda p: ("pass", "", None), encoder=lambda p: [0.1] * 768,
                      store_add=lambda e, m, v: None, submit=submit)
    mw.tick(env_bad)  # 不抛异常：先收敛在途（行标注完成），再报可辨 stalled
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "开关读取异常" in st["reason"]
    assert mw._inflight is None  # 在途已被收敛
    assert queue_lines(q) == 1  # 行被标注、留在文件里（v3.5：收敛 = 标注，不是删行）
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)


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
    assert queue_lines(q) == 1  # 第一条未完成行未动（下轮重取）


# ---------------------------------------------------------------------------
# ⑬ on_stop 有界退出（R20，S4c 审查面）：有在途编码 ⇒ 主动中断（终止编码子进程 ⇒ 在途 urlopen
# 即刻失败 ⇒ 池 worker 即刻返回 ⇒ 退出有界——线程池 join 先于普通 atexit，2026-10-13 本机探针实证），
# 在途复位、状态 off、行未标注（重放语义，I-5）；对照：无在途 ⇒ llama_server.stop 不被调用。
# 桩 llama_server.stop 记录调用，避免测试里真终止子进程。夹具已打桩 F4_DEFAULT_QUEUE 别名 ⇒
# on_stop 的 **_counts(None) 读的是本用例 tmp 队列文件，绝不读真实 ~/.niu（v1.3 R3 A-P3-1）。
# ---------------------------------------------------------------------------

def test_on_stop_with_inflight_interrupts_encoding(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, _ = make_submit_pending()  # 未完成假 future：编码真实在途（回调永不执行）
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit)
    mw.tick(env)  # 提交拍：在途记录已发布、状态 encoding、行未标注
    assert mw._inflight is not None
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)
    stop_calls = []
    monkeypatch.setattr(mw.llama_server, "stop", lambda: stop_calls.append(1))  # 桩：不真终止子进程
    mw.on_stop()
    assert len(stop_calls) == 1  # 有在途 ⇒ 主动中断编码（R20 有界退出的真中断手段，不是只记日志）
    assert mw._inflight is None  # 在途记录复位
    st = mw.get_state()
    assert st["status"] == "off"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)  # 行未标注
    assert queue_lines(q) == 1  # 行未标注 ⇒ 下次启动重放（I-5 至少一次语义不变）


def test_on_stop_without_inflight_does_not_interrupt(tmp_path, monkeypatch):
    stop_calls = []
    monkeypatch.setattr(mw.llama_server, "stop", lambda: stop_calls.append(1))
    mw.on_stop()
    assert stop_calls == []  # 无在途 ⇒ 不碰编码子进程
    st = mw.get_state()
    assert st["status"] == "off"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)
    assert mw._inflight is None


# ---------------------------------------------------------------------------
# ⑭ 向量闲置门（D-31，让路）：交互/前端向量闲置 ≥30s ∧ 交互编码不在途才允许启动新编码项；任一
# 不满足 ⇒ 本轮不启动、如实报 idle 让路等待（前端 else 分支渲染「等待开始」），下一拍再查。
# 门插在 ④ 就绪检查之后、⑥ 像素护栏之前（③/④ 短在门之前；每项之间重判）。缺省探针由 autouse
# 夹具 _neutralize_vector_gate 钉成 inf/False；门本身的用例显式注入 TickEnv 两探针。
# ---------------------------------------------------------------------------

class _RecordingLogger:
    """loguru logger 的同形替身（只收 warning）——断言「探针异常走保守分支、可辨告警、不外抛」。"""

    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))


def test_gate_closed_tick_skips_submit_and_reports_idle(tmp_path):
    """关门拍（交互向量闲置 5.0s < 30s）：就绪检查**仍先执行**（门在 ④ 之后、不插队到 ④ 之前——
    R1-A P1-1 的位置语义），随后门判「不开」⇒ 不 submit、行不动、如实报 idle + 让路 reason（前端
    既有 else 分支渲染「队列积压 N 行，等待开始」），计数如实（未完成 1 行绝不误报排空）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    ready_calls = []
    submit, futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True,
                  ready_check=lambda: (ready_calls.append(1), True)[1],
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: 5.0, text_encode_busy=lambda: False)
    mw.tick(env)
    assert ready_calls == [1]  # 就绪检查在门之前执行（R1-A P1-1）
    st = mw.get_state()
    assert st["status"] == "idle"  # 等待态（不是 stalled / off）
    assert "交互向量活跃" in st["reason"] and "让路" in st["reason"]
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)  # 计数如实
    assert futures == []  # 零 submit（门拦在提交池之前）
    assert queue_lines(q) == 1  # 行未动（门只管启动新编码项）


def test_gate_closed_but_backend_not_ready_still_stalls(tmp_path):
    """fail-loud 不被等待态掩盖（R1-A P1-1 回归）：后端未就绪 ∧ 关门 ⇒ 报 stalled（reason 点明
    编码后端），**不是**让路 idle——后端诊断信息绝不因等待态丢失。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: False,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: 5.0, text_encode_busy=lambda: False)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"
    assert "编码后端不可用" in st["reason"]
    assert "让路" not in st["reason"]  # 不是等待态掩盖
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)
    assert futures == [] and queue_lines(q) == 1


def test_gate_open_tick_behaves_like_before(tmp_path):
    """开门拍（闲置 inf ∧ 不在途 = 新进程真实初值）：与加门之前的既有路径逐字同效——提交、编码、
    写库、标注、计数全同构；门对「一直闲置」的进程零影响。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/img-0001.jpg", str(tmp_path / "1.jpg"))])
    calls = []
    submit, _ = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: calls.append(e), submit=submit,
                  vector_idle_seconds=lambda: float("inf"), text_encode_busy=lambda: False)
    mw.tick(env)
    assert calls == ["a/img-0001.jpg"]
    st = mw.get_state()
    assert st["status"] == "encoding"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)
    mw.tick(env)  # 收敛拍：行标注完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert queue_records(q)[0]["done"] is True


def test_gate_boundary_exactly_threshold_passes(tmp_path):
    """边界钉死判定符：恰 30.0s ⇒ 放行（`<` 判定而非 `<=`）；29.999s ⇒ 等待（差 1ms 也不启动）。"""
    # (a) 恰 30.0：放行
    qa = tmp_path / "queue_a.md"
    write_queue(qa, [rec("a/a.jpg", "/tmp/fake/a.jpg")])
    submit_a, futures_a = make_submit_pending()
    env_a = TickEnv(queue_path=str(qa), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_a,
                    vector_idle_seconds=lambda: 30.0, text_encode_busy=lambda: False)
    mw.tick(env_a)
    assert len(futures_a) == 1  # 恰达阈值即放行
    assert mw.get_state()["status"] == "encoding"

    # (b) 29.999：等待
    mw._inflight = None
    mw._fail_counts.clear()
    mw._set_state(status="idle", reason="", entity=None, started_at=None,
                  queue_depth=0, queue_total=0, queue_done=0)
    qb = tmp_path / "queue_b.md"
    write_queue(qb, [rec("a/b.jpg", "/tmp/fake/b.jpg")])
    submit_b, futures_b = make_submit_sync()
    env_b = TickEnv(queue_path=str(qb), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_b,
                    vector_idle_seconds=lambda: 29.999, text_encode_busy=lambda: False)
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert "让路" in st["reason"]
    assert futures_b == []  # 严格小于：差 1ms 也不启动
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)


def test_gate_probe_failure_waits_conservatively(tmp_path, monkeypatch):
    """探针异常 / 非数值 ⇒ 按「不可开」保守等待 + WARNING，绝不外抛崩 tick（异常逃出 = 每轮崩在
    同一处、状态永不更新 = 永久冻结，R14 症状）。含 A1 契约漂移：busy 探针返非 bool 不得被静默
    当成 False（那会把「在途」误判成「不在途」= 真去抢锁）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def boom():
        raise RuntimeError("探针接线炸了")

    def none_idle():
        return None  # float(None) 抛 TypeError ⇒ 保守分支

    def not_bool_busy():
        return 1  # int 不是 bool ⇒ 显式 TypeError ⇒ 保守分支

    cases = [
        ("探针抛异常", boom, lambda: False),
        ("闲置返 None", none_idle, lambda: False),
        ("在途返非 bool", lambda: float("inf"), not_bool_busy),
    ]
    for label, idle_stub, busy_stub in cases:
        recorder = _RecordingLogger()
        monkeypatch.setattr(mw, "logger", recorder)
        submit, futures = make_submit_sync()
        env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                      encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                      store_add=lambda e, m, v: None, submit=submit,
                      vector_idle_seconds=idle_stub, text_encode_busy=busy_stub)
        mw.tick(env)  # 不得抛异常
        st = mw.get_state()
        assert st["status"] == "idle", label
        assert "让路" in st["reason"], label
        assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0), label
        assert futures == [], label  # 保守 = 不启动
        assert recorder.warnings, label  # 告警可辨（不外抛也不静默）
        assert queue_lines(q) == 1, label


def test_gate_text_encode_busy_waits_even_when_idle(tmp_path):
    """用户口径②「不抢」：交互编码在途（含排队等锁段）∧ 闲置 inf ⇒ 仍不启动——绝不与前端并排抢
    _encode_lock。reason 点明「在途」（A2：按实际命中条件拼装，不是拿「闲置 inf」凑话术）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: float("inf"), text_encode_busy=lambda: True)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert "交互编码在途" in st["reason"] and "让路" in st["reason"]
    assert futures == []
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)


def test_gate_reevaluates_between_items(tmp_path):
    """门在**每项之间**重判（非一次性）：第一项启动时探针 inf；其完成后探针翻 1.0s（交互侧又来
    编码）⇒ 第二项**不启动**、如实报让路等待；第一项本身不受影响（已在途）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/i1.jpg", str(tmp_path / "1.jpg")),
                    rec("a/i2.jpg", str(tmp_path / "2.jpg"))])
    submit, futures = make_submit_pending()
    base = dict(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                store_add=lambda e, m, v: None, submit=submit)
    env_open = TickEnv(vector_idle_seconds=lambda: float("inf"), text_encode_busy=lambda: False, **base)
    mw.tick(env_open)  # 第一项启动（门开）
    assert len(futures) == 1
    assert mw.get_state()["status"] == "encoding"
    futures[0].set_result(None)
    mw.tick(env_open)  # 收敛：第一项标注完成
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 2, 1)
    env_closed = TickEnv(vector_idle_seconds=lambda: 1.0, text_encode_busy=lambda: False, **base)
    mw.tick(env_closed)  # 交互向量又活跃（闲置 1s）⇒ 第二项不启动
    st = mw.get_state()
    assert st["status"] == "idle"
    assert "让路" in st["reason"]
    assert len(futures) == 1  # 零新提交
    assert queue_lines(q) == 2  # 第一项已标注、第二项原样等待
    assert queue_records(q)[1].get("done") is not True


def test_gate_own_encoding_does_not_self_defer(tmp_path):
    """用户口径①：时钟只由**交互/文本**编码打点，媒体自家编码从不打点 ⇒ 完成一项后探针仍 inf
    ⇒ 下一拍连续取下一项——排空不被自家编码卡成每项间隔 30s（N 张 = 白等 N×30s）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/i1.jpg", str(tmp_path / "1.jpg")),
                    rec("a/i2.jpg", str(tmp_path / "2.jpg")),
                    rec("a/i3.jpg", str(tmp_path / "3.jpg"))])
    submit, futures = make_submit_pending()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: float("inf"), text_encode_busy=lambda: False)
    mw.tick(env)  # 第一项启动
    futures[0].set_result(None)
    mw.tick(env)  # 收敛第一项（探针仍 inf：自家编码不打点）
    mw.tick(env)  # 第二项立即启动（不等 30s）
    assert len(futures) == 2
    assert mw.get_state()["status"] == "encoding"
    futures[1].set_result(None)
    mw.tick(env)  # 收敛第二项
    mw.tick(env)  # 第三项立即启动
    assert len(futures) == 3
    assert mw.get_state()["status"] == "encoding"
    futures[2].set_result(None)
    mw.tick(env)  # 收敛第三项 ⇒ 排空
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 3, 3)


def test_gate_default_resolves_to_llama_server_functions(tmp_path, monkeypatch):
    """缺省接线：TickEnv 不注入两探针 ⇒ tick 前言必须解析到 ``llama_server.vector_idle_seconds`` /
    ``text_encode_busy``（误删 `or` 子句 ⇒ 探针为 None ⇒ 保护性 try 吞成保守等待，门永远开不了）。
    本用例把两个模块函数分别钉成「关门」值、TickEnv 不注入 ⇒ 只能经缺省路径生效。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    # (a) 缺省 vector_idle_seconds → 0.0 ⇒ 等待
    monkeypatch.setattr(mw.llama_server, "vector_idle_seconds", lambda: 0.0, raising=False)
    submit_a, futures_a = make_submit_sync()
    env_a = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_a)  # 两探针刻意不注入
    mw.tick(env_a)
    st = mw.get_state()
    assert st["status"] == "idle" and "让路" in st["reason"]
    assert "交互向量活跃" in st["reason"]      # 判别：真探针命中（非保守分支的「交互编码在途」）
    assert futures_a == []

    # (b) 闲置恢复 inf，缺省 text_encode_busy → True ⇒ 等待
    monkeypatch.setattr(mw.llama_server, "vector_idle_seconds", lambda: float("inf"), raising=False)
    monkeypatch.setattr(mw.llama_server, "text_encode_busy", lambda: True, raising=False)
    submit_b, futures_b = make_submit_sync()
    env_b = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                    encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                    store_add=lambda e, m, v: None, submit=submit_b)
    mw.tick(env_b)
    st = mw.get_state()
    assert st["status"] == "idle" and "让路" in st["reason"]
    assert futures_b == []
    assert queue_lines(q) == 1  # 两拍都未启动、行未动


def test_gate_closed_still_sweeps_bad_lines(tmp_path):
    """R1-A P2-2：门只管**启动新编码项**——取件/sweep/维护照常（③ 取件在门之前执行）。队首坏行 +
    关门 ⇒ 坏行照旧被弃除，随后如实报让路等待（绝不把坏行留到门开后才弃）。"""
    q = tmp_path / "queue.md"
    q.write_text("not-json\n" + json.dumps(rec("a/x.jpg", "/tmp/fake/x.jpg"),
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    submit, futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: 5.0, text_encode_busy=lambda: False)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert "让路" in st["reason"]
    assert queue_lines(q) == 1  # 坏行已被弃除（sweep 不受门影响）
    recs = queue_records(q)
    assert recs[0]["entity_name"] == "a/x.jpg"  # 好行原样、未被处置
    assert recs[0].get("done") is not True
    assert futures == []  # 好行也未启动（门拦启动，不拦 sweep）


def test_gate_probes_untouched_when_switch_off_or_inflight(tmp_path):
    """顺序不变式：① 开关、② 在途收敛都在门**之前**——开关关 ⇒ 报 off（门不参与）；有在途 ⇒ 只
    收敛、不取新行（门不参与）。两形态下门探针**零调用**（注入「被调用即炸」桩 ⇒ 不抛即证明）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])

    def boom_probe():
        raise AssertionError("①② 短路时门探针不得被调用")

    # (a) 开关关 + 关门 ⇒ off，探针零调用
    env_off = TickEnv(queue_path=str(q), switch=lambda: False, ready_check=lambda: True,
                      encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                      store_add=lambda e, m, v: None,
                      vector_idle_seconds=boom_probe, text_encode_busy=boom_probe)
    mw.tick(env_off)  # 不得抛异常
    st = mw.get_state()
    assert st["status"] == "off"
    assert "开关关" in st["reason"]

    # (b) 有在途 + 关门探针 ⇒ 只收敛、不取新行，探针零调用
    submit, futures = make_submit_pending()
    ok_base = dict(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                   encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                   store_add=lambda e, m, v: None, submit=submit)
    mw.tick(TickEnv(**ok_base))  # 先提交第一项（在途未完成）
    assert mw._inflight is not None
    boom_env = TickEnv(**{**ok_base, "vector_idle_seconds": boom_probe,
                          "text_encode_busy": boom_probe})
    mw.tick(boom_env)  # ② 在途短路：只收敛 ⇒ 门探针零调用（不抛 AssertionError）
    assert mw._inflight is not None  # future 未完成 ⇒ 记录保持在途
    assert len(futures) == 1  # 零新提交
    assert mw.get_state()["status"] == "encoding"


def test_gate_both_conditions_fail_still_waits(tmp_path):
    """钉 `or` 语义：闲置 5.0s ∧ 在途**同时**不满足 ⇒ 仍只报一次让路等待（不启动、不崩溃）；
    reason 按实际命中条件拼装（busy ⇒ 「交互编码在途」）。下一拍稳定等待。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/x.jpg", "/tmp/fake/x.jpg")])
    submit, futures = make_submit_sync()
    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit,
                  vector_idle_seconds=lambda: 5.0, text_encode_busy=lambda: True)
    mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert "交互编码在途" in st["reason"] and "让路" in st["reason"]
    assert futures == []
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)
    mw.tick(env)  # 再跑一拍：稳定等待（不崩、不误启动）
    st2 = mw.get_state()
    assert st2["status"] == "idle" and "让路" in st2["reason"]
    assert futures == []


def test_gate_empty_queue_shorts_before_gate(tmp_path):
    """钉 ③ 在 ⑤ 之前：队列为空（无活可干）+ 关门 ⇒ 报**空 reason** 的 idle（「无活可干」型），
    不是「让路」reason——没有未完成行时门根本不参与（探针零调用，注入「被调用即炸」桩）。"""
    q = tmp_path / "queue.md"
    q.write_text("", encoding="utf-8")

    def boom_probe():
        raise AssertionError("无未完成行时门探针不得被调用（③ 在 ⑤ 之前）")

    env = TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=lambda p: [0.1] * 768, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None,
                  vector_idle_seconds=boom_probe, text_encode_busy=boom_probe)
    mw.tick(env)  # 不得抛异常
    st = mw.get_state()
    assert st["status"] == "idle"
    assert st["reason"] == ""  # 「无活可干」型 idle：空 reason（不是「让路」）
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 0, 0)


# ---------------------------------------------------------------------------
# ⑮ 文件已不存在 ⇒ 不可重试（F-4/D-1）：显式 FileNotFoundError / TOCTOU（encoder 抛 RuntimeError
# 但收敛时文件已不在）⇒ 一次处置——标注完成（行保留）、清旧计数、WARNING、如实文案（含「文件不存在」
# stalled + 可辨 reason（不冻结在 encoding）；对照：**非后端** RuntimeError 且文件存在 ⇒ 同样一次处置（可重试类别仅后端故障
# ``MediaEncodeBackendError``，走 1/3→弃单）。

class _LevelLogger:
    """loguru logger 的同形替身（收 warning/error/info 三路）——断言「文件不存在」处置走
    **WARNING** 级日志（不是 ERROR）且文案如实。"""

    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, message, *args, **kwargs):
        self.infos.append(str(message))

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def error(self, message, *args, **kwargs):
        self.errors.append(str(message))


def _make_failure_env(q, encoder_boom, monkeypatch):
    """组装「在途异常」形态的 TickEnv：fake submit 同步执行、异常捕进假 future（与生产同构）；
    队列行 / 编码器 / 护栏 / 写库均桩（文件头夹具纪律）。"""
    def submit(fn):
        f = FakeFuture()
        try:
            fn()
        except BaseException as e:
            f.set_exception(e)
        else:
            f.set_result(None)
        return f

    return TickEnv(queue_path=str(q), switch=lambda: True, ready_check=lambda: True,
                  encoder=encoder_boom, guard=lambda p: ("pass", "", None),
                  store_add=lambda e, m, v: None, submit=submit)


def test_missing_file_not_retryable_marks_done_once(tmp_path, monkeypatch):
    """⑤ 显式 FileNotFoundError ⇒ 不可重试：``_fail_counts`` 不增长、``f4_mark_line_done`` **恰一次**、
    状态回 idle + 三计数 (0,1,1)（弃单计入完成比，行标完成留在文件里）；临时提示含「文件不存在」与
    「无需重试」、**不含**「将自动重试」/「连续 n 次失败」；日志走 WARNING（不落 ERROR）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/gone.jpg", "/tmp/fake/gone.jpg")])
    events, notices = [], []
    monkeypatch.setattr(mw, "_broadcast", events.append)
    monkeypatch.setattr(mw, "_notify_failure", notices.append)
    log = _LevelLogger()
    monkeypatch.setattr(mw, "logger", log)
    mark_calls = []
    real_mark = mw.f4_mark_line_done
    monkeypatch.setattr(mw, "f4_mark_line_done",
                       lambda *a, **kw: (mark_calls.append((a, kw)), real_mark(*a, **kw))[1])

    def boom(p):
        raise FileNotFoundError(f"媒体向量编码失败：文件不存在（可能入库后被删除）: {p}")

    env = _make_failure_env(q, boom, monkeypatch)
    mw.tick(env)  # tick#1 提交（异常进 future）
    assert mw.get_state()["status"] == "encoding"
    mw.tick(env)  # tick#2 收敛 ⇒ 不可重试一次处置
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert mw._fail_counts == {}  # 不可重试不写计数（旧契约会写成 1）
    assert len(mark_calls) == 1  # 标注恰一次（旧契约重试 3 次 ⇒ 标注 3 次）
    assert mw._inflight is None
    assert queue_records(q)[0]["done"] is True  # 行标完成、留在文件里（页面删文件）
    assert notices and "文件不存在" in notices[-1] and "无需重试" in notices[-1]
    assert "将自动重试" not in notices[-1] and "连续" not in notices[-1]
    assert any("文件不存在" in w and "无需重试" in w for w in log.warnings)
    assert log.errors == []  # WARNING 级，不落 ERROR
    drops = [e for e in events if e.get("type") == "media-ingest-completed" and not e.get("ok")]
    assert drops and "无需重试" in drops[-1]["message"]


def test_missing_file_toctou_runtime_error_still_not_retryable(tmp_path, monkeypatch):
    """⑥ TOCTOU 形态（R-2 复合判据钉死）：encoder 抛 `RuntimeError`（模拟第二断裂层：入口存在性
    判定已过、编码时文件被删 ⇒ `llama_server.encode_media` 抛 RuntimeError("图片文件不存在")）且
    收敛时判出文件已不在 ⇒ **仍不重试**。构造法：在 `media_worker` 模块内 monkeypatch
    `os.path.exists` 恒假（本文件唯一 `os.path` 用法即该复合判据，队列读/标注走 `os.open` 不受影响）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/toc.jpg", "/tmp/fake/toc.jpg")])
    monkeypatch.setattr(mw.os.path, "exists", lambda p: False)  # 收敛时刻：文件已不在
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom(p):
        raise RuntimeError(f"图片文件不存在: {p}")

    env = _make_failure_env(q, boom, monkeypatch)
    mw.tick(env); mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert mw._fail_counts == {}  # isinstance-only 实现 ⇒ 本条红
    assert mw._inflight is None
    assert queue_records(q)[0]["done"] is True


def test_missing_file_pops_preexisting_stale_count(tmp_path, monkeypatch):
    """⑦ 旧计数清理：``_fail_counts`` 里已有该实体的旧计数（旧契约/前一轮残留）⇒ 不可重试处置后
    被 pop（不可重试虽不加计数，但必须清旧值，否则该实体下一次真可重试的失败会从旧值起步、
    重试预算误判）。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/stale.jpg", "/tmp/fake/stale.jpg")])
    mw._fail_counts["a/stale.jpg"] = 2  # 预置旧计数
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom(p):
        raise FileNotFoundError(f"文件不存在: {p}")

    env = _make_failure_env(q, boom, monkeypatch)
    mw.tick(env); mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "idle"
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)
    assert "a/stale.jpg" not in mw._fail_counts  # 旧计数被 pop（保留 ⇒ 红）
    assert queue_records(q)[0]["done"] is True


def test_missing_file_mark_failed_stalls_with_distinguishable_reason(tmp_path, monkeypatch):
    """⑧ 标注返 "failed"：不可重试路径遇标注失败 ⇒ 发布 stalled + 可辨 reason（不冻结在 `encoding`、
    tick ② 不永久短路），同时 `_fail_counts` 计数**仍不写**（仅与既有弃单失败分支同款之形、不含计数
    写入）——下轮重新取件、再判不存在、再标注。"""
    q = tmp_path / "queue.md"
    write_queue(q, [rec("a/markfail.jpg", "/tmp/fake/markfail.jpg")])
    monkeypatch.setattr(mw, "f4_mark_line_done", lambda *a, **kw: "failed")
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)
    monkeypatch.setattr(mw, "_notify_failure", lambda m: None)

    def boom(p):
        raise FileNotFoundError(f"文件不存在: {p}")

    env = _make_failure_env(q, boom, monkeypatch)
    mw.tick(env); mw.tick(env)
    st = mw.get_state()
    assert st["status"] == "stalled"  # 冻结在 encoding ⇒ 红
    assert "文件不存在" in st["reason"] and "标注" in st["reason"]
    assert "将自动重试" not in st["reason"]
    assert mw._fail_counts == {}  # 不可重试全程不写计数（标注失败也不写）
    assert mw._inflight is None
    assert queue_lines(q) == 1  # 行仍未标注，下轮再标注
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (1, 1, 0)


def test_runtime_error_with_file_present_one_shot(tmp_path, monkeypatch):
    """⑨ 对照（2026-10-17 用户定稿）：future 抛**非后端** `RuntimeError`（如白名单外拦截）且文件**存在**
    ⇒ **一次处置**——标完成 + WARNING + ``_fail_counts`` 不写 + 文案含「无需重试」、不含「将自动重试」/
    「连续 n 次失败」：可重试的失败类别只有媒体编码后端故障（``MediaEncodeBackendError``），确定性
    拦截不占重试预算（D-2 演进）。"""
    q = tmp_path / "queue.md"
    real = tmp_path / "real.jpg"
    real.write_bytes(b"\xff\xd8\x00garbage")  # 真实存在的文件（tmp 内），exists() 真值不改
    write_queue(q, [rec("a/real.jpg", str(real))])
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)
    notices = []
    monkeypatch.setattr(mw, "_notify_failure", notices.append)

    def boom(p):
        raise RuntimeError(f"白名单外图片格式，拦截: {p}（仅支持 JPEG/PNG/WEBP/HEIC）")

    env = _make_failure_env(q, boom, monkeypatch)
    mw.tick(env); mw.tick(env)  # tick#2 收敛 ⇒ 非后端异常 ⇒ 一次处置
    st = mw.get_state()
    assert st["status"] == "idle"  # 改走重试（stalled + 1/3）⇒ 红
    assert (st["queue_depth"], st["queue_total"], st["queue_done"]) == (0, 1, 1)  # 一次处置计入完成比
    assert mw._fail_counts == {}  # 非后端失败不占重试预算（旧契约会写成 1）
    assert queue_records(q)[0]["done"] is True  # 标完成、行留在文件里
    assert notices and "无需重试" in notices[-1]
    assert "将自动重试" not in notices[-1] and "连续" not in notices[-1]
