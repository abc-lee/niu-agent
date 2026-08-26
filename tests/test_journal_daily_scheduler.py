"""journal_daily 定时任务测试（直读 DB 改造：日志即水位线）。

覆盖：
1. 触发分派：trigger_callback 按 task_kind=journal_daily 走直执行分支
2. **严禁经 ChatQueue enqueue**（journal 内容写进 messages.db 会反污染上下文窗口）
3. 配置开关 context.journalScheduledEnabled（默认开启；关闭→静默跳过返回成功语义）
4. 自理工作流派发：任务文本含起点判定/get_messages 分页/reason 分级/空批/
   覆盖标记/@end 协议；程序侧零游标零文件操作，failure/incomplete/overflow 仅落日志
5. 内置任务注册：journal-daily（journal_daily）创建 + 旧 daily-journal-check 退役

全 mock：call_subagent_with_auto_answer / runner——禁真实 LLM、禁图谱写入、
messages.db 零新增、~/.niu 零写入。
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


def _run_job(monkeypatch, tmp_path, subagent_result):
    """驱动 _run_journal_daily_job 同步执行（线程体直接调，便于断言）。

    返回 call_mock。全 mock：避让等待跳过、runner 假对象、子 Agent 结果注入。
    """
    from agent import subagent as subagent_module

    call_mock = mock.MagicMock(return_value=subagent_result)
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr(
        "niu_api.chat.get_or_create_runner",
        lambda: mock.MagicMock(llm_config={"model": "m", "apikey": "x", "apibase": "http://x"}),
    )
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)

    # 直接跑线程体（锁由执行体内部持有/释放，与生产一致）
    svc._run_journal_daily_job()
    return call_mock


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
        svc._run_journal_daily_job()
        assert called == [], "持锁期间执行体应直接跳过"
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


def test_disabled_at_run_skips_subagent(monkeypatch, tmp_path):
    """执行时开关已关 → 跳过本轮，不调子 Agent（避让等待由 _trigger 层前置检查覆盖）。"""
    from agent import subagent as subagent_module

    call_mock = mock.MagicMock()
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: False)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    svc._run_journal_daily_job()
    assert call_mock.call_count == 0, "开关关闭时不得调 journal-agent"


def test_missing_runner_skips_subagent(monkeypatch, tmp_path):
    """runner 未初始化 → 跳过本轮，不调子 Agent。"""
    from agent import subagent as subagent_module

    call_mock = mock.MagicMock()
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: None)
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    svc._run_journal_daily_job()
    assert call_mock.call_count == 0


# ---------------------------------------------------------------------------
# 3. 自理工作流派发（任务文本契约 + 程序侧零游标零文件）
# ---------------------------------------------------------------------------

def test_job_passes_self_managed_workflow_task(monkeypatch, tmp_path):
    """任务文本自理：含起点判定/get_messages 分页/reason 分级/空批/覆盖标记/@end 协议。"""
    call_mock = _run_job(monkeypatch, tmp_path, NORMAL_JSON)
    assert call_mock.call_count == 1
    assert call_mock.call_args.args[0] == "journal-agent"
    task_arg = call_mock.call_args.args[1]
    assert "覆盖至" in task_arg, "起点判定与标记协议必须内联在任务文本"
    assert "after_id" in task_arg and "limit=200" in task_arg, "分页拉取用法必须在场"
    assert 'session_id="default"' in task_arg
    assert "invalid_after_id" in task_arg and "transient" in task_arg, "reason 分级错误处理必须在场"
    assert "无新消息" in task_arg, "空批处理必须在场"
    assert "@end" in task_arg, "@end 结束协议必须在场"


def test_success_writes_no_cursor_and_no_file(monkeypatch, tmp_path):
    """正常返回即成功：程序侧零游标读写、零导出文件（水位线由子 Agent 按标记自理）。"""
    _run_job(monkeypatch, tmp_path, NORMAL_JSON)
    assert list(tmp_path.iterdir()) == [], "程序侧不得产生任何文件"


@pytest.mark.parametrize("bad_result", [
    json.dumps({"incomplete": True, "agent": "journal-agent", "reason": "STOPPED", "partial_result": ""}),
    json.dumps({"overflow": True, "agent": "journal-agent", "turns_completed": 2,
                "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}),
    "SUBAGENT_ERROR: llm down",
])
def test_bad_results_swallow_to_log(monkeypatch, tmp_path, bad_result):
    """failure/incomplete/overflow 仅落日志：不抛异常、不写任何状态（下轮 cron 自然重试）。"""
    call_mock = _run_job(monkeypatch, tmp_path, bad_result)
    assert call_mock.call_count == 1
    assert list(tmp_path.iterdir()) == []


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


def test_service_has_no_cursor_or_workset_references():
    """源码钉：scheduler 服务不再引用游标/工作集符号（退役反向钉）。"""
    source = (_root / "niu_api" / "internal" / "scheduler" / "service.py").read_text(encoding="utf-8")
    for retired in ("_export_journal_increment", "_build_journal_file_task",
                    "_parse_processed_up_to", "_read_cursor_with_lock",
                    "_write_cursor_with_lock", "JOURNAL_CURSOR_PATH",
                    "JOURNAL_WORKSET_PATH", "last_journal"):
        assert retired not in source, f"{retired} 应已退役"
