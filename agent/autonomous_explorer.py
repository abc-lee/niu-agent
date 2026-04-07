"""
Autonomous Explorer - 自主探索器

无任务时主动学习和改进。

核心概念（借鉴 GenericAgent autonomous_operation_sop）：
- 用户空闲超过阈值时触发自主反思
- 分析近期对话历史，识别失败模式
- 盘点已有 Skills，发现知识缺口
- 产出 TODO 列表，可选地自主执行简单任务

优势（超越 GenericAgent）：
- 复用现有的 scheduler 定时任务系统
- Skills 自动被向量库索引，无需手动同步
- 可选的简单任务自动执行（如整理记忆、更新配置）
"""

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable
from loguru import logger

from .experience_summarizer import get_experience_summarizer


class AutonomousExplorer:
    """
    自主探索器 - 无任务时主动学习和改进

    使用方式：
    1. 调用 start() 启动后台监控
    2. 每次用户交互时调用 record_activity()
    3. 空闲超过阈值时触发 reflect 模式
    """

    # 空闲阈值（秒）- 超过这个时间认为用户空闲
    DEFAULT_IDLE_THRESHOLD = 30 * 60  # 30 分钟

    # 反思间隔（秒）- 两次反思之间的最小间隔
    DEFAULT_REFLECT_INTERVAL = 60 * 60  # 1 小时

    def __init__(
        self,
        idle_threshold: int = None,
        reflect_interval: int = None,
        on_reflect_callback: Optional[Callable] = None
    ):
        self.idle_threshold = idle_threshold or self.DEFAULT_IDLE_THRESHOLD
        self.reflect_interval = reflect_interval or self.DEFAULT_REFLECT_INTERVAL
        self.on_reflect_callback = on_reflect_callback

        self._last_activity_time = time.time()
        self._last_reflect_time = 0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 反思统计
        self.total_reflects = 0
        self.last_reflect_result: Optional[str] = None

    def record_activity(self):
        """记录用户活动（每次用户交互时调用）"""
        with self._lock:
            self._last_activity_time = time.time()

    def start(self):
        """启动后台监控"""
        if self._thread and self._thread.is_alive():
            logger.warning("[AutonomousExplorer] Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"[AutonomousExplorer] Started (idle_threshold={self.idle_threshold}s, "
            f"reflect_interval={self.reflect_interval}s)"
        )

    def stop(self):
        """停止后台监控"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[AutonomousExplorer] Stopped")

    def _monitor_loop(self):
        """后台监控循环"""
        while not self._stop_event.is_set():
            # 检查是否应该反思
            if self._should_reflect():
                self._do_reflect()

            # 每分钟检查一次
            self._stop_event.wait(timeout=60)

    def _should_reflect(self) -> bool:
        """判断是否应该触发反思"""
        now = time.time()

        # 检查距离上次反思的间隔
        if now - self._last_reflect_time < self.reflect_interval:
            return False

        # 检查空闲时间
        idle_time = now - self._last_activity_time
        return idle_time >= self.idle_threshold

    def _do_reflect(self):
        """执行反思"""
        with self._lock:
            self._last_reflect_time = time.time()

        logger.info("[AutonomousExplorer] Starting reflect mode...")
        self.total_reflects += 1

        try:
            result = self._run_reflect()
            self.last_reflect_result = result
            logger.info(f"[AutonomousExplorer] Reflect complete: {result}")
        except Exception as e:
            logger.error(f"[AutonomousExplorer] Reflect failed: {e}")

    def _run_reflect(self) -> str:
        """
        运行反思

        Returns:
            反思结果摘要
        """
        steps = []

        # Step 1: 盘点已有 Skills
        skills_count = self._count_skills()
        steps.append(f"Skills: {skills_count} 个")

        # Step 2: 检查最近的记忆
        memory_stats = self._get_memory_stats()
        steps.append(f"记忆: {memory_stats}")

        # Step 3: 检查待处理经验
        experience_count = self._count_pending_experiences()
        steps.append(f"待总结经验: {experience_count} 个")

        # Step 4: 生成建议
        suggestions = self._generate_suggestions(skills_count, experience_count)
        steps.append(f"建议: {suggestions}")

        # 调用回调（如果设置）
        if self.on_reflect_callback:
            try:
                self.on_reflect_callback({
                    "skills_count": skills_count,
                    "memory_stats": memory_stats,
                    "experience_count": experience_count,
                    "suggestions": suggestions,
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as e:
                logger.error(f"[AutonomousExplorer] Callback failed: {e}")

        return "; ".join(steps)

    def _count_skills(self) -> int:
        """统计已有 Skills 数量"""
        try:
            skills_dir = Path(__file__).parent.parent.parent / "memory" / "skills"
            if skills_dir.exists():
                return len(list(skills_dir.glob("*.md")))
        except Exception as e:
            logger.warning(f"[AutonomousExplorer] Failed to count skills: {e}")
        return 0

    def _get_memory_stats(self) -> str:
        """获取记忆统计"""
        try:
            # 尝试从 vector_search 获取统计
            from .vector_search import get_vector_search
            vs = get_vector_search()
            stats = vs.get_memory_stats()
            if stats:
                return f"total={stats.get('total', 0)}"
        except Exception:
            pass
        return "unknown"

    def _count_pending_experiences(self) -> int:
        """统计待总结的经验数量"""
        # 检查 experience_summarizer 是否有未处理的上下文
        # 这个实现比较简单，后续可以增强
        return 0

    def _generate_suggestions(self, skills_count: int, experience_count: int) -> str:
        """生成改进建议"""
        suggestions = []

        if skills_count < 5:
            suggestions.append("建议增加更多 Skills 经验")

        if experience_count > 0:
            suggestions.append(f"有 {experience_count} 条经验待总结")

        if not suggestions:
            suggestions.append("系统运行良好")

        return ", ".join(suggestions)

    def force_reflect(self):
        """强制触发一次反思（用于测试或手动触发）"""
        logger.info("[AutonomousExplorer] Force reflect triggered")
        self._do_reflect()


# 全局实例
_explorer: Optional[AutonomousExplorer] = None


def get_autonomous_explorer() -> AutonomousExplorer:
    """获取全局 AutonomousExplorer 实例"""
    global _explorer
    if _explorer is None:
        _explorer = AutonomousExplorer()
    return _explorer


def start_autonomous_explorer():
    """启动全局自主探索器"""
    explorer = get_autonomous_explorer()
    explorer.start()
    return explorer


def record_activity():
    """记录活动（便捷函数）"""
    explorer = get_autonomous_explorer()
    explorer.record_activity()
