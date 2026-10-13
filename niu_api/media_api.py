"""媒体向量后台工作者的对外状态面（v3.0，spec D-27/R14）。

``GET /api/media/pipeline_status`` —— Chat 页第二根横条的轮询端点（同型参照：
``/api/kg/pipeline_status``，A11）。**纯读**工作者在进程状态：端点不付任何新探活
（探活只发生在 tick 内，循环线程的阻塞上界已在 §5.5 披露）⇒ 本端点恒为毫秒级。

三种「未运行」都如实回报（R14/R16）：
- 开关关 ⇒ 工作者状态 ``off`` + reason（横条永不显示，D-27）；
- 调度器未起（LLM 不可用 / 损坏取消延迟启动，R16）⇒ tick 从未运行、状态停留初值
  ``{idle, depth=0}``——此时状态机的 depth 不可信，本端点不采信它，直接数队列文件实际行数
  （毫秒级本地文件读、非探活）；实际行数 > 0 ⇒ 回报 ``scheduler-stopped`` + 可辨 reason
  （队列确为空 ⇒ 如实报 idle，不误报）；
- 队列非空但实例未加载 mmproj 或编码后端死 ⇒ 工作者状态 ``stalled`` + reason。
"""
from fastapi import APIRouter

from niu_api.internal import media_worker

router = APIRouter(prefix="/api/media", tags=["media-vector"])


@router.get("/pipeline_status")
def media_pipeline_status() -> dict:
    st = media_worker.get_state()
    # 调度器未起（R16）：tick 从未运行，状态机停留初值 {idle, depth=0}——此时状态机的
    # queue_depth 恒为 0（唯一更新者是 tick，它从未跑过），不可信 ⇒ 直接数队列文件实际行数
    # （毫秒级本地文件读、非探活、不违背纯读承诺）；否则「队列只增不耗」会伪装成「空闲」：
    # 用户以为在入库向量、实际一张没编（R14）。
    # 只在状态 idle（初值）且实际行数 > 0 时覆盖：off（开关关）/stalled/encoding 一律原样回报——
    # 它们的 reason 来自 tick、新且可辨，覆盖回报会错归因。
    sched = None
    try:
        from niu_api.internal.scheduler import get_scheduler

        sched = get_scheduler()
    except Exception:
        sched = None
    if sched is None or not sched.running:
        actual = media_worker._queue_depth(None)  # 工作者的纯读队列行数器（缺省队列文件；-1 = 文件存在但读不出）
        if st["status"] == "idle" and actual > 0:
            return {
                "busy": False,
                "status": "scheduler-stopped",
                "reason": "调度器未运行（LLM 不可用 / 启动被取消 / 延迟启动尚未完成），队列只增不耗（R16）",
                "entity": None,
                "queue_depth": actual,
            }
        if st["status"] == "idle" and actual < 0:
            # 队列文件存在但读不出（非 UTF-8/权限异常）：行数未知，状态机 depth 又恒为初值 0（tick 从未跑）
            # ——不覆盖回报就会静默成 idle（第三轮 P3-2 的端点侧形态）⇒ 一并如实回报。
            return {
                "busy": False,
                "status": "scheduler-stopped",
                "reason": "调度器未运行（R16），且队列文件读不出（非 UTF-8 / 权限异常），行数未知（需人工检查队列文件）",
                "entity": None,
                "queue_depth": -1,
            }
    return {
        "busy": st["status"] == "encoding",
        "status": st["status"],
        "reason": st["reason"],
        "entity": st.get("entity"),
        "queue_depth": st.get("queue_depth", 0),
    }
