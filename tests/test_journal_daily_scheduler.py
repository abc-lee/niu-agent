"""subagent 定时任务测试（journal_daily 硬编码直执行泛化为第三种任务类型 task_kind='subagent'）。

覆盖：
1. 触发分派：trigger_callback 按 task_kind=subagent 走后台直执行分支；未知 task_kind /
   subagent 无 agent_name 显式拒绝（logger.error + return None，**不落 reminder 兜底**
   ——治理输出落 reminder 注入 messages.db = 反污染铁律禁止）
2. **严禁经 ChatQueue enqueue**（治理输出写进 messages.db 会反污染上下文窗口）；
   report 例外通道：report_sink 非空时以 [后台任务「{task_label}」结束报告] 前缀送达主 Agent
3. 配置开关 context.journalScheduledEnabled 仅作用于内置 journal-daily 任务
   （按 task name 判断，不泛化）
4. 静默派发：agent_name/任务文本取自 task dict；程序侧零游标零文件操作，
   failure/incomplete/overflow 仅落日志（log-only，下轮 cron 自然重试）
5. 内置任务注册：journal-daily（subagent + agent_name=journal-daily-agent）创建接线 +
   旧 journal_daily 一次性迁移（保用户 cron）+ 旧 daily-journal-check 退役

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

# 内置 journal-daily 任务（与 __main__._system_tasks 条目对齐——
# content/kind/agent_name parity 由 test_builtin_task_def_uses_subagent_kind 钉住）
JOURNAL_DAILY_TASK = {
    "id": "t-journal",
    "name": "journal-daily",
    "content": "每日日志整理：程序直读 DB 增量提取写入 journal.md",
    "task_kind": "subagent",
    "agent_name": "journal-daily-agent",
}


def _run_job(monkeypatch, tmp_path, subagent_result, task=None):
    """驱动 _run_subagent_task 同步执行（线程体直接调，便于断言）。

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
    svc._run_subagent_task(task if task is not None else dict(JOURNAL_DAILY_TASK))
    return call_mock


# ---------------------------------------------------------------------------
# 1. 触发分派与直执行分支（严禁经 ChatQueue；显式拒绝不落兜底）
# ---------------------------------------------------------------------------

def test_trigger_dispatches_background_thread_not_chatqueue(monkeypatch):
    """subagent 分支：派后台线程直执行，绝不 enqueue ChatQueue。"""
    started = threading.Event()

    def fake_job(task):
        started.set()

    enqueued = []
    monkeypatch.setattr("niu_api.chat_queue.get_chat_queue",
                        lambda: (_ for _ in ()).throw(AssertionError("subagent 不得触达 ChatQueue")))
    # get_chat_queue 在模块顶部已 import——双保险 patch 引用点
    monkeypatch.setattr(svc, "get_chat_queue",
                        lambda: enqueued.append("ENQUEUED") or (_ for _ in ()).throw(AssertionError("x")))
    monkeypatch.setattr(svc, "_subagent_task_thread", None)
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_run_subagent_task", fake_job)

    result = svc._trigger_subagent_task(dict(JOURNAL_DAILY_TASK))
    assert result == "ok"
    assert started.wait(timeout=5), "后台线程应被派发执行"
    assert enqueued == [], "不得经 ChatQueue 入队"


def test_trigger_rejects_subagent_without_agent_name(monkeypatch):
    """subagent 无 agent_name = 创建契约破坏 → 显式拒绝（return None，不派发）。"""
    dispatched = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: dispatched.append(1) or mock.MagicMock())
    assert svc._trigger_subagent_task({"id": "t1", "task_kind": "subagent", "content": "x"}) is None
    assert dispatched == [], "无 agent_name 不得派发线程"


def test_trigger_callback_rejects_unknown_task_kind(monkeypatch):
    """未知 task_kind（含迁移遗漏滞留的 legacy journal_daily）→ 显式拒绝，
    不落 reminder 兜底（「[定时任务] 每日日志整理」入 ChatQueue = 治理输出反污染）。"""
    enqueued = []
    monkeypatch.setattr(
        svc, "get_chat_queue",
        lambda: mock.MagicMock(
            enqueue_sync=lambda **k: enqueued.append(k) or mock.MagicMock(queued=True)
        ),
    )
    assert svc.trigger_callback({"id": "t1", "content": "每日日志整理", "task_kind": "journal_daily"}) is None
    assert svc.trigger_callback({"id": "t2", "content": "x", "task_kind": "bogus-kind"}) is None
    assert enqueued == [], "未知 task_kind 不得落 reminder 入队（反污染铁律）"


def test_job_skips_when_previous_run_in_progress(monkeypatch):
    """上一轮未完成 → 执行体抢不到锁直接跳过（非阻塞锁去重）。"""
    assert svc._subagent_task_lock.acquire(blocking=False)
    try:
        called = []

        def boom(*a, **k):
            called.append(1)

        monkeypatch.setattr(svc, "_wait_backend_idle", boom)
        svc._run_subagent_task(dict(JOURNAL_DAILY_TASK))
        assert called == [], "持锁期间执行体应直接跳过"
    finally:
        svc._subagent_task_lock.release()


# ---------------------------------------------------------------------------
# 2. 配置开关（D13 禁用语义仅作用于内置 journal-daily，按 name 判断）
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
    """journal-daily 禁用 → 静默跳过，返回 ok（成功语义，不进失败链）。"""
    dispatched = []
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: False)
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: dispatched.append(1) or mock.MagicMock())
    assert svc._trigger_subagent_task(dict(JOURNAL_DAILY_TASK)) == "ok"
    assert dispatched == []


def test_trigger_non_journal_subagent_ignores_journal_switch(monkeypatch):
    """开关不泛化：非 journal-daily 的 subagent 任务不受 journalScheduledEnabled 拦截。"""
    started = threading.Event()
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: False)
    monkeypatch.setattr(svc, "_run_subagent_task", lambda task: started.set())
    task = {"id": "t1", "name": "my-bg-task", "agent_name": "some-agent", "content": "x"}
    assert svc._trigger_subagent_task(task) == "ok"
    assert started.wait(timeout=5), "非 journal-daily 任务不应被 journal 开关拦截"


def test_disabled_at_run_skips_subagent(monkeypatch, tmp_path):
    """执行时开关已关 → 跳过本轮，不调子 Agent（避让等待由 _trigger 层前置检查覆盖）。"""
    from agent import subagent as subagent_module

    call_mock = mock.MagicMock()
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: False)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    svc._run_subagent_task(dict(JOURNAL_DAILY_TASK))
    assert call_mock.call_count == 0, "开关关闭时不得调 journal-daily-agent"


def test_missing_runner_skips_subagent(monkeypatch, tmp_path):
    """runner 未初始化 → 跳过本轮，不调子 Agent。"""
    from agent import subagent as subagent_module

    call_mock = mock.MagicMock()
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: None)
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", call_mock)
    svc._run_subagent_task(dict(JOURNAL_DAILY_TASK))
    assert call_mock.call_count == 0


# ---------------------------------------------------------------------------
# 3. 静默派发（task dict 契约 + report 例外通道 + 程序侧零游标零文件）
# ---------------------------------------------------------------------------

def test_job_passes_self_managed_workflow_task(monkeypatch, tmp_path):
    """泛化后按 task dict 派发：agent_name="journal-daily-agent" + 任务文本=
    _system_tasks 的 content。自理工作流协议已移入 config/agents/journal-daily-agent.md
    （由 test_journal_daily_agent_md_carries_workflow_protocol 钉住）。"""
    call_mock = _run_job(monkeypatch, tmp_path, NORMAL_JSON)
    assert call_mock.call_count == 1
    assert call_mock.call_args.args[0] == "journal-daily-agent"
    assert call_mock.call_args.args[1] == JOURNAL_DAILY_TASK["content"], \
        "任务文本必须来自 task dict（=__main__._system_tasks 的 content）"
    assert isinstance(call_mock.call_args.kwargs.get("report_sink"), list), \
        "report 例外通道接线：report_sink 出参必须传入"


def test_journal_daily_agent_md_carries_workflow_protocol():
    """协议源钉：夜间整理自理工作流协议在 config/agents/journal-daily-agent.md
    （起点判定/get_messages 分页/reason 分级/空批/覆盖标记/@end 协议 + report 例外通道教学）。"""
    md = (_root / "config" / "agents" / "journal-daily-agent.md").read_text(encoding="utf-8")
    assert "覆盖至" in md, "起点判定与标记协议必须在场"
    assert "after_id" in md and "limit=200" in md, "分页拉取用法必须在场"
    assert 'session_id="default"' in md
    assert "invalid_after_id" in md and "transient" in md, "reason 分级错误处理必须在场"
    assert "无新消息" in md, "空批处理必须在场"
    assert "@end" in md, "@end 结束协议必须在场"
    assert '"report"' in md, "report 例外通道教学必须在场"


def test_success_writes_no_cursor_and_no_file(monkeypatch, tmp_path):
    """正常返回即成功：程序侧零游标读写、零导出文件（水位线由子 Agent 按标记自理）。"""
    _run_job(monkeypatch, tmp_path, NORMAL_JSON)
    assert list(tmp_path.iterdir()) == [], "程序侧不得产生任何文件"


def test_no_report_stays_silent(monkeypatch, tmp_path):
    """无 report：静默零打扰——不触达 ChatQueue。"""
    monkeypatch.setattr(svc, "get_chat_queue",
                        lambda: (_ for _ in ()).throw(AssertionError("无 report 不得触达 ChatQueue")))
    _run_job(monkeypatch, tmp_path, NORMAL_JSON)


def test_report_sink_delivered_to_main_agent(monkeypatch, tmp_path):
    """report 例外通道：sink 非空 → [后台任务「{task_label}」结束报告] 经 ChatQueue
    送达主 Agent（channel/source=scheduler, session_id=default；单向通知无挂起）。"""
    from agent import subagent as subagent_module

    def fake_call(agent_name, task, report_sink=None, **kwargs):
        if report_sink is not None:
            report_sink.append("整理失败：messages.db 锁定")
        return NORMAL_JSON

    sent = []
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr(
        "niu_api.chat.get_or_create_runner",
        lambda: mock.MagicMock(llm_config={"model": "m"}),
    )
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", fake_call)
    monkeypatch.setattr(
        svc, "get_chat_queue",
        lambda: mock.MagicMock(
            enqueue_sync=lambda **k: sent.append(k) or mock.MagicMock(queued=True)
        ),
    )
    svc._run_subagent_task(dict(JOURNAL_DAILY_TASK))
    assert len(sent) == 1
    assert sent[0]["content"] == "[后台任务「journal-daily」结束报告] 整理失败：messages.db 锁定"
    assert sent[0]["channel"] == "scheduler"
    assert sent[0]["source"] == "scheduler"
    assert sent[0]["session_id"] == "default"


def test_report_task_label_falls_back_to_content(monkeypatch, tmp_path):
    """task 无 name 时 task_label 回退 content[:20]（防 [后台任务「None」结束报告]）。"""
    from agent import subagent as subagent_module

    def fake_call(agent_name, task, report_sink=None, **kwargs):
        if report_sink is not None:
            report_sink.append("r")
        return NORMAL_JSON

    sent = []
    monkeypatch.setattr(svc, "read_journal_scheduled_enabled", lambda: True)
    monkeypatch.setattr(svc, "_wait_backend_idle", lambda sched: None)
    monkeypatch.setattr(
        "niu_api.chat.get_or_create_runner",
        lambda: mock.MagicMock(llm_config={"model": "m"}),
    )
    monkeypatch.setattr(subagent_module, "call_subagent_with_auto_answer", fake_call)
    monkeypatch.setattr(
        svc, "get_chat_queue",
        lambda: mock.MagicMock(
            enqueue_sync=lambda **k: sent.append(k) or mock.MagicMock(queued=True)
        ),
    )
    task = {"id": "t2", "agent_name": "some-agent",
            "content": "这是一个没有名字的定时后台任务内容它超过二十个字需要截断"}
    svc._run_subagent_task(task)
    assert len(sent) == 1
    assert sent[0]["content"] == f"[后台任务「{task['content'][:20]}」结束报告] r"


@pytest.mark.parametrize("bad_result", [
    json.dumps({"incomplete": True, "agent": "journal-daily-agent", "reason": "STOPPED", "partial_result": ""}),
    json.dumps({"overflow": True, "agent": "journal-daily-agent", "turns_completed": 2,
                "tokens_used": 1, "tokens_limit": 2, "partial_result": ""}),
    "SUBAGENT_ERROR: llm down",
])
def test_bad_results_swallow_to_log(monkeypatch, tmp_path, bad_result):
    """failure/incomplete/overflow 仅落日志：不抛异常、不写任何状态（下轮 cron 自然重试）。"""
    call_mock = _run_job(monkeypatch, tmp_path, bad_result)
    assert call_mock.call_count == 1
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 4. 内置任务注册（__main__ 8.6 节契约：subagent 类型 + 迁移接线）
# ---------------------------------------------------------------------------

def test_builtin_task_def_uses_subagent_kind():
    """源码钉：系统任务表 journal-daily 为 subagent 类型且带 agent_name；旧提醒已退役。"""
    source = (_root / "niu_api" / "__main__.py").read_text(encoding="utf-8")
    assert '"name": "journal-daily"' in source
    assert '"task_kind": "subagent"' in source
    assert '"agent_name": "journal-daily-agent"' in source
    assert '"task_kind": "journal_daily"' not in source, "旧 journal_daily 类型应已迁移"
    assert 'find_task_by_name("daily-journal-check")' in source, "应含旧任务一次性迁移"
    assert '"name": "daily-journal-check"' not in source, "旧 daily-journal-check 定义应删除"


def test_ensure_loop_forwards_agent_name_on_create():
    """源码钉：fresh-install 接线——ensure 循环 create_task 必须转发 agent_name，
    否则全新安装建出 agent_name=NULL 的 subagent 任务 → 每次触发被显式拒绝
    → 3-strike failed → journal 静默死。"""
    source = (_root / "niu_api" / "__main__.py").read_text(encoding="utf-8")
    assert 'agent_name=task_def.get("agent_name")' in source


def test_main_ensure_block_calls_legacy_migration():
    """源码钉：__main__ ensure 块调用 _migrate_legacy_journal_daily(ts)——
    漏接线=存量安装 journal 每日 18 点静默死。"""
    source = (_root / "niu_api" / "__main__.py").read_text(encoding="utf-8")
    # def 定义行 + ensure 块调用点 = 至少 2 处
    assert source.count("_migrate_legacy_journal_daily(ts)") >= 2, \
        "ensure 块必须调用 _migrate_legacy_journal_daily(ts)"


def test_migrate_legacy_journal_daily_updates_kind_preserves_cron(tmp_path):
    """D4 迁移具名测试：旧 journal_daily → subagent+agent_name；用户改过的
    cron_expr 不变；幂等（已迁移任务再次迁移不再动）。"""
    from niu_api.__main__ import _migrate_legacy_journal_daily
    from niu_api.internal.scheduler.task_store import TaskStore

    ts = TaskStore(str(tmp_path / "tasks.db"))
    ts.create_task(
        content=JOURNAL_DAILY_TASK["content"],
        scheduled_at="2026-09-02T07:30:00",
        event_type="recurring",
        is_recurring=True,
        cron_expr="30 7 * * *",  # 用户改过的执行时间
        name="journal-daily",
        task_kind="journal_daily",
    )
    _migrate_legacy_journal_daily(ts)
    migrated = ts.find_task_by_name("journal-daily")
    assert migrated is not None
    assert migrated["task_kind"] == "subagent"
    assert migrated["agent_name"] == "journal-daily-agent"
    assert migrated["cron_expr"] == "30 7 * * *", "用户改过的 cron 必须保留"

    # 幂等：再次迁移不再改动
    _migrate_legacy_journal_daily(ts)
    again = ts.find_task_by_name("journal-daily")
    assert again is not None
    assert again["task_kind"] == "subagent"
    assert again["cron_expr"] == "30 7 * * *"


def test_service_routes_subagent_before_reminder():
    """源码钉：trigger_callback 的 subagent 分支先于 reminder enqueue 分支；
    旧 journal_daily 分支已随迁移移除（clean cutover，不留兼容分支）。"""
    source = (_root / "niu_api" / "internal" / "scheduler" / "service.py").read_text(encoding="utf-8")
    subagent_branch = source.index('task.get("task_kind") == "subagent"')
    enqueue_branch = source.index("q.enqueue_sync(content=prompt")
    assert subagent_branch < enqueue_branch, "subagent 必须走直执行分支，先于任何 enqueue"
    assert '"journal_daily"' not in source, "旧 journal_daily 分支/常量应已删除"


def test_service_has_no_cursor_or_workset_references():
    """源码钉：scheduler 服务不再引用游标/工作集符号（退役反向钉）。"""
    source = (_root / "niu_api" / "internal" / "scheduler" / "service.py").read_text(encoding="utf-8")
    for retired in ("_export_journal_increment", "_build_journal_file_task",
                    "_parse_processed_up_to", "_read_cursor_with_lock",
                    "_write_cursor_with_lock", "JOURNAL_CURSOR_PATH",
                    "JOURNAL_WORKSET_PATH", "last_journal"):
        assert retired not in source, f"{retired} 应已退役"
