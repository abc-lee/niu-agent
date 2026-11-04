"""见缝插针末轮残留 → 独立 follow-up run（主 Agent run 结束后自动触发）。

触发点：`agent/main_runtime.py::_loop_body` 的 finally（`_active` 已清空、
handle 收口前）。补充分支已把 user 行落库 + SSE 通知，所以 follow-up 命令用
`exclude_message_ids` 把它们从历史里排除——历史 = 上一轮报告，当前输入 = 这条
补充消息，DB 顺序与组装顺序一致（不二次落库、不重复）。

失败归属唯一：一旦 `runtime.submit` 成功，items 归 follow-up 所有；setup /
submit 失败由本模块自行重入队或落中性行，`_loop_body` 的 done_callback 只记日志。
"""
from __future__ import annotations

import asyncio

from loguru import logger


def build_followup_command(items, parent, *, on_complete):
    """纯函数（可单测）：content/session/channel/exclude_message_ids 的唯一来源。"""
    from agent.main_runtime import RunCommand
    from agent.runner import format_supplement_text

    return RunCommand(
        content=format_supplement_text([i.content for i in items]),   # 与 drain_supplement 同源
        session_id=items[0].session_id or parent.session_id,
        source=items[0].source,
        channel=items[0].channel,
        channel_id=items[0].channel_id,
        stream=False,
        resources=None,
        exclude_message_ids=[i.message_id for i in items if i.message_id],
        on_complete=on_complete,
    )


def group_items(items) -> list[list]:
    """纯函数（可单测）：按 (session_id, source, channel, channel_id) 分组。

    补充队列是进程全局的——不同通道/会话的项可能混在一起。整批取首条元数据会让
    `channel="inherit"` 的程序注入项代表 Electron 项（IM 标志不被清理，桌面回复
    可能被推去 IM），因此每组一个 `RunCommand`。返回保持首次出现顺序。
    """
    groups: dict[tuple, list] = {}
    for item in items:
        key = (item.session_id, item.source, item.channel, item.channel_id)
        groups.setdefault(key, []).append(item)
    return list(groups.values())


async def submit_followup_run(runtime, items, parent) -> None:
    """把残留补充项按通道元数据分组，每组提交成一个独立 run（失败自行兜底，不抛）。"""
    from agent.main_runtime import RunOutcome
    from agent.runner import requeue_supplement_items
    from agent.session import get_message_store

    try:
        from niu_api.chat import get_or_create_runner
        from niu_api.main_agent_persist import make_on_complete, persist_run_outcome

        store = await get_message_store()
        runner = get_or_create_runner()

        def _build_command(group):
            holder = {
                "reply": "", "message_id": None, "error": None,
                "should_push_im": False, "im_channel_id": "",
            }

            def on_overflow() -> None:
                # 与 compat 正常路径一致：零参闭包，store/source 由本模块捕获
                from niu_api.chat import fire_and_forget_compaction
                fire_and_forget_compaction(store, source="ChatSession")

            persist = make_on_complete(store, holder, on_overflow=on_overflow)

            def on_complete(outcome) -> None:
                try:                                          # 快照必须在锁内取
                    holder["should_push_im"] = bool(runner.should_push_im())
                    holder["im_channel_id"] = runner.get_im_channel()
                except Exception as exc:
                    logger.warning(f"[SupplementFollowup] IM flag snapshot failed: {exc}")
                persist(outcome)
                from niu_api.chat import _main_loop
                loop = _main_loop
                if loop is None or loop.is_closed():
                    return
                try:
                    from niu_api.channel.gateway import push_im_reply
                    asyncio.run_coroutine_threadsafe(
                        push_im_reply(runner, holder.get("reply") or "", snapshot=holder), loop)
                except Exception as exc:
                    logger.warning(f"[SupplementFollowup] IM push scheduling failed: {exc}")

            return build_followup_command(group, parent, on_complete=on_complete), holder, len(group)

        # 命令构造也在 setup try 内：任何失败都原样重入队，绝不静默丢消息
        commands = [_build_command(group) for group in group_items(items)]
    except Exception as exc:
        logger.error(f"[SupplementFollowup] setup failed, requeue: {exc}")
        requeue_supplement_items(items)
        return

    for cmd, holder, count in commands:
        try:
            runtime.submit(cmd)
        except Exception as exc:                      # 不留孤儿 user 行
            logger.error(f"[SupplementFollowup] submit failed: {exc}")
            try:
                await persist_run_outcome(store, RunOutcome(error=exc, neutral_degrade=True), holder)
            except Exception:
                logger.exception("[SupplementFollowup] neutral persist failed")
            continue
        logger.info(
            f"[SupplementFollowup] resubmitted {count} supplement(s) as new run: "
            f"channel={cmd.channel} msg_ids={cmd.exclude_message_ids}"
        )
