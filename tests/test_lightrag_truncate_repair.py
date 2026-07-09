"""vdb JSON 截断修复的单元测试。

背景：vdb_entities.json 截断在 vector 字段中间的 base64 时，
_read_data_from_vdb json.load 抛 JSONDecodeError 直接返回 None，
无截断修复逻辑。本测试验证：
1. 截断在 data 数组中间时，_try_truncate_repair 能恢复断点前所有完整 entity
2. 截断在首个对象就截断时（data 数组恢复后为空）返回 None
3. 空 data（无任何完整对象）返回 None
4. content 含 } 字符时（括号配平法应正确识别完整对象边界）
5. vdb_relationships.json 同结构同样适用
"""
import json
from unittest import mock


def _make_valid_vdb(entity_count: int = 5) -> dict:
    """生成一个完整的 vdb_entities.json 结构（含 vector 字段）"""
    import base64
    import zlib
    import numpy as np

    data = []
    vectors = []
    for i in range(entity_count):
        vec = np.array([float(i)] * 8, dtype=np.float16)
        data.append({
            "__id__": f"ent-{i:04x}",
            "entity_name": f"entity_{i}",
            "content": f"这是实体 {i} 的描述",
            "vector": base64.b64encode(zlib.compress(vec.tobytes())).decode(),
        })
        vectors.append(vec)
    matrix_f32 = np.array(vectors, dtype=np.float32)
    return {
        "embedding_dim": 8,
        "data": data,
        "matrix": base64.b64encode(matrix_f32.tobytes()).decode(),
    }


def test_try_truncate_repair_recovers_complete_entities(tmp_path):
    """截断在 data 数组中间时，能恢复断点前所有完整 entity"""
    from niu_api.internal import lightrag_repair

    # 1. 生成完整 vdb
    full_vdb = _make_valid_vdb(entity_count=5)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 2. 截断：在第三个 entity 的 vector 字段中间截断
    full_text = vdb_path.read_text(encoding="utf-8")
    # 找到第三个 entity 的 vector 字段位置（"entity_2" 是第三个 entity 的 entity_name）
    marker = '"entity_2"'  # 第三个 entity 的 entity_name
    marker_pos = full_text.find(marker)
    # 在 marker 之后找 "vector": 子串，落在 vector 值中间截断（保证第三个 entity 不完整）
    vector_pos = full_text.find('"vector":', marker_pos)
    assert vector_pos > 0, "vector 字段必须存在"
    # vector 字段格式："vector": "base64..."，跳过 "vector": " 共 11 字符后在 base64 中间截断
    truncate_pos = vector_pos + len('"vector": "') + 10
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    # 3. monkeypatch _STORAGE_DIR 到 tmp_path
    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        # 4. _read_data_from_vdb 应该失败（JSON 截断）
        data = lightrag_repair._read_data_from_vdb("vdb_entities.json")
        assert data is None, "截断的 vdb 应该 json.load 失败"

        # 5. _try_truncate_repair 应该恢复前两个 entity（断点前完整的）
        truncated_data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert truncated_data is not None, "截断修复应能恢复部分数据"
        # 第三个 entity 被截断，应只恢复前两个
        assert len(truncated_data) == 2
        assert truncated_data[0]["entity_name"] == "entity_0"
        assert truncated_data[1]["entity_name"] == "entity_1"


def test_try_truncate_repair_first_entity_truncated_returns_none(tmp_path):
    """首个对象就截断时（data 数组恢复后为空）返回 None"""
    from niu_api.internal import lightrag_repair

    full_vdb = _make_valid_vdb(entity_count=3)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第一个 entity 的 vector 字段中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"entity_0"'
    marker_pos = full_text.find(marker)
    # 在 marker 之后找 "vector": 子串，在 vector 值中间截断（保证第一个 entity 不完整）
    vector_pos = full_text.find('"vector":', marker_pos)
    assert vector_pos > 0, "vector 字段必须存在"
    truncate_pos = vector_pos + len('"vector": "') + 10  # 在 base64 中间
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert data is None, "首个对象截断应返回 None（data 数组恢复后为空）"


def test_try_truncate_repair_empty_data_returns_none(tmp_path):
    """空 data（无任何完整对象）返回 None"""
    from niu_api.internal import lightrag_repair

    # 构造一个 data 数组只有半截 { 的 vdb
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(
        '{"embedding_dim": 8, "data": [{ "__id__": "ent-0000", "entity_name": "ent',
        encoding="utf-8",
    )

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert data is None, "空 data 应返回 None"


def test_try_truncate_repair_content_with_braces(tmp_path):
    """content 含 } 字符时（括号配平法应正确识别完整对象边界）"""
    from niu_api.internal import lightrag_repair

    import base64
    import zlib
    import numpy as np

    # 构造一个 content 字段含 } 的 vdb（模拟代码片段/JSON 示例）
    data = []
    vectors = []
    for i in range(4):
        vec = np.array([float(i)] * 8, dtype=np.float16)
        # content 含 } 字符（代码片段）
        content_with_brace = f"实体 {i} 代码: {{ 'key': 'val{i}' }} 结束"
        data.append({
            "__id__": f"ent-{i:04x}",
            "entity_name": f"entity_{i}",
            "content": content_with_brace,
            "vector": base64.b64encode(zlib.compress(vec.tobytes())).decode(),
        })
        vectors.append(vec)
    matrix_f32 = np.array(vectors, dtype=np.float32)
    full_vdb = {
        "embedding_dim": 8,
        "data": data,
        "matrix": base64.b64encode(matrix_f32.tobytes()).decode(),
    }
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第三个 entity 的 vector 字段中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"entity_2"'
    marker_pos = full_text.find(marker)
    # 在 marker 之后找 "vector": 子串，在 vector 值中间截断（保证第三个 entity 不完整）
    vector_pos = full_text.find('"vector":', marker_pos)
    assert vector_pos > 0, "vector 字段必须存在"
    truncate_pos = vector_pos + len('"vector": "') + 10  # 在 base64 中间
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        # 括号配平法应正确识别前两个完整对象的边界（不被 content 里的 } 干扰）
        truncated_data = lightrag_repair._try_truncate_repair("vdb_entities.json")
        assert truncated_data is not None, "content 含 } 时截断修复仍应能恢复部分数据"
        assert len(truncated_data) == 2  # 只恢复前两个完整 entity
        assert truncated_data[0]["entity_name"] == "entity_0"
        assert truncated_data[1]["entity_name"] == "entity_1"


def test_try_truncate_repair_relationships_same_logic(tmp_path):
    """vdb_relationships.json 同结构同样适用"""
    from niu_api.internal import lightrag_repair

    import base64
    import zlib
    import numpy as np

    # 生成 3 个关系的 vdb
    data = []
    vectors = []
    for i in range(3):
        vec = np.array([float(i)] * 8, dtype=np.float16)
        data.append({
            "__id__": f"rel-{i:04x}",
            "src_id": f"src_{i}",
            "tgt_id": f"tgt_{i}",
            "content": f"关系 {i} 的描述",
            "vector": base64.b64encode(zlib.compress(vec.tobytes())).decode(),
        })
        vectors.append(vec)
    matrix_f32 = np.array(vectors, dtype=np.float32)
    full_vdb = {
        "embedding_dim": 8,
        "data": data,
        "matrix": base64.b64encode(matrix_f32.tobytes()).decode(),
    }
    vdb_path = tmp_path / "vdb_relationships.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第二个关系 vector 中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"src_1"'
    marker_pos = full_text.find(marker)
    # 在 marker 之后找 "vector": 子串，在 vector 值中间截断（保证第二个关系不完整）
    vector_pos = full_text.find('"vector":', marker_pos)
    assert vector_pos > 0, "vector 字段必须存在"
    truncate_pos = vector_pos + len('"vector": "') + 10  # 在 base64 中间
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        data = lightrag_repair._try_truncate_repair("vdb_relationships.json")
        assert data is not None
        assert len(data) == 1  # 只恢复第一个关系
        assert data[0]["src_id"] == "src_0"


def test_repair_vdb_uses_truncate_repair_when_json_load_fails(tmp_path):
    """repair_vdb 在 _read_data_from_vdb 失败时，应尝试 _try_truncate_repair"""
    from niu_api.internal import lightrag_repair

    full_vdb = _make_valid_vdb(entity_count=4)
    vdb_path = tmp_path / "vdb_entities.json"
    vdb_path.write_text(json.dumps(full_vdb, ensure_ascii=False), encoding="utf-8")

    # 截断在第二个 entity vector 中间
    full_text = vdb_path.read_text(encoding="utf-8")
    marker = '"entity_1"'
    marker_pos = full_text.find(marker)
    # 在 marker 之后找 "vector": 子串，在 vector 值中间截断（保证第二个 entity 不完整）
    vector_pos = full_text.find('"vector":', marker_pos)
    assert vector_pos > 0, "vector 字段必须存在"
    truncate_pos = vector_pos + len('"vector": "') + 10  # 在 base64 中间
    truncated_text = full_text[:truncate_pos]
    vdb_path.write_text(truncated_text, encoding="utf-8")

    with mock.patch.object(lightrag_repair, "_STORAGE_DIR", tmp_path):
        # mock _embed_text 返回固定向量（避免依赖真实 embedding 模型）
        def mock_embed(text):
            return [0.1] * 8
        with mock.patch.object(lightrag_repair, "_embed_text", mock_embed):
            result = lightrag_repair.repair_vdb("vdb_entities.json")
            assert result["status"] == "ok"
            assert result["rebuilt_count"] == 1  # 只恢复第一个 entity
            assert result["source"] == "vdb_truncate_repair"
