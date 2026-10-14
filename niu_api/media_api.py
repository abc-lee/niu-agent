"""媒体向量后台工作者的对外状态面（v3.5，spec D-27/R14 + 口径③/丁 页面删文件）。

``GET /api/media/pipeline_status`` —— Chat 页第二根横条的轮询端点（同型参照：
``/api/kg/pipeline_status``）。**纯读**工作者在进程状态：端点不付任何新探活（探活只发生在
tick 内，循环线程的阻塞上界已披露）⇒ 恒为毫秒级。计数字段（v3.5，口径④-4）：
``queue_depth`` = **未完成**行数、``queue_total`` = 记录行总数、``queue_done`` = 已完成行数
——文件里完成行 + 未完成行并存（口径②），百分比 = 已完成/总数 = 真进度；``-1`` 哨兵 =
文件存在但读不出（行数未知）。

``POST /api/media/queue/dismiss`` —— Chat 页面的删文件责任（口径③/丁）：页面观察到「全部
完成」后调它（页面开着时 ≤ 一轮轮询间隔内；窗口关掉 = 延后到下次打开，不是缺陷）；端点
**持排它锁复核**「每条记录行都带完成标记，或文件无任何记录行」才删文件，**存在未完成行
⇒ 拒删、文件保持原样**（堵「观察到全部完成、删除前写入方新追加一行」的竞态）。工作者自身
永不删文件——长期无人开页面 ⇒ 文件留着（完成行累积，手册已披露，I-9）。

三种「未运行」都如实回报（R14/R16）：
- 开关关 ⇒ 工作者状态 ``off`` + reason（横条永不显示，D-27）；
- 调度器未起（LLM 不可用 / 损坏取消延迟启动，R16）⇒ tick 从未运行、状态停留初值
  ``{idle, 计数全 0}``——此时状态机的计数不可信，本端点不采信它，直接数队列文件实际行数
  （毫秒级本地文件读、非探活）；实际未完成行数 > 0 ⇒ 回报 ``scheduler-stopped`` + 可辨 reason
  （队列确无未完成行 ⇒ 如实报 idle，不误报）；
- 队列有未完成行但实例未加载 mmproj 或编码后端死 ⇒ 工作者状态 ``stalled`` + reason。
"""
from fastapi import APIRouter

from agent.md_mirror import f4_dismiss_if_all_done
from niu_api.internal import media_worker

router = APIRouter(prefix="/api/media", tags=["media-vector"])


@router.get("/pipeline_status")
def media_pipeline_status() -> dict:
    st = media_worker.get_state()
    # 调度器未起（R16）：tick 从未运行，状态机停留初值 {idle, 计数全 0}——此时状态机的计数
    # 恒为 0（唯一更新者是 tick，它从未跑过），不可信 ⇒ 直接数队列文件实际行数（毫秒级本地
    # 文件读、非探活、不违背纯读承诺）；否则「队列只增不耗」会伪装成「空闲」：用户以为在入库
    # 向量、实际一张没编（R14）。
    # 只在状态 idle（初值）且实际未完成行数 > 0 时覆盖：off（开关关）/stalled/encoding 一律原样
    # 回报——它们的 reason 来自 tick、新且可辨，覆盖回报会错归因。
    sched = None
    try:
        from niu_api.internal.scheduler import get_scheduler

        sched = get_scheduler()
    except Exception:
        sched = None
    if sched is None or not sched.running:
        actual_depth, actual_total, actual_done = media_worker._queue_counts(None)  # 缺省队列文件；-1 哨兵 = 文件存在但读不出
        if st["status"] == "idle" and actual_depth > 0:
            return {
                "busy": False,
                "status": "scheduler-stopped",
                "reason": "调度器未运行（LLM 不可用 / 启动被取消 / 延迟启动尚未完成），队列只增不耗（R16）",
                "entity": None,
                "queue_depth": actual_depth,
                "queue_total": actual_total,
                "queue_done": actual_done,
            }
        if st["status"] == "idle" and actual_depth < 0:
            # 队列文件存在但读不出（非 UTF-8/权限异常）：行数未知，状态机计数又恒为初值 0（tick 从未跑）
            # ——不覆盖回报就会静默成 idle（第三轮 P3-2 的端点侧形态）⇒ 一并如实回报。
            return {
                "busy": False,
                "status": "scheduler-stopped",
                "reason": "调度器未运行（R16），且队列文件读不出（非 UTF-8 / 权限异常），行数未知（需人工检查队列文件）",
                "entity": None,
                "queue_depth": -1,
                "queue_total": -1,
                "queue_done": -1,
            }
    return {
        "busy": st["status"] == "encoding",
        "status": st["status"],
        "reason": st["reason"],
        "entity": st.get("entity"),
        "queue_depth": st.get("queue_depth", 0),
        "queue_total": st.get("queue_total", 0),
        "queue_done": st.get("queue_done", 0),
    }


@router.post("/queue/dismiss")
def media_queue_dismiss() -> dict:
    """Chat 页面的删文件责任（v3.5 口径③/丁）：页面观察到「全部完成」（总数 > 0 且已完成 ≥
    总数）后调本端点；**锁内复核**——每条记录行都带完成标记（或文件无记录行）⇒ 删文件、
    返回 ``dismissed: true``；只要存在未完成行 ⇒ 拒删、文件保持原样（页面应保留横条，下一轮
    轮询重试，I-3）；文件本就不存在 ⇒ 视为已清，同样返回 true。纯文件操作、毫秒级、无探活。"""
    ok = f4_dismiss_if_all_done()  # 缺省队列文件；原语自身是 total（异常内部吞、返回 False）
    if ok:
        return {"dismissed": True, "detail": "队列文件已删除（或本就不存在）"}
    return {"dismissed": False, "detail": "存在未完成行，文件保持原样（页面保留横条，下一轮轮询重试）"}
