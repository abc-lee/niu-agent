"""ComputerSession —— 持久执行命名空间 + run(code) 骨架。

上游对应物：ComputerWorkerCore（worker.ts:416-753）。C1 只保留对象模型的运行时骨架：

- **持久命名空间**：窗口句柄 / 截图帧 / AX ref 跨 run 存活（上游 Scope 契约，
  prompts/tools/computer.md："persistent session; window handles, screenshot frames,
  AX refs survive calls"）——exec 进同一个 namespace dict；
- **run(code)**：执行方式 = 向持久命名空间 exec。最后一条表达式语句的值 = returnValue
  （上游 JsRuntime 语义，worker.ts:549-560 + computer.ts:195-201 stringifyReturnValue）；
  print() 捕获进输出通道（上游 onText hook，worker.ts:576-580）；
- **单活动 run**：并发 run → ToolError('Computer worker is busy')
（上游 worker.ts:467-471）。
C2 增补：`run(code, timeout=?)` run 预算（worker.ts:473-514 + :540 超时错误）；
per-run scope `wait`（run-scope.ts:336-359 移植，携带 run 预算）与 `display`
（print 别名，prelude.txt）注入命名空间。
C4 增补：`run(code, timeout=?, read_only=?)` per-run 只读闸门（上游 computer.ts
入参 read_only → RunContext.read_only → worker.ts:154-178 guardRun）；timeout
按 tool-timeouts.ts clamp [1, 300]；原生错误码附中文恢复句（errors.py，
docs/tools/computer.md "Errors and recovery"）。

会话本身不在此创建：`get_desktop_session()` 复用 vision-server 的 `_get_session()`
同进程单例（帧缓存 / AX ref 登记都在那个会话内，两套会话会把帧锚定拆开）。
"""

from __future__ import annotations

import ast
import io
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import annotate_error
from .objects import (
    ComputerToolError,
    Desktop,
    RunContext,
    _run_context_var,
    _stringify_return_value,
    get_desktop_session,
)


class _RunScopedStdout(io.TextIOBase):
    """sys.stdout 代理：run 线程内的 print → 捕获 buf；其他线程 → 透传真 stdout。

    C1 的 run() 同步单线程，进程级 sys.stdout 重定向无并发问题；C2 超时路径把
    exec 放进后台线程后，重定向会吞掉主线程同时期的 print（实测：超限 run 排空
    窗口内主线程输出丢失）。按线程分流——用户代码的 print 只发生在 run 线程。
    （上游 worker 是独立进程消息通道 onText，天然无此问题。）
    """

    def __init__(self, buf: io.StringIO, run_tid: int, real_stdout):
        self._buf = buf
        self._run_tid = run_tid
        self._real = real_stdout

    def write(self, s) -> int:
        if threading.get_ident() == self._run_tid:
            return self._buf.write(s)
        return self._real.write(s)

    def flush(self) -> None:
        self._buf.flush()
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass

    @property
    def encoding(self):
        return getattr(self._real, "encoding", None)


def _exec_code(code: str, namespace: Dict[str, Any]) -> Tuple[str, Any]:
    """在持久命名空间执行 code，返回 (stdout 文本, returnValue)。

    - 末尾的表达式语句单独 eval → returnValue（JS "最后表达式即返回值" 语义）；
    - 末尾的单目标赋值（`w = 42` / `w: int = 7`）同样产生 returnValue：上游 JS 里
      `w = …` 是 ExpressionStatement 会返回值，Python Assign 不是 Expr → exec 后按
      target 名取 namespace 值；多目标/解包赋值不处理（保持简单）；
    - print() 经 sys.stdout 重定向捕获（上游 onText hook 对应物）。
    """
    tree = ast.parse(code)
    return_expr: Optional[ast.expr] = None
    return_name: Optional[str] = None
    if tree.body:
        last = tree.body[-1]
        if isinstance(last, ast.Expr):
            return_expr = last.value
            tree.body.pop()
        else:
            targets = (last.targets if isinstance(last, ast.Assign)
                       else [last.target] if isinstance(last, ast.AnnAssign) else [])
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                return_name = targets[0].id
    module = ast.Module(body=tree.body, type_ignores=[])

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = _RunScopedStdout(buf, threading.get_ident(), old_stdout)
    try:
        exec(compile(module, "<computer-run>", "exec"), namespace)
        return_value: Any = None
        if return_expr is not None:
            return_value = eval(
                compile(ast.Expression(return_expr), "<computer-run>", "eval"), namespace)
        elif return_name is not None:
            return_value = namespace.get(return_name)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue(), return_value


def _make_wait(budget_ms: Optional[float]) -> Callable[..., Any]:
    """上游 run-scope.ts `waitForRun`（:336-359）+ `resolvePredicateTimeout`（:320-325）移植。

    - `wait(ms)`：睡 ms 毫秒（上游 :341-345）；受 run 预算上限约束——Python 线程不可
      kill，无上限 sleep 会持 busy 锁永久挂死会话（fail-loud 替代上游 untilAborted）。
    - `wait(predicate, timeout=?, interval=?)`：轮询至 truthy 并返回该值；超时抛
      ToolError（消息照抄 :358）。predicate 异常向上传播（:332 注释 "Predicate errors propagate"）。
    - predicate 超时解析（上游 computer worker 经 resolvePredicateTimeout，worker.ts:531）：
      budget_bound = max(1, run_budget_ms - 1000)（CELL_BUDGET_SLACK_MS，:301）；
      显式 timeout>0 → min(timeout, budget_bound)；timeout=0/inf → budget_bound；
      省略或垃圾值（负数/NaN/非数值）→ min(30_000, budget_bound)
      （DEFAULT_PREDICATE_TIMEOUT_MS，:304；上游对垃圾值回退默认）。无 run 预算 → inf。
    - interval：max(interval ?? 100, 10) ms（:355）。
    """
    if budget_ms is None:
        budget_bound = float("inf")
    else:
        budget_bound = max(1, int(budget_ms) - 1000)

    def wait(ms_or_fn: Any, timeout: Optional[float] = None,
             interval: Optional[float] = None) -> Any:
        if isinstance(ms_or_fn, (int, float)):
            # 受 run 预算上限约束（同谓词口径）：无上限 sleep 会持 busy 锁永久挂死会话
            time.sleep(min(float(ms_or_fn), budget_bound) / 1000.0)
            return None
        if not callable(ms_or_fn):
            raise ComputerToolError(
                "wait(...) expects milliseconds (number) or a predicate function to poll")
        if timeout == 0:
            eff_timeout = budget_bound
        elif not isinstance(timeout, (int, float)) or not (timeout > 0):
            # 省略或垃圾值（负数/NaN/非数值）→ 回退默认（上游 resolvePredicateTimeout :320-325）
            eff_timeout = min(30_000, budget_bound)
        elif timeout == float("inf"):
            eff_timeout = budget_bound
        else:
            eff_timeout = min(float(timeout), budget_bound)
        eff_interval = max(interval if interval is not None else 100, 10)
        deadline = time.monotonic() + eff_timeout / 1000.0
        while True:
            value = ms_or_fn()
            if value:
                return value
            if time.monotonic() + eff_interval / 1000.0 > deadline:
                raise ComputerToolError(
                    f"wait(predicate) timed out after {int(eff_timeout)}ms — "
                    "predicate never returned truthy")
            time.sleep(eff_interval / 1000.0)

    return wait


class ComputerSession:
    """一个持久 computer 会话（对应上游一个 worker）。"""

    def __init__(self):
        self._namespace: Optional[Dict[str, Any]] = None
        # run-scope 名的 pristine 对象（每 run 重注入，上游 setRunScope，runtime.ts:237-240）
        self._run_scope: Dict[str, Any] = {}
        self._lock = threading.Lock()
        # 活动 run 事实（线程, 启动时刻 monotonic, 预算秒）——busy 错误文本用；
        # 在 _run_sync 拿到锁之后写入，挂死 run 期间持续可查
        self._active_run: Optional[Tuple[threading.Thread, float, Optional[float]]] = None

    def _ensure_namespace(self) -> Dict[str, Any]:
        """上游 #ensureSession + #ensureRuntime（worker.ts:443-465）：首次 run 惰性构建。"""
        if self._namespace is not None:
            return self._namespace
        session = get_desktop_session()
        if session is None:
            raise ComputerToolError("desktop session unavailable (niu_natives missing or platform unsupported)")
        desktop_obj = Desktop(session)
        # display = print 别名（上游 JsRuntime prelude 短别名，eval/js/shared/prelude.txt——
        # 两者同走 run 文本输出通道；Python 里 print 经 stdout 重定向捕获）
        self._namespace = {"desktop": desktop_obj, "display": print}
        # pristine 副本：用户代码覆写 desktop/display 后，下一 run 重注入自愈
        self._run_scope = {"desktop": desktop_obj, "display": print}
        return self._namespace

    def _busy_message(self) -> str:
        """busy 错误文本带事实（fail-loud）：活动 run 已运行多长、其预算；显著超阈值
        （≥5× 该 run 预算且 ≥30s）→ 升级为"需重启"文案。Python 线程不可 kill，挂死 run
        会永久持锁——给模型/用户可行动的事实而非裸 busy（上游等价物是杀 worker 重建）。"""
        base = "Computer worker is busy"
        active = self._active_run
        if not active:
            return base
        _thread, started, budget_s = active
        elapsed = time.monotonic() - started
        msg = f"{base} — previous run has been running for {elapsed:.0f}s"
        if budget_s is not None:
            msg += f" (budget {int(budget_s)}s)"
            if elapsed >= max(30.0, 5.0 * budget_s):
                msg += "; previous run hung — session requires restart (Niu 重启后可恢复)"
        return msg

    def run(self, code: str, timeout: Optional[float] = None, read_only: bool = False) -> str:
        """执行一段代码并返回文本：stdout + 截图回执（按序）+ 字符串化 returnValue。

        timeout（秒）= run 预算。上游对应物：worker.ts:473-514 `AbortSignal.timeout(message.timeoutMs)`
        + onCancel → ToolError(`Computer code execution timed out after <ms>ms`，:540）。
        Python 线程不可 kill：超限时立即抛错（消息照抄上游），后台线程继续持有 busy 锁直到
        执行排空——期间后续调用得到 "Computer worker is busy"（保持单活动 run 不变式；
        上游等价物是杀 worker 后重建，这里保留共享原生会话故不重建）。
        timeout=None = 无预算（C1 行为，run(code) 原签名语义不变）；数值按上游
        tool-timeouts.ts computer {default:120, min:1, max:300} clamp（超出即 clamp，不报错）。

        read_only=True = inspection only：截图/枚举/AX 读/剪贴板读放行，一切输入与变更
        由 guard_run 抛 `read-only run: '<method>' requires read_only: false`
        （上游 computer.ts 入参 → worker.ts:154-178）。per-run 语义（上游 AsyncLocalStorage
        按 run 携带），不是会话级开关。

        原生错误（`{code}: {message}`）抛出前经 errors.annotate_error 附中文恢复句
        （docs/tools/computer.md "Errors and recovery"）；未识别码原文不变。
        """
        if timeout is not None:
            timeout = max(1.0, min(float(timeout), 300.0))
        if timeout is None:
            return self._run_sync(code, read_only=read_only)
        result: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                result["out"] = self._run_sync(
                    code, budget_ms=timeout * 1000.0, read_only=read_only)
            except BaseException as e:  # 原样回传调用方（含 ComputerToolError）
                result["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise ComputerToolError(
                f"Computer code execution timed out after {int(timeout * 1000)}ms")
        if "err" in result:
            raise result["err"]
        return result["out"]

    def _run_sync(self, code: str, budget_ms: Optional[float] = None,
                  read_only: bool = False) -> str:
        """执行一段代码并返回文本：stdout + 截图回执（按序）+ 字符串化 returnValue。"""
        if not self._lock.acquire(blocking=False):
            raise ComputerToolError(self._busy_message())
        try:
            # 活动 run 事实：拿到锁之后才写（挂死 run 持锁期间，后续 busy 报错读到的是它）
            self._active_run = (threading.current_thread(), time.monotonic(),
                                budget_ms / 1000.0 if budget_ms is not None else None)
            namespace = self._ensure_namespace()
            # 每 run 重注入 run-scope 名（上游 setRunScope，runtime.ts:237-240：每次 run 前
            # Object.assign(globalThis, {desktop, assert, wait})）——上一 run 覆写
            # desktop/display 后本 run 自愈；wait 是 per-run scope（worker.ts:524-536）携带预算
            namespace.update(self._run_scope)
            namespace["wait"] = _make_wait(budget_ms)
            context = RunContext(read_only=read_only)
            token = _run_context_var.set(context)
            try:
                stdout_text, return_value = _exec_code(code, namespace)
            except ComputerToolError as e:
                # 原生错误码附恢复句（errors.py）；无码前缀的 ToolError（busy/超时/
                # read-only 闸门/wait 超时）原文不变
                raise ComputerToolError(annotate_error(str(e))) from e
            finally:
                _run_context_var.reset(token)
        finally:
            self._lock.release()

        parts = []
        if stdout_text.strip():
            parts.append(stdout_text.rstrip("\n"))
        parts.extend(context.output)
        if return_value is not None:
            parts.append(_stringify_return_value(return_value))
        return "\n".join(parts)
