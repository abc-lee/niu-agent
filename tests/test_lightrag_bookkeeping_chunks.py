"""F-3 T1 单测：LightRAGAdapter 两个新原语（删人物只清「该人物自己的簿记块」）。

覆盖方案 §2 T1 的用例 ⓪–⑫：
⓪ get_chunk_by_source_id：按 sid 精确命中/未命中（返回 None）/多块同 sid 取最新/
   空 sid 返 None/rag is None 与 _data is None 同型（与 get_chunk_by_source 不同，
   本方法不做 file_path 条件匹配——<路径>_<名> 虚拟块的 file_path 是路径、
   source_id 是路径_名，必须能按 sid 单独读到）
① rename_<名>: <名> 簿记块命中删除（默认期望串）
② custom_kg_<名>: <名>（person_id=…）显式传入该形态串命中；默认期望串（名: 名）
   不命中它（调用方只传名: 名，此用例钉的是原语按传入串全等匹配）
③ merge_<名>: <名> 命中
④ 带真实描述的同名前缀块（custom_kg_<名>: <名>是…）不命中
⑤ 含 -> 的关系块（<名>->X: 关系、X-><名>: 关系）不命中
⑥ 非声明族 sid（照片真实块 <路径>、unknown_source、空 sid）即使内容等于期望串
   也绝不命中（I-5：知识块永不因实体被删而删）
⑦ 名字互为前缀/后缀：<名>xx 与 xx<名> 都不命中（全等匹配，无前缀误删）
⑧ _part 尾巴（分段子块）命中
⑨ 空 entity_name / 空 expected_contents ⇒ 返 0 不抛
⑩ 无命中返 0 不抛 / rag is None / _data is None 返回体同型
⑪ 多行描述（<名>: <名>\n更多内容）不命中（整段剩余全等）
⑫ 裸名形态（<名> 无 ": "）不命中（保守漏删）
⑬ F-3 微修回归钉子（质量审查 P2/P3）：P2——sid 名字本身含裸 _part<整数>（无尾随
   _<整数>）时不得按首个 _part 整段剥掉漏删；分段子块尾部 _part<整数>_<整数> 只剥
   尾部、仍命中；实体名含该形态不误伤；规则 2 的 -> 显式期望串不删（此前该规则零
   覆盖）；多元素 expected_contents 只删逐字命中者；⑩ 的 _data is None 守卫由
   logger.error 静默钉死（误删检查后 AttributeError 被外层 except 吞掉仍留日志）

桩缝＝patch.object(LightRAGAdapter, "_get_rag") + patch 模块级 call_async
（先例 tests/test_lightrag_chunk_refresh.py:28-34,54-58）；**不得**类级 patch
整个 LightRAGAdapter（那会把被测方法本身 mock 掉 ⇒ 断言全空假绿）。
call_async 的 side_effect 必须真跑协程（独立事件循环 run_until_complete）。
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


class TestGetChunkBySourceId:
    """⓪ 精确 sid 命中/未命中/多块取最新 + 空 sid / rag None / _data None。"""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_hit_exact_sid_ignores_file_path(self, mock_get_rag, mock_call_async):
        """<路径>_<名> 虚拟块（file_path≠source_id）按 sid 单独可读；真实块按自己的
        sid 精确命中、互不串号。"""
        mock_call_async.side_effect = _run_closing
        virtual = {
            "content": "安安: 安安（person_id=x）",
            "file_path": PATH,
            "source_id": PATH + "_安安",
        }
        real = {
            "content": "照片 20261016_180058：安安",
            "file_path": PATH,
            "source_id": PATH,
        }
        mock_get_rag.return_value = _make_rag({"virtual": virtual, "real": real})
        assert LightRAGAdapter().get_chunk_by_source_id(PATH + "_安安") is virtual
        assert LightRAGAdapter().get_chunk_by_source_id(PATH) is real

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_miss_returns_none(self, mock_get_rag, mock_call_async):
        """库里有块但 sid 无一精确相等 ⇒ 未命中返 None。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "other-sid": {"content": "x", "file_path": PATH, "source_id": "别的源"},
                "prefix-only": {
                    "content": "y",
                    "file_path": PATH,
                    "source_id": PATH + "_安安",  # 前缀相似 ≠ 精确等值
                },
            }
        )
        assert LightRAGAdapter().get_chunk_by_source_id(PATH) is None

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_multi_chunks_same_sid_takes_last(self, mock_get_rag, mock_call_async):
        """多块同 source_id 时取最后一条（dict 迭代序＝插入序，最新插入在最后）。"""
        mock_call_async.side_effect = _run_closing
        old = {"content": "旧内容", "file_path": PATH, "source_id": PATH}
        new = {"content": "新内容", "file_path": PATH, "source_id": PATH}
        mock_get_rag.return_value = _make_rag({"first": old, "second": new})
        result = LightRAGAdapter().get_chunk_by_source_id(PATH)
        assert result is new
        assert result["content"] == "新内容"

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rag_none_returns_none(self, mock_get_rag):
        """rag is None ⇒ None（不抛、不触事件循环）。"""
        mock_get_rag.return_value = None
        assert LightRAGAdapter().get_chunk_by_source_id(PATH) is None

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_sid_returns_none(self, mock_get_rag):
        """空 sid ⇒ None（守卫，参照 F-2 空 source_id）。"""
        mock_get_rag.return_value = _make_rag({"a": {"source_id": "", "content": "x"}})
        assert LightRAGAdapter().get_chunk_by_source_id("") is None

    @patch("niu_api.internal.lightrag_adapter.logger")
    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_returns_none(self, mock_get_rag, mock_call_async, mock_logger):
        """_data 为 None（未初始化）⇒ None，且静默返回（logger.error 不被调用）——
        钉死 _data is None 检查：误删后 NoneType.values() 的 AttributeError 会被
        外层 except Exception → None 吞掉、用例表面仍绿，但 error 日志暴露异常 ⇒ 红。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({})
        rag.text_chunks._data = None
        assert LightRAGAdapter().get_chunk_by_source_id(PATH) is None
        mock_logger.error.assert_not_called()


class TestDeleteBookkeepingChunks:
    """①–⑫ 声明族簿记块按内容精确等值删；知识块/关系块/非声明族 sid 永不删。"""

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rename_family_hit(self, mock_get_rag, mock_call_async):
        """① rename_ 声明块内容等于默认期望串（名: 名）⇒ 删除 + 双 storage 落盘。"""
        mock_call_async.side_effect = _run_closing
        target = {
            "content": "测试甲: 测试甲",
            "file_path": "custom_kg",
            "source_id": "rename_未命名人物_5_测试甲",
        }
        rag = mock_get_rag.return_value = _make_rag({"b0": target})
        result = LightRAGAdapter().delete_bookkeeping_chunks("测试甲")
        assert result["status"] == "ok"
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1
        assert result["deleted_vectors"] == 1
        rag.text_chunks.delete.assert_called_once_with(["b0"])
        rag.chunks_vdb.delete.assert_called_once_with(["b0"])
        rag.text_chunks.index_done_callback.assert_called_once()
        rag.chunks_vdb.index_done_callback.assert_called_once()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_custom_kg_person_id_form_hit_with_explicit_expected(
        self, mock_get_rag, mock_call_async
    ):
        """② 显式把 person_id 形态串传入 expected_contents ⇒ 全等命中；
        默认期望串（名: 名）不命中它（调用方只传名: 名，原语不猜形态）。"""
        mock_call_async.side_effect = _run_closing
        pid = "ce52c86a-0000-0000-0000-000000000000"
        chunk = {
            "content": f"任飞: 任飞（person_id={pid}）",
            "file_path": "custom_kg",
            "source_id": "custom_kg_任飞",
        }
        mock_get_rag.return_value = _make_rag({"b0": chunk})
        result = LightRAGAdapter().delete_bookkeeping_chunks(
            "任飞", expected_contents=[f"任飞: 任飞（person_id={pid}）"]
        )
        assert result["status"] == "ok"
        assert result["matched_ids"] == ["b0"]

        mock_get_rag.return_value = _make_rag({"b0": chunk})
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_merge_family_hit(self, mock_get_rag, mock_call_async):
        """③ merge_ 声明块（名: 名）⇒ 删除。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "刘永辉: 刘永辉", "file_path": "custom_kg",
                    "source_id": "merge_刘永辉"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("刘永辉")
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_real_description_not_deleted(self, mock_get_rag, mock_call_async):
        """④ 带真实描述的声明块（名: 名是…描述）⇒ 内容不等值 ⇒ 保留。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "b0": {
                    "content": "西柏坡: 西柏坡，河北平山县革命纪念地；<user>2009年6月在此拍摄的老照片已入库",
                    "file_path": "custom_kg",
                    "source_id": "custom_kg_西柏坡",
                }
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("西柏坡")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_relation_chunks_not_deleted(self, mock_get_rag, mock_call_async):
        """⑤ 含 -> 的关系块（任一端点是该人物都算）⇒ 知识，保留。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "out": {"content": "任飞-><user>: 同游西柏坡", "file_path": "custom_kg",
                        "source_id": "custom_kg_<user>"},
                "in": {"content": "<user>->任飞: 同游西柏坡", "file_path": "custom_kg",
                       "source_id": "custom_kg_任飞"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_non_declaration_sid_never_deleted(self, mock_get_rag, mock_call_async):
        """⑥ 非声明族 sid 一律不看——即使内容逐字等于期望串（照片真实块 /
        unknown_source / 空 sid）。"""
        mock_call_async.side_effect = _run_closing
        expected = "安安: 安安"
        rag = mock_get_rag.return_value = _make_rag(
            {
                "real-photo": {"content": expected, "file_path": PATH,
                               "source_id": PATH},
                "unknown": {"content": expected, "file_path": "unknown_source",
                            "source_id": "unknown_source"},
                "empty-sid": {"content": expected, "file_path": "x",
                              "source_id": ""},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("安安")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_name_prefix_suffix_not_deleted(self, mock_get_rag, mock_call_async):
        """⑦ 名字互为前缀/后缀的他人簿记块 ⇒ 全等匹配不命中（不得前缀误删）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "suffix": {"content": "任飞子: 任飞子", "file_path": "custom_kg",
                           "source_id": "rename_任飞子"},
                "prefix": {"content": "小任飞: 小任飞", "file_path": "custom_kg",
                           "source_id": "rename_小任飞"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_part_suffix_stripped_and_hit(self, mock_get_rag, mock_call_async):
        """⑧ 分段子块 sid 的 _part 尾巴剥掉后仍是声明族 ⇒ 内容等值则删。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "b0": {"content": "测试甲: 测试甲", "file_path": "custom_kg",
                       "source_id": "rename_未命名人物_5_测试甲_part0_2"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("测试甲")
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_entity_name_zero_guard(self, mock_get_rag, mock_call_async):
        """⑨ 空 entity_name ⇒ 直接返 0（不推理事件循环、不抛）。"""
        rag = mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "测试甲: 测试甲", "file_path": "custom_kg",
                    "source_id": "rename_x"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("")
        assert result == {
            "status": "ok",
            "deleted_chunks": 0,
            "deleted_vectors": 0,
            "matched_ids": [],
        }
        mock_call_async.assert_not_called()
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_empty_expected_contents_zero_guard(self, mock_get_rag, mock_call_async):
        """⑨ 显式传空 expected_contents ⇒ 同样直接返 0。"""
        mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "测试甲: 测试甲", "file_path": "custom_kg",
                    "source_id": "rename_x"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("测试甲", expected_contents=[])
        assert result["status"] == "ok"
        assert result["deleted_chunks"] == 0
        mock_call_async.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_no_match_zero_no_delete(self, mock_get_rag, mock_call_async):
        """⑩ 无命中 ⇒ ok/0、不删不落盘（宁漏不误删）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "a": {"content": "别的知识: 别的知识", "file_path": "custom_kg",
                      "source_id": "custom_kg_别的知识"},
                "b": {"content": "任飞: 任飞（person_id=别的id）", "file_path": "custom_kg",
                      "source_id": "custom_kg_任飞"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()
        rag.text_chunks.index_done_callback.assert_not_called()
        rag.chunks_vdb.index_done_callback.assert_not_called()

    @patch.object(LightRAGAdapter, "_get_rag")
    def test_rag_none_error_dict(self, mock_get_rag):
        """⑩ rag is None ⇒ error dict（与相邻原语同型）。"""
        mock_get_rag.return_value = None
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result == {"status": "error", "message": "LightRAG not available"}

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_data_none_error_dict(self, mock_get_rag, mock_call_async):
        """⑩ _data 为 None（未初始化）⇒ error（文案含「未初始化」），不触发删除。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag({})
        rag.text_chunks._data = None
        result = LightRAGAdapter().delete_bookkeeping_chunks("任飞")
        assert result == {"status": "error", "message": "LightRAG text_chunks 未初始化"}
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_multi_line_content_not_deleted(self, mock_get_rag, mock_call_async):
        """⑪ 内容在期望串之后还有换行+更多文本 ⇒ 整段剩余不等值 ⇒ 保留。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "b0": {
                    "content": "测试甲: 测试甲\n西柏坡老照片的后续说明",
                    "file_path": "custom_kg",
                    "source_id": "rename_x",
                }
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("测试甲")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_bare_name_content_not_deleted(self, mock_get_rag, mock_call_async):
        """⑫ 裸名形态（无 ": "）⇒ 与期望串不等 ⇒ 保守漏删、不抛。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "b0": {"content": "测试甲", "file_path": "custom_kg",
                       "source_id": "rename_x"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("测试甲")
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()

    # ── ⑬ F-3 微修回归钉子（质量审查 P2/P3）──────────────────────────────────

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_part_int_name_in_sid_hits(self, mock_get_rag, mock_call_async):
        """P2 回归钉子：sid 名字本身含裸 _part<整数>（无尾随 _<整数>）⇒ 不得按首个
        _part 整段剥掉（剥掉即漏出声明族白名单 ⇒ 漏删）⇒ 命中删除。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {
                "b0": {"content": "part2: part2", "file_path": "custom_kg",
                       "source_id": "custom_kg_part2"},
                "b1": {"content": "part2: part2", "file_path": "custom_kg",
                       "source_id": "rename_part2"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("part2")
        assert result["status"] == "ok"
        assert result["matched_ids"] == ["b0", "b1"]
        assert result["deleted_chunks"] == 2

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_part01_subblock_still_hit(self, mock_get_rag, mock_call_async):
        """分段子块 sid 尾部 _part<整数>_<整数> 只剥尾部 ⇒ 剥后仍是声明族 ⇒ 命中
        （对子块该剥离本是空转：前缀在头部，startswith 无需剥离即命中）。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "X: X", "file_path": "custom_kg",
                    "source_id": "custom_kg_X_part0_2"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks("X")
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_entity_name_with_part_form_not_misfired(self, mock_get_rag, mock_call_async):
        """实体名本身含 _part<整数>_<整数> 形态 ⇒ 只剥 sid 尾部（基名 custom_kg_A
        仍声明族）、内容按显式 expected_contents 全等 ⇒ 命中，防过度剥离误杀。"""
        mock_call_async.side_effect = _run_closing
        mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "A_part0_1: A_part0_1", "file_path": "custom_kg",
                    "source_id": "custom_kg_A_part0_1"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks(
            "A_part0_1", expected_contents=["A_part0_1: A_part0_1"]
        )
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_arrow_content_explicit_expected_not_deleted(self, mock_get_rag, mock_call_async):
        """规则 2 钉子：声明族 sid + 内容含 -> + 显式 expected_contents 就是该串
        ⇒ 仍是关系块＝知识，不删（删掉 "->" in content 条件本用例必红；此前套件
        对该规则零覆盖——默认期望串本就不含 ->）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {"b0": {"content": "任飞-><user>: 同游西柏坡", "file_path": "custom_kg",
                    "source_id": "custom_kg_任飞"}}
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks(
            "任飞", expected_contents=["任飞-><user>: 同游西柏坡"]
        )
        assert result["status"] == "ok"
        assert result["matched_ids"] == []
        assert result["deleted_chunks"] == 0
        rag.text_chunks.delete.assert_not_called()
        rag.chunks_vdb.delete.assert_not_called()

    @patch("niu_api.internal.lightrag_adapter.call_async")
    @patch.object(LightRAGAdapter, "_get_rag")
    def test_multi_expected_contents_only_matching_deleted(self, mock_get_rag, mock_call_async):
        """多元素 expected_contents=[甲: 甲, 乙: 乙]：仅内容逐字等于乙: 乙 的块被
        删，甲/丙 保留（原语按传入串列表全等匹配，不猜形态）。"""
        mock_call_async.side_effect = _run_closing
        rag = mock_get_rag.return_value = _make_rag(
            {
                "b0": {"content": "乙: 乙", "file_path": "custom_kg",
                       "source_id": "custom_kg_乙"},
                "b1": {"content": "丙: 丙", "file_path": "custom_kg",
                       "source_id": "custom_kg_丙"},
            }
        )
        result = LightRAGAdapter().delete_bookkeeping_chunks(
            "乙", expected_contents=["甲: 甲", "乙: 乙"]
        )
        assert result["status"] == "ok"
        assert result["matched_ids"] == ["b0"]
        assert result["deleted_chunks"] == 1
        rag.text_chunks.delete.assert_called_once_with(["b0"])
        rag.chunks_vdb.delete.assert_called_once_with(["b0"])
