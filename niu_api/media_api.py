"""媒体向量后台工作者的对外状态面（v3.5，spec D-27/R14 + 口径③/丁 页面清空文件）。

``GET /api/media/pipeline_status`` —— Chat 页第二根横条的轮询端点（同型参照：
``/api/kg/pipeline_status``）。**纯读**工作者在进程状态：端点不付任何新探活（探活只发生在
tick 内，循环线程的阻塞上界已披露）⇒ 恒为毫秒级。计数字段（v3.5，口径④-4）：
``queue_depth`` = **未完成**行数、``queue_total`` = 记录行总数、``queue_done`` = 已完成行数
——文件里完成行 + 未完成行并存（口径②），百分比 = 已完成/总数 = 真进度；``-1`` 哨兵 =
文件存在但读不出（行数未知）。

``POST /api/media/queue/dismiss`` —— Chat 页面的清空文件责任（口径③/丁）：页面观察到「全部
完成」后调它（页面开着时 ≤ 一轮轮询间隔内；窗口关掉 = 延后到下次打开，不是缺陷）；端点
**持排它锁复核**「每条记录行都带完成标记，或文件无任何记录行」才清空文件（截断到 0，文件本身保留），
**存在未完成行 ⇒ 拒清空、文件保持原样**（堵「观察到全部完成、清空前写入方新追加一行」的竞态）。工作者自身
永不清空文件——长期无人开页面 ⇒ 文件留着（完成行累积，手册已披露，I-9）。

三种「未运行」都如实回报（R14/R16）：
- 开关关 ⇒ 工作者状态 ``off`` + reason（横条永不显示，D-27）；
- 调度器未起（LLM 不可用 / 损坏取消延迟启动，R16）⇒ tick 从未运行、状态停留初值
  ``{idle, 计数全 0}``——此时状态机的计数不可信，本端点不采信它，直接数队列文件实际行数
  （毫秒级本地文件读、非探活）；实际未完成行数 > 0 ⇒ 回报 ``scheduler-stopped`` + 可辨 reason
  （队列确无未完成行 ⇒ 如实报 idle，不误报）；
- 队列有未完成行但实例未加载 mmproj 或编码后端死 ⇒ 工作者状态 ``stalled`` + reason。
"""
import threading
from fastapi import APIRouter, Query
from loguru import logger

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
    """Chat 页面的清空文件责任（v3.5 口径③/丁）：页面观察到「全部完成」（总数 > 0 且已完成 ≥
    总数）后调本端点；**锁内复核**——每条记录行都带完成标记（或文件无记录行）⇒ 清空文件（截断到 0，
    文件本身保留）、返回 ``dismissed: true``；只要存在未完成行 ⇒ 拒清空、文件保持原样（页面应保留横条，下一轮
    轮询重试，I-3）；文件本就不存在 ⇒ 视为已清，同样返回 true。纯文件操作、毫秒级、无探活。"""
    ok = f4_dismiss_if_all_done()  # 缺省队列文件；原语自身是 total（异常内部吞、返回 False）
    if ok:
        return {"dismissed": True, "detail": "队列文件已清空（截断到 0，文件本身保留；或本就无内容）"}
    return {"dismissed": False, "detail": "存在未完成行，文件保持原样（页面保留横条，下一轮轮询重试）"}

# ============== 图谱窗口搜索面：GET /api/media/search（只读面，与 search_media 工具同口径） ==============
# 口径来源：lightrag-server 的 search_media 工具（磁盘面 /lightrag/search_media）——本路由是同一语义的
# HTTP 面：入口守卫、top_k 封顶、文本编码（encode_batch，纯文本、不需要 mmproj、**不拉起编码后端**
# ——只读面不为窗口搜索启动推理服务）、媒体库独立计分检索（media_vectors.search，D-13）、命中项路径/
# 描述查询时从图谱节点解析（D-23：节点缺失 ⇒ 空值 + degraded 可辨标记，命中不丢）。三面全部复用
# niu_api/internal 共享模块，不造第二套口径（I-1）。

_adapter = None
_adapter_lock = threading.Lock()


def _get_adapter():
    """Get or create the singleton LightRAGAdapter instance (thread-safe).

    与 kg_api._get_adapter 同型的单例模式（kg_api 既有先例）：只读消费 LightRAGAdapter 的
    get_entity_info（节点解析），不触碰图谱写入/嵌入路径。
    """
    global _adapter
    if _adapter is not None:
        return _adapter
    with _adapter_lock:
        if _adapter is not None:
            return _adapter
        from niu_api.internal.lightrag_adapter import LightRAGAdapter

        _adapter = LightRAGAdapter()
    return _adapter


def _resolve_media_node_fields(entity_name: str) -> tuple[str, str, bool]:
    """从图谱节点取路径与描述（与 search_media 工具的 _resolve_media_node_fields 同语义）。

    节点缺失 / LightRAG 不可用 / 异常 ⇒ ("", "", True)：命中项仍返回（向量与实体名的
    绑定不受影响），空值 + 可辨降级标记，不静默吞掉、也不整体报错（R17）。
    """
    try:
        adapter = _get_adapter()
        result = adapter.get_entity_info(entity_name)
        if result.get("status") == "ok":
            graph_data = result.get("data", {}).get("graph_data", {}) or {}
            file_path = str(graph_data.get("file_path", "") or "")
            description = str(graph_data.get("description", "") or "")
            if file_path or description:
                return file_path, description, False
            return "", "", True  # 节点在但无位置/描述字段：同样按降级处理（可辨）
        return "", "", True
    except Exception:
        return "", "", True


@router.get("/search")
def media_search(
    query: str = Query(default=""),
    top_k: str = Query(default="20"),
) -> dict:
    """图谱窗口搜索框的媒体检索面（只读；与 search_media 工具同口径，I-1）。

    守卫与报文**逐字同** search_media 工具：空/全空白 query、top_k 非正整数、min(top_k, 50)
    封顶、编码面/存储面异常各自转明确 status:error（宽捕获各包一段，I-6 同款纪律）、库空
    ⇒ {"status": "ok", "results": []}（不抛、不是失败）。

    top_k 声明为 str（不设 Query 数值约束）：FastAPI 对非法整型参数会回 422，其形状不是
    本端点的错误契约——本端点恒 200 + status 字段，与 renderer 的 body.status 判定协议一致
    （同 kg_api 三端点 E3 D7 先例：任何 HTTP 状态都被 apiRequest JSON.parse 后 resolve，
    前端只认 body.status）。
    """
    if not query or not str(query).strip():
        return {"status": "error", "message": "query 不能为空"}
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        return {"status": "error", "message": "top_k 必须是正整数"}
    if top_k < 1:
        return {"status": "error", "message": "top_k 必须是正整数"}
    top_k = min(top_k, 50)

    try:
        # 函数级 import：与 search_media 工具同一接缝（测试 patch niu_api.internal.llama_server 即生效）
        from niu_api.internal.llama_server import encode_batch

        vectors = encode_batch([query])
        if not vectors:
            raise RuntimeError("encode_batch 返回空列表：查询文本未产生向量")
        query_vector = vectors[0]
    except Exception as e:
        logger.warning(f"[MEDIA_SEARCH] Query encode failed: {e}")
        return {
            "status": "error",
            "message": f"媒体查询文本编码失败（{type(e).__name__}）: {e}",
        }

    from niu_api.internal import media_vectors

    try:
        hits = media_vectors.search(query_vector, top_k)
    except Exception as e:
        logger.warning(f"[MEDIA_SEARCH] Media store search failed: {e}")
        return {
            "status": "error",
            "message": f"媒体库检索失败（存储面：库文件可能损坏，需检查/修复存储，不是编码后端问题）: {e}",
        }

    results = []
    for hit in hits:
        item = {
            "entity_name": hit["entity_name"],
            "media_type": hit["media_type"],
            "score": hit["score"],
        }
        file_path, description, degraded = _resolve_media_node_fields(hit["entity_name"])
        item["file_path"] = file_path
        item["description"] = description
        if degraded:
            item["degraded"] = True  # 可辨降级标记（D-23）：命中不丢
        results.append(item)
    return {"status": "ok", "results": results}
