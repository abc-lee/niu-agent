"""整理管道队列测试：投递失败重抛（入口 8 机械压实内联直调契约已随统一压缩入口退役）。

设计见 docs/superpowers/plans/2026-08-23-remove-outer-subagent-timeouts.md §3.3（入口 8 内联化，
不经队列、无外层等待上限）与 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.1 入口 9。
T6（统一压缩入口 spec 2026-09-06）：runner._on_context_high_usage 已删除——主 Agent 响应后
不再压实（压缩只在 agent_loop 发送前门执行），原「入口 8 内联直调」四个用例随之退役；
本文件现仅钉 compat._pipeline_enqueue 的投递失败重抛契约。
"""
import pytest

import niu_api.compat as compat
from niu_api.compat import stop_pipeline_queue


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()
    yield
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()


async def test_enqueue_failure_reraises(monkeypatch):
    """compat _pipeline_enqueue 投递失败（put_nowait 抛异常）：重新 raise（调用方契约是返回 Future）。

    T6：force/runner-force 去重表已退役，仅剩「异常重新抛出」语义。
    """
    from niu_api.compat import start_pipeline_queue
    start_pipeline_queue()
    q = compat._pipeline_queue

    def _boom_put(*a, **k):
        raise RuntimeError("queue closed")

    monkeypatch.setattr(q, "put_nowait", _boom_put)
    with pytest.raises(RuntimeError, match="queue closed"):
        compat._pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
