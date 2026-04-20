"""
KG Scanner

知识图谱待处理项扫描服务。定时扫描 KG 中 entity_status='pending' 的 Document，
放入内存队列，串行启动 entity-extractor 子 Agent 处理。
"""

import queue
import threading
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger


class KGScanner:
    """
    KG 待处理项扫描服务

    扫描 KG 中 entity_status='pending' 的 Document 节点，
    放入内存队列，串行启动 entity-extractor 子 Agent。
    """

    PROCESSING_TIMEOUT_MINUTES = 10
    MAX_RETRY_COUNT = 3
    QUEUE_MAX_SIZE = 100
    SCAN_INTERVAL = 60  # 秒

    def __init__(self, scan_interval: int = 60):
        self.scan_interval = scan_interval
        self._queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX_SIZE)
        self._stop_event = threading.Event()
        self._scan_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._processing = False  # 是否正在处理
        self._db = None  # KuzuDB Database (shared, thread-safe)
        self._conn = None  # KGScanner's own Connection

    def start(self):
        """启动扫描和处理线程"""
        if self._scan_thread and self._scan_thread.is_alive():
            return

        self._stop_event.clear()

        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()

        self._process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._process_thread.start()

        logger.info(f"[KGScanner] Started (scan_interval: {self.scan_interval}s)")

    def stop(self):
        """停止扫描和处理线程"""
        self._stop_event.set()
        if self._scan_thread:
            self._scan_thread.join(timeout=5)
        if self._process_thread:
            self._process_thread.join(timeout=5)
        self._conn = None
        self._db = None
        logger.info("[KGScanner] Stopped")

    def _get_connection(self):
        """获取 KGScanner 专属的 KuzuDB 连接（与主线程隔离）"""
        if self._conn is None:
            from niu_kg_server import get_db_path, _init_schema
            import kuzu
            db_path = get_db_path()
            self._db = kuzu.Database(str(db_path))
            self._conn = kuzu.Connection(self._db)
            # Schema 已由主线程初始化，不需要再调用 _init_schema
        return self._conn

    def _scan_loop(self):
        """扫描循环（异常不会终止线程）"""
        while not self._stop_event.is_set():
            try:
                self._scan_and_enqueue()
            except Exception as e:
                logger.error(f"[KGScanner] Scan failed: {e}", exc_info=True)
            self._stop_event.wait(self.scan_interval)

    def _scan_and_enqueue(self):
        """扫描 KG 中待处理项，放入队列"""
        pending_docs = self._query_pending_docs()
        if not pending_docs:
            return

        # 去重：检查队列中已有的 URI
        existing_uris = set()
        for item in list(self._queue.queue):
            if isinstance(item, dict) and "uri" in item:
                existing_uris.add(item["uri"])

        enqueued = 0
        for doc in pending_docs:
            if doc["uri"] in existing_uris:
                continue
            try:
                self._queue.put_nowait(doc)
                existing_uris.add(doc["uri"])
                enqueued += 1
            except queue.Full:
                logger.warning("[KGScanner] Queue full, skipping pending docs")
                break

        if enqueued > 0:
            logger.info(f"[KGScanner] Enqueued {enqueued} pending docs")

    def _query_pending_docs(self) -> list[dict]:
        """查询 KG 中待处理的 Document 节点"""
        try:
            conn = self._get_connection()
            now = datetime.now().isoformat()
            timeout_cutoff = (datetime.now() - timedelta(minutes=self.PROCESSING_TIMEOUT_MINUTES)).isoformat()

            # 1. pending 文档
            result = conn.execute(
                "MATCH (d:Document) WHERE d.entity_status = 'pending' RETURN d.uri, d.title, d.content, d.source LIMIT 20"
            )
            pending = [self._row_to_dict(row, "pending") for row in result]

            # 2. processing 超时
            result = conn.execute(
                "MATCH (d:Document) WHERE d.entity_status = 'processing' AND d.processing_at < $cutoff RETURN d.uri, d.title, d.content, d.source LIMIT 10",
                {"cutoff": timeout_cutoff},
            )
            timed_out = [self._row_to_dict(row, "pending") for row in result]  # 重置为 pending

            # 3. failed 可重试
            result = conn.execute(
                f"MATCH (d:Document) WHERE d.entity_status = 'failed' AND d.retry_count < {self.MAX_RETRY_COUNT} RETURN d.uri, d.title, d.content, d.source LIMIT 10"
            )
            retryable = [self._row_to_dict(row, "retry") for row in result]

            return pending + timed_out + retryable

        except Exception as e:
            logger.warning(f"[KGScanner] Failed to query KG: {e}")
            return []

    @staticmethod
    def _row_to_dict(row, reason: str) -> dict:
        """将 KuzuDB 查询结果行转为字典"""
        return {
            "uri": row[0],
            "title": row[1] or "",
            "content": row[2] or "",
            "source": row[3] or "document",
            "reason": reason,
        }

    def _process_loop(self):
        """处理循环：从队列取出文档，启动 entity-extractor"""
        while not self._stop_event.is_set():
            try:
                doc = self._queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                self._process_document(doc)
            except Exception as e:
                logger.error(f"[KGScanner] Process failed for {doc.get('uri')}: {e}", exc_info=True)
                self._update_status(doc["uri"], "failed", retry_increment=True)

    def _process_document(self, doc: dict):
        """处理单个文档：启动 entity-extractor 子 Agent"""
        uri = doc["uri"]
        logger.info(f"[KGScanner] Processing: {uri} (reason: {doc.get('reason')})")

        # 标记 processing
        self._update_status(uri, "processing", processing_at=datetime.now().isoformat())

        # 构建任务描述
        task = f"请处理以下文档的实体提取：\n\nURI: {uri}\n标题: {doc.get('title', '')}\n内容: {doc.get('content', '')}\n来源: {doc.get('source', '')}\n\n请提取实体、建立关联，完成后调用 update_entity_status 更新状态为 completed。"

        # 获取 LLM 配置
        try:
            from agent.runner import get_runner
            runner = get_runner()
            if runner is None or not hasattr(runner, 'llm_config') or not runner.llm_config:
                logger.warning("[KGScanner] Runner not initialized, resetting to pending")
                self._update_status(uri, "pending")
                return
            llm_config = runner.llm_config
        except Exception:
            logger.warning("[KGScanner] Failed to get runner config, resetting to pending")
            self._update_status(uri, "pending")
            return

        # 启动子 Agent
        from agent.subagent import call_subagent
        result = call_subagent(
            agent_name="entity-extractor",
            task=task,
            llm_config=llm_config,
            mcp_client=None,
        )

        logger.info(f"[KGScanner] entity-extractor result for {uri}: {str(result)[:200]}")

        # 子 Agent 应该已经更新了 entity_status，这里做兜底检查
        try:
            conn = self._get_connection()
            check = conn.execute(
                "MATCH (d:Document {uri: $uri}) RETURN d.entity_status",
                {"uri": uri},
            )
            for row in check:
                if row[0] == "processing":
                    # 子 Agent 没有更新状态，根据返回值判断
                    if result and "completed" in str(result).lower():
                        self._update_status(uri, "completed")
                    else:
                        logger.warning(f"[KGScanner] Sub-agent did not complete for {uri}, marking failed")
                        self._update_status(uri, "failed", retry_increment=True)
        except Exception:
            pass

    def _update_status(self, uri: str, status: str, processing_at: str = None, retry_increment: bool = False):
        """更新 Document 的 entity_status"""
        try:
            from niu_kg_server import update_entity_status
            if retry_increment:
                # 获取当前 retry_count 并 +1
                conn = self._get_connection()
                result = conn.execute(
                    "MATCH (d:Document {uri: $uri}) RETURN d.retry_count",
                    {"uri": uri},
                )
                retry_count = 0
                for row in result:
                    retry_count = (row[0] or 0) + 1
                if retry_count >= 3:
                    update_entity_status(uri, "failed_permanent", retry_count=retry_count)
                else:
                    update_entity_status(uri, status, retry_count=retry_count)
            else:
                update_entity_status(uri, status, processing_at=processing_at)
        except Exception as e:
            logger.warning(f"[KGScanner] Failed to update status for {uri}: {e}")


# 全局实例
_kg_scanner: Optional[KGScanner] = None
_kg_scanner_lock = threading.Lock()


def get_kg_scanner(auto_start: bool = True) -> KGScanner:
    """获取全局 KGScanner 实例（线程安全）"""
    global _kg_scanner
    if _kg_scanner is None:
        with _kg_scanner_lock:
            if _kg_scanner is None:
                instance = KGScanner()
                if auto_start:
                    instance.start()
                _kg_scanner = instance
    return _kg_scanner
