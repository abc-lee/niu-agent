"""
F-2 T1 单测：LightRAGAdapter 三个新原语（人物改名/合并后刷 chunk 层）。

覆盖方案 §2 T1 的四个用例：
① get_chunk_by_source 命中/未命中/多块同 source_id 取最后一条（最新插入）
② delete_chunks_by_source_id 删除范围＝source_id 等值 ∪ _part 前缀
   （<path>_xy 不命中、<path>_part0_0 命中）、无命中返 0 不抛、双 storage 落盘
③ upsert_chunks 委派 inject_custom_kg 且 entities==[]/relationships==[]/
   chunks 原样透传；空入参不实例化 ingester
④ 三方法在 rag is None / _data is None 的返回体（get 返 None，两 delete/upsert
   返 dict，与 F-1 相邻方法同型）

桩缝＝类级 patch niu_api.internal.lightrag_adapter.LightRAGAdapter 的 _get_rag
与模块级 call_async（先例 tests/test_lightrag_adapter.py / test_lightrag_delete_chunks.py）；
upsert_chunks 另 patch 模块级 LightRAGIngester。
call_async 的 side_effect 必须真跑协程（独立事件循环 run_until_complete）——
纯 return_value mock 会让闭包永不执行、断言全空（假绿）。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from niu_api.internal.lightrag_adapter import LightRAGAdapter

PATH = "/users/lilei/photos/20261016_180058.jpg"


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


class TestGetChunkBySource:
    """① 命中/未命中/多块取最后一条 + ④ rag None / 空路径 / _data None。"""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_hit_returns_chunk(self, mock_get_rag, mock_call_async):
        """file_path == path 且 source_id == path ⇒ 返回该块 dict。"""
        mock_call_async.side_effect = _run_closing
        target = {
            "content": "照片 20261016_180058：安安",
            "file_path": PATH,
            "source_id": PATH,
        }
        mock_get_rag.return_value = _make_rag(
            {
                "real": target,
                "virtual-person": {
                    "content": "安安: 安安（person_id=x）",
                    "file_path": PATH,
                    "source_id": PATH + "_安安",
                },
                "unrelated": {
                    "content": "无关",
                    "file_path": "/other.jpg",
                    "source_id": "/other.jpg",
                },
            }
        )
        result = LightRAGAdapter().get_chunk_by_source(PATH)
        assert result is target
        assert result["content"].startswith("照片 20261016_180058")

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_miss_when_only_one_side_matches(self, mock_get_rag, mock_call_async):
        """只满足单侧条件（file_path 或 source_id 之一）⇒ 未命中返 None。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "file_path-only": {
                    "content": "x",
                    "file_path": PATH,
                    "source_id": "别的源",
                },
                "source_id-only": {
                    "content": "y",
                    "file_path": "别的路径",
                    "source_id": PATH,
                },
            }
        )
        assert LightRAGAdapter().get_chunk_by_source(PATH) is None

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_multi_chunks_same_source_id_takes_last(self, mock_get_rag, mock_call_async):
        """①：多块同 source_id 时取最后一条（dict 迭代序＝插入序，最新插入在最后）。"""
        mock_call_async.side_effect = _run_closing
        old = {"content": "旧内容", "file_path": PATH, "source_id": PATH}
        new = {"content": "新内容", "file_path": PATH, "source_id": PATH}
        mock_get_rag.return_value = _make_rag({"first": old, "second": new})
        result = LightRAGAdapter().get_chunk_by_source(PATH)
        assert result is new
        assert result["content"] == "新内容"

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rag_none_returns_none(self, mock_get_rag):
        """④：rag is None ⇒ None（不抛）。"""
        mock_get_rag.return_value = None
        assert LightRAGAdapter().get_chunk_by_source(PATH) is None

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_path_returns_none_no_op(self, mock_get_rag, mock_call_async):
        """④：空 source_path ⇒ None，不触达协程。"""
        mock_get_rag.return_value = _make_rag({"a": {"file_path": PATH, "source_id": PATH}})
        assert LightRAGAdapter().get_chunk_by_source("") is None
        mock_call_async.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_returns_none(self, mock_get_rag, mock_call_async):
        """④：text_chunks._data 为 None（未初始化）⇒ None（协程真跑，不留未等待协程）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"a": {"file_path": PATH}})
        rag.text_chunks._data = None
        assert LightRAGAdapter().get_chunk_by_source(PATH) is None

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_exception_returns_none(self, mock_get_rag, mock_call_async):
        """④：协程内异常经 call_async 传播 ⇒ None（不抛）。"""
        mock_call_async.side_effect = _run_closing

        class _BoomStorage:
            @property
            def _data(self):
                raise RuntimeError("boom")

        rag = mock_get_rag.return_value = MagicMock()
        rag.text_chunks = _BoomStorage()
        assert LightRAGAdapter().get_chunk_by_source(PATH) is None


class TestDeleteChunksBySourceId:
    """② 等值 ∪ _part 前缀 + ④ 空/无命中/rag None/_data None/异常 + 落盘。"""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_equal_and_part_prefix_hit(self, mock_get_rag, mock_call_async):
        """②：<path>_xy 不命中、<path>_part0_0 命中；KV 与向量收到同一份 id。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "exact": {"content": "c1", "file_path": PATH, "source_id": PATH},
                "part": {"content": "c2", "file_path": PATH, "source_id": PATH + "_part0_0"},
                "sibling-xy": {"content": "c3", "file_path": PATH, "source_id": PATH + "_xy"},
                "sibling-plain": {"content": "c4", "file_path": PATH, "source_id": PATH + "_x"},
                "unrelated": {"content": "c5", "file_path": "/other.jpg", "source_id": "n"},
            }
        )
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
        assert result["status"] == "ok"
        assert set(result["matched_ids"]) == {"exact", "part"}
        assert result["deleted_chunks"] == 2
        assert result["deleted_vectors"] == 2
        assert "sibling-xy" not in result["matched_ids"]
        assert "sibling-plain" not in result["matched_ids"]
        rag.text_chunks.delete.assert_called_once_with(result["matched_ids"])
        rag.chunks_vdb.delete.assert_called_once_with(result["matched_ids"])

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_part_prefix_requires_underscore(self, mock_get_rag, mock_call_async):
        """② 边界：_part 前缀必须带下划线——<path>part/<path>xy/<path>_x/
        <path>xy 不得命中；<path>_part、<path>_part0_0、<path>_partxy 命中。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "bare-part": {"content": "c1", "source_id": PATH + "part"},
                "no-underscore": {"content": "c2", "source_id": PATH + "xy"},
                "exact-part": {"content": "c3", "source_id": PATH + "_part"},
            }
        )
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
        assert set(result["matched_ids"]) == {"exact-part"}
        rag.text_chunks.delete.assert_called_once_with(result["matched_ids"])

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_source_id_zero_return(self, mock_get_rag, mock_call_async):
        """④：空 source_id 直接返回 0，不删任何东西。"""
        rag = mock_get_rag.return_value = _make_rag({"a": {"source_id": PATH}})
        result = LightRAGAdapter().delete_chunks_by_source_id("")
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
    def test_no_match_zero_return(self, mock_get_rag, mock_call_async):
        """④：无命中返回 0 不抛，不触发任何删除/落盘。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {"other": {"content": "x", "file_path": "/other.jpg", "source_id": "n"}}
        )
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
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
    def test_rag_none_error_dict(self, mock_get_rag):
        """④：rag is None ⇒ error dict（与 F-1 同型）。"""
        mock_get_rag.return_value = None
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
        assert result == {"status": "error", "message": "LightRAG not available"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_error_dict(self, mock_get_rag, mock_call_async):
        """④：text_chunks._data 为 None（未初始化）⇒ error（文案含「未初始化」），不触发删除。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"a": {"source_id": PATH}})
        rag.text_chunks._data = None
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
        assert result == {"status": "error", "message": "LightRAG text_chunks 未初始化"}
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_exception_error_dict(self, mock_get_rag, mock_call_async):
        """④：协程内异常经 call_async 传播 ⇒ error dict（协程真跑，不留未等待协程）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"hit": {"source_id": PATH}})
        rag.text_chunks.delete = AsyncMock(side_effect=RuntimeError("boom"))
        result = LightRAGAdapter().delete_chunks_by_source_id(PATH)
        assert result == {"status": "error", "message": "boom"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_index_done_callback_per_storage(self, mock_get_rag, mock_call_async):
        """命中后两个 storage 实例的 index_done_callback 各被调用一次。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({"hit": {"source_id": PATH}})
        LightRAGAdapter().delete_chunks_by_source_id(PATH)
        rag.text_chunks.index_done_callback.assert_called_once()
        rag.chunks_vdb.index_done_callback.assert_called_once()
        rag.text_chunks.delete.assert_called_once()
        rag.chunks_vdb.delete.assert_called_once()


class TestUpsertChunks:
    """③ 委派 inject_custom_kg（空实体/关系、chunks 原样透传）+ 空入参不实例化。"""

    @patch("niu_api.internal.lightrag_adapter.LightRAGIngester")
    def test_delegates_with_empty_entities_and_relationships(self, mock_ingester_cls):
        """③：entities==[]、relationships==[]、chunks 原样透传（元素同一对象）。"""
        mock_ingester = mock_ingester_cls.return_value
        mock_ingester.inject_custom_kg.return_value = {
            "status": "ok",
            "entities": 0,
            "relationships": 0,
            "chunks": 2,
        }
        chunks = [
            {"content": "照片 x：安安", "source_id": PATH, "file_path": PATH},
            {"content": "安安: 安安（person_id=x）", "source_id": PATH + "_安安", "file_path": PATH},
        ]
        result = LightRAGAdapter().upsert_chunks(chunks)
        assert result == {"status": "ok", "entities": 0, "relationships": 0, "chunks": 2}
        mock_ingester_cls.assert_called_once()
        mock_ingester.inject_custom_kg.assert_called_once()
        kwargs = mock_ingester.inject_custom_kg.call_args.kwargs
        assert kwargs["entities"] == []
        assert kwargs["relationships"] == []
        # 原样透传：收到的是同一批 dict 对象（浅拷贝 list，不改调用方入参）
        assert kwargs["chunks"] == chunks
        assert [c is orig for c, orig in zip(kwargs["chunks"], chunks, strict=True)] == [True, True]

    @patch("niu_api.internal.lightrag_adapter.LightRAGIngester")
    def test_empty_chunks_zero_return_no_ingester(self, mock_ingester_cls):
        """空列表入参 ⇒ 零值 dict，不实例化 ingester。"""
        result = LightRAGAdapter().upsert_chunks([])
        assert result == {"status": "ok", "entities": 0, "relationships": 0, "chunks": 0}
        mock_ingester_cls.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.LightRAGIngester")
    def test_none_chunks_zero_return_no_ingester(self, mock_ingester_cls):
        """None 入参 ⇒ 零值 dict，不实例化 ingester。"""
        result = LightRAGAdapter().upsert_chunks(None)
        assert result == {"status": "ok", "entities": 0, "relationships": 0, "chunks": 0}
        mock_ingester_cls.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.LightRAGIngester")
    def test_error_dict_passthrough_no_raise(self, mock_ingester_cls):
        """③：inject_custom_kg 返 error dict ⇒ upsert_chunks 原样透传，不抛异常。"""
        error = {"status": "error", "message": "boom"}
        mock_ingester = mock_ingester_cls.return_value
        mock_ingester.inject_custom_kg.return_value = error
        result = LightRAGAdapter().upsert_chunks([{"content": "x", "source_id": PATH}])
        assert result == error
        assert result is error
        mock_ingester_cls.assert_called_once()
