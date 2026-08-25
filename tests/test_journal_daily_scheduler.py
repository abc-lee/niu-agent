"""journal_daily 定时任务测试（T7：journal 迁出睡眠管道 → scheduler 直执行）。

覆盖：
1. 触发分派：trigger_callback 按 task_kind=journal_daily 走直执行分支
2. **严禁经 ChatQueue enqueue**（journal 内容写进 messages.db 会反污染上下文窗口）
3. 配置开关 context.journalScheduledEnabled（默认开启；关闭→静默跳过返回成功语义）
4. 游标推进：成功按 processed_up_to=N 查映射推进 / incomplete·overflow·failure 不动 /
   兜底区间末尾 / 游标被删回退
5. 增量导出：无增量不调子 Agent；工作集文件清理
6. 内置任务注册：journal-daily（journal_daily）创建 + 旧 daily-journal-check 退役

全 mock：call_subagent_with_auto_answer / runner / 游标文件——禁真实 LLM、禁图谱写入、
messages.db 零新增。
"""
import json
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import niu_api.internal.scheduler.service as svc


NORMAL_JSON = json.dumps({"ok": True})


class _Msg:
    def __init__(self, mid, content="hello"):
        self.id = mid
        self.role = "user"
        self.content = content
        self.tool_calls = None
        self.tool_call_id = ""
        self.created_at = "t"


def _real_export(messages, last_cursor_id, out_path):
    """真实导出函数（仅路径参数已由调用方 patch 隔离）。"""
    from niu_api.compat import _export_journal_increment as fn
    return fn(messages, last_cursor_id, out_path)


def _make_runner(messages):
    runner = mock.MagicMock()
    runner.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
    runner._sync_get_messages = lambda: messages
    return runner


def _run_job(monkeypatch, tmp_path, subagent_result, messages=None, cursor_value=""):
    """驱动 _run_journal_daily_job 同步执行（线程体直接调，便于断言）。

    返回 (call_mock, paths)。
    """
    from agent import subagent as subagent_module

    messages = messages if messages is not None else [_Msg("m1"), _Msg("m2")]
    call_mock = mock.MagicMock(return_value=subagent_result)
    cursor = tmp_path / "last_journal.json"
    workset = tmp_path / "md" / "journal_workset.md"
    if cursor_value:
        cursor.write_text(json.dumps({"last_journal_id": cursor_value}), encoding="utf-8")

    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr("niu_api.compat.JOURNAL_CURSOR_PATH", cursor)
    monkeypatch.setattr("niu_api.compat.JOURNAL_WORKSET_PATH", workset)
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: _make_runner(messages))
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)

    # 直接跑线程体（锁由执行体内部持有/释放，与生产一致）
    svc._run_journal_daily_job()
    return call_mock, {"cursor": cursor, "workset": workset}


# ---------------------------------------------------------------------------
# 1. 触发分派与直执行分支（严禁经 ChatQueue）
# ---------------------------------------------------------------------------

def test_trigger_dispatches_background_thread_not_chatqueue(monkeypatch):
    """journal_daily 分支：派后台线程直执行，绝不 enqueue ChatQueue。"""
    started = threading.Event()

    def fake_job():
        started.set()

    enqueued = []
    monkeypatch.setattr("niu_api.chat_queue.get_chat_queue",
                        lambda: (_ for _ in ()).throw(AssertionError("journal_daily 不得触达 ChatQueue")))
    # get_chat_queue 在模块顶部已 import——双保险 patch 引用点
    monkeypatch.setattr(svc, "get_chat_queue",
                        lambda: enqueued.append("ENQUEUED") or (_ for _ in ()).throw(AssertionError("x")))
    monkeypatch.setattr(svc, "_journal_thread", None)
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_run_journal_daily_job", fake_job)

    result = svc._trigger_journal_daily({"id": "t1"})
    assert result == "ok"
    assert started.wait(timeout=5), "后台线程应被派发执行"
    assert enqueued == [], "不得经 ChatQueue 入队"


def test_job_skips_when_previous_run_in_progress(monkeypatch):
    """上一轮未完成 → 执行体抢不到锁直接跳过（非阻塞锁去重）。"""
    assert svc._journal_run_lock.acquire(blocking=False)
    try:
        called = []

        def boom(*a, **k):
            called.append(1)

        monkeypatch.setattr(svc, "_wait_backend_idle", boom)
        monkeypatch.setattr("niu_api.compat.JOURNAL_CURSOR_PATH", Path("/nonexistent/x.json"))
        svc._run_journal_daily_job()
        assert called == [] and not Path("/nonexistent/x.json").exists(), "持锁期间执行体应直接跳过"
    finally:
        svc._journal_run_lock.release()


# ---------------------------------------------------------------------------
# 2. 配置开关（D13 禁用语义覆盖 journal）
# ---------------------------------------------------------------------------

def test_read_journal_scheduled_enabled_default_true(tmp_path):
    """context.journalScheduledEnabled 缺省 → 默认开启。"""
    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")
    with mock.patch("niu_api.config.CONFIG_PATH", str(cfg)):
        assert svc.read_journal_scheduled_enabled() is True


def test_read_journal_scheduled_disabled(tmp_path):
    """显式 false → 关闭。"""
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"context": {"journalScheduledEnabled": False}}), encoding="utf-8")
    with mock.patch("niu_api.config.CONFIG_PATH", str(cfg)):
        assert svc.read_journal_scheduled_enabled() is False


def test_trigger_disabled_returns_ok_without_dispatch(monkeypatch):
    """禁用 → 静默跳过，返回 ok（成功语义，不进失败链）。"""
    dispatched = []
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: False)
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: dispatched.append(1) or mock.MagicMock())
    assert svc._trigger_journal_daily({"id": "t1"}) == "ok"
    assert dispatched == []


# ---------------------------------------------------------------------------
# 3. 游标推进（程序侧按映射推进，F1 同款模式）
# ---------------------------------------------------------------------------

def test_cursor_advances_per_processed_up_to(monkeypatch, tmp_path):
    """成功 → 解析 processed_up_to=N 查映射推进游标并写回。"""
    result = NORMAL_JSON + "\n处理完成 @end\nprocessed_up_to=1"
    call_mock, paths = _run_job(
        monkeypatch, tmp_path, result,
        messages=[_Msg("m1", "做 A"), _Msg("m2", "做 B")],
    )
    data = json.loads(paths["cursor"].read_text(encoding="utf-8"))
    assert data["last_journal_id"] == "m1", "processed_up_to=1 → m1"
    assert "last_journal_at" in data
    # 子 Agent task 含自读指令与工作集路径
    task_arg = call_mock.call_args.args[1]
    assert str(paths["workset"]) in task_arg
    assert "processed_up_to" in task_arg


def test_cursor_fallback_to_range_end_without_marker(monkeypatch, tmp_path):
    """未输出 processed_up_to → 兜底区间末尾。"""
    result = NORMAL_JSON + "\n处理完成 @end"
    _, paths = _run_job(
        monkeypatch, tmp_path, result,
        messages=[_Msg("m1"), _Msg("m2")], cursor_value="m1",
    )
    data = json.loads(paths["cursor"].read_text(encoding="utf-8"))
    assert data["last_journal_id"] == "m2"


@pytest.mark.parametrize("bad_result", [
    json.dumps({"incomplete": True, "agent": "journal-agent", "reason": "STOPPED", "partial_result": ""}),
    json.dumps({"overflow": True, "agent": "journal-agent", "turns_completed": 2,
                "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}),
    "SUBAGENT_ERROR: llm down",
])
def test_cursor_not_advanced_on_bad_results(monkeypatch, tmp_path, bad_result):
    """incomplete/overflow/failure → 游标不动（下轮重跑同区间）。"""
    _, paths = _run_job(
        monkeypatch, tmp_path, bad_result,
        messages=[_Msg("m1"), _Msg("m2")], cursor_value="m1",
    )
    data = json.loads(paths["cursor"].read_text(encoding="utf-8"))
    assert data["last_journal_id"] == "m1"


def test_cursor_reverts_when_deleted_from_db(monkeypatch, tmp_path):
    """推进目标已被并发删除 → 回退旧游标；旧游标也消失则不写。"""
    # m2 不在 fresh 列表（runner 二次读取只返回 m1）→ 回退 m1
    calls = {"n": 0}

    def flaky_messages():
        calls["n"] += 1
        return [_Msg("m1")] if calls["n"] >= 2 else [_Msg("m1"), _Msg("m2")]

    result = NORMAL_JSON + "\nprocessed_up_to=2"
    cursor = tmp_path / "last_journal.json"
    workset = tmp_path / "md" / "journal_workset.md"
    cursor.write_text(json.dumps({"last_journal_id": "m1"}), encoding="utf-8")

    from agent import subagent as subagent_module
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr("niu_api.compat.JOURNAL_CURSOR_PATH", cursor)
    monkeypatch.setattr("niu_api.compat.JOURNAL_WORKSET_PATH", workset)
    runner = _make_runner(None)
    runner._sync_get_messages = flaky_messages
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: runner)
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer",
                        mock.MagicMock(return_value=result))

    svc._run_journal_daily_job()

    data = json.loads(cursor.read_text(encoding="utf-8"))
    assert data["last_journal_id"] == "m1", "m2 已删应回退 m1"


def test_no_increment_skips_subagent(monkeypatch, tmp_path):
    """游标已在末条 → 零增量，不调子 Agent。"""
    call_mock, paths = _run_job(
        monkeypatch, tmp_path, NORMAL_JSON,
        messages=[_Msg("m1")], cursor_value="m1",
    )
    assert call_mock.call_count == 0, "零增量不得调 journal-agent"
    assert not paths["workset"].exists()


def test_workset_cleaned_after_success(monkeypatch, tmp_path):
    """成功后清理工作集临时文件。"""
    _, paths = _run_job(
        monkeypatch, tmp_path,
        NORMAL_JSON + "\nprocessed_up_to=2",
        messages=[_Msg("m1"), _Msg("m2")],
    )
    assert not paths["workset"].exists()


# ---------------------------------------------------------------------------
# 4. 内置任务注册（__main__ 8.6 节契约）
# ---------------------------------------------------------------------------

def test_builtin_task_def_uses_journal_daily_kind():
    """源码钉：系统任务表含 journal-daily 且 task_kind=journal_daily；旧提醒已退役。"""
    source = (_root / "niu_api" / "__main__.py").read_text(encoding="utf-8")
    assert '"name": "journal-daily"' in source
    assert '"task_kind": "journal_daily"' in source
    assert 'find_task_by_name("daily-journal-check")' in source, "应含旧任务一次性迁移"
    assert '"name": "daily-journal-check"' not in source, "旧 daily-journal-check 定义应删除"


def test_service_routes_journal_daily_before_reminder():
    """源码钉：trigger_callback 的 journal_daily 分支先于 reminder enqueue 分支。"""
    source = (_root / "niu_api" / "internal" / "scheduler" / "service.py").read_text(encoding="utf-8")
    jd_branch = source.index('task.get("task_kind") == "journal_daily"')
    enqueue_branch = source.index("q.enqueue_sync(content=prompt")
    assert jd_branch < enqueue_branch, "journal_daily 必须走直执行分支，先于任何 enqueue"
