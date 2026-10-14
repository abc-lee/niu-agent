"""媒体向量后台工作者（v3.0：队列 + FIFO 消费者；spec D-21/D-22/D-26）。

挂载点（D-22）：既有调度器 ``Scheduler._run_loop`` 的常驻循环**每轮**调一次 ``tick()``，
**在 ``check_and_trigger`` 之外**（它在无到期任务时提前返回、整段持 ``_check_lock``，
挂其内部会静默永不运行、队列一直涨 = R14 的症状）。零新轮询线程；工作者与调度器同生死
（调度器未起 ⇒ 工作者不起、队列只增不耗，R16）。

循环线程零阻塞契约（D-22 / spec §5.5）：
- 扫描/状态收敛在循环线程，毫秒级：条件链**按序求值、队列非空先判并短路**（v3.2 更正：
  队列空 = 常态 ⇒ 本轮在第一条件即结束，**不探活、不判就绪**——``media_ready()`` 内含
  同步 HTTP 探活（timeout=5），若排在「队列非空」之前，服务端死时每轮（10s）阻塞 5s）；
- 仅队列非空时才付**至多每轮一次、有界 5s** 的探活（旗标真而服务端死/卡时）——这是循环
  线程的**唯一**阻塞上界，如实披露（§5.5）；
- 编码 + 写库提交到调度器既有 ``ThreadPoolExecutor``（``bind_executor`` 绑定），循环线程
  **不等待结果**；同一时刻至多一条在途记录；每轮仅 ``done()`` 轮询收敛（成功 ⇒ 删行；
  删行失败 ⇒ ``stalled`` + 可辨 reason、下轮重试删行再收敛——第四轮 P2-1 修掉「假进度 + 永久冻结」的静默分支；
  失败 ⇒ 保留行 + 有界重试/弃单）；
- 在途记录带**开始时刻**（初值 = 提交时刻，``_process_one`` 开头回写为编码实际开始时刻）：
  超过 ``INFLIGHT_TIMEOUT`` 仍未 ``done()``——阈值**严格大于**媒体超时上限 ``llama_server._MEDIA_HTTP_TIMEOUT ≈ 1031.5s``
  且判定只计「编码已实际开始」的时段（池内排队段不计**入超时判定** ⇒ 池饱和时正常编码不被误判，R19）——判该条失败
  （原因可辨「在途超时」）并允许取下一条；放弃 = **不删行**（行留在队列，重放语义不变，R15）。
  编码**尚未开始**（池内排队）而排队时长已超阈值 ⇒ 不判失败、不删行，只把状态改 ``stalled`` +
  可辨 reason「线程池饱和，编码排队中」（有界上报、不显示假进度，R14/D-27）——记录保持在途，
  池腾出后 worker 自然开跑、收敛照常。
- 读队首是 **total 路径**（第二轮 P1-1 不变量）：队列文件非 UTF-8 字节（多字节字符撕裂 /
  外部写入者写非 UTF-8）/ 非 ``FileNotFoundError`` 的 ``OSError``（权限等）⇒ 本轮停取 + 报 ``stalled``
  + 醒目 ERROR，**任何异常不得离开 tick**——外抛会每 10s 一轮崩在同一队首行上、行永不删除、
  状态永不更新 ⇒ 媒体队列永久冻结（R14 症状）；``queue_depth`` 以 -1 哨兵记录「行数未知」（横条显示「积压未知」，绝不误报 0 行）。
tick 接缝：队列路径 / 开关读取 / 就绪判定 / 编码器 / 护栏 / 写库 / 提交策略全部可注入
（生产缺省全取真值），``Scheduler._run_loop`` 的调用与测试直调走**同一个函数**——测试用
临时队列文件 + 桩（同 v2.x ``NIU_MEDIA_STORE`` 接缝思路，spec D-22/AC-3/AC-7 的取据入口）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from agent.md_mirror import F4HeadCorruptError, f4_drop_prefix, f4_take_head  # S3 交付
from agent.md_mirror import MEDIA_QUEUE_PATH as F4_DEFAULT_QUEUE
from niu_api.internal import llama_server, media_downscale, media_vectors, pixel_guard
from niu_api.internal.media_ingest_config import media_ingest_enabled

#: 同一队列行的连续失败上限：达到即弃单（D-21：D-21 明示的边界，不算丢记录）；
#: 内存计数，重启清零；弃单原因可辨（ERROR 与 SSE 事件都带原因）。
MAX_CONSECUTIVE_FAILURES = 3
#: 在途超时阈值（初始值，S6 可同步收紧，判据见模块上方 docstring 与 plan S4b Interfaces 注）。
#: 必须严格大于 llama_server._MEDIA_HTTP_TIMEOUT ≈ 1031.5s。
INFLIGHT_TIMEOUT = 1200.0
#: 单轮 tick 内坏行连弃上限（防病态队列文件在一轮里烧掉循环线程）
_MAX_BADLINE_SWEEPS = 32


@dataclass
class _Inflight:
    """在途记录（同一时刻至多一条，D-22）。

    started_at = 编码实际开始时刻（初值 = 提交时刻，``_process_one`` 开头回写为 worker 线程
    真正开始执行的时刻）；started = 编码是否已实际开始（在途超时判定只对「编码已开始」的
    时段起算——池内排队段不计**入判定**，池饱和时正常编码不被误判，R19；排队本身超阈值
    由收敛分支按「编码排队中」上报可辨状态，见 ``_converge_inflight``）。
    """

    entity_name: str
    file_path: str
    started_at: float
    future: Any  # concurrent.futures.Future；测试可注入假 future（须有 done()/result()，不得为 None）
    started: bool = False

    def done(self) -> bool:
        # 严格语义：future 未赋值（记录先于 submit 创建、worker 可能已先行开跑）≠ 完成——
        # 若当成完成，收敛会把未提交的记录当成功删行（破坏至少一次语义）。
        return self.future is not None and self.future.done()


@dataclass
class TickEnv:
    """tick 接缝（D-22）：全部为 None ⇒ 生产缺省真值；测试逐项注入桩。"""

    queue_path: Optional[str] = None  # 队列文件（缺省 F4 默认路径；测试用临时文件）
    switch: Optional[Callable[[], bool]] = None  # 开关读取（缺省 media_ingest_enabled，每轮现读）
    ready_check: Optional[Callable[[], bool]] = None  # 实例就绪（缺省 llama_server.media_ready）
    encoder: Optional[Callable[[str], list]] = None  # 绝对路径 → 768 维向量（缺省 media_downscale.encode_media_any）
    guard: Optional[Callable[[str], tuple]] = None  # 路径 → (动作, 原因, 尺寸)；动作 ∈ "pass"|"downscale"|"reject"（缺省 pixel_guard.classify）
    store_add: Optional[Callable] = None  # (entity_name, media_type, vector)（缺省 media_vectors.add）
    submit: Optional[Callable] = None  # 零参回调 → Future（缺省调度器池；测试注入「同步执行、异常捕进假 future」版）


#: 调度器既有 ThreadPoolExecutor（start_scheduler 经 bind_executor 绑定一次；R19：与到期任务
#: 回调共享，延迟方向恒为「媒体等任务」——循环线程永不等待池内结果）。
_executor: Optional[Any] = None

_state_lock = threading.Lock()
#: 状态只报「最后一次 tick 观测到的事实」；状态端点纯读它、不付新探活（R14 的三种"未运行"
#: 原因由端点侧对调度器未起情形做覆盖，见 media_api）。
_state: dict[str, Any] = {
    "status": "idle",  # idle | encoding | stalled | off
    "reason": "",  # stalled/off 时的可辨原因（R14：三种"未运行"如实回报）
    "entity": None,  # 在途/最后处理条目的 entity_name
    "started_at": None,  # 在途记录开始时刻
    "queue_depth": 0,  # 队列积压行数（诊断用）
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


def _queue_depth(queue_path: Optional[str]) -> int:
    """队列积压行数（非空行计数）；文件缺失/空 ⇒ 0；文件存在但读不出（非 UTF-8/权限异常）⇒ -1 哨兵
    （行数未知——横条/端点按哨兵渲染「未知」，绝不误报「0 行」，第三轮 P3 合并修法）。仅诊断，绝不影响主流程。"""
    try:
        with open(queue_path or F4_DEFAULT_QUEUE, "r", encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())
    except FileNotFoundError:
        return 0
    except Exception:
        return -1  # 文件存在但读不出：行数未知（非 UTF-8 / 权限异常）——哨兵，不是 0


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
    """失败临时提示通道（D-27：按知识图谱错误的临时显示同模式——只推不落库、刷新即消失）。"""
    try:
        from niu_api.chat import notify_system_notice_sync

        notify_system_notice_sync(f"⚠️ {message}", "media_worker")
    except Exception:
        pass


# ---------- 在途收敛（循环线程，毫秒级） ----------

def _converge_inflight(env: TickEnv) -> None:
    """在途记录收敛（仅 done() 轮询，D-22）：在途超时 ⇒ 判失败；done ⇒ 成功删行 /
    删行失败 ⇒ stalled + 可辨 reason（下轮重试删行、再收敛，第四轮 P2-1）/
    失败保留行（有界重试/弃单）。循环线程不等待任何未完成的 future。"""
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
        # 但排队本身超阈值 ⇒ 按「积压未运行」上报可辨状态——不判失败、不删行、不清计数，记录保持在途，
        # 池腾出后 worker 自然开跑。无此分支时状态会永停 encoding、横条显示假进度「媒体入库：X」
        # 且后续队列行永不消费（第二轮 P2-1）。
        _set_state(status="stalled", reason=f"线程池饱和，编码排队中（已等 {time.time() - inflight.started_at:.0f}s，R19）", entity=inflight.entity_name, started_at=inflight.started_at, queue_depth=_queue_depth(env.queue_path))
        return
    if not inflight.done():
        # worker 真正开跑（started 由 False 转 True）后，把之前「编码排队中」的可辨状态改回 encoding——
        # 否则整段实际编码期间横条一直显示「编码排队中（已等 Ns）」，与事实相反（第三轮 P3-1 回归）。
        if inflight.started and get_state()["status"] != "encoding":
            _set_state(status="encoding", reason="", entity=inflight.entity_name, started_at=inflight.started_at, queue_depth=_queue_depth(env.queue_path))
        return  # 仍在途且未超时：留给下一轮（10s 后）
    try:
        if inflight.future is not None:
            inflight.future.result()  # 重抛异常 → 失败分支
    except Exception as e:
        _record_failure(inflight, f"处理失败（{type(e).__name__}）: {e}", env)
        return
    # 成功：记录完成后才删行（至少一次，D-21）；写库为同键覆盖 ⇒ 删行重试幂等
    if f4_drop_prefix(1, queue_path=env.queue_path):
        _inflight = None
        _fail_counts.pop(inflight.entity_name, None)
        logger.info(f"[MediaWorker] 编码完成，删行: {inflight.entity_name}")
        _set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": True, "message": "向量已入库"})
    else:
        # 删行失败（锁/IO/文件在编码窗口内被外部改成不可读或被删）：保留在途记录，下一轮再收敛——
        # 向量已落库（同键覆盖），下一轮删行成功后自然收敛，不破坏至少一次语义。
        # 但必须发布可辨状态（第四轮 P2-1）：不发布则状态永停 encoding（假进度，违 D-27/R14）、
        # 且 tick ② 此后每轮以 _inflight is not None 短路 ⇒ 队列永不再消费、无 stalled 上报——
        # 与同文件「读不出 ⇒ stalled」「弃单删不掉 ⇒ stalled」「护栏拒收删不掉 ⇒ stalled」口径一致。
        # 不可读文件下 _queue_depth 返 -1 哨兵 ⇒ 横条按「积压未知」渲染，绝不误报 0 行。
        logger.warning(f"[MediaWorker] 删行失败，下一轮再收敛: {inflight.entity_name}")
        _set_state(
            status="stalled",
            reason=f"编码完成但删行失败（行仍在队首，下轮重试；若持续出现需人工检查队列文件）: {inflight.entity_name}",
            entity=inflight.entity_name,
            started_at=None,
            queue_depth=_queue_depth(env.queue_path),
        )


def _record_failure(inflight: _Inflight, reason: str, env: TickEnv) -> None:
    """在途记录失败：未达上限 ⇒ 保留行、下轮重试（计数 +1，原因可辨）；
    达上限 ⇒ 弃单（删行 + 醒目 ERROR + SSE 失败事件；D-21 明示的边界，不算丢记录）。
    弃单**尊重 ``f4_drop_prefix`` 返回值**：删行失败 ⇒ 保留失败计数 + 报 stalled（下轮自然
    重试弃单），防「删行失败但计数被清零 ⇒ 再凑 3 次失败 ⇒ 再弃单又没删掉」的无限循环。"""
    global _inflight
    _inflight = None
    n = _fail_counts.get(inflight.entity_name, 0) + 1
    if n >= MAX_CONSECUTIVE_FAILURES:
        if f4_drop_prefix(1, queue_path=env.queue_path):
            _fail_counts.pop(inflight.entity_name, None)
            logger.error(f"[MediaWorker] 弃单（连续 {n} 次失败，原因可辨）: {inflight.entity_name}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"连续 {n} 次失败后弃单: {reason}"})
            _notify_failure(f"媒体向量后台入库连续 {n} 次失败已弃单：{inflight.entity_name}（{reason}）")
            _set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        else:
            _fail_counts[inflight.entity_name] = n
            logger.error(f"[MediaWorker] 弃单失败（删行失败，行仍在队列，下轮将重新编码并再次弃单）: {inflight.entity_name}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"弃单失败（下轮将重新编码并再次弃单）: {reason}"})
            _set_state(status="stalled", reason=f"弃单失败（删行失败，下轮将重新编码并再次弃单）：{reason}", entity=inflight.entity_name, started_at=None, queue_depth=_queue_depth(env.queue_path))
    else:
        _fail_counts[inflight.entity_name] = n
        logger.warning(f"[MediaWorker] 失败 {n}/{MAX_CONSECUTIVE_FAILURES}，保留行下轮再试: {inflight.entity_name}: {reason}")
        _broadcast({"type": "media-ingest-completed", "entity": inflight.entity_name, "ok": False, "message": f"失败（下轮自动重试）: {reason}"})
        _notify_failure(f"媒体向量后台入库失败（将自动重试，{n}/{MAX_CONSECUTIVE_FAILURES}）：{inflight.entity_name}（{reason}）")
        _set_state(status="stalled", reason=f"失败重试中（{n}/{MAX_CONSECUTIVE_FAILURES}）：{reason}", entity=inflight.entity_name, started_at=None, queue_depth=_queue_depth(env.queue_path))


# ---------- 单行处理（跑在调度器既有线程池的 worker 线程） ----------

def _process_one(env: TickEnv, record: dict, inflight: _Inflight) -> None:
    """池 worker 线程内执行：开头回写在途记录的 started/started_at（在途超时判定只计
    「编码实际开始」的时段，池内排队段不计——R19：池饱和时正常编码不被误判）；
    护栏已在循环线程放行（D-26 唯一关口）⇒ 编码 → 写库（纯索引三键）。
    任何异常（编码失败 / 文件缺失 / 写库失败）都传播进 future，由 tick 的收敛按原因归类。"""
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


def _read_head_or_sweep(env: TickEnv) -> tuple[str, Any]:
    """读队首，**total 函数**：返回 (kind, payload)，kind ∈ "head" | "empty" | "swept" | "unreadable"。
    - "head"：payload = 队首记录（tick 继续 ④–⑥）。
    - "empty"：无活可干（文件缺失/空/仅空行）⇒ tick 报 idle。
    - "swept"：单轮弃坏行上限（32）耗尽，本轮停取（刻意取舍：报 idle + 醒目 ERROR，见 S5 Interfaces 注）。
    - "unreadable"：队首坏行**删不掉**（``f4_drop_prefix`` 返回 0——排它锁竞争/IO 失败/撕裂尾行拒删，
      不把它当新坏行重复推进，防每轮烧 32 次整文件读写），或非 ``F4HeadCorruptError`` 的一切读异常
      （队列文件非 UTF-8 字节：多字节字符撕裂/外部写入者写 UTF-16 等；``os.open`` 抛 ``PermissionError``
      等非 ``FileNotFoundError`` 的 ``OSError``）⇒ 本轮停取，tick 报 stalled + 可辨 reason，**绝不外抛**
      （异常离开 tick 会每 10s 一轮崩在同一行上：行永不删除、状态永不更新、``_queue_depth`` 同文件也读不出
      ⇒ 端点与横条全报空闲 = 完全静默的永久冻结，第二轮 P1-1）。
    """
    swept = 0
    while True:
        try:
            head = f4_take_head(env.queue_path)
        except F4HeadCorruptError as e:
            if swept >= _MAX_BADLINE_SWEEPS:
                logger.error(f"[MediaWorker] 队首连续 {swept} 条坏行，本轮停取（需人工检查队列文件）: {e.reason}")
                return "swept", None
            logger.error(f"[MediaWorker] 醒目告警：弃队首坏行（{e.reason}）: {e.line[:120]!r}")
            if not f4_drop_prefix(1, queue_path=env.queue_path):
                logger.error(f"[MediaWorker] 队首坏行删不掉，本轮停取（需人工检查队列文件）: {e.reason}")
                return "unreadable", f"队首坏行删不掉（{e.reason}）"
            swept += 1
            continue
        except Exception as e:
            logger.error(f"[MediaWorker] 读队首异常，本轮停取（需人工检查队列文件）: {type(e).__name__}: {e}")
            return "unreadable", f"{type(e).__name__}: {e}"
        if head is None:
            return "empty", None
        return "head", head


# ---------- tick：一轮媒体队列事务 ----------

def tick(env: Optional[TickEnv] = None) -> None:
    """一轮媒体队列事务。由 ``Scheduler._run_loop`` 循环体每轮调用（``check_and_trigger``
    之外），测试直调（D-22 接缝）。

    循环线程代价：常态（队列空）在第一条件短路，毫秒级、零探活；队列非空时至多付一次
    有界 5s 的探活（§5.5 的唯一阻塞上界，如实披露）。编码永不发生在循环线程（提交池）。
    """
    global _inflight
    env = env or TickEnv()
    switch = env.switch or media_ingest_enabled
    ready_check = env.ready_check or llama_server.media_ready
    guard = env.guard or pixel_guard.classify

    # ① 开关关：只收敛在途（热翻转开→关：在途条完成并处理掉，不再取新行，AC-12），然后短路。
    # switch 自身是 total（media_ingest_enabled 对坏配置一律返 False）；保护性 try 是第二道防线
    # （第三轮 P2：被调方契约漂移/测试注入桩时也不外抛）——异常逃出会在 tick 第一步中止、状态永不更新：
    # 兜底 = 先收敛在途（不让在途记录冻结）再报可辨 stalled，本轮停取。
    try:
        enabled = switch()
    except Exception as e:
        logger.error(f"[MediaWorker] 开关读取异常，本轮停取: {type(e).__name__}: {e}")
        _converge_inflight(env)
        _set_state(status="stalled", reason=f"开关读取异常，本轮停取（需人工检查配置）：{type(e).__name__}: {e}", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return
    if not enabled:
        _converge_inflight(env)
        _set_state(status="off", reason="开关关（media_ingest.enabled=false）", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return

    # ② 有在途记录：仅 done() 收敛，不取新行（同一时刻至多一条在途，D-22）
    if _inflight is not None:
        _converge_inflight(env)
        return

    # ③ 队列非空先判并短路（v3.2 条件求值顺序）：常态到此为止——不探活、不判就绪。
    # 读队首是 total 路径：读不出（非 UTF-8/权限/坏行删不掉）⇒ 报 stalled + 可辨 reason，绝不外抛崩 tick。
    kind, head = _read_head_or_sweep(env)
    if kind == "unreadable":
        _set_state(status="stalled", reason=f"队列文件读不出，本轮停取（需人工检查）：{head}", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return
    if kind != "head":  # empty / swept：报 idle（swept 的醒目告警已在 _read_head_or_sweep 内打出，刻意取舍见 S5 Interfaces 注）
        _set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return

    # ④ 就绪检查（仅队列非空才走到）：media_ready = 旗标 ∧ 同步探活（有界 ≤5s，循环线程唯一阻塞上界）。
    # media_ready 是 total（旗标 ∧ _health_ok，探活异常内部吞成 False）；保护性 try 同 ①：异常逃出会把
    # 「已取队首」之后的分支全部截断、状态不更新 ⇒ 兜底按「未就绪」同族报可辨 stalled（reason 区分「就绪检查异常」）。
    try:
        ready = ready_check()
    except Exception as e:
        logger.error(f"[MediaWorker] 就绪检查异常，本轮停取: {type(e).__name__}: {e}")
        _set_state(status="stalled", reason=f"就绪检查异常，本轮停取（需人工检查编码后端）：{type(e).__name__}: {e}", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return
    if not ready:
        _set_state(status="stalled", reason="实例未加载 mmproj 或编码后端不可用（队列只增不耗）", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return

    # ⑤ 像素护栏（同步、毫秒级、D-26 唯一关口、total 函数）：拒收（病态：体积超预算）= 确定性
    # 失败，重试无意义 ⇒ 弃行 + WARNING；pass / downscale 都放行到 ⑥（downscale 的实际缩放
    # 发生在池 worker 的编码器分派里，循环线程不解码任何像素——I-11）。保护性 try 是第二道
    # 防线：动作按放行处理、绝不崩本轮 tick。
    try:
        action, reason, _dims = guard(head["file_path"])
    except Exception as e:
        logger.warning(f"[MediaWorker] 护栏调用异常，按放行处理（服务端 --image-max-tokens 是最后防线）: {e}")
        action, reason, _dims = "pass", "", None
    if action == "reject":
        try:
            dropped = f4_drop_prefix(1, queue_path=env.queue_path)
        except Exception as e:  # f4_drop_prefix 是结构性 total（整段 try/except + finally 释放锁），此 try 是双保险
            logger.warning(f"[MediaWorker] 护栏拒收删行异常，按删行失败处理: {e}")
            dropped = 0
        if dropped:
            _fail_counts.pop(head["entity_name"], None)
            logger.warning(f"[MediaWorker] 护栏病态拒收（未发送、不产生向量）: {head['file_path']}: {reason}")
            _broadcast({"type": "media-ingest-completed", "entity": head["entity_name"], "ok": False, "message": f"护栏拒收: {reason}"})
            _notify_failure(f"图片被护栏拒收（不产生向量，为保护检索面做的取舍）：{head['file_path']}（{reason}）")
            _set_state(status="idle", reason="", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        else:
            # 删行失败（锁/IO/撕裂尾行）：行仍在队首——保留失败计数（如有）、报 stalled、下轮重试拒收；
            # 清零 + 报 idle 会让同一行每轮重复触发「拒收 + 提示」。
            logger.warning(f"[MediaWorker] 护栏拒收但删行失败，下轮重试: {head['file_path']}: {reason}")
            _set_state(status="stalled", reason=f"护栏拒收但删行失败（下轮重试）：{reason}", entity=head["entity_name"], started_at=None, queue_depth=_queue_depth(env.queue_path))
        return
    # pass / downscale ⇒ 落入 ⑥ 提交池（downscale 的缩放在 _process_one 的分派器内执行）

    # ⑥ 提交池（循环线程不等结果）；在途记录**先创建**（future 暂未赋值）再 submit、回填 future——
    # 记录引用经 lambda 传入 worker 线程（其内回写 started/started_at）；done() 的严格语义保证
    # 「future 未赋值」绝不会被当成完成（见 _Inflight.done 的注释）。
    # submit 是 tick 内最后一个外部调用点（第三轮 P2）：池未绑定 / 已 shutdown / 线程创建失败都会抛
    # RuntimeError——一抛，在途记录不发布、状态不发布、下一轮同一行在同一处再抛 ⇒ 与 P1-B/P1-1 同族的
    # 永久冻结。保护性 try（与 ⑤ 同型）：异常 ⇒ 本轮停取 + 报可辨 stalled，绝不外抛崩 tick。
    submit = env.submit or _default_submit
    inflight = _Inflight(entity_name=head["entity_name"], file_path=head["file_path"], started_at=time.time(), future=None)
    try:
        inflight.future = submit(lambda: _process_one(env, head, inflight))
    except Exception as e:
        logger.error(f"[MediaWorker] 提交线程池失败，本轮停取（需检查调度器线程池绑定与状态）: {type(e).__name__}: {e}")
        _set_state(status="stalled", reason=f"媒体线程池不可提交，本轮停取：{type(e).__name__}: {e}", entity=None, started_at=None, queue_depth=_queue_depth(env.queue_path))
        return
    _inflight = inflight
    logger.info(f"[MediaWorker] 开始编码: {head['entity_name']}（队列深度 {_queue_depth(env.queue_path)}）")
    _set_state(status="encoding", reason="", entity=head["entity_name"], started_at=_inflight.started_at, queue_depth=_queue_depth(env.queue_path))
    _broadcast({"type": "media-ingest-started", "entity": head["entity_name"], "queue_depth": _queue_depth(env.queue_path)})


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
    行不删 ⇒ 下次启动重放（至少一次语义不变，R15）：向量可能已写库（同键覆盖幂等），也可能
    未写（行留队列）。
    边角情形（如实披露于手册）：编码后端是**外部实例**占用 19878 端口（非本进程子进程，
    中断无效）时，退出拖尾仍以编码自身超时为界。
    """
    global _inflight
    if _inflight is not None:
        elapsed = time.time() - _inflight.started_at
        logger.warning(
            f"[MediaWorker] 停止时存在在途编码，放弃（行留在队列，下次启动重放）: "
            f"entity={_inflight.entity_name}（已进行 {elapsed:.0f}s）"
        )
        _notify_failure(f"应用停止时在途编码被中断（下次启动将重放）：{_inflight.entity_name}")
        try:
            llama_server.stop()  # 终止编码子进程：在途 urlopen 即刻失败、池 worker 即刻返回（幂等，重复调用无副作用）
        except Exception as e:
            logger.warning(f"[MediaWorker] 停止时中断编码子进程失败（退出拖尾可能拖到编码自身超时）: {e}")
    _inflight = None
    _fail_counts.clear()
    _set_state(status="off", reason="调度器停止中", entity=None, started_at=None, queue_depth=_queue_depth(None))
