"""
Tests for query_data 精确名检索短路（niu_api/internal/lightrag_adapter.py）。

query 恰为实体名时，图层精确索引命中实体置顶——向量排序不决定精确名查询命运。
全 mock：patch.object(_get_rag) + patch call_async（先例 tests/test_lightrag_adapter.py TestQueryData）。
禁真实 LLM、禁图谱写入。
"""

from unittest.mock import MagicMock, patch

from niu_api.internal.lightrag_adapter import LightRAGAdapter


def _success_result(entity_names):
    """构造 aquery_data success 形态返回：{status: success, data: {entities: [...]}}"""
    return {
        "status": "success",
        "message": "Query executed successfully",
        "data": {
            "entities": [
                {"entity_name": n, "entity_type": "concept", "description": f"{n} 描述"}
                for n in entity_names
            ],
            "relationships": [],
        },
        "metadata": {},
    }


def _entity_info_ok(name):
    """构造 get_entity_info ok 形态返回（字段埋在 data.graph_data）"""
    return {
        "status": "ok",
        "data": {
            "graph_data": {
                "entity_type": "person",
                "description": f"{name} 的图层描述",
                "source_id": "chunk-1",
                "file_path": "memory",
                "created_at": "2026-08-22T12:00:00",
            }
        },
    }


def _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity, entity_info=None):
    mock_get_rag.return_value = MagicMock()
    mock_call_async.return_value = vector_result
    adapter = LightRAGAdapter()
    adapter.has_entity = MagicMock(return_value=has_entity)
    adapter.get_entity_info = MagicMock(
        return_value=entity_info if entity_info is not None else _entity_info_ok("张三")
    )
    return adapter


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_hit_not_in_list_inserted_first_and_truncated(mock_get_rag, mock_call_async):
    """T2-1 命中且不在列：精确命中实体插入首位，总数截断到 top_k"""
    adapter = _make_adapter(
        mock_get_rag, mock_call_async,
        _success_result(["实体A", "实体B", "实体C"]),
        has_entity=True,
    )
    result = adapter.query_data("张三", mode="local", top_k=3)

    entities = result["data"]["entities"]
    assert entities[0]["entity_name"] == "张三"
    assert entities[0]["entity_type"] == "person"
    assert entities[0]["description"] == "张三 的图层描述"
    assert len(entities) == 3  # 插入首位后截断 top_k，末位被挤出


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_hit_in_list_moved_to_first(mock_get_rag, mock_call_async):
    """T2-2 命中且在列：已在列实体移到首位，总数不变"""
    adapter = _make_adapter(
        mock_get_rag, mock_call_async,
        _success_result(["实体A", "实体B", "张三"]),
        has_entity=True,
    )
    result = adapter.query_data("张三", mode="local", top_k=3)

    entities = result["data"]["entities"]
    assert entities[0]["entity_name"] == "张三"
    assert [e["entity_name"] for e in entities] == ["张三", "实体A", "实体B"]
    assert len(entities) == 3  # 已在列重排，总数不变


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_no_hit_returns_original(mock_get_rag, mock_call_async):
    """T2-3 未命中：has_entity False → 结果原样"""
    vector_result = _success_result(["实体A", "实体B"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=False)

    result = adapter.query_data("不存在的实体", mode="local", top_k=10)

    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A", "实体B"]
    adapter.get_entity_info.assert_not_called()


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_composite_query_not_shortcut(mock_get_rag, mock_call_async):
    """T2-4 组合 query 不短路：'张三 农行' has_entity False → 结果原样（锁语义）"""
    vector_result = _success_result(["实体A"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=False)

    result = adapter.query_data("张三 农行", mode="local", top_k=10)

    adapter.has_entity.assert_called_once_with("张三 农行")
    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A"]


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_shortcut_exception_returns_original(mock_get_rag, mock_call_async):
    """T2-5 短路异常防御：has_entity 抛异常 → 返回原向量结果 + warning"""
    vector_result = _success_result(["实体A", "实体B"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=True)
    adapter.has_entity = MagicMock(side_effect=RuntimeError("graph locked"))

    with patch("niu_api.internal.lightrag_adapter.logger") as mock_logger:
        result = adapter.query_data("张三", mode="local", top_k=10)

    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A", "实体B"]
    mock_logger.warning.assert_called_once()


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_failure_status_not_shortcut(mock_get_rag, mock_call_async):
    """T2-6 failure 不短路：status=='failure' → 即使 has_entity True 也原样返回"""
    failure_result = {"status": "failure", "message": "Query returned no results", "data": {}}
    adapter = _make_adapter(mock_get_rag, mock_call_async, failure_result, has_entity=True)

    result = adapter.query_data("张三", mode="local", top_k=10)

    assert result["status"] == "failure"
    assert result["data"] == {}
    adapter.has_entity.assert_not_called()


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_filter_lambda_present_not_shortcut(mock_get_rag, mock_call_async):
    """T2-7 filter_lambda 在场不短路：技能通道契约不绕过"""
    vector_result = _success_result(["实体A"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=True)

    result = adapter.query_data("张三", mode="local", top_k=10, filter_lambda=lambda kw: True)

    adapter.has_entity.assert_not_called()
    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A"]


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_entity_info_error_not_shortcut(mock_get_rag, mock_call_async):
    """T2-8 get_entity_info status=error 不短路 → 原样返回"""
    vector_result = _success_result(["实体A"])
    adapter = _make_adapter(
        mock_get_rag, mock_call_async, vector_result,
        has_entity=True,
        entity_info={"status": "error", "message": "not found"},
    )

    result = adapter.query_data("张三", mode="local", top_k=10)

    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A"]


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_naive_mode_not_shortcut(mock_get_rag, mock_call_async):
    """T2-9 mode 门控：mode='naive' 实体数组契约为空 → 即使 has_entity True 也跳过"""
    vector_result = _success_result(["实体A"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=True)

    result = adapter.query_data("张三", mode="naive", top_k=10)

    adapter.has_entity.assert_not_called()
    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A"]


@patch("niu_api.internal.lightrag_adapter.call_async")
@patch.object(LightRAGAdapter, "_get_rag")
def test_long_name_over_50_chars_not_shortcut(mock_get_rag, mock_call_async):
    """T2-10 长度门控：51 字符实体名（has_entity True）→ 跳过短路"""
    vector_result = _success_result(["实体A"])
    adapter = _make_adapter(mock_get_rag, mock_call_async, vector_result, has_entity=True)
    long_name = "李" * 51

    result = adapter.query_data(long_name, mode="local", top_k=10)

    adapter.has_entity.assert_not_called()  # len>50 门控先于 has_entity
    assert [e["entity_name"] for e in result["data"]["entities"]] == ["实体A"]
