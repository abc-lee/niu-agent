"""F-2 T2 单测：人物改名/合并后，刷新受影响照片的文本层（DB abstract + 照片真实块 + 人物虚拟块）。

全 mock 面（不触真 KG、不触网、不加载人脸模型、不碰 ~/.niu 真实存储）：
- 照片库：`NIU_DB_PATH` 指向临时目录，并复位模块级连接缓存
  `niu_photo_server._conn` / `_db_path_failed`（get_connection 只在
  `_conn is None` 时重连，不复位会指向真实 `~/.niu/work/photos.db`）。
- 工作区：`WORKSPACE_PATH` 指向临时目录。
- adapter 原语：类级 patch `niu_api.internal.lightrag_adapter.LightRAGAdapter`
  （helper 函数内惰性 import 走同一模块属性缝，调用时取到 patch 后的类；
  先例 test_lightrag_delete_chunks.py）。F-3 起旧虚拟块删除受 D-3' 精确等值
  守卫约束：`get_chunk_by_source_id` 默认返 None（块不存在 ⇒ 静默跳过）；
  需要"守卫放行、旧块照删"的正常态用例，显式把该 sid 的簿记块内容
  （`名: 名（person_id=…）`）喂给 `get_chunk_by_source_id`。
- KG registry：patch `agent.tool_registry.get_registry`（registry.get 一律
  返回 None ⇒ name_person/merge_persons 的 KG 段降级为"工具缺失"只记
  kg_errors/告警，且 helper 自身禁用 registry，严格计数断言不受影响）；
  patch `niu_photo_server._entity_exists_in_kg` → False。

用例（与方案 §2 T2 ①-⑥ 对应）：
① 改名路径按当前人物重写真实块（新名在、旧名不在、其余行逐字保留）；
② 合并路径同样 + 两张照片的 DB abstract 都刷新；
③ 人物虚拟块换名（删旧 source_id、写新 source_id 含新名）；
④ 一张照片抛异常 ⇒ 其余照片仍处理、errors 记一条、status 不翻转；
⑤ 无照片 ⇒ 计数 0 不抛、adapter 原语零调用；
⑥ 取名口径：name 以「未命名人物」开头 ⇒ 取 auto_label（与生成侧一致，
   显式点名场景下 `name or auto_label` 会算出不同旧名、旧虚拟块删不中）。
T2 微修口径（质量审查 1×P2 + 5×P3）：F-1 人物行 strip 防人名泄漏进地点位；
F-2 人物名按名保序去重（与生成侧同口径）；F-3 删块返回检查；
F-4 循环外一次性 commit；F-5 upsert 恒 error 时计数不增、errors 记录、
status 不翻转。
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
    """模块级连接缓存必须测试间复位（同 test_photo_media_ingest.py）。"""
    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False


@pytest.fixture()
def photo_env(tmp_path, monkeypatch):
    """照片库/工作区全隔离（photo_env 模式）。"""
    _reset_photo_module_state()
    monkeypatch.setenv("NIU_DB_PATH", str(tmp_path / "photos.db"))
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path / "workspace"))
    yield tmp_path
    _reset_photo_module_state()


@pytest.fixture()
def kg_mocks():
    """类级 patch adapter 原语 + KG registry 降级（helper 禁用 registry）。"""
    mock_adapter_cls = MagicMock()
    adapter = mock_adapter_cls.return_value
    adapter.get_chunk_by_source.return_value = None
    # F-3 D-3' 守卫：默认「块不存在」（静默跳过、不计错）；需要"守卫放行、
    # 旧虚拟块照删"的正常态用例，显式把簿记块内容喂给 get_chunk_by_source_id
    adapter.get_chunk_by_source_id.return_value = None
    adapter.delete_chunks_by_source_id.return_value = {
        "status": "ok", "deleted_chunks": 0, "deleted_vectors": 0, "matched_ids": [],
    }
    adapter.upsert_chunks.return_value = {
        "status": "ok", "entities": 0, "relationships": 0, "chunks": 1,
    }
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter", mock_adapter_cls), \
            patch("agent.tool_registry.get_registry", return_value=mock_registry), \
            patch("niu_photo_server._entity_exists_in_kg", return_value=False):
        yield {"adapter": adapter, "adapter_cls": mock_adapter_cls, "registry": mock_registry}


def _norm(file_path: str) -> str:
    """与 format_photo_ingest_data / helper 同一把归一化键（正斜杠 + 小写）。"""
    return file_path.replace("\\", "/").lower()


def _seed_person_photo(
    env, person_id, name, auto_label, photo_id, file_name,
    old_abstract, taken_at=TAKEN_AT, seed_person=True,
):
    conn = niu_photo_server.get_connection()
    if seed_person:
        conn.execute(
            "INSERT INTO persons (id, name, auto_label, photo_count) VALUES (?, ?, ?, 1)",
            (person_id, name, auto_label),
        )
    file_path = str(Path(env) / file_name)
    conn.execute(
        "INSERT INTO photos (id, file_path, taken_at, abstract, kg_synced) VALUES (?, ?, ?, ?, 1)",
        (photo_id, file_path, taken_at, old_abstract),
    )
    conn.execute(
        "INSERT INTO faces (id, photo_id, person_id) VALUES (?, ?, ?)",
        (f"f_{photo_id}", photo_id, person_id),
    )
    conn.commit()
    return file_path


def _chunk_dict(stem: str, content: str, norm_path: str) -> dict:
    return {"content": content, "file_path": norm_path, "source_id": norm_path}


def _virtual_bookkeeping(kg_name: str, person_id: str) -> dict:
    """入库侧逐照片簿记块的正常形态：`名: 名（person_id=<该人自己的 id>）`。
    F-3 D-3' 守卫正常态夹具：把该 dict 喂给 get_chunk_by_source_id ⇒ 守卫放行、
    旧虚拟块照删。"""
    return {"content": f"{kg_name}: {kg_name}（person_id={person_id}）", "source_id": ""}


def _upsert_contents(adapter) -> list:
    return [call.args[0][0]["content"] for call in adapter.upsert_chunks.call_args_list]


def _upsert_source_ids(adapter) -> list:
    return [call.args[0][0]["source_id"] for call in adapter.upsert_chunks.call_args_list]


# ---------------------------------------------------------------------------
# ⑥ 取名口径（共用函数直接判 + 改名链路端到端）
# ---------------------------------------------------------------------------

class TestKgPersonName:
    """⑥ 三条件口径：空 / 「未命名人物」前缀 / name == auto_label ⇒ auto_label。"""

    def test_three_conditions(self):
        assert _kg_person_name("", "auto_1") == "auto_1"
        assert _kg_person_name(None, "auto_2") == "auto_2"
        assert _kg_person_name("未命名人物_9", "auto_9") == "auto_9"
        assert _kg_person_name("同名", "同名") == "同名"
        assert _kg_person_name("真名", "auto_x") == "真名"
        assert _kg_person_name("", None) == ""

    def test_explicit_unnamed_prefix_uses_auto_label_end_to_end(self, photo_env, kg_mocks):
        """name 以「未命名人物」开头且 != auto_label ⇒ 旧虚拟块在 auto_label 名下：
        `name or auto_label` 口径会算出「未命名人物_9」→ 删不中旧块，本用例必红。"""
        file_path = _seed_person_photo(
            photo_env, "p_unnamed", "未命名人物_9", "auto_9",
            "ph_u", "IMG_U.jpg", "未命名人物_9，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _chunk_dict("img_u", "照片 img_u：旧\n实体：img_u, auto_9\n人物：auto_9", p)
            if p == norm else None
        )
        # F-3 守卫正常态：auto_label 名下的旧簿记块存在 ⇒ 守卫放行、照删
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("auto_9", "p_unnamed") if sid == norm + "_auto_9" else None
        )

        result = niu_photo_server.name_person("p_unnamed", "小红")

        assert result["status"] == "success"
        deletes = [call.args[0] for call in adapter.delete_chunks_by_source_id.call_args_list]
        assert norm + "_auto_9" in deletes, "旧虚拟块应按 auto_label 名删（共用口径）"
        assert norm + "_未命名人物_9" not in deletes, "不得按显式 name 删（name or auto_label 陷阱）"
        assert norm + "_小红" in _upsert_source_ids(adapter)


# ---------------------------------------------------------------------------
# ① + ③ 改名路径：真实块重写 + 人物虚拟块换名
# ---------------------------------------------------------------------------

class TestRenameRefresh:
    """① 真实块按当前人物重写（新名在、旧名不在、其余行逐字保留）；
    ③ 人物虚拟块删旧 source_id、写新 source_id 含新名。"""

    def test_rename_rewrites_real_and_virtual_chunks(self, photo_env, kg_mocks):
        file_path = _seed_person_photo(
            photo_env, "p1", None, "未命名人物_7",
            "ph1", "IMG_20240101.jpg", "未命名人物_7，2024年01月01日",
        )
        norm = _norm(file_path)
        old_content = (
            "照片 img_20240101：未命名人物_7，2024年01月01日\n"
            "实体：img_20240101, 北京, 未命名人物_7\n"
            "人物：未命名人物_7\n"
            "地点：北京\n"
            "拍摄时间：2024:01:01 12:00:00\n"
            "相机：iPhone 15\n"
            "类别：生活"
        )
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _chunk_dict("img_20240101", old_content, p) if p == norm else None
        )
        # F-3 守卫正常态：旧名簿记块存在 ⇒ 守卫放行、照删
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_7", "p1") if sid == norm + "_未命名人物_7" else None
        )

        result = niu_photo_server.name_person("p1", "安安")

        # 返回体：计数齐备，KG 段降级不阻断
        assert result["status"] == "success"
        assert result["abstracts_refreshed"] == 1
        assert result["chunks_refreshed"] == 2  # 真实块 + 人物虚拟块
        assert kg_mocks["adapter_cls"].called  # helper 确实直连 adapter

        # ① DB abstract 按当前人物刷新（不再含旧名）
        conn = niu_photo_server.get_connection()
        new_abstract = conn.execute("SELECT abstract FROM photos WHERE id = 'ph1'").fetchone()[0]
        assert new_abstract == f"安安，{DATE_TEXT}"

        # ① 真实块新内容：第 1 行 = 刷新后 abstract，实体/人物行 = 新名，
        #    地点沿用，拍摄时间/相机/类别逐字保留，旧名绝迹
        contents = _upsert_contents(adapter)
        source_ids = _upsert_source_ids(adapter)
        real_content = contents[0]
        assert real_content.split("\n")[0] == f"照片 img_20240101：安安，{DATE_TEXT}"
        assert "实体：img_20240101, 北京, 安安" in real_content
        assert "人物：安安" in real_content
        assert "地点：北京" in real_content
        for kept_line in ("拍摄时间：2024:01:01 12:00:00", "相机：iPhone 15", "类别：生活"):
            assert kept_line in real_content, f"其余行必须逐字保留: {kept_line}"
        assert "未命名人物_7" not in real_content
        assert source_ids[0] == norm and adapter.upsert_chunks.call_args_list[0].args[0][0]["file_path"] == norm

        # ③ 人物虚拟块换名：删旧 source_id（path_旧KG名）、写新 source_id（path_新名）
        deletes = [call.args[0] for call in adapter.delete_chunks_by_source_id.call_args_list]
        assert norm in deletes  # 真实块旧 id
        assert norm + "_未命名人物_7" in deletes  # 旧人物虚拟块
        assert norm + "_安安" in source_ids
        virtual_content = contents[1]
        assert virtual_content == "安安: 安安（person_id=p1）"

    def test_rename_photo_without_old_chunk_reports_skip(self, photo_env, kg_mocks):
        """真实块取不到 ⇒ 跳过该步并计数上报（errors 一条），人物虚拟块仍换名、不抛。"""
        _seed_person_photo(
            photo_env, "p2", None, "未命名人物_2",
            "ph2", "IMG_X.jpg", "未命名人物_2，2024年01月01日",
        )
        kg_mocks["adapter"].get_chunk_by_source.return_value = None  # 取不到

        result = niu_photo_server.name_person("p2", "小明")

        assert result["status"] == "success"
        assert result["abstracts_refreshed"] == 1  # DB 层照常刷
        assert result["chunks_refreshed"] == 1     # 只有人物虚拟块成功
        assert any("真实块取不到" in e for e in result["kg_errors"])


# ---------------------------------------------------------------------------
# ② 合并路径：两张照片的真实块 + DB abstract 全部刷新
# ---------------------------------------------------------------------------

class TestMergeRefresh:
    """② 合并后 person_a 名下全部照片（含刚从 b 迁入的）逐张刷新；
    返回体新增 kg_errors（空列表）+ 两个计数键。"""

    def test_merge_refreshes_both_photos_and_abstracts(self, photo_env, kg_mocks):
        env = photo_env
        pa_path = _seed_person_photo(
            env, "pa", "安安", "auto_a",
            "ph_a", "IMG_20240102.jpg", "安安，2024年01月01日",
        )
        pb_path = _seed_person_photo(
            env, "pb", None, "未命名人物_8",
            "ph_b", "IMG_20240103.jpg", "未命名人物_8，2024年01月01日",
        )
        norm_a, norm_b = _norm(pa_path), _norm(pb_path)
        # 照片 A：带地点行；照片 B：无地点行（验实体行兜底剥掉人物名后无残留）
        old_a = (
            "照片 img_20240102：安安，2024年01月01日\n"
            "实体：img_20240102, 上海, 安安\n"
            "人物：安安\n"
            "地点：上海"
        )
        old_b = (
            "照片 img_20240103：未命名人物_8，2024年01月01日\n"
            "实体：img_20240103, 未命名人物_8\n"
            "人物：未命名人物_8"
        )

        def fake_get_chunk(p):
            if p == norm_a:
                return _chunk_dict("img_20240102", old_a, p)
            if p == norm_b:
                return _chunk_dict("img_20240103", old_b, p)
            return None

        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = fake_get_chunk
        # F-3 守卫正常态（合并：old_person_id=person_b_id，期望串里的 pid 必须是 B 的）：
        # 照片 B 名下有 B 的旧簿记块 ⇒ 放行照删；照片 A 名下无 B 的块 ⇒ 静默跳过
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_8", "pb") if sid == norm_b + "_未命名人物_8" else None
        )

        result = niu_photo_server.merge_persons("pa", "pb")

        assert result["status"] == "success"
        assert result["merged_into"] == "pa"
        assert result["deleted_person_id"] == "pb"
        # 新增返回体：kg_errors（干净路径为空）+ 两个计数键
        assert result["kg_errors"] == []
        assert result["abstracts_refreshed"] == 2
        assert result["chunks_refreshed"] == 4  # 两张照片各：真实块 + 人物虚拟块

        # ② 两张照片的 DB abstract 都刷新（缺口 A：合并路径原先连 DB abstract 都不刷）
        conn = niu_photo_server.get_connection()
        for photo_id in ("ph_a", "ph_b"):
            abstract = conn.execute("SELECT abstract FROM photos WHERE id = ?", (photo_id,)).fetchone()[0]
            assert abstract == f"安安，{DATE_TEXT}", f"{photo_id} 的 abstract 必须刷新"

        # 两张照片的真实块都指向保留的人物名；照片 B 无地点行 ⇒ 兜底剥离后无地点残留
        contents = _upsert_contents(adapter)
        srcs = _upsert_source_ids(adapter)
        real_by_src = dict(zip(srcs, contents, strict=True))
        assert "实体：img_20240102, 上海, 安安" in real_by_src[norm_a]
        assert "地点：上海" in real_by_src[norm_a]
        assert "未命名人物_8" not in real_by_src[norm_a]
        assert "实体：img_20240103, 安安" in real_by_src[norm_b]
        assert "地点：" not in real_by_src[norm_b]
        assert "未命名人物_8" not in real_by_src[norm_b]

        # ③ 两张照片的人物虚拟块都换到保留人名下
        deletes = [call.args[0] for call in adapter.delete_chunks_by_source_id.call_args_list]
        # 旧虚拟块删除受 F-3 D-3' 守卫：照片 B 名下有 B 的簿记块（内容里的 pid
        # 是 pb 自己的，调用点补传 old_person_id=person_b_id 后守卫放行）⇒ 照删；
        # 照片 A 名下无 B 的块 ⇒ 静默跳过、不发起删除调用
        assert norm_a + "_未命名人物_8" not in deletes
        assert norm_b + "_未命名人物_8" in deletes  # 照片 B 的旧虚拟块（被并入）
        assert norm_a + "_安安" in srcs and norm_b + "_安安" in srcs


# ---------------------------------------------------------------------------
# ④ 逐张异常隔离 + ⑤ 无照片零计数
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    """④ 一张照片抛异常 ⇒ 其余照片仍处理、errors 记一条、status 不翻转。"""

    def test_one_photo_failure_isolates_and_reports(self, photo_env, kg_mocks):
        env = photo_env
        pa_path = _seed_person_photo(
            env, "p1", None, "未命名人物_1",
            "ph1", "IMG_A.jpg", "未命名人物_1，2024年01月01日",
        )
        pb_path = _seed_person_photo(
            env, "p1", None, "未命名人物_1",
            "ph2", "IMG_B.jpg", "未命名人物_1，2024年01月01日",
            seed_person=False,
        )
        norm_a, norm_b = _norm(pa_path), _norm(pb_path)

        def fake_get_chunk(p):
            if p == norm_a:
                raise RuntimeError("boom")
            if p == norm_b:
                return _chunk_dict("img_b", "照片 img_b：旧\n实体：img_b, 未命名人物_1", p)
            return None

        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = fake_get_chunk
        # F-3 守卫正常态：健康照片 B 名下有簿记块 ⇒ 守卫放行、旧块照删
        # （异常照片 A 在真实块步就抛了，走不到虚拟块步）
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_1", "p1") if sid == norm_b + "_未命名人物_1" else None
        )

        result = niu_photo_server.name_person("p1", "圆圆")

        assert result["status"] == "success"  # 异常隔离，status 不翻转
        assert result["abstracts_refreshed"] == 2  # 两张照片的 DB 层都刷了
        assert result["chunks_refreshed"] == 2     # 只有健康照片的 真实块+虚拟块
        assert any("boom" in e for e in result["kg_errors"])
        assert len([e for e in result["kg_errors"] if "boom" in e]) == 1

        conn = niu_photo_server.get_connection()
        for photo_id in ("ph1", "ph2"):
            abstract = conn.execute("SELECT abstract FROM photos WHERE id = ?", (photo_id,)).fetchone()[0]
            assert abstract == f"圆圆，{DATE_TEXT}"
        contents = _upsert_contents(adapter)
        assert any("img_a" in c for c in contents) is False  # 异常照片没写任何块
        assert any("圆圆" in c and "img_b" in c for c in contents)  # 健康照片照常

    def test_no_photos_zero_counts_no_adapter_calls(self, photo_env, kg_mocks):
        """⑤ 无照片 ⇒ 计数 0 不抛，adapter 三原语零调用。"""
        _seed_person_photo(
            photo_env, "p_solo", "独行", "auto_solo",
            "ph_solo", "IMG_SOLO.jpg", "独行，2024年01月01日",
        )
        # 把该人物的 face 行删掉 ⇒ 人名下无照片
        conn = niu_photo_server.get_connection()
        conn.execute("DELETE FROM faces WHERE person_id = 'p_solo'")
        conn.commit()

        result = niu_photo_server.name_person("p_solo", "独行侠")

        assert result["status"] == "success"
        assert result["abstracts_refreshed"] == 0
        assert result["chunks_refreshed"] == 0
        kg_mocks["adapter_cls"].assert_not_called()

    def test_top_level_guard_returns_zero_on_uniterable_conn(self, kg_mocks):
        """顶层兜底：连接不可迭代（既有测试的裸 MagicMock 形态）⇒ 全 0 计数，不外溢。"""
        fake_conn = MagicMock()
        # fetchall() → object() 不可迭代 ⇒ 「查照片」结果迭代时抛 TypeError
        # （既有用例的裸 MagicMock 连接形态）——没有顶层兜底这会打穿 name_person
        fake_conn.execute.return_value = MagicMock(fetchall=MagicMock(return_value=object()))
        refresh = niu_photo_server._refresh_person_photo_texts(
            "ghost", "旧名", "新名", fake_conn
        )
        assert refresh["abstracts_refreshed"] == 0
        assert refresh["chunks_refreshed"] == 0
        assert len(refresh["errors"]) == 1 and "not iterable" in refresh["errors"][0]
        kg_mocks["adapter_cls"].assert_not_called()


# ---------------------------------------------------------------------------
# T2 微修（F-1/F-2/F-5）：人物行 strip、按名去重、upsert 失败隔离
# ---------------------------------------------------------------------------

class TestTextRefreshMicrofix:
    """微修用例：① 无地点行的两人合影——strip 后人名不得泄漏进地点位；
    ② upsert 恒 error ⇒ 计数不增、errors 记录、status 不翻转；
    ③ 两人合影（有地点行）⇒ 人物行按名去重、不出现同名重复。"""

    def _seed_two_person_photo(self, env, photo_id, file_name, persons):
        """一张照片、两张分属不同人物的脸。persons = [(person_id, name, auto_label), ...]"""
        conn = niu_photo_server.get_connection()
        for pid, name, label in persons:
            conn.execute(
                "INSERT INTO persons (id, name, auto_label, photo_count) VALUES (?, ?, ?, 1)",
                (pid, name, label),
            )
        file_path = str(Path(env) / file_name)
        conn.execute(
            "INSERT INTO photos (id, file_path, taken_at, abstract, kg_synced) VALUES (?, ?, ?, ?, 1)",
            (photo_id, file_path, TAKEN_AT, "占位摘要"),
        )
        for i, (pid, _, _) in enumerate(persons):
            conn.execute(
                "INSERT INTO faces (id, photo_id, person_id) VALUES (?, ?, ?)",
                (f"f_{photo_id}_{i}", photo_id, pid),
            )
        conn.commit()
        return file_path

    def test_group_photo_without_location_line_no_name_leak(self, photo_env, kg_mocks):
        """① F-1：两人合影、旧块**无**地点行、人物行带逗号后空格（「安安, 小明」）。
        不 strip 时「 小明」进不了 skip 集，经实体行兜底被错收进 实体/地点 行。"""
        file_path = self._seed_two_person_photo(
            photo_env, "ph_g", "IMG_GROUP.jpg",
            [("p_an", "安安", "auto_a"), ("p_xm", "小明", "auto_xm")],
        )
        norm = _norm(file_path)
        old_content = (
            "照片 img_group：安安、小明合影，2024年01月01日\n"
            "实体：img_group, 安安, 小明\n"
            "人物：安安, 小明\n"
            "拍摄时间：2024:01:01 12:00:00"
        )
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _chunk_dict("img_group", old_content, p) if p == norm else None
        )
        # F-3 守卫正常态：p_xm 的旧 KG 名是「小明」，其簿记块存在 ⇒ 放行照删
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("小明", "p_xm") if sid == norm + "_小明" else None
        )

        result = niu_photo_server.name_person("p_xm", "小红")

        assert result["status"] == "success"
        assert result["abstracts_refreshed"] == 1
        assert result["chunks_refreshed"] == 2

        contents = _upsert_contents(adapter)
        srcs = _upsert_source_ids(adapter)
        real_content = contents[srcs.index(norm)]
        assert real_content.split("\n")[0] == f"照片 img_group：安安、小红合影，{DATE_TEXT}"
        assert "地点：" not in real_content, f"无地点行的合影不得出现地点行: {real_content}"
        assert "小明" not in real_content, "旧人名不得泄漏进实体/地点位"
        assert "实体：img_group, 安安, 小红" in real_content
        assert "人物：安安, 小红" in real_content
        assert "拍摄时间：2024:01:01 12:00:00" in real_content

    def test_upsert_always_error_counts_not_inflated(self, photo_env, kg_mocks):
        """② F-5：upsert_chunks 恒返回 error dict ⇒ abstracts_refreshed 照常计、
        chunks_refreshed 保持 0、errors 各记一条真实/虚拟块失败、status 不翻转。"""
        file_path = _seed_person_photo(
            photo_env, "p_err", None, "未命名人物_5",
            "ph_e", "IMG_E.jpg", "未命名人物_5，2024年01月01日",
        )
        norm = _norm(file_path)
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _chunk_dict(
                "img_e", "照片 img_e：未命名人物_5，2024年01月01日\n实体：img_e\n人物：未命名人物_5", p
            )
            if p == norm else None
        )
        # F-3 守卫正常态：旧簿记块存在 ⇒ 旧块照删，本用例只验新块 upsert 失败路径
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("未命名人物_5", "p_err") if sid == norm + "_未命名人物_5" else None
        )
        adapter.upsert_chunks.return_value = {"status": "error", "message": "KG 服务不可用"}

        result = niu_photo_server.name_person("p_err", "新新")

        assert result["status"] == "success"          # upsert 失败不翻转 status
        assert result["abstracts_refreshed"] == 1     # DB 层照常刷新
        assert result["chunks_refreshed"] == 0        # error 不得计入成功数
        upsert_errors = [e for e in result["kg_errors"] if "KG 服务不可用" in e]
        assert len(upsert_errors) == 2, f"真实/虚拟块失败各须记一条: {result['kg_errors']}"
        assert any("真实块重写失败" in e for e in upsert_errors)
        assert any("人物虚拟块换名失败" in e for e in upsert_errors)

    def test_same_display_name_dedups_in_person_line(self, photo_env, kg_mocks):
        """③ F-2：两个不同人物显示名相同（张三），旧块有地点行 ⇒ 新块人物行
        按名保序去重（「人物：张三」），不出现「张三, 张三」；abstract 同步收敛
        为单人式（与生成侧同口径）。直接调 helper：name_person 的同名检测会
        need_confirm 拦路，走不到刷新。"""
        file_path = self._seed_two_person_photo(
            photo_env, "ph_d", "IMG_DUP.jpg",
            [("p_a", "张三", "auto_a"), ("p_b", "张三", "auto_b")],
        )
        norm = _norm(file_path)
        old_content = (
            "照片 img_dup：张三，2024年01月01日\n"
            "实体：img_dup, 杭州, 张三\n"
            "人物：张三\n"
            "地点：杭州\n"
            "拍摄时间：2024:01:01 12:00:00"
        )
        adapter = kg_mocks["adapter"]
        adapter.get_chunk_by_source.side_effect = lambda p: (
            _chunk_dict("img_dup", old_content, p) if p == norm else None
        )
        # F-3 守卫正常态：直接调 helper 不传 old_person_id ⇒ 期望 pid 缺省取
        # person_id（p_a）；把 p_a 名下的簿记块喂给守卫 ⇒ 放行、errors 保持空
        adapter.get_chunk_by_source_id.side_effect = lambda sid: (
            _virtual_bookkeeping("张三", "p_a") if sid == norm + "_张三" else None
        )

        conn = niu_photo_server.get_connection()
        refresh = niu_photo_server._refresh_person_photo_texts("p_a", "张三", "张三", conn)

        assert refresh["abstracts_refreshed"] == 1
        assert refresh["chunks_refreshed"] == 2
        assert refresh["errors"] == []
        # abstract 按名去重后收敛为单人式（不再断言「张三、张三合影」）
        assert (
            conn.execute("SELECT abstract FROM photos WHERE id = 'ph_d'").fetchone()[0]
            == f"张三，{DATE_TEXT}"
        )

        contents = _upsert_contents(adapter)
        srcs = _upsert_source_ids(adapter)
        real_content = contents[srcs.index(norm)]
        assert "人物：张三" in real_content
        assert "张三, 张三" not in real_content, "人物行不得出现同名重复"
        assert "张三、张三" not in real_content
        assert "实体：img_dup, 杭州, 张三" in real_content
        assert "地点：杭州" in real_content
