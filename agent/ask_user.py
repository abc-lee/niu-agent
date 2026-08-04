import threading

TERMINATED_SIGNAL = '__TERMINATED__'
_ASK_TIMEOUT = 600  # 10 分钟（比 ask_main_agent 的 300s 长，用户响应慢）


class AskUserFuture:
    """子 Agent 向用户提问的 future，阻塞等待用户回答。"""

    def __init__(self):
        self._event = threading.Event()
        self._answer = None

    def set_answer(self, answer: str):
        self._answer = answer
        self._event.set()

    def wait(self, timeout: float = _ASK_TIMEOUT) -> str | None:
        if self._event.wait(timeout=timeout):
            return self._answer
        return None  # 超时


class UserAskRegistry:
    """管理 unique_name → AskUserFuture。"""

    def __init__(self):
        self._futures: dict[str, AskUserFuture] = {}
        self._lock = threading.Lock()

    def register(self, unique_name: str) -> AskUserFuture:
        with self._lock:
            old = self._futures.get(unique_name)
            if old is not None:
                old.set_answer(TERMINATED_SIGNAL)
            future = AskUserFuture()
            self._futures[unique_name] = future
            return future

    def set_answer(self, unique_name: str, answer: str) -> bool:
        with self._lock:
            future = self._futures.pop(unique_name, None)
            if future is None:
                return False
            future.set_answer(answer)
            return True

    def cancel_pending_ask(self, unique_name: str):
        with self._lock:
            future = self._futures.pop(unique_name, None)
        if future is not None:
            future.set_answer(TERMINATED_SIGNAL)
        else:
            # future 不存在——设 _ask_user_terminated 标志防止后续 register 阻塞
            try:
                from agent.subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(unique_name)
                if instance is not None:
                    instance._ask_user_terminated = True
            except Exception:
                pass

    def unregister(self, unique_name: str):
        with self._lock:
            self._futures.pop(unique_name, None)

    def is_waiting(self, unique_name: str) -> bool:
        with self._lock:
            return unique_name in self._futures


_user_ask_registry = UserAskRegistry()


def get_user_ask_registry() -> UserAskRegistry:
    return _user_ask_registry
