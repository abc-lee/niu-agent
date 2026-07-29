"""
KG Sync — LightRAG 版本

将 photos.db 和 vectors.db 中的数据同步到 LightRAG 知识图谱。
替代旧版 KuzuDB 同步逻辑。

主要功能：
1. 照片文档同步 → ainsert() (LightRAG 自动提取实体)
2. 人物实体同步 → inject_entity() / inject_relation()
3. 向量库文档同步 → ainsert()

注意：此模块保留以兼容旧代码引用 (get_kg_sync)，
实际后台同步由 lightrag_sync.py 处理。
"""

import threading

from loguru import logger


class KGSync:
    """LightRAG-based knowledge graph sync.

    Provides both on-demand and background sync capabilities.
    Background sync is delegated to LightRAGSync (lightrag_sync.py).
    """

    def __init__(self, sync_interval: int = 21600):
        self.sync_interval = sync_interval
        self._lightrag_sync = None

    def run_full_sync(self) -> dict:
        """Run one full sync cycle using LightRAGSync singleton."""
        from agent.injector.lightrag_sync import get_lightrag_sync

        syncer = get_lightrag_sync(self.sync_interval)
        return syncer.run_sync()

    def start_background_sync(self):
        """Start the LightRAG background sync thread."""
        from agent.injector.lightrag_sync import get_lightrag_sync

        self._lightrag_sync = get_lightrag_sync(self.sync_interval, auto_start=True)
        logger.info("[KGSync] Delegated to LightRAGSync background thread")

    def stop_background_sync(self):
        """Stop the LightRAG background sync thread."""
        if self._lightrag_sync:
            self._lightrag_sync.stop_background_sync()
            logger.info("[KGSync] LightRAGSync stopped")


# Global instance + thread-safe lock
_kg_sync: KGSync | None = None
_kg_sync_lock = threading.Lock()


def get_kg_sync(sync_interval: int = 21600) -> KGSync:
    """Get the global KGSync instance (LightRAG-backed)."""
    global _kg_sync
    with _kg_sync_lock:
        if _kg_sync is None:
            _kg_sync = KGSync(sync_interval)
    return _kg_sync
