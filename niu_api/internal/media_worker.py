"""媒体向量后台工作者（v3.5：队列 + 第一条未完成消费者 + 完成标注；spec D-21/D-22/D-27，
口径见 docs/superpowers/plans/2026-10-14-media-bar-queue-lifecycle.md §0）。

生命周期语义（v3.5 相对 v3.0 的变化，其余不变）：
- **标注完成不删行**（口径②）：三条「处置完成」路径——成功 / 有界重试用尽弃单 / 护栏拒收——
  都是把该行改写为带 ``done: true`` 的行（原键保留），文件里已完成行 + 未完成行并存 ⇒
  百分比 = 已完成/总条数 = 真进度（刷新/中途打开都准）。**清空队列文件的唯一触发方 = Chat 页面**
  （经 dismiss 端点的锁内复核，口径③/丁）；工作者永不清空文件。关窗 = 延后到下次打开
  （下次页面加载的初始轮询照旧读队列：有未完成行 ⇒ 继续显示；全完成 ⇒ 关条 + 清空文件），
  长期无人开页面 ⇒ 文件留着——有意设计，不是缺陷。
- **取件 = 第一条未完成行**（口径④-1，取代 v3.0 的「取队首」）：完成行留在文件里会排在未完成
  行之前，取队首会重复编码已完成行。行号契约依赖「同一时刻至多一条在途 + 单一消费者」
  （D-22：在途时 tick ② 短路，不扫描不取件；写入方只追加不改前缀 ⇒ 行号稳定）。
- **先成功后标注**（口径④-2/乙）：编码 → 写库（同键覆盖）→ 标注。崩溃落在「已写库、未标注」
  之间 ⇒ 行仍未标注 ⇒ 重启照旧重放（同键覆盖幂等，至少一次语义不破）；已标注行永不重放。
- **失败只作临时提示**（口径乙）：弃单/拒收的失败信息经临时系统提示通道发 Chat 页（不落库、
  刷新即消失），不留在文件里当状态 ⇒ 进度条不会被失败行卡死。重试进行中（未达上限）的行仍
  是未完成：状态如实报 stalled + 可辨原因，百分比不动 = 如实反映。

挂载点（D-22，不变）：既有调度器 ``Scheduler._run_loop`` 的常驻循环**每轮**调一次 ``tick()``，
**在 ``check_and_trigger`` 之外**（它在无到期任务时提前返回、整段持 ``_check_lock``，挂其内部
会静默永不运行、队列一直涨 = R14 的症状）。循环线程零阻塞契约、条件链按序短路（未完成行
非空先判）、仅未完成行非空才付**至多每轮一次、有界 5s** 的探活（循环线程的唯一阻塞上界，
如实披露）、编码提交调度器既有 ``ThreadPoolExecutor``（``bind_executor`` 绑定）且循环线程
**不等待结果**、同一时刻至多一条在途、在途超时阈值**严格大于** ``llama_config.media_http_timeout()``
且判定只计「编码已实际开始」的时段（池内排队段不计入判定，R19）、读队列是 **total 路径**
（读不出 ⇒ stalled + -1 哨兵，任何异常不得离开 tick）——全部沿用 v3.0/v3.2 已验收的条款。
tick 接缝：队列路径 / 开关 / 就绪判定 / 编码器 / 护栏 / 写库 / 提交策略全部可注入
（生产缺省全取真值），``Scheduler._run_loop`` 的调用与测试直调走**同一个函数**。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from agent.md_mirror import (  # v3.5 F4 原语换代（I-4）
    F4LineCorruptError,
    f4_drop_line,
    f4_mark_line_done,
    f4_take_first_pending,
)
from agent.md_mirror import MEDIA_QUEUE_PATH as F4_DEFAULT_QUEUE
from niu_api.internal import llama_server, media_downscale, media_vectors, pixel_guard
from niu_api.internal.media_ingest_config import media_ingest_enabled

#: 同一队列行的连续失败上限：达到即弃单并**标完成**（口径乙：弃单 = 「过了」）；
#: 内存计数，重启清零；弃单原因可辨（ERROR 与 SSE 事件与临时提示都带原因）。
MAX_CONSECUTIVE_FAILURES = 3
#: 在途超时阈值（初始值，S6 可同步收紧；必须严格大于 llama_config.media_http_timeout()
#: （缺省 1031.52s，可被用户配置覆盖）——配置抬高到 ≥ 本值时启动自检会告警，不阻断）。
INFLIGHT_TIMEOUT = 1200.0
#: 单轮 tick 内坏行连弃上限（防病态队列文件在一轮里烧掉循环线程；v3.5：坏行可在文件任意位置）
_MAX_BADLINE_SWEEPS = 32

def _startup_selfcheck() -> None:
    """启动自检（只告警、绝不抛、不阻断，模块导入时跑一次）：配置的媒体超时 ≥ 在途超时
    阈值 ⇒ 正常在途（编码尚未完成）也可能被在途收敛判成「超时」误伤——启动时明确告警，
    由用户决定调低 media_http_timeout 或接受风险。"""
    try:
        from niu_api.internal import llama_config  # 函数级导入，保持本模块导入轻量

        cfg = llama_config.media_http_timeout()
        if cfg >= INFLIGHT_TIMEOUT:
            logger.warning(
                f"[MediaWorker] 启动自检：配置的媒体超时 {cfg:.2f}s ≥ 在途超时阈值 "
                f"{INFLIGHT_TIMEOUT:.0f}s——编码尚未完成即可能在途收敛被判「在途超时」误判失败，"
                f"请调低 media_http_timeout 或抬高 INFLIGHT_TIMEOUT"
            )
    except Exception:  # noqa: BLE001 —— 自检绝不阻断启动（读配置意外也只丢告警）
        pass


_startup_selfcheck()


@dataclass
class _Inflight:
    """在途记录（同一时刻至多一条，D-22）。

    line_index = 该行在队列文件中的行号（v3.5：take 返回、标注按行号改写，I-4）；行号稳定
    契约见模块 docstring。started_at = 编码实际开始时刻（初值 = 提交时刻，``_process_one``
    开头回写为 worker 真正开始执行的时刻；在途超时判定只对「编码已开始」的时段起算，R19）。
    """

    entity_name: str
    file_path: str
    line_index: int
    started_at: float
    future: Any  # concurrent.futures.Future；测试可注入假 future（须有 done()/result()，不得为 None）
    started: bool = False

    def done(self) -> bool:
        # 严格语义：future 未赋值（记录先于 submit 创建、worker 可能已先行开跑）≠ 完成——
        # 若当成完成，收敛会把未提交的记录当成功标注（破坏先成功后标注，I-5）。
        return self.future is not None and self.future.done()


@dataclass
class TickEnv:
    """tick 接缝（D-22）：全部为 None ⇒ 生产缺省真值；测试逐项注入桩。"""

    queue_path: Optional[str] = None  # 队列文件（缺省 F4 默认路径；测试用临时文件）
    switch: Optional[Callable[[], bool]] = None  # 开关读取（缺省 media_ingest_enabled，每轮现读）
    ready_check: Optional[Callable[[], bool]] = None  # 实例就绪（缺省 llama_server.media_ready）
    vector_idle_seconds: Optional[Callable[[], float]] = None  # 交互向量闲置秒数（缺省 llama_server.vector_idle_seconds）
    text_encode_busy: Optional[Callable[[], bool]] = None      # 交互编码是否在途（缺省 llama_server.text_encode_busy）
    encoder: Optional[Callable[[str], list]] = None  # 绝对路径 → 768 维向量（缺省 media_downscale.encode_media_any）
    guard: Optional[Callable[[str], tuple]] = None  # 路径 → (动作, 原因, 尺寸)；动作 ∈ "pass"|"downscale"|"reject"（缺省 pixel_guard.classify）
    store_add: Optional[Callable] = None  # (entity_name, media_type, vector)（缺省 media_vectors.add）
    submit: Optional[Callable] = None  # 零参回调 → Future（缺省调度器池；测试注入「同步执行、异常捕进假 future」版）


#: 调度器既有 ThreadPoolExecutor（start_scheduler 经 bind_executor 绑定一次；R19：与到期任务
#: 回调共享，延迟方向恒为「媒体等任务」——循环线程永不等待池内结果）。
_executor: Optional[Any] = None

#: 媒体编码让路门（D-31；用户口径①②）：媒体编码只在「交互/前端向量闲置 ≥ 本值（秒）」
#: 时才启动**新**项。时钟只由文本/交互编码（encode_batch）驱动，媒体自身编码不计。
VECTOR_IDLE_GATE_SECONDS = 30.0

_state_lock = threading.Lock()
#: 状态只报「最后一次 tick 观测到的事实」；状态端点纯读它、不付新探活（R14 的三种"未运行"
#: 原因由端点侧对调度器未起情形做覆盖，见 media_api）。
#: v3.5 计数字段（I-8，口径④-4）：queue_depth = **未完成**行数；queue_total = 记录行总数；
#: queue_done = 已完成行数。文件存在但读不出 ⇒ 三字段同为 -1 哨兵（行数未知，绝不误报 0）。
_state: dict[str, Any] = {
    "status": "idle",  # idle | encoding | stalled | off
    "reason": "",  # stalled/off 时的可辨原因（R14：三种"未运行"如实回报）
    "entity": None,  # 在途/最后处理条目的 entity_name
    "started_at": None,  # 在途记录开始时刻
    "queue_depth": 0,  # 队列未完成行数（口径④-4：只数未完成）
    "queue_total": 0,  # 记录行总数（已完成 + 未完成；口径②：完成行留在文件里）
    "queue_done": 0,  # 已完成行数（带 done 标记的行）
}
_inflight: Optional[_Inflight] = None
_fail_counts: dict[str, int] = {}  # entity_name → 连续失败计数（内存态，重启清零）


def bind_executor(executor) -> None:
    """由 ``start_scheduler()`` 在构造 Scheduler 后调用一次：工作者把编码提交到调度器
    既有线程池（R19 的载体）。未绑定就 tick 属 R16 的降级形态（生产不会发生：
    tick 只由运行中的调度循环调用）。"""
    global _executor
    _executor = executor


# ---------- 对外读取 / SSE ----------

def get_state() -> dict:
    """纯读在进程状态（状态端点只报它，不付新探活，毫秒级）。"""
    with _state_lock:
        return dict(_state)


def _set_state(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def _queue_counts(queue_path: Optional[str]) -> tuple[int, int, int]:
    """单次全文读队列文件，返回 (未完成数, 总数, 已完成数)（v3.5，I-8：一个状态更新
    **绝不读两遍文件**）。文件缺失/空/仅空行 ⇒ (0, 0, 0)；文件存在但读不出（非 UTF-8/
    权限异常）⇒ (-1, -1, -1) 哨兵（行数未知——横条/端点按哨兵渲染「未知」，绝不误报 0）。
    撕裂尾行（文件尾无换行、写了一半）**不是完整记录**（与 ``md_mirror._f4_split_records``
    同边界：写入方可能正在写）⇒ 三计数都不数它——若数成未完成，一次崩溃落在 O_APPEND 写半
    之后会让同一 ``_state`` 快照「status==idle」与「queue_depth>0」自相矛盾、真进度永停
    (N-1)/N（第二轮 P2-2）。单行坏行（其余行可读）按未完成计入（它会阻塞进度直到工作者
    弃除，如实反映）。"""
    import json as _json

    try:
        with open(queue_path or F4_DEFAULT_QUEUE, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return 0, 0, 0
    except Exception:
        return -1, -1, -1  # 文件存在但读不出——哨兵，不是 0
    lines = content.split("\n")
    if lines:
        lines.pop()  # 末元素 = 换行伪影或撕裂尾行，二者都不是完整记录（与 md_mirror._f4_split_records 同边界）
    total = done = 0
    for line in lines:
        if not line.strip():
            continue  # 空行不是记录（D-24）
        total += 1
        try:
            rec = _json.loads(line)
        except (ValueError, TypeError):
            continue  # 坏行：未完成（将被工作者弃除）
        if isinstance(rec, dict) and rec.get("done") is True:
            done += 1
    return total - done, total, done


def _counts(queue_path: Optional[str]) -> dict[str, int]:
    """``_queue_counts`` → ``_set_state`` 的字段字典（全部状态发布点统一走它，防漏字段）。"""
    depth, total, done = _queue_counts(queue_path)
    return {"queue_depth": depth, "queue_total": total, "queue_done": done}


def _broadcast(event: dict) -> None:
    """经既有 SSE 总线推给所有已连接客户端（同型先例：kg_api._notify_ingest_started）。
    主循环未就绪/已关闭 ⇒ 事件丢失可接受——轮询端点始终是真相（D-27）。"""
    try:
        from niu_api.chat import _main_loop, _sync_broadcast

        loop = _main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except Exception:
        pass


def _notify_failure(message: str) -> None:
    """失败临时提示通道（口径乙/D-27：按知识图谱错误的临时显示同模式——只推不落库、刷新即消失）。
    弃单/拒收/在途中断的失败信息只走这里进 Chat 页，**不写进队列文件当状态**（I-6）。"""
    try:
        from niu_api.chat import notify_system_notice_sync

        notify_system_notice_sync(f"⚠️ {message}", "media_worker")
    except Exception:
        pass


# ---------- 在途收敛（循环线程，毫秒级） ----------

def _converge_inflight(env: TickEnv) -> None:
    """在途记录收敛（仅 done() 轮询，D-22）：在途超时 ⇒ 判失败；done ⇒ 成功**标注完成** /
    标注失败 ⇒ stalled + 可辨 reason（下轮重试标注再收敛，第四轮 P2-1 同型分支）/ 失败保留
    行（有界重试/弃单）。循环线程不等待任何未完成的 future。"""
    global _inflight
    inflight = _inflight
    if inflight is None:
        return
    if inflight.started and not inflight.done() and time.time() - inflight.started_at > INFLIGHT_TIMEOUT:
        # 在途超时（v3.2）：阈值严格大于媒体超时上限、且只计「编码已实际开始」的时段 ⇒ 正常
        # 在途不会走到这里；走到这里 = 编码已开始却永不完成（编码假死 / future 永不完成），判失败放行下一条。
        _record_failure(
            inflight,
            f"在途超时（编码已进行 {time.time() - inflight.started_at:.0f}s > 阈值 {INFLIGHT_TIMEOUT:.0f}s，future 既未完成也未可放弃）",
            env,
        )
        return
    if not inflight.started and time.time() - inflight.started_at > INFLIGHT_TIMEOUT:
        # 编码从未开始（线程池被占满，R19 池饱和形态）：排队段不计入超时**判定**（防正常编码被误判），
        # 但排队本身超阈值 ⇒ 按「积压未运行」上报可辨状态——不判失败、不标注、不清计数，记录保持在途，
        # 池腾出后 worker 自然开跑。无此分支时状态会永停 encoding、横条显示假进度（第二轮 P2-1）。
        _set_state(status="stalled", reason=f"线程池饱和，编码排队中（已等 {time.time() - inflight.started_at:.0f}s，R19）", entity=inflight.entity_name, started_at=inflight.started_at, **_counts(env.queue_path))
        return
    if not inflight.done():
        # worker 真正开跑（started 由 False 转 True）后，把之前「编码排队中」的可辨状态改回 encoding——
        # 否则整段实际编码期间横条一直显示「编码排队中（已等 Ns）」，与事实相反（第三轮 P3-1 回归）。
        if inflight.started and get_state()["status"] != "encoding":
            _set_state(status="encoding", reason="", entity=inflight.entity_name, started_at=inflight.started_at, **_counts(env.queue_path))
        return  # 仍在途且未超时：留给下一轮（10s 后）
    try:
        if inflight.future is not None:
            inflight.future.result()  # 重抛异常 → 失败分支
    except Exception as e:
        # F-4/D-1 复合判据（文件缺失形态）：显式 FileNotFoundError，或收敛时文件已不在（TOCTOU：
        # 入口判过存在、编码时文件被删 ⇒ 抛的是 RuntimeError("图片文件不存在") 而非 FileNotFoundError）
        # ⇒ 判「文件已不在」（missing，仅供文案区分）。2026-10-17 用户定稿：可重试的失败类别
        # **只有**媒体编码后端（llama-server）故障——`MediaEncodeBackendError` 且文件仍在（missing 仍优先判一次性：
        # 文件都没了，重试也只会再失败）；其余异常（白名单外 / 护栏拒收等非后端 RuntimeError、
        # 其他异常）⇒ 一次处置（标完成 + WARNING + 不写重试计数）。
        missing = isinstance(e, FileNotFoundError) or not os.path.exists(inflight.file_path)
        retryable = isinstance(e, llama_server.MediaEncodeBackendError) and not missing
        _record_failure(inflight, f"处理失败（{type(e).__name__}）: {e}", env, retryable=retryable)
        return
    # 成功：向量已写库（同键覆盖），现在标注完成（先成功后标注，I-5）；标注缺失形态按页面删文件收敛
    k = f4_mark_line_done(inflight.line_index, queue_path=env.queue_path,
                          expect=(inflight.entity_name, inflight.file_path))
    if k in ("marked", "missing"):
        # "missing" = Chat 页面已观察到全部完成并删了文件（I-3/口径丁 的竞态形态：页面删文件
        # 恰撞上本行标注窗口）⇒ 该行随文件一起消失，按收敛处理，不误报 stalled。
        _inflight = None
        _fail_counts.pop(inflight.entity_name, None)
        logger.info(f"[MediaWorker] 编码完成（{k}）: {inflight.entity_name}")
        _set_state(status="idle", reason="", entity=None, started_at=None, **_counts(env.queue_path))
        _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": True, "message": "向量已入库"})
    else:
        # 标注失败（锁/IO/内容护栏不符/文件在编码窗口内被外部改坏）：保留在途记录，下一轮再收敛——
        # 向量已落库（同键覆盖），下一轮标注成功后自然收敛，不破坏至少一次语义（I-5）。
        # 但必须发布可辨状态（第四轮 P2-1）：不发布则状态永停 encoding（假进度，违 D-27/R14）、
        # 且 tick ② 此后每轮以 _inflight is not None 短路 ⇒ 队列永不再消费、无 stalled 上报。
        logger.warning(f"[MediaWorker] 标注完成失败，下一轮再收敛: {inflight.entity_name}")
        _set_state(
            status="stalled",
            reason=f"编码完成但标注失败（行仍未标注，下轮重试；若持续出现需人工检查队列文件）: {inflight.entity_name}",
            entity=inflight.entity_name,
            started_at=None,
            **_counts(env.queue_path),
        )


def _record_failure(inflight: _Inflight, reason: str, env: TickEnv, retryable: bool = True) -> None:
    """在途记录失败。``retryable=False``（F-4/D-1 复合判据判出文件已不在——显式 ``FileNotFoundError``
    或收敛时文件已不在，含 TOCTOU；2026-10-17 起另含**非后端**异常——可重试类别只有
    ``MediaEncodeBackendError``，其余一律一次处置）⇒ **不可重试**：标注完成（行保留）+ 清旧失败计数
    （如有）+ 如实临时提示（**不含**「将自动重试」「连续 n 次失败」；文件确实不在时文案含「文件不存在」，
    否则为通用「跳过媒体向量（无需重试）」）+ **WARNING** 日志，全程**不写** ``_fail_counts``
    （不占重试预算）；标注返 "failed" ⇒ 发布 stalled + 可辨 reason（仅与弃单失败分支同款之形、不写计数，
    下轮重新取件再标注）。
    缺省 ``retryable=True`` ⇒ 既有路径：未达上限 ⇒ 行保持未完成、下轮重试（计数 +1，原因可辨，
    I-6：重试期百分比不动 = 如实反映，横条按 stalled + 原因渲染）；达上限 ⇒ 弃单 = **标完成** +
    醒目 ERROR + SSE 失败事件 + 临时提示（口径乙：弃单也算「过了」，失败信息只作临时提示、不留在
    文件里当状态）。标注**尊重返回值**：missing（文件被页面删）⇒ 按弃单收敛；failed ⇒ 保留失败
    计数 + 报 stalled（下轮自然重试弃单），防「标注失败但计数被清零 ⇒ 再凑 3 次失败 ⇒ 再弃单又
    没标注」的无限循环。"""
    global _inflight
    _inflight = None
    if not retryable:
        # 一次处置（retryable=False）：标完成（行保留）、清旧计数，全程不写 _fail_counts
        # （重试预算只留给真可重试的后端故障）；文案按文件是否仍在缺失区分两种措辞。
        file_gone = not os.path.exists(inflight.file_path)
        try:
            k = f4_mark_line_done(inflight.line_index, queue_path=env.queue_path,
                                  expect=(inflight.entity_name, inflight.file_path))
        except Exception as e:  # f4_mark_line_done 是结构性 total（整段 try/except + finally 释放锁），此 try 是双保险（同 ⑥ 护栏拒收分支）
            logger.warning(f"[MediaWorker] 文件不存在路径的标注异常，按标注失败处理: {e}")
            k = "failed"
        if k in ("marked", "missing"):
            _fail_counts.pop(inflight.entity_name, None)
            if file_gone:
                logger.warning(f"[MediaWorker] 文件不存在（可能入库后被删除）：标完成、跳过媒体向量，无需重试: {inflight.entity_name}: {reason}")
                _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"文件不存在，跳过媒体向量（无需重试）: {reason}"})
                _notify_failure(f"照片文件不存在（入库后被删除），跳过媒体向量，无需重试：{inflight.entity_name}（{reason}）")
            else:
                logger.warning(f"[MediaWorker] 跳过媒体向量（无需重试）: {inflight.entity_name}: {reason}")
                _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"跳过媒体向量（无需重试）: {reason}"})
                _notify_failure(f"跳过媒体向量（无需重试）：{inflight.entity_name}（{reason}）")
            _set_state(status="idle", reason="", entity=None, started_at=None, **_counts(env.queue_path))
        else:
            # 标注失败（行仍未标注）：仅发布 stalled + 可辨 reason（与弃单失败分支同款之形，
            # 不写 _fail_counts 计数）——下轮重新取件、再处置、再标注（文件仍缺失 ⇒ 仍走本分支）。
            # 清旧失败计数（如有）：残留的旧计数会让该实体下次真可重试的失败从旧值起步、
            # 重试预算误判（与上面 marked/missing 分支同语义，审查 P2-2）。
            _fail_counts.pop(inflight.entity_name, None)
            if file_gone:
                logger.warning(f"[MediaWorker] 文件不存在但行标注完成失败（下轮再标注）: {inflight.entity_name}: {reason}")
                _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"文件不存在，跳过媒体向量（行标注失败，下轮再标注）: {reason}"})
                _set_state(status="stalled", reason=f"文件不存在（入库后被删除），行标注完成失败（下轮重新取件再标注）：{reason}", entity=inflight.entity_name, started_at=None, **_counts(env.queue_path))
            else:
                logger.warning(f"[MediaWorker] 行标注完成失败（下轮重新取件再标注）: {inflight.entity_name}: {reason}")
                _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"跳过媒体向量（行标注失败，下轮再标注）: {reason}"})
                _set_state(status="stalled", reason=f"跳过媒体向量（无需重试），行标注完成失败（下轮重新取件再标注）：{reason}", entity=inflight.entity_name, started_at=None, **_counts(env.queue_path))
        return
    n = _fail_counts.get(inflight.entity_name, 0) + 1
    if n >= MAX_CONSECUTIVE_FAILURES:
        k = f4_mark_line_done(inflight.line_index, queue_path=env.queue_path,
                              expect=(inflight.entity_name, inflight.file_path))
        if k in ("marked", "missing"):
            _fail_counts.pop(inflight.entity_name, None)
            logger.error(f"[MediaWorker] 弃单（连续 {n} 次失败，原因可辨，行已标完成不重放）: {inflight.entity_name}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"连续 {n} 次失败后弃单: {reason}"})
            _notify_failure(f"媒体向量后台入库连续 {n} 次失败已弃单（完成比已计入，不影响其余行）：{inflight.entity_name}（{reason}）")
            _set_state(status="idle", reason="", entity=None, started_at=None, **_counts(env.queue_path))
        else:
            _fail_counts[inflight.entity_name] = n
            logger.error(f"[MediaWorker] 弃单失败（标注失败，行仍未标注，下轮将重新编码并再次弃单）: {inflight.entity_name}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"弃单失败（下轮将重新编码并再次弃单）: {reason}"})
            _set_state(status="stalled", reason=f"弃单失败（标注失败，下轮将重新编码并再次弃单）：{reason}", entity=inflight.entity_name, started_at=None, **_counts(env.queue_path))
    else:
        _fail_counts[inflight.entity_name] = n
        logger.warning(f"[MediaWorker] 失败 {n}/{MAX_CONSECUTIVE_FAILURES}，行保持未完成下轮再试: {inflight.entity_name}: {reason}")
        _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"失败（下轮自动重试）: {reason}"})
        _notify_failure(f"媒体向量后台入库失败（将自动重试，{n}/{MAX_CONSECUTIVE_FAILURES}）：{inflight.entity_name}（{reason}）")
        _set_state(status="stalled", reason=f"失败重试中（{n}/{MAX_CONSECUTIVE_FAILURES}）：{reason}", entity=inflight.entity_name, started_at=None, **_counts(env.queue_path))


# ---------- 单行处理（跑在调度器既有线程池的 worker 线程） ----------

def _process_one(env: TickEnv, record: dict, inflight: _Inflight) -> None:
    """池 worker 线程内执行：开头回写在途记录的 started/started_at（在途超时判定只计
    「编码实际开始」的时段，池内排队段不计——R19：池饱和时正常编码不被误判）；
    护栏已在循环线程放行（D-26 唯一关口）⇒ 编码 → 写库（纯索引三键，同键覆盖）。
    **写库在前、标注在后**（I-5）：标注由循环线程的收敛完成；任何异常（编码失败 / 文件缺失 /
    写库失败）都传播进 future，由 tick 的收敛按原因归类。"""
    inflight.started = True
    inflight.started_at = time.time()
    encoder = env.encoder or media_downscale.encode_media_any
    store_add = env.store_add or (lambda en, mt, vec: media_vectors.add(en, mt, vec))
    vector = encoder(record["file_path"])  # 文件缺失 ⇒ FileNotFoundError（D-24 的显式失败原因）
    store_add(record["entity_name"], "image", vector)


def _default_submit(fn: Callable[[], None]):
    if _executor is None:
        raise RuntimeError("媒体工作者线程池未绑定（调度器未起）——R16 的降级形态")
    return _executor.submit(fn)


def _read_first_pending_or_sweep(env: TickEnv) -> tuple[str, Any]:
    """取「第一条未完成记录」，**total 函数**：返回 (kind, payload)，kind ∈ "head" | "empty"
    | "swept" | "unreadable"。
    - "head"：payload = (行号, 记录)（tick 继续 ④–⑦）。
    - "empty"：无活可干（文件缺失/空/仅空行与已完成行）⇒ tick 报 idle（完成行不是「活」，I-2/I-3）。
    - "swept"：单轮弃坏行上限（32）耗尽，本轮停取（刻意取舍：报 idle + 醒目 ERROR，v3.0 原条款）。
    - "unreadable"：遇到的坏行**弃不掉**（``f4_drop_line`` 返回 "failed"——排它锁竞争/IO 失败/
      越界，不把它当新坏行重复推进，防每轮烧 32 次整文件读写），或非 ``F4LineCorruptError``
      的一切读异常（队列文件非 UTF-8 字节：多字节字符撕裂/外部写入者写非 UTF-8；``os.open`` 抛
      ``PermissionError`` 等非 ``FileNotFoundError`` 的 ``OSError``）⇒ 本轮停取，tick 报 stalled
      + 可辨 reason，**绝不外抛**（异常离开 tick 会每 10s 一轮崩在同一行上：行永不处置、状态
      永不更新、计数同为哨兵 ⇒ 端点与横条全报空闲 = 完全静默的永久冻结，R14 症状）。
    坏行可在文件**任意位置**（v3.5：完成行留文件后，坏行可能夹在中间）——按行号弃除，与位置无关。
    """
    swept = 0
    while True:
        try:
            head = f4_take_first_pending(env.queue_path)
        except F4LineCorruptError as e:
            if swept >= _MAX_BADLINE_SWEEPS:
                logger.error(f"[MediaWorker] 单轮连弃 {swept} 条坏行达上限，本轮停取（需人工检查队列文件）: {e.reason}")
                return "swept", None
            logger.error(f"[MediaWorker] 醒目告警：弃第 {e.line_index} 行坏行（{e.reason}）: {e.line[:120]!r}")
            k = f4_drop_line(e.line_index, queue_path=env.queue_path)
            if k == "failed":
                logger.error(f"[MediaWorker] 第 {e.line_index} 行坏行弃不掉，本轮停取（需人工检查队列文件）: {e.reason}")
                return "unreadable", f"坏行（第 {e.line_index} 行）弃不掉（{e.reason}）"
            swept += 1  # "dropped" / "missing" 都算处置掉：继续扫描下一行
            continue
        except Exception as e:
            logger.error(f"[MediaWorker] 读队列异常，本轮停取（需人工检查队列文件）: {type(e).__name__}: {e}")
            return "unreadable", f"{type(e).__name__}: {e}"
        if head[0] is None:
            return "empty", None
        return "head", head


# ---------- tick：一轮媒体队列事务 ----------

def tick(env: Optional[TickEnv] = None) -> None:
    """一轮媒体队列事务。由 ``Scheduler._run_loop`` 循环体每轮调用（``check_and_trigger``
    之外），测试直调（D-22 接缝）。

    循环线程代价：常态（无未完成行——含「只剩完成行」形态）在第一条件短路，毫秒级、零探活；
    有未完成行时至多付一次有界 5s 的探活（唯一阻塞上界，如实披露）。编码永不发生在循环线程
    （提交池）。条件链①–⑦与 v3.0 同构，仅 ③ 取件与 ⑦ 在途记录构造按 v3.5 语义
    （第一条未完成 + 行号）。
    """
    global _inflight
    env = env or TickEnv()
    switch = env.switch or media_ingest_enabled
    ready_check = env.ready_check or llama_server.media_ready
    guard = env.guard or pixel_guard.classify

    # ① 开关关：只收敛在途（热翻转开→关：在途条完成并处置掉，不再取新行，AC-12），然后短路。
    # switch 自身是 total（media_ingest_enabled 对坏配置一律返 False）；保护性 try 是第二道防线
    # （被调方契约漂移/测试注入桩时也不外抛）——异常逃出会在 tick 第一步中止、状态永不更新：
    # 兜底 = 先收敛在途（不让在途记录冻结）再报可辨 stalled，本轮停取。
    try:
        enabled = switch()
    except Exception as e:
        logger.error(f"[MediaWorker] 开关读取异常，本轮停取: {type(e).__name__}: {e}")
        _converge_inflight(env)
        _set_state(status="stalled", reason=f"开关读取异常，本轮停取（需人工检查配置）：{type(e).__name__}: {e}", entity=None, started_at=None, **_counts(env.queue_path))
        return
    if not enabled:
        _converge_inflight(env)
        _set_state(status="off", reason="开关关（media_ingest.enabled=false）", entity=None, started_at=None, **_counts(env.queue_path))
        return

    # ② 有在途记录：仅 done() 收敛，不取新行（同一时刻至多一条在途，D-22）
    if _inflight is not None:
        _converge_inflight(env)
        return

    # ③ 未完成行先判并短路（v3.2 条件求值顺序）：常态到此为止——不探活、不判就绪。
    # 取件是 total 路径：读不出（非 UTF-8/权限/坏行弃不掉）⇒ 报 stalled + 可辨 reason，绝不外抛。
    kind, head = _read_first_pending_or_sweep(env)
    if kind == "unreadable":
        _set_state(status="stalled", reason=f"队列文件读不出，本轮停取（需人工检查）：{head}", entity=None, started_at=None, **_counts(env.queue_path))
        return
    if kind != "head":  # empty / swept：报 idle（swept 的醒目告警已在 _read_first_pending_or_sweep 内打出，刻意取舍见 v3.0 S5 Interfaces 注）
        _set_state(status="idle", reason="", entity=None, started_at=None, **_counts(env.queue_path))
        return
    line_index, rec = head

    # ④ 就绪检查（仅有未完成行才走到）：media_ready = 旗标 ∧ 同步探活（有界 ≤5s，循环线程唯一阻塞上界）。
    # media_ready 是 total（旗标 ∧ _health_ok，探活异常内部吞成 False）；保护性 try 同 ①：异常逃出会把
    # 「已取第一条未完成」之后的分支全部截断、状态不更新 ⇒ 兜底按「未就绪」同族报可辨 stalled（reason 区分「就绪检查异常」）。
    try:
        ready = ready_check()
    except Exception as e:
        logger.error(f"[MediaWorker] 就绪检查异常，本轮停取: {type(e).__name__}: {e}")
        _set_state(status="stalled", reason=f"就绪检查异常，本轮停取（需人工检查编码后端）：{type(e).__name__}: {e}", entity=None, started_at=None, **_counts(env.queue_path))
        return
    if not ready:
        _set_state(status="stalled", reason="实例未加载 mmproj 或编码后端不可用（队列只增不耗）", entity=None, started_at=None, **_counts(env.queue_path))
        return

    # ⑤ 向量闲置门（D-31，让路；有意放在就绪检查之后——后端未加载 mmproj/进程已死时照旧由 ④
    #    报 stalled，fail-loud 诊断不被等待态掩盖，审查 R1-A P1-1）。两条件全真才允许启动新项：
    #    (a) 交互向量闲置 ≥ VECTOR_IDLE_GATE_SECONDS——时钟**只由文本/交互编码**打点驱动，
    #        媒体自身编码从不打点（否则连编自我推迟，N 张白等 N×30s；用户口径①）；
    #    (b) 交互编码**不在途**（含排队等锁段）——不与前端并排抢 _encode_lock（用户口径②）。
    #    （无「1 秒缓冲」：官方架构确认单槽请求按到达顺序串行 = FIFO ⇒ 前端已排队的请求天然
    #     排在我们前面；用户口径「FIFO 就不需要等那一秒」。）
    #    任一条不满足 ⇒ 本轮不启动、如实报 idle（前端既有 else 分支渲染「队列积压 N 行，等待
    #    开始」；reason 仅诊断用、前端不渲染），下一拍再查。探针异常/非数值按「不可开」保守
    #    处理，绝不外抛崩 tick（与 ①④⑥⑦ 同型的保护性 try）。
    #    门只管**启动新编码项**：取件/sweep/维护照常（③ 取件在门之前执行，坏行弃除不受门影响）。
    idle_probe = env.vector_idle_seconds or llama_server.vector_idle_seconds
    busy_probe = env.text_encode_busy or llama_server.text_encode_busy
    try:
        # float() 强转也在 try 内：桩/契约漂移返回 None 等非数值时走保守分支，而不是让
        # `None < 30.0` 抛 TypeError 崩掉本轮 tick。
        idle_seconds = float(idle_probe())
        busy_val = busy_probe()
        if not isinstance(busy_val, bool):
            raise TypeError(f"text_encode_busy 探针返回非 bool：{type(busy_val).__name__}（{busy_val!r}）")
        text_busy = busy_val
    except Exception as e:
        logger.warning(f"[MediaWorker] 向量活跃探针异常，本轮停取: {type(e).__name__}: {e}")
        idle_seconds, text_busy = 0.0, True
    if idle_seconds < VECTOR_IDLE_GATE_SECONDS or text_busy:
        gate_detail = "交互编码在途" if text_busy else f"交互向量活跃（闲置 {idle_seconds:.0f}s < {VECTOR_IDLE_GATE_SECONDS:.0f}s）"
        _set_state(
            status="idle",
            reason=f"{gate_detail}，媒体编码让路等待",
            entity=None, started_at=None, **_counts(env.queue_path),
        )
        return

    # ⑥ 像素护栏（同步、毫秒级、D-26 唯一关口、total 函数）：拒收（病态：体积超预算）= 确定性
    # 失败、重试无意义 ⇒ **标完成**（口径乙：拒收也算「过了」）+ 醒目 WARNING + 临时提示；pass /
    # downscale 都放行到 ⑦（downscale 的实际缩放在池 worker 的编码器分派里，循环线程不解码任何
    # 像素——I-11）。保护性 try 是第二道防线：动作按放行处理、绝不崩本轮 tick。
    try:
        action, reason, _dims = guard(rec["file_path"])
    except Exception as e:
        logger.warning(f"[MediaWorker] 护栏调用异常，按放行处理（服务端 --image-max-tokens 是最后防线）: {e}")
        action, reason, _dims = "pass", "", None
    if action == "reject":
        try:
            k = f4_mark_line_done(line_index, queue_path=env.queue_path, expect=(rec["entity_name"], rec["file_path"]))
        except Exception as e:  # f4_mark_line_done 是结构性 total（整段 try/except + finally 释放锁），此 try 是双保险
            logger.warning(f"[MediaWorker] 护栏拒收标注异常，按标注失败处理: {e}")
            k = "failed"
        if k in ("marked", "missing"):
            _fail_counts.pop(rec["entity_name"], None)
            logger.warning(f"[MediaWorker] 护栏病态拒收（未发送、不产生向量，行已标完成）: {rec['file_path']}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": rec["entity_name"], "ok": False, "message": f"护栏拒收: {reason}"})
            _notify_failure(f"图片被护栏拒收（不产生向量，为保护检索面做的取舍）：{rec['file_path']}（{reason}）")
            _set_state(status="idle", reason="", entity=None, started_at=None, **_counts(env.queue_path))
        else:
            # 标注失败（锁/IO）：行仍未标注——保留失败计数（如有）、报 stalled、下轮重试拒收；
            # 清零 + 报 idle 会让同一行每轮重复触发「拒收 + 提示」。
            logger.warning(f"[MediaWorker] 护栏拒收但标注失败，下轮重试: {rec['file_path']}: {reason}")
            _set_state(status="stalled", reason=f"护栏拒收但标注失败（下轮重试）：{reason}", entity=rec["entity_name"], started_at=None, **_counts(env.queue_path))
        return
    # pass / downscale ⇒ 落入 ⑦ 提交池（downscale 的缩放在 _process_one 的分派器内执行）

    # ⑦ 提交池（循环线程不等结果）；在途记录**先创建**（带行号，future 暂未赋值）再 submit、
    # 回填 future——记录引用经 lambda 传入 worker 线程（其内回写 started/started_at）；done() 的
    # 严格语义保证「future 未赋值」绝不会被当成完成。
    # submit 是 tick 内最后一个外部调用点（第三轮 P2）：池未绑定 / 已 shutdown / 线程创建失败都会抛
    # RuntimeError——一抛，在途记录不发布、状态不发布、下一轮同一行在同一处再抛 ⇒ 与 P1-B/P1-1 同族的
    # 永久冻结。保护性 try（与 ⑥ 同型）：异常 ⇒ 本轮停取 + 报可辨 stalled，绝不外抛崩 tick。
    submit = env.submit or _default_submit
    inflight = _Inflight(entity_name=rec["entity_name"], file_path=rec["file_path"], line_index=line_index, started_at=time.time(), future=None)
    try:
        inflight.future = submit(lambda: _process_one(env, rec, inflight))
    except Exception as e:
        logger.error(f"[MediaWorker] 提交线程池失败，本轮停取（需检查调度器线程池绑定与状态）: {type(e).__name__}: {e}")
        _set_state(status="stalled", reason=f"媒体线程池不可提交，本轮停取：{type(e).__name__}: {e}", entity=None, started_at=None, **_counts(env.queue_path))
        return
    _inflight = inflight
    # tick ⑦：一个状态更新只读一遍文件（I-8）——日志 / 状态发布 / SSE 广播共用同一 `_counts` 快照
    _c6 = _counts(env.queue_path)
    logger.info(f"[MediaWorker] 开始编码: {rec['entity_name']}（未完成 {_c6['queue_depth']} 行 / 总 {_c6['queue_total']}）")
    _set_state(status="encoding", reason="", entity=rec["entity_name"], started_at=_inflight.started_at, **_c6)
    _broadcast({"type": "media-ingest-started", "entity": rec["entity_name"], "queue_depth": _c6["queue_depth"]})


# ---------- 停机钩子（R20） ----------

def on_stop() -> None:
    """``Scheduler.stop()`` 的停止钩子（R20）：让在途编码的退出拖尾**有界**。

    放弃 = 主动中断在途编码：调 ``llama_server.stop()`` 终止编码子进程 ⇒ 在途 HTTP 即刻失败 ⇒
    池 worker 线程即刻返回 ⇒ 解释器的线程池 join（先于普通 atexit 执行——2026-10-13 本机探针
    实证：60s 在途任务下进程挂到超时被杀，普通 atexit 处理器一行未跑，依赖 atexit 注册的
    子进程关停切不断在途 HTTP）迅速完成 ⇒ 进程退出**不被拖到编码自身超时（≈17 分钟）**。
    在途**解码段**（超门槛大图的客户端全图解码，跑在池 worker 进程内）**不被杀子进程打断**——解码跑完才在
    健康检查处失败 ⇒ 停止时若在解码大图，退出拖尾以解码剩余时长为界（大图可达数十秒级）；在途**发送段**
    仍被杀子进程即刻打断（原语义不变）。
    行**未标注** ⇒ 下次启动重放（至少一次语义不变，口径乙/I-5：行仍未完成 ⇒ 照旧重放；向量可能
    已写库（同键覆盖幂等），也可能未写（行留队列））。
    边角情形（如实披露于手册）：编码后端是**外部实例**占用 19878 端口（非本进程子进程，
    中断无效）时，退出拖尾仍以编码自身超时为界。
    """
    global _inflight
    if _inflight is not None:
        elapsed = time.time() - _inflight.started_at
        logger.warning(
            f"[MediaWorker] 停止时存在在途编码，放弃（行未标注，下次启动重放）: "
            f"entity={_inflight.entity_name}（已进行 {elapsed:.0f}s）"
        )
        _notify_failure(f"应用停止时在途编码被中断（下次启动将重放）：{_inflight.entity_name}")
        try:
            llama_server.stop()  # 终止编码子进程：在途 urlopen 即刻失败、池 worker 即刻返回（幂等，重复调用无副作用）
        except Exception as e:
            logger.warning(f"[MediaWorker] 停止时中断编码子进程失败（退出拖尾可能拖到编码自身超时）: {e}")
    _inflight = None
    _fail_counts.clear()
    _set_state(status="off", reason="调度器停止中", entity=None, started_at=None, **_counts(None))
