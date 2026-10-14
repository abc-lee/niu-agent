"""S5：媒体向量状态端点（``GET /api/media/pipeline_status``）行为断言（AC-13 可编程半）+ v3.5 页面
删文件端点（``POST /api/media/queue/dismiss``）行为断言。

不真起调度器：直接调端点函数 ``media_pipeline_status()`` / ``media_queue_dismiss()``，monkeypatch
工作者状态机的各状态形态 + ``niu_api.internal.scheduler.get_scheduler``。**双命名空间打桩**：同时
monkeypatch ``media_worker.F4_DEFAULT_QUEUE``（覆盖探测 ``_queue_counts(None)`` 读的工作者侧兜底路径）
与 ``agent.md_mirror.MEDIA_QUEUE_PATH``（无参 ``f4_dismiss_if_all_done()`` 在 md_mirror 命名空间解析的
缺省路径，v3.5 新增 dismiss 接缝）指向同一临时队列文件，再经 ``append_record(line + "\\n",
md_path=临时队列文件)`` 写行（S3 的唯一队列写入路径——既有原语，排它锁 + O_APPEND 持锁写、失败告警
不抛；数的是真实文件行数；绝不写真 ~/.niu）。沙箱另做真实路径前后快照并断言相等——任何用例经任何
路径触碰真实队列文件，teardown 即判红（autouse 对全文件所有用例生效）。

计数字段（v3.5，口径④-4）：``queue_depth`` = **未完成**行数、``queue_total`` = 记录行总数、
``queue_done`` = 已完成行数（完成行 + 未完成行并存文件里，口径②）；``-1`` 哨兵 = 文件存在但读不出
（行数未知）。

① 状态 ``encoding`` + entity ⇒ ``{busy: True, status: "encoding", entity, 三计数字段}``（调度器在跑）；
② ``off`` + reason ⇒ 原样回报（即使调度器也未在运行——开关关的 reason 优先，不被 scheduler-stopped 覆盖）；
③ ``stalled`` + depth>0 ⇒ reason 可辨（「实例未加载 mmproj…」）；
④ ``get_scheduler`` 抛 RuntimeError 且**队列文件真实写入一行未完成行**（状态机保持初值 ``{idle, 计数全 0}``
   ——模拟调度器从未启动的 R16 常规形态，不用 monkeypatch 伪造计数）⇒ ``status == "scheduler-stopped"``
   + ``queue_depth == 1``（数的是队列文件、不是状态机的 0）+ R16 reason；覆盖判定 = 未完成数 > 0；
   对照变体：队列文件为空（未完成数 0）⇒ 如实报 ``idle``（不误报）；
⑤ 调度器在跑 + 状态 idle + 队列空 ⇒ 原样 idle（不伪装、不覆盖）；
⑥ ``get_scheduler`` 抛 RuntimeError + 队列文件写入非 UTF-8 字节（调度器未起 × 文件读不出叠加形态）
   ⇒ ``status == "scheduler-stopped"`` + reason 含「队列文件读不出」+ 三计数全 ``-1``
   （-1 哨兵：行数未知，绝不误报 0 行，第三轮 P3 合并回归）；
⑦ 页面删文件端点：全部完成 ⇒ 删文件（文件消失）/ 文件缺失 ⇒ 视为已清；存在未完成行 ⇒ 拒删、文件
   逐字节不变（页面保留横条，下一轮轮询重试）；坏行（非 JSON）⇒ 拒删（坏行只由工作者的弃除路径消费，I-3）。

SSE 事件广播断言在 ``tests/test_media_worker.py``（``media_worker._broadcast`` 注入收集器）；
UI 轮询策略断言（「轮询永不停止」）在 ``tests/test_media_polling.js``（node vm 行为探针）。
"""
import json
import os  # 需新增：快照/断言用 os.stat / os.path.exists

import pytest  # 已有

import agent.md_mirror as mdm  # 需新增：模块对象——无参 f4_dismiss_if_all_done() 对它的命名空间解析默认路径
import niu_api.internal.scheduler as scheduler_pkg  # 已有
import niu_api.internal.media_worker as mw  # 已有
from agent.md_mirror import append_record  # 已有
from niu_api.media_api import media_pipeline_status, media_queue_dismiss


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class _FakeRunningScheduler:
    """端点只读 ``running`` 旗标：缺省调度器实例（不真起线程、不建线程池）。"""

    running = True


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    """端点沙箱（v3.5 双命名空间打桩 + 真实路径安全断言）。唯一正确顺序：先真实路径「前」快照、后打桩。"""
    # ★ 必须早于下方 mdm 打桩：此行若挪到 monkeypatch.setattr(mw/mdm, …) 之后，
    #   快照到的就是 tmp 路径，安全断言失效。
    real = mdm.MEDIA_QUEUE_PATH  # 真实路径（~/.niu/md/media_vector_queue.md）
    try:
        real_before = (os.stat(real).st_mtime_ns, open(real, "rb").read())  # 存在 ⇒ (mtime, 全文件字节)
    except FileNotFoundError:
        real_before = None  # 不存在 ⇒ None
    # 既有接缝保留：NIU_MEDIA_STORE 重定向
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_store.json"))
    queue_file = tmp_path / "queue.md"
    monkeypatch.setattr(mw, "F4_DEFAULT_QUEUE", str(queue_file))   # 工作者侧兜底（_queue_counts(None) 路径）
    monkeypatch.setattr(mdm, "MEDIA_QUEUE_PATH", str(queue_file))  # md_mirror 默认路径（无参 dismiss 在此解析）
    # 既有接缝保留：_set_state 初值复位 / _broadcast 置空桩
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0, queue_total=0, queue_done=0)
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)  # 端点纯读不广播，双保险
    yield queue_file
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0, queue_total=0, queue_done=0)
    # 「后」快照：用例结束后再拍一次并断言相等——误写同时改变 mtime + 字节、误删使其消失、
    # 误建使 before=None / after≠None，任一情况用例判红。
    after = None if not os.path.exists(real) else (os.stat(real).st_mtime_ns, open(real, "rb").read())
    assert after == real_before, "测试沙箱触碰了真实队列文件（须同步打桩 mw.F4_DEFAULT_QUEUE 与 mdm.MEDIA_QUEUE_PATH）"


def _scheduler_running(monkeypatch):
    """模拟调度器在运行（get_scheduler 返回 running=True 实例）。"""
    monkeypatch.setattr(scheduler_pkg, "get_scheduler", lambda: _FakeRunningScheduler())


def _scheduler_absent(monkeypatch):
    """模拟调度器从未启动（R16 常规形态：get_scheduler 抛 RuntimeError，与 service.py 真实行为同型）。"""
    def boom():
        raise RuntimeError("Scheduler not initialized")

    monkeypatch.setattr(scheduler_pkg, "get_scheduler", boom)


def write_queue_line(queue_file):
    """经 S3 唯一写入路径追加一行合法队列记录（真实文件行数 +1；三键、无 done ⇒ 未完成行）。"""
    rec = {"entity_name": "a/img-0001.jpg", "file_path": "/tmp/fake/img.jpg", "ts": "2026-10-13T00:00:00"}
    assert append_record(json.dumps(rec, ensure_ascii=False) + "\n", md_path=str(queue_file))


def write_done_queue_line(queue_file):
    """追加一行已由工作者标注完成的记录（原三键 + done: true）——构造 dismiss 用例的「全部完成」形态。"""
    rec = {"entity_name": "a/img-0002.jpg", "file_path": "/tmp/fake/img2.jpg", "ts": "2026-10-13T00:00:01", "done": True}
    assert append_record(json.dumps(rec, ensure_ascii=False) + "\n", md_path=str(queue_file))


# ---------------------------------------------------------------------------
# ① encoding + entity：在途编码原样回报（busy=True），三计数字段透传
# ---------------------------------------------------------------------------

def test_encoding_reports_busy_and_entity(monkeypatch):
    _scheduler_running(monkeypatch)
    mw._set_state(status="encoding", reason="", entity="a/img-0001.jpg",
                  started_at=1760000000.0, queue_depth=2, queue_total=5, queue_done=3)
    r = media_pipeline_status()
    assert r["busy"] is True
    assert r["status"] == "encoding"
    assert r["entity"] == "a/img-0001.jpg"
    assert r["queue_depth"] == 2
    assert r["queue_total"] == 5
    assert r["queue_done"] == 3


# ---------------------------------------------------------------------------
# ② off：开关关的 reason 优先，即使调度器也未在运行（不被 scheduler-stopped 覆盖）
# ---------------------------------------------------------------------------

def test_off_reports_as_is_even_when_scheduler_absent(sandbox, monkeypatch):
    _scheduler_absent(monkeypatch)
    write_queue_line(sandbox)  # 队列有积压（未完成数 1），但开关关 ⇒ 只报 off
    mw._set_state(status="off", reason="开关关（media_ingest.enabled=false）",
                  entity=None, started_at=None, queue_depth=1, queue_total=1, queue_done=0)
    r = media_pipeline_status()
    assert r["status"] == "off"
    assert r["busy"] is False
    assert "开关关" in r["reason"]
    assert r["entity"] is None
    assert r["queue_depth"] == 1
    assert r["queue_total"] == 1
    assert r["queue_done"] == 0


# ---------------------------------------------------------------------------
# ③ stalled：队列有未完成行但编码面不可用，reason 可辨、原样回报
# ---------------------------------------------------------------------------

def test_stalled_reports_distinguishable_reason(monkeypatch):
    _scheduler_running(monkeypatch)
    mw._set_state(status="stalled",
                  reason="实例未加载 mmproj 或编码后端不可用（队列只增不耗）",
                  entity=None, started_at=None, queue_depth=3, queue_total=4, queue_done=1)
    r = media_pipeline_status()
    assert r["status"] == "stalled"
    assert r["busy"] is False
    assert "mmproj" in r["reason"]
    assert r["queue_depth"] == 3
    assert r["queue_total"] == 4
    assert r["queue_done"] == 1


# ---------------------------------------------------------------------------
# ④ 调度器从未启动（R16 常规形态）：状态机停留初值，端点直接数队列文件覆盖回报
# ---------------------------------------------------------------------------

def test_scheduler_absent_with_backlog_overrides_idle(sandbox, monkeypatch):
    """get_scheduler 抛错 + 队列文件真实一行未完成行（无 done）+ 状态机保持初值 {idle, 计数全 0}
    ⇒ scheduler-stopped + queue_depth == 1（数的是队列文件，不是状态机的 0）+ R16 reason。
    覆盖判定 = 未完成数 > 0（口径④-4）。"""
    _scheduler_absent(monkeypatch)
    write_queue_line(sandbox)  # 真实写一行未完成行；状态机不经 tick、停在初值（idle, 计数全 0）
    st = mw.get_state()
    assert st["status"] == "idle" and st["queue_depth"] == 0  # 初值形态，端点必须不信它
    r = media_pipeline_status()
    assert r["status"] == "scheduler-stopped"
    assert r["queue_depth"] == 1
    assert r["queue_total"] == 1
    assert r["queue_done"] == 0
    assert r["busy"] is False
    assert "R16" in r["reason"] or "队列只增不耗" in r["reason"]
    assert r["entity"] is None


def test_scheduler_absent_with_empty_queue_reports_idle(monkeypatch):
    """对照变体：调度器未起但队列确无未完成行（文件不存在 ⇒ 未完成数 0）⇒ 如实报 idle（不误报 scheduler-stopped）。"""
    _scheduler_absent(monkeypatch)
    # 不写任何队列行（文件不存在 ⇒ 未完成数 0）
    r = media_pipeline_status()
    assert r["status"] == "idle"
    assert r["queue_depth"] == 0
    assert r["queue_total"] == 0
    assert r["queue_done"] == 0
    assert r["reason"] == ""


# ---------------------------------------------------------------------------
# ⑤ 调度器在跑 + idle + 队列空：原样 idle（不伪装、不覆盖）
# ---------------------------------------------------------------------------

def test_idle_reports_as_is_when_scheduler_running(monkeypatch):
    _scheduler_running(monkeypatch)
    r = media_pipeline_status()
    assert r["status"] == "idle"
    assert r["queue_depth"] == 0
    assert r["queue_total"] == 0
    assert r["queue_done"] == 0
    assert r["busy"] is False


# ---------------------------------------------------------------------------
# ⑥ 调度器未起 × 队列文件读不出（非 UTF-8）叠加形态：三计数全 -1 哨兵，绝不误报 0 行
# ---------------------------------------------------------------------------

def test_scheduler_absent_with_unreadable_queue_file_reports_minus_one(sandbox, monkeypatch):
    _scheduler_absent(monkeypatch)
    sandbox.write_bytes(b"\xff\xfe\x00bad-not-utf8")  # 文件存在但非 UTF-8 ⇒ _queue_counts 三计数全 -1
    assert mw._queue_counts(None) == (-1, -1, -1)  # 哨兵前置断言（与端点同一路径）
    r = media_pipeline_status()
    assert r["status"] == "scheduler-stopped"
    assert r["queue_depth"] == -1
    assert r["queue_total"] == -1
    assert r["queue_done"] == -1
    assert r["busy"] is False
    assert "队列文件读不出" in r["reason"]


# ---------------------------------------------------------------------------
# ⑦ 页面删文件端点（v3.5 口径③/丁）：全完成删文件 / 缺失视为已清 / 未完成与坏行拒删
# ---------------------------------------------------------------------------

def test_dismiss_all_done_removes_file(sandbox):
    """队列两行都带完成标记 ⇒ 每条记录行都带完成标记 ⇒ 删文件、dismissed: true、文件消失。"""
    write_done_queue_line(sandbox)
    write_done_queue_line(sandbox)
    r = media_queue_dismiss()
    assert r["dismissed"] is True
    assert not sandbox.exists()


def test_dismiss_pending_line_keeps_file(sandbox):
    """存在无 done 的行（未完成）⇒ 拒删、文件内容逐字节不变（页面保留横条，下一轮轮询重试）。"""
    write_queue_line(sandbox)
    before = sandbox.read_bytes()
    r = media_queue_dismiss()
    assert r["dismissed"] is False
    assert sandbox.read_bytes() == before


def test_dismiss_missing_file_reports_true():
    """队列文件不存在 ⇒ 本来就没有，视为已清，同样返回 dismissed: true（「下次打开」形态）。"""
    r = media_queue_dismiss()
    assert r["dismissed"] is True


def test_dismiss_bad_line_keeps_file(sandbox):
    """队列含坏行（非 JSON）⇒ 坏行不算「已完成」⇒ 拒删、文件逐字节不变——坏行只由工作者的弃除
    路径消费，页面不删文件（I-3）。"""
    sandbox.write_text("{not valid json}\n", encoding="utf-8")
    before = sandbox.read_bytes()
    r = media_queue_dismiss()
    assert r["dismissed"] is False
    assert sandbox.read_bytes() == before
