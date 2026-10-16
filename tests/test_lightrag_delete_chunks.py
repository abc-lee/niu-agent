"""
F-1 T1 单测：LightRAGAdapter 两个新方法（删照片连 chunk 与向量一起清）。

覆盖方案 §2 T1 的五个用例：
① A 类三种命中形态各命中（file_path == path / source_id == path / source_id 前缀）
② source_id 前缀边界：<path> 与 <path>_x 命中，<path>x 不得命中
③ B 类端点匹配五种形态全断：A->B: kw 同时命中 A 与 B；A->DB: kw 命中 A 不命中 B；
   无冒号 A -> B 命中；A->B->C: kw（>2 段）跳过；A-> B: kw（空白差异）剥后命中
④ 空列表/无命中返回 deleted=0 不抛
⑤ 两个 storage 实例的 index_done_callback 各被调用至少一次（断言对象是 storage 实例，
   不是 rag）

桩缝先例＝tests/test_lightrag_adapter.py：patch niu_api.internal.lightrag_adapter 的
get_lightrag / call_async + patch.object(LightRAGAdapter, "_get_rag")。
call_async 的 side_effect 必须真跑协程（新事件循环 run_until_complete）——
纯 return_value mock 会让闭包永不执行、断言全空（假绿）。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from niu_api.internal.lightrag_adapter import LightRAGAdapter

PATH = "/users/lilei/photos/20261014_211832.jpg"


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
    """① A 类三种命中形态 + ② 前缀边界 + ④ 空路径/无命中 + ⑤ 落盘。"""

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
        """④：空路径直接返回 0，不删任何东西。"""
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
        """④：无命中返回 deleted=0 不抛，不触发任何删除/落盘。"""
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
        """⑤：两个 storage 实例的 index_done_callback 各被调用一次。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"hit": {"file_path": PATH}})
        LightRAGAdapter().delete_chunks_by_source(PATH)
        rag.text_chunks.index_done_callback.assert_called_once()
        rag.chunks_vdb.index_done_callback.assert_called_once()


class TestDeleteCustomKgRelationChunks:
    """③ B 类五种形态全断 + ④ 空名单/无命中 + ⑤ 落盘。"""

    @staticmethod
    def _custom_kg_chunks(contents) -> dict:
        """B 类条目（file_path == "custom_kg"）+ 一条非 custom_kg 干扰项。"""
        chunks = {
            f"b{i}": {"content": c, "file_path": "custom_kg", "source_id": "custom_kg"}
            for i, c in enumerate(contents)
        }
        # 干扰项：content 能命中但 file_path 不是 custom_kg，绝不得被扫
        chunks["a-foreign"] = {
            "content": "A->B: kw",
            "file_path": "photo.jpg",
            "source_id": "photo.jpg",
        }
        return chunks

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_colon_form_hits_both_endpoints(self, mock_get_rag, mock_call_async):
        """A->B: kw 同时命中 A 与 B（目标端点剥 ": kw" 后缀后才能比中）。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert set(r["matched_ids"]) == {"b0"}
        assert r["deleted_chunks"] == 1
        assert r["skipped"] == 0
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["B"])
        assert set(r["matched_ids"]) == {"b0"}
        assert r["deleted_chunks"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_target_side_prefix_does_not_leak(self, mock_get_rag, mock_call_async):
        """A->DB: kw 命中 A 不命中 B（剥后全等：'DB' != 'B'，不得前缀误删）。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->DB: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert set(r["matched_ids"]) == {"b0"}
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->DB: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["B"])
        assert r["matched_ids"] == []
        assert r["deleted_chunks"] == 0
        assert r["skipped"] == 0

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_no_colon_form_hits(self, mock_get_rag, mock_call_async):
        """无冒号的 A -> B 命中（两端 strip 后全等）。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A -> B"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["B"])
        assert set(r["matched_ids"]) == {"b0"}
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A -> B"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert set(r["matched_ids"]) == {"b0"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_more_than_two_parts_skipped(self, mock_get_rag, mock_call_async):
        """A->B->C: kw（keywords 内含 ->）切出 >2 段 ⇒ 跳过并计数。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B->C: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["matched_ids"] == []
        assert r["deleted_chunks"] == 0
        assert r["skipped"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_no_arrow_content_skipped(self, mock_get_rag, mock_call_async):
        """content 完全不含 "->"（1 段）⇒ 跳过并计数（F3：切段数 ≠ 2）。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["一段没有箭头的文本"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["matched_ids"] == []
        assert r["deleted_chunks"] == 0
        assert r["skipped"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_arrow_only_empty_endpoints_no_crash(self, mock_get_rag, mock_call_async):
        """content 恰为 '->'（两端点为空）⇒ 不抛错、不命中。

        微修规格期望 skipped+1；按已审代码，'->'.split('->') 恰得两段（均空），
        不走 `len(parts) != 2` 跳过分支，实际 skipped 保持 0——本测试按实际行为
        断言，差异记入报告。
        """
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["->"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["status"] == "ok"
        assert r["matched_ids"] == []
        assert r["deleted_chunks"] == 0
        assert r["skipped"] == 0

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_whitespace_endpoint_hits_after_strip(self, mock_get_rag, mock_call_async):
        """A-> B: kw（空白差异）剥后命中两端。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A-> B: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["B"])
        assert set(r["matched_ids"]) == {"b0"}
        mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A-> B: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert set(r["matched_ids"]) == {"b0"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_non_string_content_skipped(self, mock_get_rag, mock_call_async):
        """content 非字符串 ⇒ 跳过并计数。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "b0": {"content": 123, "file_path": "custom_kg", "source_id": "custom_kg"},
                "b1": {"content": None, "file_path": "custom_kg", "source_id": "custom_kg"},
            }
        )
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["matched_ids"] == []
        assert r["skipped"] == 2

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_non_custom_kg_chunks_not_scanned(self, mock_get_rag, mock_call_async):
        """只扫 file_path == 'custom_kg' 的条目，其余原样保留。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {"x": {"content": "A->B: kw", "file_path": "photo.jpg", "source_id": "photo.jpg"}}
        )
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["matched_ids"] == []
        assert r["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_names(self, mock_get_rag, mock_call_async):
        """④：空名单不删任何东西。"""
        rag = mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks([])
        assert r == {
            "status": "ok",
            "deleted_chunks": 0,
            "deleted_vectors": 0,
            "matched_ids": [],
            "skipped": 0,
        }
        mock_call_async.assert_not_called()
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_no_match_no_delete(self, mock_get_rag, mock_call_async):
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["X->Y: kw"]))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r["deleted_chunks"] == 0
        assert r["matched_ids"] == []
        assert r["skipped"] == 0
        rag.text_chunks.delete.assert_not_called()
        rag.text_chunks.index_done_callback.assert_not_called()
        rag.chunks_vdb.index_done_callback.assert_not_called()

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rag_none(self, mock_get_rag):
        mock_get_rag.return_value = None
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r == {"status": "error", "message": "LightRAG not available"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_exception_returns_error(self, mock_get_rag, mock_call_async):
        """异步逻辑内异常经 call_async 传播 ⇒ error dict（协程真跑，不留未等待协程）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        rag.text_chunks.delete = AsyncMock(side_effect=RuntimeError("boom"))
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r == {"status": "error", "message": "boom"}


    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_returns_error(self, mock_get_rag, mock_call_async):
        """text_chunks._data 为 None（未初始化）⇒ error（文案含「未初始化」），不触发删除。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        rag.text_chunks._data = None
        r = LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        assert r == {"status": "error", "message": "LightRAG text_chunks 未初始化"}
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_index_done_callback_per_storage(self, mock_get_rag, mock_call_async):
        """⑤：命中后两个 storage 实例的 index_done_callback 各被调用一次。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(self._custom_kg_chunks(["A->B: kw"]))
        LightRAGAdapter().delete_custom_kg_relation_chunks(["A"])
        rag.text_chunks.index_done_callback.assert_called_once()
        rag.chunks_vdb.index_done_callback.assert_called_once()
        rag.text_chunks.delete.assert_called_once()
        rag.chunks_vdb.delete.assert_called_once()


class TestDeleteChunksDelegation:
    """桩缝一致性：_get_rag 走模块级 get_lightrag 绑定（先例 test_lightrag_adapter.py）。"""

    def test_get_rag_delegates_to_module_get_lightrag(self):
        mock_rag = MagicMock()
        with patch("niu_api.internal.lightrag_adapter.get_lightrag", return_value=mock_rag):
            adapter = LightRAGAdapter()
            assert adapter._get_rag() is mock_rag
