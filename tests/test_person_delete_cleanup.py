"""F-3 T2 单测：delete_person 簿记清理接线 + _refresh_person_photo_texts 移除模式。

全 mock 面（不触真 KG、不触网、不加载人脸模型、不碰 ~/.niu 真实存储）：
- 照片库：``NIU_DB_PATH`` 指向临时目录，并复位模块级连接缓存
  ``niu_photo_server._conn`` / ``_db_path_failed``（get_connection 只在
  ``_conn is None`` 时重连，不复位会指向真实 ``~/.niu/work/photos.db``）。
- 工作区：``WORKSPACE_PATH`` 指向临时目录。
- adapter 原语：类级 patch ``niu_api.internal.lightrag_adapter.LightRAGAdapter``
  （photo-server 侧函数内惰性 import 走同一模块属性缝，调用时取到 patch 后的类；
  类级 patch 在本侧成立——被测的是 delete_person/helper 逻辑，mock 的是
  adapter 原语，不是被测方法本身）。F-3 起旧虚拟块删除受 D-3' 精确等值
  守卫约束：``get_chunk_by_source_id`` 默认返 None（块不存在 ⇒ 静默跳过）；
  需要"守卫放行、旧块照删"的正常态，显式把该 sid 的簿记块内容喂进去。
- KG registry：patch ``agent.tool_registry.get_registry``，
  ``lightrag_delete_entity`` 默认在 registry 里（可逐个用例改为缺失/抛异常）。

用例（方案 §2/T2 ①–⑫）：
① 移除模式 helper：删旧虚拟块（守卫放行）、**不写**新块、真实块不含该人名、
   无人物时无 ``人物：`` 行、abstract 重算为 ``未检测到人物，<日期>``；
② photo_rows 显式传入时不发起"受影响照片集合"DB 查询（裸 MagicMock 连接可跑通）；
③ 多人照片删一人 ⇒ 另一人仍在 ``人物：``/``实体：`` 行、abstract = 另一人名；
④ 端到端：delete_person 计数正确、kg_errors 空、KG 名取自 _kg_person_name；
⑤ 某步抛异常 ⇒ 其余继续、status 恒 success、kg_errors 记一条；
⑥ 取名口径：name 以「未命名人物」开头 ⇒ 删的是 auto_label 名下的一切；
⑦ 无照片的人物 ⇒ 计数 0、不抛；
⑧ registry 无 lightrag_delete_entity ⇒ 记 kg_errors 且簿记清理仍执行；
⑨ D-3' 精确等值守卫（两处都测）：delete_person 路径——(a) 带描述的知识块 /
   (b) 同名的另一个人（person_id 不同）⇒ 不删、记 kg_errors；helper 移除模式
   路径——(c) 裸 ``名: 名``（pid 为空的老块）⇒ 不删、记 kg_errors；
   内容逐字等于期望串才删（①⑪ 即正向对照）；
⑩ 合并模式 pid 归属：调用点传 old_person_id=person_b_id ⇒ B 的旧块（内容里是
   B 自己的 id）被正确删掉；只传 A 的 person_id（错法）⇒ 不命中；
⑪ photo_texts_refreshed 口径：真实块+旧虚拟块都在 ⇒ ==1（只数真实块重写）；
   变体：只有虚拟块 ⇒ 虚拟块照删而 ==0；
⑫ Person not found 早退：status=error 且三个计数字段 0/空（返回体形状统一）。
"""


import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_photo_server  # noqa: E402
from niu_photo_server import _kg_person_name  # noqa: E402

TAKEN_AT = "2024:01:01 12:00:00"
# generate_l0_abstract 对 TAKEN_AT 的格式化结果（"%Y:%m:%d %H:%M:%S" → "%Y年%m月%d日"）
DATE_TEXT = "2024年01月01日"


def _reset_photo_module_state():
    """模块级连接缓存必须测试间复位（同 test_photo_delete_cleanup.py）。"""
    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False


@pytest.fixture()
def person_env(tmp_path, monkeypatch):
    """照片库/工作区全隔离（photo_env 模式）。"""
    _reset_photo_module_state()
    monkeypatch.setenv("NIU_DB_PATH", str(tmp_path / "photos.db"))
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path / "workspace"))
    yield tmp_path
    _reset_photo_module_state()


@pytest.fixture()
def kg_mocks():
    """类级 patch adapter 原语 + 可控 registry（lightrag_delete_entity 默认在册）。"""
    mock_adapter_cls = MagicMock()
    adapter = mock_adapter_cls.return_value
    adapter.get_chunk_by_source.return_value = None
    # F-3 D-3' 守卫：默认「块不存在」（静默跳过、不计错）；正常态用例显式喂簿记块
    adapter.get_chunk_by_source_id.return_value = None
    adapter.delete_chunks_by_source_id.return_value = {
        "status": "ok", "deleted_chunks": 1, "deleted_vectors": 1, "matched_ids": ["chunk-x"],
    }
    adapter.delete_bookkeeping_chunks.return_value = {
        "status": "ok", "deleted_chunks": 0, "deleted_vectors": 0, "matched_ids": [],
    }
    adapter.upsert_chunks.return_value = {
        "status": "ok", "entities": 0, "relationships": 0, "chunks": 1,
    }
    delete_entity_fn = MagicMock(return_value={"status": "ok"})
    mock_registry = MagicMock()
    mock_registry.get.side_effect = lambda name: (
        delete_entity_fn if name == "lightrag-server/lightrag_delete_entity" else None
    )
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", mock_adapter_cls), \
            patch("agent.tool_registry.get_registry", return_value=mock_registry):
        yield {
            "adapter": adapter,
            "adapter_cls": mock_adapter_cls,
            "registry": mock_registry,
            "delete_entity_fn": delete_entity_fn,
        }


def _norm(file_path: str) -> str:
    """与 format_photo_ingest_data / helper 同一把归一化键（正斜杠 + 小写）。"""
    return file_path.replace("\\", "/").lower()


def _seed_person(env, person_id, name, auto_label):
    conn = niu_photo_server.get_connection()
    conn.execute(
        "INSERT INTO persons (id, name, auto_label, photo_count) VALUES (?, ?, ?, 0)",
        (person_id, name, auto_label),
    )
    conn.commit()


def _seed_photo(env, photo_id, file_name, person_ids=(), taken_at=TAKEN_AT, abstract="占位摘要"):
    """照片 + 各人物的 face 行；文件不落盘（文件存在与否不影响簿记清理路径）。"""
    conn = niu_photo_server.get_connection()
    file_path = str(Path(env) / file_name)
    conn.execute(
        "INSERT INTO photos (id, file_path, taken_at, abstract, kg_synced) VALUES (?, ?, ?, ?, 1)",
        (photo_id, file_path, taken_at, abstract),
    )
    for i, pid in enumerate(person_ids):
        conn.execute(
            "INSERT INTO faces (id, photo_id, person_id) VALUES (?, ?, ?)",
            (f"f_{photo_id}_{i}", photo_id, pid),
        )
    conn.commit()
    return file_path


def _virtual_bookkeeping(kg_name: str, person_id: str) -> dict:
    """入库侧逐照片簿记块的正常形态：`名: 名（person_id=<该人自己的 id>）`。
    喂给 get_chunk_by_source_id ⇒ 守卫放行、旧虚拟块照删。"""
    return {"content": f"{kg_name}: {kg_name}（person_id={person_id}）", "source_id": ""}


def _real_chunk(content: str, norm_path: str) -> dict:
    return {"content": content, "file_path": norm_path, "source_id": norm_path}


def _deleted_sids(adapter) -> list:
    return [call.args[0] for call in adapter.delete_chunks_by_source_id.call_args_list]


def _upsert_dicts(adapter) -> list:
    return [call.args[0][0] for call in adapter.upsert_chunks.call_args_list]


# ---------------------------------------------------------------------------
# ① 移除模式 helper：删旧虚拟块、不写新块、真实块/abstract 按删后现状刷新
# ---------------------------------------------------------------------------

class TestRemoveModeHelper:
    """① 移除模式（new_kg_name=None）：旧虚拟块经 D-3' 守卫放行照删、**不写**
    新块（I-4）、真实块按删后人物集合重写（无人物时不再写 人物： 行）、
    DB abstract 重算为「未检测到人物，<日期>」。"""

    def test_remove_mode_deletes_old_virtual_writes_no_new(self, person_env, kg_mocks):
        _seed_person(person_env, "p1", None, "未命名人物_1")
        file_path = _seed_photo(
            person_env, "ph1", "IMG_20240101.jpg", ("p1",),
            abstract="未命名人物_1，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk(
                "照片 img_20240101：未命名人物_1，2024年01月01日\n"
                "实体：img_20240101, 北京, 未命名人物_1\n"
                "人物：未命名人物_1\n"
                "地点：北京\n"
                "拍摄时间：2024:01:01 12:00:00",
                p,
            ) if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_1", "p1") if sid == norm + "_未命名人物_1" else None
        )

        # 模拟 delete_person 已删 faces 行的 DB 现状（helper 经显式 photo_rows 刷新）
        conn = niu_photo_server.get_connection()
        conn.execute("DELETE FROM faces WHERE person_id = 'p1'")
        conn.commit()
        refresh = niu_photo_server._refresh_person_photo_texts(
            "p1", "未命名人物_1", None, conn,
            photo_rows=[("ph1", file_path, TAKEN_AT)],
        )

        # I-4：移除模式不写新虚拟块——upsert 恰好一次，且是真实块
        written = _upsert_dicts(adapter)
        assert len(written) == 1
        assert written[0]["source_id"] == norm
        content = written[0]["content"]
        assert "未命名人物_1" not in content, "真实块不得再含被删人名"
        assert "人物：" not in content, "照片已无人物 ⇒ 不写 人物： 行"
        assert "实体：img_20240101, 北京" in content
        assert "地点：北京" in content
        assert "拍摄时间：2024:01:01 12:00:00" in content, "其余行逐字沿用"

        # 旧虚拟块经守卫放行照删（真实块旧 id 的删除照旧）
        deletes = _deleted_sids(adapter)
        assert norm + "_未命名人物_1" in deletes
        assert norm in deletes

        # 口径：真实块重写计 1，旧虚拟块删除不计入 chunks_refreshed
        assert refresh["chunks_refreshed"] == 1
        assert refresh["errors"] == []
        # DB abstract 重算为「未检测到人物，<日期>」（faces 行已删的如实口径）
        abstract = conn.execute("SELECT abstract FROM photos WHERE id = 'ph1'").fetchone()[0]
        assert abstract == f"未检测到人物，{DATE_TEXT}"

    def test_explicit_photo_rows_skips_photo_set_query(self, kg_mocks):
        """② photo_rows 显式传入 ⇒ 不发起"受影响照片集合"查询（faces 行已删、
        自查必空）；裸 MagicMock 连接也能跑完整流程。"""
        fake_conn = MagicMock()
        adapter = kg_mocks["adapter"]
        norm = "/tmp/x/img_20240101.jpg"
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk(
                "照片 img_20240101：未命名人物_1，2024年01月01日\n"
                "实体：img_20240101, 未命名人物_1\n人物：未命名人物_1",
                p,
            ) if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_1", "p1") if sid == norm + "_未命名人物_1" else None
        )
        refresh = niu_photo_server._refresh_person_photo_texts(
            "p1", "未命名人物_1", None, fake_conn,
            photo_rows=[("ph1", norm, TAKEN_AT)],
        )
        assert refresh["abstracts_refreshed"] == 1
        assert refresh["chunks_refreshed"] == 1
        assert refresh["errors"] == []
        executed = [call.args[0] for call in fake_conn.execute.call_args_list]
        assert not any("FROM faces f JOIN photos p" in sql for sql in executed), (
            "显式 photo_rows ⇒ 不得发起受影响照片集合查询"
        )


# ---------------------------------------------------------------------------
# ③④⑤⑥⑦⑧ delete_person 端到端
# ---------------------------------------------------------------------------

class TestDeletePersonEndToEnd:
    """③–⑧ 端到端：计数口径、异常隔离、取名口径、无照片、registry 缺失。"""

    def test_multi_person_photo_keeps_other_person(self, person_env, kg_mocks):
        """③ 两人合影删一人 ⇒ 另一人仍在 实体/人物 行、abstract = 另一人名。"""
        _seed_person(person_env, "p_an", "安安", "auto_a")
        _seed_person(person_env, "p_xm", "小明", "auto_xm")
        file_path = _seed_photo(
            person_env, "ph1", "IMG_GROUP.jpg", ("p_an", "p_xm"),
            abstract="安安、小明合影，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk(
                "照片 img_group：安安、小明合影，2024年01月01日\n"
                "实体：img_group, 安安, 小明\n人物：安安, 小明",
                p,
            ) if p == norm else None
        )

        result = niu_photo_server.delete_person("p_an")

        assert result["status"] == "success"
        assert result["person_id"] == "p_an"
        assert result["kg_errors"] == []
        written = _upsert_dicts(adapter)
        real = [c for c in written if c["source_id"] == norm]
        assert len(real) == 1
        assert "人物：小明" in real[0]["content"]
        assert "实体：img_group, 小明" in real[0]["content"]
        assert "安安" not in real[0]["content"], "被删人名不得残留"
        conn = niu_photo_server.get_connection()
        abstract = conn.execute("SELECT abstract FROM photos WHERE id = 'ph1'").fetchone()[0]
        assert abstract == f"小明，{DATE_TEXT}", "abstract 必须按删后现状（剩余人物）重算"
        # DB 侧：人物行与 faces 行已删
        assert conn.execute("SELECT COUNT(*) FROM persons WHERE id = 'p_an'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id = 'p_an'").fetchone()[0] == 0

    def test_end_to_end_counts_and_kg_name(self, person_env, kg_mocks):
        """④ 端到端：逐照片簿记块 + 声明族簿记块各 1 块被删 ⇒ kg_chunks_deleted=2；
        photo_texts_refreshed=1（真实块重写）；kg_errors 空；KG 名取自
        _kg_person_name（name 为空 ⇒ auto_label，图谱实体删除用同一把名）。"""
        _seed_person(person_env, "p3", None, "未命名人物_3")
        file_path = _seed_photo(
            person_env, "ph3", "IMG_20240103.jpg", ("p3",),
            abstract="未命名人物_3，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk(
                "照片 img_20240103：未命名人物_3，2024年01月01日\n"
                "实体：img_20240103, 未命名人物_3\n人物：未命名人物_3",
                p,
            ) if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_3", "p3") if sid == norm + "_未命名人物_3" else None
        )
        adapter.delete_bookkeeping_chunks.return_value = {
            "status": "ok", "deleted_chunks": 1, "deleted_vectors": 1, "matched_ids": ["rename_x"],
        }

        result = niu_photo_server.delete_person("p3")

        assert result["status"] == "success"
        assert result["kg_errors"] == []
        assert result["kg_chunks_deleted"] == 2, "逐照片簿记块 1 + 声明族簿记块 1"
        assert result["photo_texts_refreshed"] == 1, "只数真实块重写成功数"
        # I-1：KG 名来自 _kg_person_name（name 空 ⇒ auto_label）
        assert _kg_person_name(None, "未命名人物_3") == "未命名人物_3"
        kg_mocks["delete_entity_fn"].assert_called_once_with(entity_name="未命名人物_3")
        adapter.delete_bookkeeping_chunks.assert_called_once_with("未命名人物_3")

    def test_step_exception_isolates_and_keeps_status_success(self, person_env, kg_mocks):
        """⑤ 声明族清理抛异常 ⇒ 记一条 kg_errors，其余清理照做（逐照片簿记块
        已删、照片文本层照常刷新），status 恒 success。"""
        _seed_person(person_env, "p4", None, "未命名人物_4")
        file_path = _seed_photo(
            person_env, "ph4", "IMG_20240104.jpg", ("p4",),
            abstract="未命名人物_4，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_20240104：未命名人物_4\n实体：img_20240104, 未命名人物_4\n人物：未命名人物_4", p)
            if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_4", "p4") if sid == norm + "_未命名人物_4" else None
        )
        adapter.delete_bookkeeping_chunks.side_effect = RuntimeError("声明族清理炸了")

        result = niu_photo_server.delete_person("p4")

        assert result["status"] == "success", "任何失败不得翻转 status"
        assert len(result["kg_errors"]) == 1
        assert "声明族清理炸了" in result["kg_errors"][0]
        assert result["kg_chunks_deleted"] == 1, "异常之前的逐照片清理照常计数"
        assert result["photo_texts_refreshed"] == 1, "异常之后的文本层刷新照常执行"

    def test_unnamed_prefix_name_deletes_auto_label(self, person_env, kg_mocks):
        """⑥ 取名口径：name 以「未命名人物」开头且 != auto_label ⇒ 簿记清理与
        图谱实体删除都落在 auto_label 名下（两条件取名会算错名、簿记块删不中）。"""
        _seed_person(person_env, "p6", "未命名人物_9", "auto_9")
        file_path = _seed_photo(
            person_env, "ph6", "IMG_U.jpg", ("p6",),
            abstract="未命名人物_9，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("auto_9", "p6") if sid == norm + "_auto_9" else None
        )
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_u：未命名人物_9\n实体：img_u, 未命名人物_9\n人物：未命名人物_9", p)
            if p == norm else None
        )

        result = niu_photo_server.delete_person("p6")

        assert result["status"] == "success"
        assert result["kg_errors"] == []
        assert result["kg_chunks_deleted"] == 1, "auto_label 名下的簿记块被删中"
        kg_mocks["delete_entity_fn"].assert_called_once_with(entity_name="auto_9")
        queried = [call.args[0] for call in adapter.get_chunk_by_source_id.call_args_list]
        assert norm + "_auto_9" in queried
        assert norm + "_未命名人物_9" not in queried, "不得按显式 name 拼 sid"

    def test_person_without_photos_zero_counts(self, person_env, kg_mocks):
        """⑦ 无照片的人物 ⇒ 三个计数全 0、不抛；声明族清理与图谱实体删除仍执行。"""
        _seed_person(person_env, "p7", "孤影", "auto_7")

        result = niu_photo_server.delete_person("p7")

        assert result["status"] == "success"
        assert result["kg_chunks_deleted"] == 0
        assert result["photo_texts_refreshed"] == 0
        assert result["kg_errors"] == []
        kg_mocks["delete_entity_fn"].assert_called_once_with(entity_name="孤影")
        kg_mocks["adapter"].delete_bookkeeping_chunks.assert_called_once_with("孤影")

    def test_registry_missing_delete_entity_logs_but_cleans(self, person_env, kg_mocks):
        """⑧ registry 无 lightrag_delete_entity ⇒ 记 kg_errors 一条，
        簿记清理三段照常执行、status 不翻转。"""
        kg_mocks["registry"].get.side_effect = lambda name: None  # 工具全部缺失
        _seed_person(person_env, "p8", None, "未命名人物_8")
        file_path = _seed_photo(
            person_env, "ph8", "IMG_B.jpg", ("p8",),
            abstract="未命名人物_8，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_b：未命名人物_8\n实体：img_b, 未命名人物_8\n人物：未命名人物_8", p)
            if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_8", "p8") if sid == norm + "_未命名人物_8" else None
        )

        result = niu_photo_server.delete_person("p8")

        assert result["status"] == "success"
        assert result["kg_errors"] == ["lightrag_delete_entity 不在 registry 中，图谱节点未删"]
        assert result["kg_chunks_deleted"] == 1, "簿记清理不受图谱工具缺失影响"
        assert result["photo_texts_refreshed"] == 1


# ---------------------------------------------------------------------------
# ⑨ D-3' 精确等值守卫：内容不等值 ⇒ 保留 + 记错（delete_person 与 helper 各一条）
# ---------------------------------------------------------------------------

class TestGuardKeepsMismatchedBlocks:
    """⑨ <路径>_<名> 处若是带描述的知识块 / 同名另一个人（pid 不同）⇒ 不删、
    记 kg_errors；内容逐字等于期望串才删（①/⑪ 即正向对照）。"""

    def test_delete_person_keeps_knowledge_and_same_name_other(self, person_env, kg_mocks):
        """delete_person 路径：(a) 带描述的知识块、(b) 同名另一个人（pid 不同）
        两块都保留、各记一条 kg_errors，零块被删。"""
        _seed_person(person_env, "p9", "测试甲", "auto_jia")
        fp_a = _seed_photo(person_env, "ph_a", "IMG_A.jpg", ("p9",), abstract="测试甲，2024年01月01日")
        fp_b = _seed_photo(person_env, "ph_b", "IMG_B.jpg", ("p9",), abstract="测试甲，2024年01月01日")
        norm_a, norm_b = _norm(fp_a), _norm(fp_b)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: None  # 无真实块可重写
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            {"content": "测试甲: 测试甲是一位测试人物", "source_id": sid}
            if sid == norm_a + "_测试甲"
            else {"content": "测试甲: 测试甲（person_id=p_other）", "source_id": sid}
            if sid == norm_b + "_测试甲"
            else None
        )

        result = niu_photo_server.delete_person("p9")

        assert result["status"] == "success"
        assert result["kg_chunks_deleted"] == 0, "两块内容都不等值 ⇒ 零块被删"
        deletes = _deleted_sids(adapter)
        assert norm_a + "_测试甲" not in deletes
        assert norm_b + "_测试甲" not in deletes
        assert any("测试甲是一位测试人物" in e for e in result["kg_errors"])
        assert any("person_id=p_other" in e for e in result["kg_errors"])

    def test_helper_remove_mode_rejects_bare_name_block(self, person_env, kg_mocks):
        """helper 移除模式路径：(c) pid 为空的老块内容为裸 ``名: 名`` ⇒
        精确等值不命中 ⇒ 保留 + 记一条 errors（R-9 保守漏删，不误删）。"""
        _seed_person(person_env, "p9", "测试甲", "auto_jia")
        file_path = _seed_photo(
            person_env, "ph_c", "IMG_C.jpg", ("p9",), abstract="测试甲，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_c：测试甲，2024年01月01日\n实体：img_c, 测试甲\n人物：测试甲", p)
            if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            {"content": "测试甲: 测试甲", "source_id": sid} if sid == norm + "_测试甲" else None
        )
        conn = niu_photo_server.get_connection()
        conn.execute("DELETE FROM faces WHERE person_id = 'p9'")
        conn.commit()
        refresh = niu_photo_server._refresh_person_photo_texts(
            "p9", "测试甲", None, conn, photo_rows=[("ph_c", file_path, TAKEN_AT)],
        )
        assert len(refresh["errors"]) == 1
        assert "测试甲: 测试甲" in refresh["errors"][0]
        assert norm + "_测试甲" not in _deleted_sids(adapter), "裸名老块必须保留"
        assert refresh["chunks_refreshed"] == 1, "真实块重写照常、与虚拟块保留互不影响"

    def test_delete_person_deletes_verbatim_matching_block(self, person_env, kg_mocks):
        """正向对照：内容逐字等于 ``名: 名（person_id=本人 id）`` 才删。"""
        _seed_person(person_env, "p9", "测试甲", "auto_jia")
        file_path = _seed_photo(
            person_env, "ph_d", "IMG_D.jpg", ("p9",), abstract="测试甲，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_d：测试甲\n实体：img_d, 测试甲\n人物：测试甲", p)
            if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("测试甲", "p9") if sid == norm + "_测试甲" else None
        )
        result = niu_photo_server.delete_person("p9")
        assert result["kg_chunks_deleted"] == 1
        assert result["kg_errors"] == []
        assert norm + "_测试甲" in _deleted_sids(adapter)


# ---------------------------------------------------------------------------
# ⑩ 合并模式 pid 归属：old_person_id=person_b_id 才能让 B 的旧簿记块命中
# ---------------------------------------------------------------------------

class TestMergeOldPersonIdAttribution:
    """⑩ B 的旧簿记块内容里写的是 **B 自己的** person_id：调用点必须补传
    old_person_id=person_b_id ⇒ 守卫期望串用 B 的 id 才命中；只传幸存者 A 的
    person_id（缺省错法）⇒ 必不命中、B 的簿记块永远删不掉。"""

    def _seed_merge_pair(self, env, norm_b_sid_owner, kg_mocks):
        """A(安安) 与 B(未命名人物_8) 同框一张照片；B 的旧簿记块按 owner pid 合成。"""
        _seed_person(env, "pa", "安安", "auto_a")
        _seed_person(env, "pb", None, "未命名人物_8")
        file_path = _seed_photo(
            env, "ph_m", "IMG_M.jpg", ("pa", "pb"), abstract="安安、未命名人物_8合影，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk(
                "照片 img_m：安安、未命名人物_8合影，2024年01月01日\n"
                "实体：img_m, 安安, 未命名人物_8\n人物：安安, 未命名人物_8",
                p,
            ) if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_8", norm_b_sid_owner)
            if sid == norm + "_未命名人物_8" else None
        )
        return norm, adapter

    def test_wrong_pid_does_not_match_b_block(self, person_env, kg_mocks):
        """错法：直接调 helper 且旧块 pid 是 B 的、期望串却用 A 的 person_id
        （不传 old_person_id）⇒ 守卫不命中 ⇒ B 的块保留、记一条 errors。"""
        norm, adapter = self._seed_merge_pair(person_env, "pb", kg_mocks)
        conn = niu_photo_server.get_connection()
        conn.execute("DELETE FROM faces WHERE person_id = 'pb'")
        conn.commit()
        refresh = niu_photo_server._refresh_person_photo_texts(
            "pa", "未命名人物_8", "安安", conn,
            photo_rows=[("ph_m", norm, TAKEN_AT)],  # 不传 old_person_id ⇒ 缺省用 pa
        )
        assert norm + "_未命名人物_8" not in _deleted_sids(adapter), "A 的 id 匹配不了 B 的块"
        assert len(refresh["errors"]) == 1
        assert "person_id=pb" in refresh["errors"][0]

    def test_merge_persons_passes_person_b_id_and_deletes_b_block(self, person_env, kg_mocks):
        """正法：merge_persons 调用点补传 old_person_id=person_b_id ⇒ B 的旧块
        （内容含 B 自己的 id）被正确删掉，kg_errors 干净。"""
        norm, _adapter = self._seed_merge_pair(person_env, "pb", kg_mocks)
        result = niu_photo_server.merge_persons("pa", "pb")
        assert result["status"] == "success"
        assert result["kg_errors"] == []
        deletes = _deleted_sids(kg_mocks["adapter"])
        assert norm + "_未命名人物_8" in deletes, "B 的簿记块必须被删中"
        # 合并后照片真实块只含保留人名
        written = _upsert_dicts(kg_mocks["adapter"])
        real = [c for c in written if c["source_id"] == norm]
        assert len(real) == 1
        assert "人物：安安" in real[0]["content"]
        assert "未命名人物_8" not in real[0]["content"]


# ---------------------------------------------------------------------------
# ⑪ photo_texts_refreshed 口径（R7/G-1）
# ---------------------------------------------------------------------------

class TestPhotoTextsRefreshedSemantics:
    """⑪ 移除模式下 photo_texts_refreshed = 真实块重写成功数：旧虚拟块删除
    **不计入**该字段；变体——真实块不存在时虚拟块照删而计数为 0。"""

    def test_both_real_and_virtual_present_counts_one(self, person_env, kg_mocks):
        _seed_person(person_env, "p11", None, "未命名人物_11")
        file_path = _seed_photo(
            person_env, "ph11", "IMG_K.jpg", ("p11",), abstract="未命名人物_11，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _real_chunk("照片 img_k：未命名人物_11\n实体：img_k, 未命名人物_11\n人物：未命名人物_11", p)
            if p == norm else None
        )
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_11", "p11") if sid == norm + "_未命名人物_11" else None
        )
        result = niu_photo_server.delete_person("p11")
        assert result["photo_texts_refreshed"] == 1, "真实块+旧虚拟块都在 ⇒ 只数真实块重写"
        assert norm + "_未命名人物_11" in _deleted_sids(adapter), "旧虚拟块照删（只是不计入该字段）"

    def test_virtual_only_still_deletes_but_counts_zero(self, person_env, kg_mocks):
        """变体：真实块不存在（get_chunk_by_source 取不到）⇒ 真实块重写缺失、
        计数 0，但旧虚拟块仍经守卫照删。"""
        _seed_person(person_env, "p11", None, "未命名人物_11")
        file_path = _seed_photo(
            person_env, "ph11", "IMG_K.jpg", ("p11",), abstract="未命名人物_11，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.return_value = None  # 真实块取不到
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_11", "p11") if sid == norm + "_未命名人物_11" else None
        )
        result = niu_photo_server.delete_person("p11")
        assert result["status"] == "success"
        assert result["photo_texts_refreshed"] == 0, "真实块缺失 ⇒ 无重写成功可计"
        assert norm + "_未命名人物_11" in _deleted_sids(adapter), "虚拟块仍照删"
        assert any("真实块取不到" in e for e in result["kg_errors"])


# ---------------------------------------------------------------------------
# ⑫ Person not found 早退：返回体形状统一（R7/G-3）
# ---------------------------------------------------------------------------

class TestPersonNotFoundEarlyReturn:
    """⑫ 人物不存在 ⇒ status=error 且三个计数字段按 0/空补齐（返回体形状统一），
    绝不触碰 adapter 与 registry。"""

    def test_early_return_has_zero_fields_and_no_side_effects(self, person_env, kg_mocks):
        result = niu_photo_server.delete_person("no_such_person")
        assert result["status"] == "error"
        assert result["message"] == "Person not found: no_such_person"
        assert result["kg_chunks_deleted"] == 0
        assert result["photo_texts_refreshed"] == 0
        assert result["kg_errors"] == []
        kg_mocks["adapter_cls"].assert_not_called()
        assert not kg_mocks["delete_entity_fn"].called
