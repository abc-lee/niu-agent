"""T1：照片 EXIF（taken_at/camera/category）落进照片实体描述与 chunk。

背景（方案 docs/superpowers/plans/2026-10-16-photo-exif-into-entity.md v1.3）：
描述里旧的「拍摄于…」日期是从文件名 stem 前 8 位解析的——那是**入库时间**，
不是拍摄时间（截图/下载图无 EXIF，文件名用当前时间生成 ⇒ 描述谎称拍摄时间）。
本组测试钉住新行为：

- taken_at 有值 ⇒ 描述冠「拍摄于」（EXIF 拍摄时间是唯一真值来源；解析失败取
  taken_at[:10] 原文照写，对齐 generate_l0_abstract 口径，仍冠「拍摄于」）；
- taken_at 无值 ⇒ stem 前 8 位解析，冠「入库于」（绝不出现「拍摄于」）；
- chunk 三行「拍摄时间/相机/类别」统一 truthy 判据（有值才写，不写空行/占位符），
  追加在既有行全部之后，顺序 拍摄时间→相机→类别；category=None（测试/脚本
  直调路径）⇒ 不写「类别」行。

三桩（不触网 / 不写真图谱 / 不碰真库）：
1. `niu_photo_server.geocode.reverse_geocode` —— 逆地理是函数体内局部 import
   （`from .geocode import reverse_geocode`），桩必须打在源模块属性上；打
   `niu_photo_server.reverse_geocode` 桩不住、会真发 HTTPS 到高德（本机 key 是活的）。
2. `agent.tool_registry.get_registry` —— MockRegistry 记录
   lightrag_insert_custom_kg 的 chunks 实参供断言；裸 pytest 里 registry 为空
   ⇒ 函数提前返回 error、chunk 无从观测，若恰好有真工具 ⇒ 真写用户图谱。
3. DB 隔离 —— _do_sync_photo_to_kg_sync 的 Step 3 会 get_connection() 执行
   `UPDATE photos SET kg_synced=1` ⇒ 复用 tests/test_photo_media_ingest.py 的
   photo_env 先例：_reset_photo_module_state() 复位连接缓存 + NIU_DB_PATH 指向临时库。
"""
import os
import sys

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_photo_server  # noqa: E402
import niu_photo_server.geocode  # noqa: E402,F401  # 确保 geocode 子模块已装载，桩目标存在


def _reset_photo_module_state():
    """模块级缓存必须测试间复位：连接缓存不复位会指向真实库（photo_env 先例）。"""
    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False
    niu_photo_server._ingest_sessions.clear()


def _photo_description(captured_entities: list[dict]) -> str:
    """取照片实体（entity_type=Photo）的描述。"""
    photos = [e for e in captured_entities if e.get("entity_type") == "Photo"]
    assert len(photos) == 1, f"恰有 1 个 Photo 实体，实际: {[e.get('entity_name') for e in captured_entities]}"
    return photos[0]["description"]


def _chunk_content(kg_env: dict) -> str:
    chunks = kg_env["captured"]["chunks"]
    assert chunks, "lightrag_insert_custom_kg 未被调用（registry 桩失效？）"
    return chunks[0]["content"]


@pytest.fixture()
def kg_env(tmp_path, monkeypatch):
    """DB 隔离 + 逆地理桩 + registry 桩：chunk 断言真跑 _do_sync_photo_to_kg_sync。"""
    _reset_photo_module_state()
    db_file = tmp_path / "photos.db"
    monkeypatch.setenv("NIU_DB_PATH", str(db_file))

    captured = {"chunks": None, "entities": None, "relationships": None}

    class MockRegistry:
        def get(self, name):
            if name == "lightrag-server/lightrag_insert_custom_kg":
                def fake_custom_kg(entities=None, relationships=None, chunks=None, source_id=None, **_kw):
                    captured["chunks"] = chunks
                    captured["entities"] = entities
                    captured["relationships"] = relationships
                    return {"status": "ok"}
                return fake_custom_kg
            return None

    monkeypatch.setattr("agent.tool_registry.get_registry", lambda: MockRegistry())

    # 固定坐标 → 固定地名（桩打源模块属性，见文件头说明）
    monkeypatch.setattr(
        "niu_photo_server.geocode.reverse_geocode",
        lambda lat, lon: "北京市东城区东华门街道",
    )
    # _entity_exists_in_kg 内部走 LightRAGAdapter（可能探测真实图谱），
    # 照 tests/test_lightrag_unified.py 先例直接桩 False
    monkeypatch.setattr(niu_photo_server, "_entity_exists_in_kg", lambda name: False)

    yield {"db_file": db_file, "captured": captured}
    _reset_photo_module_state()


# ---------------------------------------------------------------------------
# ①–⑤ 行为判据（方案 §2 T1）
# ---------------------------------------------------------------------------


def test_01_full_exif_description_and_chunk_lines(kg_env):
    """① taken_at + GPS + camera 齐备 ⇒ 描述含「拍摄于<EXIF 日期>」、逆地理地名、
    相机型号；传 category='工作' ⇒ chunk 含「拍摄时间」「相机」「类别」行。"""
    file_path = "/photos/20240101_120000.jpg"
    exif = {
        "taken_at": "2024:01:01 12:00:00",
        "location": "39.9087,116.3975",
        "camera": "iPhone 14 Pro",
    }
    result = niu_photo_server._do_sync_photo_to_kg_sync(file_path, "测试摘要", [], exif, category="工作")
    assert result["status"] == "success", f"注入失败: {result}"

    desc = _photo_description(kg_env["captured"]["entities"])
    assert "拍摄于2024年01月01日" in desc, f"描述须含 EXIF 拍摄日期: {desc!r}"
    assert "北京市东城区东华门街道" in desc, f"描述须含逆地理地名: {desc!r}"
    assert "iPhone 14 Pro" in desc, f"描述须含相机型号: {desc!r}"
    assert "入库于" not in desc, "有 taken_at 时不得出现「入库于」"

    content = _chunk_content(kg_env)
    assert "拍摄时间：2024:01:01 12:00:00" in content, f"chunk 缺拍摄时间行: {content!r}"
    assert "相机：iPhone 14 Pro" in content, f"chunk 缺相机行: {content!r}"
    assert "类别：工作" in content, f"chunk 缺类别行: {content!r}"
    # 三行追加在既有行全部之后，顺序 拍摄时间→相机→类别
    assert content.index("拍摄时间：") < content.index("相机：") < content.index("类别：")


def test_02_no_exif_ingested_on_not_taken(kg_env):
    """② taken_at 缺失（该用例 exif 三字段全缺）⇒ 描述含「入库于」且不含「拍摄于」；
    chunk 不含「拍摄时间」「相机」行（truthy 判据 ⇒ 不写空行）。"""
    file_path = "/photos/20261016_132941.jpg"
    exif = {"taken_at": None, "location": None, "camera": None}
    result = niu_photo_server._do_sync_photo_to_kg_sync(file_path, "测试摘要", [], exif, category="生活")
    assert result["status"] == "success", f"注入失败: {result}"

    desc = _photo_description(kg_env["captured"]["entities"])
    assert "入库于2026年10月16日" in desc, f"stem 日期须冠「入库于」: {desc!r}"
    assert "拍摄于" not in desc, "无 taken_at 时不得出现「拍摄于」"

    content = _chunk_content(kg_env)
    assert "拍摄时间：" not in content, f"taken_at 缺 ⇒ 不写拍摄时间行: {content!r}"
    assert "相机：" not in content, f"camera 缺 ⇒ 不写相机行: {content!r}"
    assert "类别：生活" in content, "category 有值 ⇒ 类别行照写"


def test_03_taken_at_without_gps_no_coordinates_no_relations(kg_env):
    """③ taken_at 有、GPS 无 ⇒ 描述含「拍摄于」、不含任何坐标/地名；
    relationships 为空（无 location 实体、无「拍摄于」边）。"""
    file_path = "/photos/20240505_080000.jpg"
    exif = {"taken_at": "2024:05:05 08:00:00", "location": None, "camera": None}
    result = niu_photo_server._do_sync_photo_to_kg_sync(file_path, "测试摘要", [], exif)
    assert result["status"] == "success", f"注入失败: {result}"

    desc = _photo_description(kg_env["captured"]["entities"])
    assert "拍摄于2024年05月05日" in desc, f"描述须含「拍摄于」: {desc!r}"
    assert "GPS" not in desc, f"无 GPS 时描述不得含坐标: {desc!r}"
    assert "北京市东城区东华门街道" not in desc, f"无 GPS 时描述不得含地名: {desc!r}"

    assert kg_env["captured"]["relationships"] == [], "无 GPS ⇒ 无 location 实体与「拍摄于」边"


def test_04_unparseable_taken_at_kept_verbatim():
    """④ taken_at 存在但不可解析 ⇒ 描述含「拍摄于2008」/「拍摄于2008070310」
    （taken_at[:10] 口径，对齐 generate_l0_abstract 先例；不足 10 位取全串）。"""
    desc = niu_photo_server._generate_stable_description("20240101_120000", "摘要", None, None, "2008")
    assert "拍摄于2008" in desc, f"不足 10 位取全串原文照写: {desc!r}"
    assert "入库于" not in desc, "有 taken_at 时不得回落到 stem 日期"

    desc = niu_photo_server._generate_stable_description("20240101_120000", "摘要", None, None, "2008070310")
    assert "拍摄于2008070310" in desc, f"≥10 位不可解析值取前 10 字符: {desc!r}"


def test_05_category_none_no_category_line(kg_env):
    """⑤ category 不传（None，模拟测试/脚本直调）⇒ chunk 不含「类别」行（I-3）；
    其余有值行（拍摄时间）不受影响。"""
    file_path = "/photos/20240101_120000.jpg"
    exif = {"taken_at": "2024:01:01 12:00:00", "location": None, "camera": None}
    result = niu_photo_server._do_sync_photo_to_kg_sync(file_path, "测试摘要", [], exif)
    assert result["status"] == "success", f"注入失败: {result}"

    content = _chunk_content(kg_env)
    assert "类别：" not in content, f"category=None ⇒ 不写类别行: {content!r}"
    assert "拍摄时间：2024:01:01 12:00:00" in content, "拍摄时间行与 category 无关，照写"


def test_06_no_date_phrase_when_both_missing():
    """⑥ taken_at 无值且 stem 前 8 位非数字 ⇒ 不写日期短语（兜底分支）：
    返回值恰为「照片 <stem>」，既不含「拍摄于」也不含「入库于」。"""
    desc = niu_photo_server._generate_stable_description("风景照_001", "未检测到人物", None, None, None)
    assert desc == "照片 风景照_001", f"两边日期皆缺 ⇒ 不写日期短语: {desc!r}"
