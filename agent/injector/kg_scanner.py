"""
KG Scanner — 已禁用

LightRAG 内部通过 ainsert() 自动提取实体，无需单独的 KGScanner 扫描。
此模块保留仅为兼容旧代码引用，所有功能已迁移到 LightRAG。

迁移路径：
- KGScanner.scan_and_extract() → LightRAG ainsert() 自动提取
- entity-extractor 子 Agent → 不再需要（LightRAG 内置实体提取）
- KuzuDB pending 状态 → 不再适用（LightRAG 无 pending 概念）
"""

from loguru import logger

logger.info("[KGScanner] Disabled — entity extraction is now handled by LightRAG ainsert()")

# 全局实例设为 None，兼容旧代码引用
_kg_scanner = None


def get_kg_scanner(*args, **kwargs):
    """兼容旧接口，返回 None。"""
    logger.warning("[KGScanner] get_kg_scanner() called but KGScanner is disabled (LightRAG handles entity extraction)")
    return None


class KGScanner:
    """禁用的 KGScanner — 所有方法为空操作。"""

    def __init__(self, *args, **kwargs):
        logger.warning("[KGScanner] KGScanner is disabled. Use LightRAG ainsert() for entity extraction.")

    def start(self, *args, **kwargs):
        pass

    def stop(self, *args, **kwargs):
        pass

    def scan_and_extract(self, *args, **kwargs):
        logger.warning("[KGScanner] scan_and_extract() is disabled. Use LightRAG ainsert().")
        return []
