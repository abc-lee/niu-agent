"""HAWatcher 守护线程 — WebSocket 长连接 + subscribe_trigger + ChatQueue 推送。"""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_watcher = None
_init_lock = threading.Lock()
CONFIG_PATH = os.path.expanduser("~/.niu/ha-config.json")


def start_watcher():
    global _watcher
    with _init_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None
        w = _HAWatcher()
        w.start()
        _watcher = w


def stop_watcher():
    global _watcher
    with _init_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


def check_and_start():
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("ha_url") and config.get("ha_token"):
            start_watcher()
    except json.JSONDecodeError as e:
        print(f"[HAWatcher] 配置文件格式错误: {e}")
    except Exception as e:
        print(f"[HAWatcher] 启动失败: {e}")

def _is_restored_state_transition(trigger_data: dict) -> bool:
    if not isinstance(trigger_data, dict):
        return False
    if trigger_data.get("platform") != "state":
        return False
    from_state = trigger_data.get("from_state")
    if from_state is None:
        return True
    if not isinstance(from_state, dict):
        return False
    if "state" not in from_state:
        return False
    to_state = trigger_data.get("to_state")
    if not isinstance(to_state, dict):
        return False
    if "state" not in to_state:
        return False
    from_val = from_state.get("state")
    s = "" if from_val is None else str(from_val).strip().lower()
    if s == "" or s in ("unavailable", "unknown", "none"):
        return True
    to_val = to_state.get("state")
    if from_val == to_val:
        return True
    return False
def _normalize_triggers(raw) -> list:
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        eid = item.get("entity_id")
        if not isinstance(eid, str) or not eid.strip():
            continue
        out.append(item)
    return out

def _state_text(value) -> str:
    """把 State dict / 标量转成短文本；只取 .state，不展开 attributes。"""
    if isinstance(value, dict):
        value = value.get("state")
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    if value is None:
        return ""
    return " ".join(str(value).split())[:200]


def _format_trigger_context(trigger_data: dict, variables: dict) -> str:
    """把 HA trigger 的通用字段拼成给 Agent 的多行上下文。"""
    if not isinstance(trigger_data, dict):
        trigger_data = {}
    lines = []

    entity_id = _state_text(trigger_data.get("entity_id"))
    if entity_id:
        lines.append(f"实体: {entity_id}")

    platform = _state_text(trigger_data.get("platform") or trigger_data.get("trigger"))
    if platform:
        lines.append(f"触发类型: {platform}")

    from_val = _state_text(trigger_data.get("from_state"))
    to_val = _state_text(trigger_data.get("to_state"))
    if from_val or to_val:
        lines.append(f"状态变化: {from_val or '?'} -> {to_val or '?'}")

    extras = []
    if isinstance(variables, dict):
        for key, value in variables.items():
            if key == "trigger" or not isinstance(key, str):
                continue
            key_text = " ".join(key.split())
            text = _state_text(value)
            if key_text and text:
                extras.append(f"{key_text}={text}")
            if len(extras) >= 10:
                break
    if extras:
        lines.append("附加: " + ", ".join(extras))

    if not lines:
        return ""
    lines.append("时间: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)


class _HAWatcher:

    def __init__(self):
        self._thread = None
        self._running = False
        self._last_mtime = 0
        self._current_subscriptions = {}
        self._ignored_log_last = {}
        # 推送解耦（方案 §3.10）：单 worker 线程池（FIFO 保事件顺序）+ 无界队列。
        # 监听循环 recv 到事件后 submit 即返回——ping/配置检查/重连永不被推送阻塞。
        self._push_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-push")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            from niu_ha_server import _config_event
            _config_event.set()
        except ImportError:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        # 不等待在途推送（wait=False）：worker 内部有 LLM read_timeout 保底必收敛。
        # worker 非 daemon（concurrent.futures 语义），进程退出依赖 /api/shutdown 的
        # os._exit(0) 兜底（绕过 atexit join），不会因在途推送挂住退出流程
        self._push_pool.shutdown(wait=False)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._running:
                try:
                    result = loop.run_until_complete(self._connect_and_listen())
                    if result == "auth_failed":
                        print("[HAWatcher] HA 认证失败，等待配置变更...")
                        self._wait_for_config_change(timeout=300)
                except Exception as e:
                    print(f"[HAWatcher] 连接异常: {e}, 5秒后重连...")
                    time.sleep(5)
        finally:
            loop.close()

    async def _connect_and_listen(self):
        import websockets

        config = self._read_config()
        if not config or not config.get("ha_url") or not config.get("ha_token"):
            self._wait_for_config_change(timeout=30)
            return

        ha_url = config["ha_url"]
        ha_token = config.get("ha_token", "")
        triggers = _normalize_triggers(config.get("triggers", []))

        if not triggers:
            self._wait_for_config_change(timeout=30)
            return

        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected: {msg}")

            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                return "auth_failed"

            self._current_subscriptions = {}
            msg_id = 1
            for trigger in triggers:
                msg_id += 1
                trigger_config = self._build_trigger_config(trigger)
                await ws.send(json.dumps({
                    "id": msg_id,
                    "type": "subscribe_trigger",
                    "trigger": trigger_config,
                }))
                # 按匹配 id 读取响应，跳过中间消息（pong/event）
                while True:
                    result = json.loads(await ws.recv())
                    if result.get("id") == msg_id:
                        break
                    if result.get("type") == "event":
                        self._handle_trigger_event(result, triggers)
                if result.get("success"):
                    self._current_subscriptions[trigger["id"]] = msg_id
                else:
                    print(f"[HAWatcher] 订阅失败: {trigger.get('id')}: {result.get('error')}")

            # 订阅完成后记录当前 mtime，避免立即重连
            try:
                self._last_mtime = os.path.getmtime(CONFIG_PATH)
            except OSError:
                pass

            last_ping = time.time()
            while self._running:
                if self._check_config_changed():
                    return

                if time.time() - last_ping > 30:
                    msg_id += 1
                    await ws.send(json.dumps({"id": msg_id, "type": "ping"}))
                    last_ping = time.time()

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    if msg.get("type") == "event":
                        self._handle_trigger_event(msg, triggers)
                except TimeoutError:
                    continue

    def _build_trigger_config(self, trigger: dict) -> dict:
        condition = trigger.get("condition", "state_change")
        entity_id = trigger.get("entity_id", "")

        if condition == "state_change":
            config = {"platform": "state", "entity_id": entity_id}
            if "from_state" in trigger:
                config["from"] = trigger["from_state"]
            if "to_state" in trigger:
                config["to"] = trigger["to_state"]
        elif condition == "above":
            config = {"platform": "numeric_state", "entity_id": entity_id, "above": trigger.get("threshold", 0)}
        elif condition == "below":
            config = {"platform": "numeric_state", "entity_id": entity_id, "below": trigger.get("threshold", 0)}
        else:
            config = {"platform": "state", "entity_id": entity_id}

        # HA 只渲染 trigger 内部 variables，并把它合并到 event.variables 顶层；
        # 顶层 subscribe_trigger.variables 不会回到事件（HA TriggerInfo 仅用于 enabled 等）。
        variables = trigger.get("variables")
        if not isinstance(variables, dict):
            variables = None
        else:
            # 手改 ha-config.json 可能绕过 ha_subscribe 的校验；触发侧也必须剔除保留键与非标量。
            allowed = (str, int, float, bool)
            variables = {
                k: v for k, v in variables.items()
                if k != "trigger" and isinstance(v, allowed)
            }
        if variables:
            config["variables"] = variables
        return config

    def _handle_trigger_event(self, msg: dict, triggers: list):
        triggers = _normalize_triggers(triggers)
        if not isinstance(msg, dict):
            return
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        sub_id = msg.get("id")  # subscribe_trigger 返回的 msg_id

        # 通过 msg_id 查找 trigger_id，再查找描述
        trigger_id = None
        for tid, mid in self._current_subscriptions.items():
            if mid == sub_id:
                trigger_id = tid
                break

        matched_trigger = None
        description = ""
        for t in triggers:
            if not isinstance(t, dict):
                continue
            if t.get("id") == trigger_id:
                matched_trigger = t
                description = t.get("description", f"{t.get('entity_id', '')} 状态变化")
                break

        variables = event.get("variables")
        if not isinstance(variables, dict):
            variables = {}
        trigger_data = variables.get("trigger")
        if not isinstance(trigger_data, dict):
            trigger_data = {}

        if not description:
            entity_id = trigger_data.get("entity_id", "") if isinstance(trigger_data, dict) else ""
            description = f"{entity_id} 状态变化"

        bypass_guard = isinstance(matched_trigger, dict) and "from_state" in matched_trigger
        if (not bypass_guard) and _is_restored_state_transition(trigger_data):
            from_state = trigger_data.get("from_state") if isinstance(trigger_data, dict) else None
            to_state = trigger_data.get("to_state") if isinstance(trigger_data, dict) else None
            from_val = from_state.get("state") if isinstance(from_state, dict) else from_state
            to_val = to_state.get("state") if isinstance(to_state, dict) else to_state
            label = trigger_id
            if label is None:
                label = trigger_data.get("entity_id", "") if isinstance(trigger_data, dict) else ""
            if label is None:
                label = ""
            key = (label, from_val, to_val)
            now = time.time()
            last = self._ignored_log_last.get(key, 0)
            if now - last >= 60:
                self._ignored_log_last[key] = now
                print(f"[HAWatcher] 忽略非真实状态转换（重启/恢复回填）: {from_val} -> {to_val} ({label})")
            return

        context = _format_trigger_context(trigger_data, variables)
        message = f"{description}\n{context}" if context else description
        self._submit_push(message)

    def _push_to_chat(self, description: str):
        try:
            import asyncio

            from niu_api.chat import _main_loop
            from niu_api.chat_queue import get_chat_queue

            loop = _main_loop
            if loop is None or loop.is_closed():
                print(f"[HAWatcher] Main event loop not available, cannot push: {description}")
                return

            # 通过 ChatQueue 入队并等待 Agent 回复（timeout=None：排队到底——
            # 等待语义符合公理，worker 自身有 LLM read_timeout 保底必收敛）
            q = get_chat_queue()
            fut = asyncio.run_coroutine_threadsafe(
                q.enqueue_and_wait_with_future(  # 返回 (result, reply_future)——reply_future 是同一 asyncio.Future
                    content=f"[智能家居] {description}",
                    source="ha-watcher",
                    session_id="default",
                    timeout=None,
                ),
                loop,
            )
            agent_reply, reply_future = fut.result()  # 解包（外层不再设超时）

            if not agent_reply:
                print(f"[HAWatcher] Agent returned empty reply for: {description}")
                return

            # IM 通道推送
            try:
                from niu_api.channel import get_channel_router
                router = get_channel_router()
                if router.has_channel("im"):
                    # 读 chat_queue 挂到 reply_future 上的确定性标志（"本回合已由 chat_queue 终结卡片"）
                    if not getattr(reply_future, "_im_finalized", False):
                        # 无卡（chat_queue 未终结）→ 保持现状自推（保留 run_coroutine_threadsafe 桥接——同步线程函数）
                        push_future = asyncio.run_coroutine_threadsafe(
                            router.push(agent_reply, "im", ""),
                            loop,
                        )
                        push_future.result(timeout=30)
                    # else：有卡（chat_queue 已 send_sync 终结）→ watcher 不自推（防双投递）
            except Exception as e:
                print(f"[HAWatcher] IM push failed: {e}")

        except Exception as e:
            print(f"[HAWatcher] 推送失败: {e}")

    def _submit_push(self, description: str):
        """把一次推送提交到单 worker 线程池。worker 内 wrap try/except：
        单个推送的异常不得杀掉常驻 worker（否则后续事件全部滞留队列）。"""

        def _worker():
            try:
                self._push_to_chat(description)
            except Exception as e:  # noqa: BLE001 — 兜底隔离，保 worker 存活
                print(f"[HAWatcher] 推送 worker 异常: {e}")

        self._push_pool.submit(_worker)

    def _read_config(self) -> dict:
        try:
            if not os.path.exists(CONFIG_PATH):
                return {}
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[HAWatcher] ha-config.json 读取失败（静默降级为空配置）: {e}")
            return {}

    def _check_config_changed(self) -> bool:
        # 先检查 Event（并立即清除），避免 mtime + event 双重触发
        try:
            from niu_ha_server import _config_event
            if _config_event.is_set():
                _config_event.clear()
                return True
        except ImportError:
            pass

        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                return True
        except OSError:
            pass

        return False

    def _wait_for_config_change(self, timeout: float = 30):
        deadline = time.time() + timeout
        while self._running and time.time() < deadline:
            if self._check_config_changed():
                return
            time.sleep(2)
