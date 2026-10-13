"""S5：媒体向量状态端点（``GET /api/media/pipeline_status``）行为断言（AC-13 可编程半）。

不真起调度器：直接调端点函数 ``media_pipeline_status()``，monkeypatch 工作者状态机的各状态形态
+ ``niu_api.internal.scheduler.get_scheduler``。**先 monkeypatch ``media_worker.F4_DEFAULT_QUEUE``
指向临时队列文件**，再经 ``append_record(line + "\\n", md_path=临时队列文件)`` 写行（S3 的唯一队列
写入路径——既有原语，排它锁 + O_APPEND 持锁写、失败告警不抛；数的是真实文件行数；绝不写真 ~/.niu）：

① 状态 ``encoding`` + entity ⇒ ``{busy: True, status: "encoding", entity, queue_depth}``（调度器在跑）；
② ``off`` + reason ⇒ 原样回报（即使调度器也未在运行——开关关的 reason 优先，不被 scheduler-stopped 覆盖）；
③ ``stalled`` + depth>0 ⇒ reason 可辨（「实例未加载 mmproj…」）；
④ ``get_scheduler`` 抛 RuntimeError 且**队列文件真实写入一行**（状态机保持初值 ``{idle, depth=0}``
   ——模拟调度器从未启动的 R16 常规形态，不用 monkeypatch 伪造 depth）⇒ ``status == "scheduler-stopped"``
   + ``queue_depth == 1``（数的是队列文件、不是状态机）+ R16 reason；
   对照变体：队列文件为空 ⇒ 如实报 ``idle``（不误报）；
⑤ 调度器在跑 + 状态 idle + 队列空 ⇒ 原样 idle（不伪装）；
⑥ ``get_scheduler`` 抛 RuntimeError + 队列文件写入非 UTF-8 字节（调度器未起 × 文件读不出叠加形态）
   ⇒ ``status == "scheduler-stopped"`` + reason 含「队列文件读不出」+ ``queue_depth == -1``
   （-1 哨兵：行数未知，绝不误报 0 行，第三轮 P3 合并回归）。

SSE 事件广播断言在 ``tests/test_media_worker.py``（``media_worker._broadcast`` 注入收集器）；
UI 轮询策略断言（「轮询永不停止」）在 ``tests/test_media_polling.js``（node vm 行为探针）。
"""
import json

import pytest

import niu_api.internal.scheduler as scheduler_pkg
import niu_api.internal.media_worker as mw
from agent.md_mirror import append_record
from niu_api.media_api import media_pipeline_status


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class _FakeRunningScheduler:
    """端点只读 ``running`` 旗标：缺省调度器实例（不真起线程、不建线程池）。"""

    running = True


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    """端点沙箱：队列文件指向临时目录（覆盖探测的 ``_queue_depth(None)`` 读的是它）、
    状态机复位到初值 ``{idle, depth=0}``、媒体库接缝重定向（绝不写真 ~/.niu）。"""
    monkeypatch.setenv("NIU_MEDIA_STORE", str(tmp_path / "media_store.json"))
    queue_file = tmp_path / "queue.md"
    monkeypatch.setattr(mw, "F4_DEFAULT_QUEUE", str(queue_file))
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0)
    monkeypatch.setattr(mw, "_broadcast", lambda e: None)  # 端点纯读不广播，双保险
    yield queue_file
    mw._set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=0)


def _scheduler_running(monkeypatch):
    """模拟调度器在运行（get_scheduler 返回 running=True 实例）。"""
    monkeypatch.setattr(scheduler_pkg, "get_scheduler", lambda: _FakeRunningScheduler())


def _scheduler_absent(monkeypatch):
    """模拟调度器从未启动（R16 常规形态：get_scheduler 抛 RuntimeError，与 service.py 真实行为同型）。"""
    def boom():
        raise RuntimeError("Scheduler not initialized")

    monkeypatch.setattr(scheduler_pkg, "get_scheduler", boom)


def write_queue_line(queue_file):
    """经 S3 唯一写入路径追加一行合法队列记录（真实文件行数 +1）。"""
    rec = {"entity_name": "a/img-0001.jpg", "file_path": "/tmp/fake/img.jpg", "ts": "2026-10-13T00:00:00"}
    assert append_record(json.dumps(rec, ensure_ascii=False) + "\n", md_path=str(queue_file))


# ---------------------------------------------------------------------------
# ① encoding + entity：在途编码原样回报（busy=True）
# ---------------------------------------------------------------------------

def test_encoding_reports_busy_and_entity(monkeypatch):
    _scheduler_running(monkeypatch)
    mw._set_state(status="encoding", reason="", entity="a/img-0001.jpg",
                  started_at=1760000000.0, queue_depth=2)
    r = media_pipeline_status()
    assert r["busy"] is True
    assert r["status"] == "encoding"
    assert r["entity"] == "a/img-0001.jpg"
    assert r["queue_depth"] == 2


# ---------------------------------------------------------------------------
# ② off：开关关的 reason 优先，即使调度器也未在运行（不被 scheduler-stopped 覆盖）
# ---------------------------------------------------------------------------

def test_off_reports_as_is_even_when_scheduler_absent(sandbox, monkeypatch):
    _scheduler_absent(monkeypatch)
    write_queue_line(sandbox)  # 队列有积压，但开关关 ⇒ 只报 off
    mw._set_state(status="off", reason="开关关（media_ingest.enabled=false）",
                  entity=None, started_at=None, queue_depth=1)
    r = media_pipeline_status()
    assert r["status"] == "off"
    assert r["busy"] is False
    assert "开关关" in r["reason"]
    assert r["entity"] is None


# ---------------------------------------------------------------------------
# ③ stalled：队列非空但编码面不可用，reason 可辨、原样回报
# ---------------------------------------------------------------------------

def test_stalled_reports_distinguishable_reason(monkeypatch):
    _scheduler_running(monkeypatch)
    mw._set_state(status="stalled",
                  reason="实例未加载 mmproj 或编码后端不可用（队列只增不耗）",
                  entity=None, started_at=None, queue_depth=3)
    r = media_pipeline_status()
    assert r["status"] == "stalled"
    assert r["busy"] is False
    assert "mmproj" in r["reason"]
    assert r["queue_depth"] == 3


# ---------------------------------------------------------------------------
# ④ 调度器从未启动（R16 常规形态）：状态机停留初值，端点直接数队列文件覆盖回报
# ---------------------------------------------------------------------------

def test_scheduler_absent_with_backlog_overrides_idle(sandbox, monkeypatch):
    """get_scheduler 抛错 + 队列文件真实一行 + 状态机保持初值 {idle, depth=0}
    ⇒ scheduler-stopped + queue_depth == 1（数的是队列文件，不是状态机的 0）+ R16 reason。"""
    _scheduler_absent(monkeypatch)
    write_queue_line(sandbox)  # 真实写一行；状态机不经 tick、停在初值（idle, depth=0）
    st = mw.get_state()
    assert st["status"] == "idle" and st["queue_depth"] == 0  # 初值形态，端点必须不信它
    r = media_pipeline_status()
    assert r["status"] == "scheduler-stopped"
    assert r["queue_depth"] == 1
    assert r["busy"] is False
    assert "R16" in r["reason"] or "队列只增不耗" in r["reason"]
    assert r["entity"] is None


def test_scheduler_absent_with_empty_queue_reports_idle(monkeypatch):
    """对照变体：调度器未起但队列确为空 ⇒ 如实报 idle（不误报 scheduler-stopped）。"""
    _scheduler_absent(monkeypatch)
    # 不写任何队列行（文件不存在 ⇒ 行数 0）
    r = media_pipeline_status()
    assert r["status"] == "idle"
    assert r["queue_depth"] == 0
    assert r["reason"] == ""


# ---------------------------------------------------------------------------
# ⑤ 调度器在跑 + idle + 队列空：原样 idle（不伪装、不覆盖）
# ---------------------------------------------------------------------------

def test_idle_reports_as_is_when_scheduler_running(monkeypatch):
    _scheduler_running(monkeypatch)
    r = media_pipeline_status()
    assert r["status"] == "idle"
    assert r["queue_depth"] == 0
    assert r["busy"] is False


# ---------------------------------------------------------------------------
# ⑥ 调度器未起 × 队列文件读不出（非 UTF-8）叠加形态：-1 哨兵，绝不误报 0 行
# ---------------------------------------------------------------------------

def test_scheduler_absent_with_unreadable_queue_file_reports_minus_one(sandbox, monkeypatch):
    _scheduler_absent(monkeypatch)
    sandbox.write_bytes(b"\xff\xfe\x00bad-not-utf8")  # 文件存在但非 UTF-8 ⇒ _queue_depth 返 -1
    assert mw._queue_depth(None) == -1  # 哨兵前置断言（与端点同一路径）
    r = media_pipeline_status()
    assert r["status"] == "scheduler-stopped"
    assert r["queue_depth"] == -1
    assert r["busy"] is False
    assert "队列文件读不出" in r["reason"]
