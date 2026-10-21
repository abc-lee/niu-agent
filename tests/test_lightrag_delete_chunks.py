"""
F-1 T1 单测：LightRAGAdapter 删照片连 chunk 与向量一起清（F-3 T0 回退 B 类关系虚拟块后仅剩 delete_chunks_by_source）。

覆盖方案 §2 T1 的用例（F-3 T0 回退 B 类后）：
① A 类三种命中形态各命中（file_path == path / source_id == path / source_id 前缀）
② source_id 前缀边界：<path> 与 <path>_x 命中，<path>x 不得命中
③ 空列表/无命中返回 deleted=0 不抛
④ 两个 storage 实例的 index_done_callback 各被调用至少一次（断言对象是 storage 实例，
   不是 rag）

桩缝先例＝tests/test_lightrag_adapter.py：patch niu_api.internal.lightrag_adapter 的
get_lightrag / call_async + patch.object(LightRAGAdapter, "_get_rag")。
call_async 的 side_effect 必须真跑协程（新事件循环 run_until_complete）——
纯 return_value mock 会让闭包永不执行、断言全空（假绿）。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from niu_api.internal.lightrag_adapter import LightRAGAdapter

PATH = "/tmp/niu-test/photos/20261014_211832.jpg"


def _run_closing(coro, *args, **kwargs):
    """call_async 替身：在独立事件循环里把协程真跑完并返回其返回值。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_rag(chunks: dict) -> MagicMock:
    """构造 mock rag：text_chunks._data 用给定 dict；两个 storage 实例各有
    AsyncMock 的 delete 与 index_done_callback（断言只落在 storage 实例上）。"""
    rag = MagicMock()
    rag.text_chunks = MagicMock()
    rag.text_chunks._data = dict(chunks)
    rag.text_chunks.delete = AsyncMock()
    rag.text_chunks.index_done_callback = AsyncMock()
    rag.chunks_vdb = MagicMock()
    rag.chunks_vdb.delete = AsyncMock()
    rag.chunks_vdb.index_done_callback = AsyncMock()
    return rag


class TestDeleteChunksBySource:
    """① A 类三种命中形态 + ② 前缀边界 + ③ 空路径/无命中 + ④ 落盘。"""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_three_match_forms_each_hit(self, mock_get_rag, mock_call_async):
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "id-by-file_path": {
                    "content": "照片 x：摘要",
                    "file_path": PATH,
                    "source_id": "别的路径",
                },
                "id-by-source_id": {
                    "content": "实体: 描述",
                    "file_path": "other",
                    "source_id": PATH,
                },
                "id-by-prefix": {
                    "content": "实体2: 描述",
                    "file_path": "other",
                    "source_id": PATH + "_地点名",
                },
                "id-unrelated": {
                    "content": "无关",
                    "file_path": "/other.jpg",
                    "source_id": "无关源",
                },
            }
        )
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert result["status"] == "ok"
        assert list(result["matched_ids"]) == [
            "id-by-file_path",
            "id-by-source_id",
            "id-by-prefix",
        ]
        assert result["deleted_chunks"] == 3
        assert result["deleted_vectors"] == 3
        # KV 与向量收到同一份 id 列表
        rag = mock_get_rag.return_value
        rag.text_chunks.delete.assert_called_once_with(result["matched_ids"])
        rag.chunks_vdb.delete.assert_called_once_with(result["matched_ids"])

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_prefix_boundary(self, mock_get_rag, mock_call_async):
        """②：<path> 与 <path>_x 命中；<path>x 不得命中。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "exact": {"content": "c1", "source_id": PATH},
                "sibling": {"content": "c2", "source_id": PATH + "_x"},
                "near-miss": {"content": "c3", "source_id": PATH + "x"},
            }
        )
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert set(result["matched_ids"]) == {"exact", "sibling"}
        assert "near-miss" not in result["matched_ids"]

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_path_no_delete(self, mock_get_rag, mock_call_async):
        """③：空路径直接返回 0，不删任何东西。"""
        rag = mock_get_rag.return_value = _make_rag({"a": {"file_path": PATH}})
        result = LightRAGAdapter().delete_chunks_by_source("")
        assert result == {
            "status": "ok",
            "deleted_chunks": 0,
            "deleted_vectors": 0,
            "matched_ids": [],
        }
        mock_call_async.assert_not_called()
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_no_match_no_delete(self, mock_get_rag, mock_call_async):
        """③：无命中返回 deleted=0 不抛，不触发任何删除/落盘。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {"other": {"content": "x", "file_path": "/other.jpg", "source_id": "n"}}
        )
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert result == {
            "status": "ok",
            "deleted_chunks": 0,
            "deleted_vectors": 0,
            "matched_ids": [],
        }
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()
        rag.text_chunks.index_done_callback.assert_not_called()
        rag.chunks_vdb.index_done_callback.assert_not_called()

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rag_none(self, mock_get_rag):
        mock_get_rag.return_value = None
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert result == {"status": "error", "message": "LightRAG not available"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_exception_returns_error(self, mock_get_rag, mock_call_async):
        """异步逻辑内异常经 call_async 传播 ⇒ error dict（协程真跑，不留未等待协程）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"hit": {"file_path": PATH}})
        rag.text_chunks.delete = AsyncMock(side_effect=RuntimeError("boom"))
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert result == {"status": "error", "message": "boom"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_returns_error(self, mock_get_rag, mock_call_async):
        """text_chunks._data 为 None（未初始化）⇒ error（文案含「未初始化」），不触发删除。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"a": {"file_path": PATH}})
        rag.text_chunks._data = None
        result = LightRAGAdapter().delete_chunks_by_source(PATH)
        assert result == {"status": "error", "message": "LightRAG text_chunks 未初始化"}
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_index_done_callback_per_storage(self, mock_get_rag, mock_call_async):
        """④：两个 storage 实例的 index_done_callback 各被调用一次。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"hit": {"file_path": PATH}})
        LightRAGAdapter().delete_chunks_by_source(PATH)
        rag.text_chunks.index_done_callback.assert_called_once()
        rag.chunks_vdb.index_done_callback.assert_called_once()


class TestDeleteChunksDelegation:
    """桩缝一致性：_get_rag 走模块级 get_lightrag 绑定（先例 test_lightrag_adapter.py）。"""

    def test_get_rag_delegates_to_module_get_lightrag(self):
        mock_rag = MagicMock()
        with patch("niu_api.internal.lightrag_adapter.get_lightrag", return_value=mock_rag):
            adapter = LightRAGAdapter()
            assert adapter._get_rag() is mock_rag
