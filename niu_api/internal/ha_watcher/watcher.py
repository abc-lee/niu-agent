"""HAWatcher 守护线程 — WebSocket 长连接 + subscribe_trigger + ChatQueue 推送。"""

import asyncio
import json
import os
import threading
import time

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
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        if config.get("ha_url") and config.get("ha_token"):
            start_watcher()
    except json.JSONDecodeError as e:
        print(f"[HAWatcher] 配置文件格式错误: {e}")
    except Exception as e:
        print(f"[HAWatcher] 启动失败: {e}")


class _HAWatcher:
    def __init__(self):
        self._thread = None
        self._running = False
        self._last_mtime = 0
        self._current_subscriptions = {}

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
        triggers = config.get("triggers", [])

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
            return config
        elif condition == "above":
            return {"platform": "numeric_state", "entity_id": entity_id, "above": trigger.get("threshold", 0)}
        elif condition == "below":
            return {"platform": "numeric_state", "entity_id": entity_id, "below": trigger.get("threshold", 0)}
        return {"platform": "state", "entity_id": entity_id}

    def _handle_trigger_event(self, msg: dict, triggers: list):
        event = msg.get("event", {})
        sub_id = msg.get("id")  # subscribe_trigger 返回的 msg_id

        # 通过 msg_id 查找 trigger_id，再查找描述
        trigger_id = None
        for tid, mid in self._current_subscriptions.items():
            if mid == sub_id:
                trigger_id = tid
                break

        description = ""
        for t in triggers:
            if t.get("id") == trigger_id:
                description = t.get("description", f"{t.get('entity_id', '')} 状态变化")
                break

        if not description:
            trigger_data = event.get("variables", {}).get("trigger", {})
            entity_id = trigger_data.get("entity_id", "")
            description = f"{entity_id} 状态变化"

        self._push_to_chat(description)

    def _push_to_chat(self, description: str):
        try:
            import asyncio

            from niu_api.chat import _main_loop
            from niu_api.chat_queue import get_chat_queue

            loop = _main_loop
            if loop is None or loop.is_closed():
                print(f"[HAWatcher] Main event loop not available, cannot push: {description}")
                return

            # 通过 ChatQueue 入队并等待 Agent 回复
            q = get_chat_queue()
            future = asyncio.run_coroutine_threadsafe(
                q.enqueue_and_wait(
                    content=f"[智能家居] {description}",
                    source="ha-watcher",
                    session_id="default",
                ),
                loop,
            )
            agent_reply = future.result(timeout=300)

            if not agent_reply:
                print(f"[HAWatcher] Agent returned empty reply for: {description}")
                return

            # IM 通道推送
            try:
                from niu_api.channel import get_channel_router
                router = get_channel_router()
                if router.has_channel("im"):
                    push_future = asyncio.run_coroutine_threadsafe(
                        router.push(agent_reply, "im", ""),
                        loop,
                    )
                    push_future.result(timeout=30)
            except Exception as e:
                print(f"[HAWatcher] IM push failed: {e}")

        except Exception as e:
            print(f"[HAWatcher] 推送失败: {e}")

    def _read_config(self) -> dict:
        try:
            if not os.path.exists(CONFIG_PATH):
                return {}
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
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
