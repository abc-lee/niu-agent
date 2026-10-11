"""Tests for the T3 embed guard in niu_api/internal/lightrag_adapter.py.

方案 2026-10-11-s8 §2 D-3/D-3.1 / §4 I-4,I-5,I-6,I-10,I-11,I-13,I-14。

全 mock：禁真实 LLM、禁写图谱。守卫的权威计量在生产走 llama-server /tokenize
（Gemma 口径），测试统一 patch `niu_api.internal.lightrag_adapter._count_embed_tokens`
为确定性函数 `4 * len(text)`（方案 R-3 实测的 Gemma 最坏密度 4.000 token/字符）：
- ≤ 2000 字符 → 预筛放行、不进计量（最坏 8000 token < 8192）；
- > 2048 字符 → 计量值 4×len > 8192，判超限。

I-13 / I-14 判据（v1.8/v1.9 双审 A-P3 指出的两个验证洞）：
- I-13：用「短实体名 + 长**存储侧**描述」驱动 allow_rename 改名。短名不过快速失败
  层、直接输入串（新名+本次 description）不超限，**只有级联重嵌串超限**——只实现
  实体名长度快速失败层的构建会让该测试红。
- I-14：用「新名已存在 + allow_merge=True + 旧名串超限而新名串不超限」驱动合流
  分支。只量新名（v1.8 的漏量形态）或只实现改名分支主层的构建会让该测试红。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from niu_api.internal.lightrag_adapter import (
    EmbedLimitError,
    LightRAGAdapter,
    LightRAGIngester,
)

_GUARD_MOD = "niu_api.internal.lightrag_adapter"
_COUNT_PATH = f"{_GUARD_MOD}._count_embed_tokens"


def _tokens_4x(text: str) -> int:
    """确定性 mock：Gemma 最坏密度 4.000 token/字符（方案 R-3）。"""
    return 4 * len(text)


class _FakeNxGraph:
    """最小 NetworkX 面（has_node / nodes / edges / get_edge_data / copy）。

    edges(node) 与 networkx 一致：node 在前。只供守卫枚举关联边用。
    """

    def __init__(self) -> None:
        self._adj: dict[str, dict[str, dict]] = {}

    def add_node(self, node_id: str) -> None:
        self._adj.setdefault(node_id, {})

    def add_edge(self, u: str, v: str, **data) -> None:
        self.add_node(u)
        self.add_node(v)
        self._adj[u][v] = data
        self._adj[v][u] = data

    def has_node(self, node_id: str) -> bool:
        return node_id in self._adj

    def nodes(self):
        return list(self._adj)

    def edges(self, node_id: str):
        return [(node_id, nbr) for nbr in self._adj.get(node_id, {})]

    def get_edge_data(self, u: str, v: str):
        return self._adj.get(u, {}).get(v)

    def copy(self) -> "_FakeNxGraph":
        clone = _FakeNxGraph()
        for u, nbrs in self._adj.items():
            clone._adj[u] = dict(nbrs)
        return clone


class _FakeGraphStorage:
    """lightrag chunk_entity_relation_graph 的最小面（暴露 _graph）。"""

    def __init__(self, graph: _FakeNxGraph) -> None:
        self._graph = graph


def _make_rag(graph: _FakeNxGraph | None = None) -> MagicMock:
    rag = MagicMock(name="rag")
    if graph is not None:
        rag.chunk_entity_relation_graph = _FakeGraphStorage(graph)
    rag.ainsert_custom_kg = AsyncMock(return_value=None)
    rag.acreate_entity = AsyncMock(return_value={"status": "success"})
    rag.acreate_relation = AsyncMock(return_value={"status": "success"})
    rag.aedit_entity = AsyncMock(return_value={"graph_data": {"description": "d"}})
    rag.aedit_relation = AsyncMock(return_value={"graph_data": {"description": "d"}})
    rag.amerge_entities = AsyncMock(return_value={"status": "success"})
    return rag


def _count_patch():
    return patch(_COUNT_PATH, side_effect=_tokens_4x)


# ===========================================================================
# I-4 / I-6：未超限 chunk 恒等透传；调用方对象不被污染
# ===========================================================================


class TestUnderLimitIdentity:
    def test_short_inputs_skip_measurement_and_pass_through(self):
        """≤2000 字符不调 /tokenize；chunk content/source_id/chunk_order_index 原样。"""
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        caller_chunks = [
            {"content": "很短的内容", "source_id": "s1", "chunk_order_index": 0},
            {"content": "另一段", "source_id": "s2", "chunk_order_index": 1},
        ]
        entities = [{"entity_name": "E", "entity_type": "Concept", "description": "描述"}]
        with patch.object(ingester, "_get_rag", return_value=rag), \
                _count_patch() as counter:
            result = ingester.inject_custom_kg(
                entities=entities, relationships=[], chunks=caller_chunks,
            )
        assert result["status"] == "ok"
        counter.assert_not_called()  # 预筛拦下的都不该进权威计量
        custom_kg = rag.ainsert_custom_kg.call_args[0][0]
        # 前两条即调用方 chunk 原样透传（第三条是实体虚拟块，既有逻辑，不计入本条）
        assert len(custom_kg["chunks"]) == 3
        for i, orig in enumerate(caller_chunks):
            got = custom_kg["chunks"][i]
            assert got["content"] == orig["content"]
            assert got["source_id"] == orig["source_id"]
            assert got["chunk_order_index"] == orig["chunk_order_index"]
        # I-6：调用方传入的 chunks 列表与 dict 均不被就地修改
        assert caller_chunks[0]["chunk_order_index"] == 0
        assert "part1" not in caller_chunks[0].get("source_id", "")


# ===========================================================================
# I-5 / I-11：超限 chunk 分段（首块保原 source_id，后缀唯一，顺序递增）
# ===========================================================================


class TestChunkSplit:
    def test_over_limit_chunk_split_identity(self):
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        paragraphs = [f"第{i}段" + "内" * 300 for i in range(10)]
        content = "\n\n".join(paragraphs)  # ≈ 3029 字符 > 2000 → 计量 → 超限
        caller_chunks = [{
            "content": content,
            "source_id": "orig",
            "file_path": "fp",
            "chunk_order_index": 3,
        }]
        with patch.object(ingester, "_get_rag", return_value=rag), _count_patch():
            result = ingester.inject_custom_kg(
                entities=[], relationships=[], chunks=caller_chunks,
            )
        assert result["status"] == "ok"
        chunks = rag.ainsert_custom_kg.call_args[0][0]["chunks"]
        assert len(chunks) >= 2  # 确实发生了分段

        # I-11：首块保留调用方原 source_id（不加后缀）与原 chunk_order_index
        assert chunks[0]["source_id"] == "orig"
        assert chunks[0]["chunk_order_index"] == 3
        # I-5：其余子块 source_id 唯一且带后缀；顺序按原文递增
        rest_sids = [c["source_id"] for c in chunks[1:]]
        assert len(set(rest_sids)) == len(rest_sids)
        for sid in rest_sids:
            assert sid != "orig" and sid.startswith("orig_part3_")
        orders = [c["chunk_order_index"] for c in chunks]
        assert orders[0] == 3                      # 首块保持原文顺序值
        assert orders == sorted(orders)            # 按原文顺序递增
        assert len(set(orders)) == len(orders)     # 严格递增
        assert orders == [3 + i for i in range(len(chunks))]

        # 每子块 ≤ 预算 1500 字符（最坏 6000 token < 8192）；拼接还原原文
        for c in chunks:
            assert len(c["content"]) <= 1500
        assert "".join(c["content"] for c in chunks) == content

        # I-6：调用方列表未被就地修改
        assert len(caller_chunks) == 1
        assert caller_chunks[0]["source_id"] == "orig"

    def test_chunk_at_token_boundary_passes(self):
        """边界：嵌入串 2048 字符 = 8192 token（不超）；2049 字符才超。"""
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(ingester, "_get_rag", return_value=rag), _count_patch():
            result = ingester.inject_custom_kg(
                entities=[], relationships=[],
                chunks=[{"content": "x" * 2048, "source_id": "b"}],
            )
        assert result["status"] == "ok"
        chunks = rag.ainsert_custom_kg.call_args[0][0]["chunks"]
        assert len(chunks) == 1  # 未分段


# ===========================================================================
# I-10：六个写入方法逐一 fail-loud（raise 且报文含名字 + 真实 token 数，
# 不被 except → {"status":"error"} 兜底吞掉）
# ===========================================================================


def _assert_fail_loud(exc: EmbedLimitError, name: str, embed_text: str) -> None:
    """报文必须含实体名/关系名 + 真实 token 数（I-10）；token 数 = 4×嵌入串长度。"""
    msg = str(exc)
    assert name in msg            # 报文含实体名/关系名
    assert str(4 * len(embed_text)) in msg  # 报文含真实 token 数
    assert "8192" in msg


class TestFailLoudPerMethod:
    def test_inject_custom_kg_entity_description(self):
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(ingester, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                ingester.inject_custom_kg(
                    entities=[{"entity_name": "超限实体", "description": "d" * 2100}],
                    relationships=[], chunks=[],
                )
        _assert_fail_loud(ei.value, "超限实体", "超限实体\n" + "d" * 2100)
        rag.ainsert_custom_kg.assert_not_called()

    def test_inject_custom_kg_relation_description(self):
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(ingester, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                ingester.inject_custom_kg(
                    entities=[],
                    relationships=[{
                        "src_id": "A", "tgt_id": "B",
                        "keywords": "k" * 2100, "description": "d",
                    }],
                    chunks=[],
                )
        _assert_fail_loud(ei.value, "A->B", "k" * 2100 + "\tA\nB\nd")
        rag.ainsert_custom_kg.assert_not_called()

    def test_create_entity_description(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.create_entity("实体甲", "Concept", description="d" * 2100)
        _assert_fail_loud(ei.value, "实体甲", "实体甲\n" + "d" * 2100)
        rag.acreate_entity.assert_not_called()

    def test_create_relation_description(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.create_relation("甲", "乙", keywords="k", description="d" * 2100)
        _assert_fail_loud(ei.value, "甲->乙", "k\t甲\n乙\n" + "d" * 2100)
        rag.acreate_relation.assert_not_called()

    def test_edit_entity_description_no_rename(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_entity("旧名", {"description": "d" * 2100})
        _assert_fail_loud(ei.value, "旧名", "旧名\n" + "d" * 2100)
        rag.aedit_entity.assert_not_called()

    def test_edit_relation_description(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_relation("甲", "乙", {"description": "d" * 2100})
        _assert_fail_loud(ei.value, "甲->乙", "甲\t乙\n\n" + "d" * 2100)
        rag.aedit_relation.assert_not_called()

    def test_merge_entities_target_description(self):
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_node("源")
        graph.add_node("目标")
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.merge_entities(
                    source_entities=["源"], target_entity="目标",
                    target_entity_data={"description": "d" * 2100},
                )
        _assert_fail_loud(ei.value, "目标", "目标\n" + "d" * 2100)
        rag.amerge_entities.assert_not_called()


# ===========================================================================
# I-13：级联重嵌主层独立可验（短名 + 长存储侧描述）
# ===========================================================================


class TestCascadeMainLayer:
    def test_short_name_plus_long_storage_desc_blocks(self):
        """短实体名（不过快速失败层）+ 长**存储侧**描述：只有级联串超限。

        直接输入串 = "n\nshort"（7 字符）不超限；级联串
        "n\\tother\\nkw\\n" + 9000 字符描述 ≈ 36028 token 超限。
        只实现实体名长度快速失败层的构建无法拦截 → 本测试红（I-13）。
        """
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_edge("old", "other", keywords="kw", description="x" * 9000)
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_entity(
                    "old",
                    {"entity_name": "n", "description": "short"},
                    allow_rename=True,
                )
        # 拦截必须来自级联串（报文里的关系标识是级联公式的 sorted 端点对）
        assert "n|other" in str(ei.value)
        rag.aedit_entity.assert_not_called()

    def test_control_same_inputs_without_edges_pass(self):
        """对照：同样短名 + 短描述、无关联边 → 不拦截（证明上面的 raise 源自
        级联串计量，而非直接输入或快速失败层）。"""
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_node("old")
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            result = adapter.edit_entity(
                "old",
                {"entity_name": "n", "description": "short"},
                allow_rename=True,
            )
        assert result["status"] == "ok"
        rag.aedit_entity.assert_called_once()

    def test_rename_without_allow_rename_still_guards_direct_input(self):
        """allow_rename=False 且请求改名：aedit_entity 在任何写入前即 raise、
        无嵌入发生 ⇒ 级联串不可能生成，不计量；但直接输入仍受守卫（fail-loud）。"""
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        # 存储侧描述超限的关联边： generously 超限也不该被计量（本次编辑不会重嵌它）
        graph.add_edge("old", "other", keywords="kw", description="x" * 9000)
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_entity(
                    "old",
                    {"entity_name": "n", "description": "d" * 2100},
                    allow_rename=False,
                )
        # 拦截来自直接输入串（改名分支公式 = 新名 + 描述），而非级联串
        assert "'n'" in str(ei.value)
        rag.aedit_entity.assert_not_called()

    def test_rename_without_allow_rename_short_inputs_pass(self):
        """对照：同上但不超限 → 守卫放行（级联计量被正确跳过，不会误拦；
        下游 aedit_entity 是否报错是 LightRAG 的职责，mock 下返回 ok）。"""
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_edge("old", "other", keywords="kw", description="x" * 9000)
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            result = adapter.edit_entity(
                "old",
                {"entity_name": "n", "description": "short"},
                allow_rename=False,
            )
        assert result["status"] == "ok"
        rag.aedit_entity.assert_called_once()


# ===========================================================================
# I-14：合流分支独立可验（新名已存在 + allow_merge=True，旧名先行嵌入）
# ===========================================================================


class TestMergeBranchOldName:
    def test_merge_branch_blocks_old_name_embed(self):
        """新名已存在 + allow_merge=True：non_name_updates 先以**旧名**嵌入
        `旧名 + "\\n" + description`（utils_graph.py:645-668 → 401）。

        构造使旧名串超限、新名串不超限：
        - 旧名 200 字符（= 800 token，不触发 200 上界的快速失败层）
        - 描述 1876 字符（7504 token）
        - 旧名串 2077 字符 = 8308 token > 8192 → 必须拦截
        - 新名串 1878 字符 = 7512 token < 8192 → 直接输入层放行
        只量新名（v1.8 漏量形态）或只实现改名分支主层的构建 → 本测试红（I-14）。
        """
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_node("旧" * 200)
        graph.add_node("new")  # 新名已存在 → 合流分支
        rag = _make_rag(graph)
        old_name = "旧" * 200
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_entity(
                    old_name,
                    {"entity_name": "new", "description": "d" * 1876},
                    allow_rename=True,
                    allow_merge=True,
                )
        assert old_name[:32] in str(ei.value)  # 报文定位到旧名这一串
        rag.aedit_entity.assert_not_called()

    def test_merge_branch_short_inputs_pass(self):
        """对照：合流分支下各串均未超限 → 放行（证明拦截确实由旧名串驱动）。"""
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_node("old")
        graph.add_node("new")
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            result = adapter.edit_entity(
                "old",
                {"entity_name": "new", "description": "短描述"},
                allow_rename=True,
                allow_merge=True,
            )
        assert result["status"] == "ok"
        rag.aedit_entity.assert_called_once()


# ===========================================================================
# D-3.1 快速失败层：实体名长度上界（与主层叠加，不互相替代）
# ===========================================================================


class TestNameFastFail:
    def test_create_entity_long_name_raises(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        long_name = "x" * 201
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.create_entity(long_name, "Concept", description="短")
        assert "长度" in str(ei.value)
        rag.acreate_entity.assert_not_called()

    def test_edit_entity_long_new_name_raises(self):
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.edit_entity("old", {"entity_name": "y" * 201, "description": "短"})
        assert "长度" in str(ei.value)
        rag.aedit_entity.assert_not_called()

    def test_name_at_boundary_passes(self):
        """200 字符实体名不触发快速失败层（留给主层逐条计量）。"""
        adapter = LightRAGAdapter()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            result = adapter.create_entity("x" * 200, "Concept", description="短")
        assert result["status"] == "ok"


# ===========================================================================
# D-3 表 merge 行：merge_entities 源/目标两侧关系重嵌计量
# ===========================================================================


class TestMergeRelationReembed:
    def test_merge_relation_cascade_blocks(self):
        adapter = LightRAGAdapter()
        graph = _FakeNxGraph()
        graph.add_node("源")
        graph.add_node("目标")
        graph.add_node("旁支")
        # merge 会重嵌该边：keywords\t目标\n旁支\n描述（源端点归一到目标）
        graph.add_edge("源", "旁支", keywords="kw", description="x" * 9000)
        rag = _make_rag(graph)
        with patch.object(adapter, "_get_rag", return_value=rag), _count_patch():
            with pytest.raises(EmbedLimitError) as ei:
                adapter.merge_entities(source_entities=["源"], target_entity="目标")
        assert "旁支|目标" in str(ei.value)
        rag.amerge_entities.assert_not_called()


# ===========================================================================
# 降级路径：权威计量不可用时不猜不吞，放行由 S7 护栏兜底（AC-0 前置）
# ===========================================================================


class TestMeasurementUnavailable:
    def test_tokenize_none_passes_through(self):
        ingester = LightRAGIngester()
        rag = _make_rag(_FakeNxGraph())
        with patch.object(ingester, "_get_rag", return_value=rag), \
                patch(_COUNT_PATH, return_value=None):
            result = ingester.inject_custom_kg(
                entities=[{"entity_name": "E", "description": "d" * 3000}],
                relationships=[], chunks=[],
            )
        assert result["status"] == "ok"
        rag.ainsert_custom_kg.assert_called_once()
