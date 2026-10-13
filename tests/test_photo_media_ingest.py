"""T3：入库接线（照片）— 三态语义 / 绑定键 / I-6 降级 / 公开入口透传 / 磁盘面。

全 mock 面（不写真库、不加载重型模型）：
- encode_media：photo-server **无模块级 import**（`ingest_photo` 内函数级
  `from niu_api.internal.llama_server import encode_media`）⇒ patch 源模块
  `niu_api.internal.llama_server.encode_media`（函数级 import 在调用时重新
  从源模块解析属性，patch 源模块即生效）。
- 照片库：`NIU_DB_PATH` 指向临时目录，并复位模块级连接缓存
  `niu_photo_server._conn` / `_db_path_failed`（get_connection 只在
  `_conn is None` 时重连，不复位会指向真实 `~/.niu/work/photos.db`）。
- 工作区：`WORKSPACE_PATH` 指向临时目录。
- 人脸：打桩 `detect_faces` → []（不构造 FaceAnalysis，先例
  test_photo_server_model_path.py）。
- sync_photo_to_kg：打桩（走 registry，真调会触网）。
- 媒体库：`NIU_MEDIA_STORE` 重定向到临时目录（media_vectors.media_store_path
  每次现读环境变量）。
- 配置：`media_ingest_enabled` 现读 `niu_api.config.CONFIG_PATH` ⇒
  monkeypatch 该常量指向临时 user-config.json。

用例 ①–⑧ 直调 ingest_photo 钉 D-3 三态语义；⑨ 经公开入口 ingest() 单文件
分派；⑩ 目录 start 纯图片一次排空；⑪ 真驱动 DiskExecutor 走完
_apply_defaults，守"磁盘面不注入 default ⇒ 未传参仍按配置产向量"。
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from loguru import logger

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import niu_photo_server  # noqa: E402
from niu_photo_server import media_vectors  # noqa: E402

FAKE_VECTOR = [0.5] * media_vectors.DIM

# 约定集合：ingest_photo 成功返回体的既有 10 个字段（T3 前原样、一个不动）。
# 本工程只新增 media_vector（I-8：只带状态，不带任何由图片推导的文本）。
BASE_FIELDS = {
    "status", "photo_id", "file_path", "original_path", "category",
    "detected_persons", "abstract", "exif", "lightrag_sync", "kg_entities",
}


def _expected_entity(file_path: str) -> str:
    """绑定键期望值：落盘后路径归一化（正斜杠+小写）后的 stem。
    与图谱侧 photo_entity_name（format_photo_ingest_data）逐字同一把键。"""
    return Path(file_path.replace("\\", "/").lower()).stem


def _make_photo(base: Path, name="IMG_0001.jpg") -> Path:
    p = base / name
    p.write_bytes(b"\xff\xd8\xff\xdb\x00" + b"\x00" * 64)  # 假 JPEG 字节即可（EXIF/人脸已打桩）
    return p


def _reset_photo_module_state():
    """模块级缓存必须测试间复位：连接缓存不复位会指向真实库，
    会话缓存不复位会让目录用例串状态。"""
    niu_photo_server._conn = None
    niu_photo_server._db_path_failed = False
    niu_photo_server._ingest_sessions.clear()


@pytest.fixture()
def photo_env(tmp_path, monkeypatch):
    """全套 mock 面：临时工作区/照片库/媒体库 + 人脸与 KG 同步打桩。"""
    _reset_photo_module_state()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_file = tmp_path / "photos.db"
    media_store = tmp_path / "media" / "media_vectors.json"
    config_file = tmp_path / "user-config.json"
    config_file.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NIU_DB_PATH", str(db_file))
    monkeypatch.setenv("WORKSPACE_PATH", str(workspace))
    monkeypatch.setenv("NIU_MEDIA_STORE", str(media_store))
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(config_file))

    fake_prefs = {
        "categories": {"photos": ["生活"], "documents": ["其他"]},
        "storage": {"structure": {"photos": "{year}"}},
    }
    monkeypatch.setattr(niu_photo_server, "get_preferences", lambda: fake_prefs)
    monkeypatch.setattr(
        niu_photo_server, "extract_exif",
        lambda file_path: {"taken_at": "2024:01:01 12:00:00", "location": None, "camera": None},
    )
    monkeypatch.setattr(niu_photo_server, "detect_faces", lambda file_path: [])
    monkeypatch.setattr(
        niu_photo_server, "sync_photo_to_kg",
        lambda *args, **kwargs: {"status": "synced", "kg_entities": []},
    )

    encode_mock = MagicMock(side_effect=lambda abs_path: list(FAKE_VECTOR))
    monkeypatch.setattr("niu_api.internal.llama_server.encode_media", encode_mock)

    yield {
        "workspace": workspace,
        "db_file": db_file,
        "media_store": media_store,
        "config_file": config_file,
        "encode": encode_mock,
    }
    _reset_photo_module_state()


def _set_media_ingest(config_file: Path, enabled: bool):
    config_file.write_text(
        json.dumps({"media_ingest": {"enabled": enabled}}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# ①–⑧ 三态语义（直调 ingest_photo）
# ---------------------------------------------------------------------------


def test_01_param_none_config_off_skips(photo_env, tmp_path):
    """① 参数未传 + 配置默认(关) → 不调 encode，返回体无 media_vector。"""
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success"
    assert "media_vector" not in r
    photo_env["encode"].assert_not_called()
    assert media_vectors.count() == 0


def test_02_param_true_saves(photo_env, tmp_path):
    """② 参数 True → 调用一次、写入媒体库、saved: true。"""
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src), media_vector=True)
    assert r["status"] == "success"
    photo_env["encode"].assert_called_once()
    assert r["media_vector"] == {"saved": True, "reason": ""}
    assert media_vectors.count() == 1
    assert media_vectors.has(_expected_entity(r["file_path"]))


def test_03_encode_failure_degrades(photo_env, tmp_path):
    """③ encode 抛错 → 入库仍成功、媒体库未写、有 WARNING、无异常向上（I-6）。"""
    src = _make_photo(tmp_path)
    photo_env["encode"].side_effect = RuntimeError("llama-server 未就绪（health 检查失败）")

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        r = niu_photo_server.ingest_photo(str(src), media_vector=True)
    finally:
        logger.remove(sink_id)

    assert r["status"] == "success"
    assert media_vectors.count() == 0
    assert r["media_vector"]["saved"] is False
    # 返回体只带状态：reason 是编码异常的机械原因，不含任何由图片推导的内容
    assert "20240101" not in r["media_vector"]["reason"]
    assert "IMG_" not in r["media_vector"]["reason"]
    assert any("Media vector failed" in m for m in messages), "必须记 WARNING"


def test_04_reingest_same_entity_overwrites(photo_env, tmp_path):
    """④ 同照片重复入库 → 媒体库 count() 不增（同键覆盖）。

    驱动方式：copy 模式每次都重命名（handle_photo_conflict 对已存在的同名
    文件追加 _1），两次 copy 必然落到不同 stem ⇒ 不同实体。故第二次改用
    **reference 模式对落盘后路径**再调 ingest_photo —— 不复制、不改名，
    两次落到同一实体键，验证 add 的覆盖语义（count 不增）。
    """
    src = _make_photo(tmp_path)
    r1 = niu_photo_server.ingest_photo(str(src), media_vector=True)
    assert r1["status"] == "success"
    landing = r1["file_path"]
    assert media_vectors.count() == 1

    r2 = niu_photo_server.ingest_photo(landing, mode="reference", media_vector=True)
    assert r2["status"] == "success"
    assert r2["file_path"] == landing
    assert media_vectors.count() == 1, "同键应覆盖而非新增"
    assert media_vectors.has(_expected_entity(landing))


def test_05_return_fields_exact(photo_env, tmp_path):
    """⑤ 真裁决调用返回体的字段集合恰为 既有10字段 + media_vector（I-8）。"""
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src), media_vector=True)
    assert set(r) == BASE_FIELDS | {"media_vector"}


def test_06_binding_key_is_landing_stem(photo_env, tmp_path):
    """⑥ 绑定键与图谱侧一致：落盘后路径的 stem（不是入参 file_path 的 stem）。

    源文件名与落盘名刻意不同（copy 模式按 EXIF 时间重命名）——若实现误用
    源 stem，本用例必红（错键陷阱的守门）。
    """
    src = _make_photo(tmp_path, "My Photo.JPG")
    r = niu_photo_server.ingest_photo(str(src), media_vector=True)
    assert r["status"] == "success"
    landing = r["file_path"]
    assert landing != str(src), "copy 模式应已重命名"
    assert media_vectors.has(_expected_entity(landing))
    source_stem = Path(str(src).replace("\\", "/").lower()).stem
    assert not media_vectors.has(source_stem), "不得用入参路径的 stem 当绑定键"


def test_07_config_on_param_none_saves(photo_env, tmp_path):
    """⑦ 配置开(enabled=true) + 参数未传 → 调用一次、写库、saved: true。

    这是主用户流：手册让用户改配置开关、Agent 不传参。
    """
    _set_media_ingest(photo_env["config_file"], True)
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success"
    photo_env["encode"].assert_called_once()
    assert r["media_vector"]["saved"] is True
    assert media_vectors.count() == 1


def test_08_config_on_explicit_false_skips(photo_env, tmp_path):
    """⑧ 配置开 + 显式 False → 不调用，且返回体**不含** media_vector 字段
    （与"缺省"同形，防实现返回 saved:false 混过）。"""
    _set_media_ingest(photo_env["config_file"], True)
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src), media_vector=False)
    assert r["status"] == "success"
    photo_env["encode"].assert_not_called()
    assert "media_vector" not in r
    assert media_vectors.count() == 0


# ---------------------------------------------------------------------------
# ⑨ 公开入口透传（守 ingest() 单文件分派）
# ---------------------------------------------------------------------------


def test_09_ingest_single_file_passthrough(photo_env, tmp_path):
    """⑨ 经公开入口 ingest(<单文件>, media_vector=True) → 单文件分派透传 → 写库。"""
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest(str(src), category="生活", media_vector=True)
    assert r["status"] == "success"
    photo_env["encode"].assert_called_once()
    assert r["media_vector"]["saved"] is True
    assert media_vectors.count() == 1


# ---------------------------------------------------------------------------
# ⑩ 目录 start 纯图片一次排空（守目录循环透传）
# ---------------------------------------------------------------------------


def test_10_ingest_directory_pure_images(photo_env, tmp_path):
    """⑩ 纯图片临时目录 + ingest(dir, action='start')：start 一次把所有照片
    处理完（照片分支恒自动循环），每张都进媒体库（逐张核对绑定键），不再发 interact。"""
    d = tmp_path / "photos"
    d.mkdir()
    for i in range(3):
        _make_photo(d, f"P_{i}.jpg")

    r = niu_photo_server.ingest(str(d), action="start", media_vector=True)
    assert r["status"] == "success"
    assert r["photos"] == 3
    assert len(r["details"]["photos"]) == 3
    assert photo_env["encode"].call_count == 3
    # 逐张绑定键断言：目录层整批错绑（3 张→3 个不同错键）时计数不变、照样绿
    for entry in r["details"]["photos"]:
        assert media_vectors.has(_expected_entity(entry["result"]["file_path"])), (
            f"绑定键缺失: {entry['file']}"
        )
    assert media_vectors.count() == 3


# ---------------------------------------------------------------------------
# ⑪ 磁盘面：真驱动 DiskExecutor 走完 _apply_defaults
# ---------------------------------------------------------------------------


def test_11_disk_surface_config_on_no_param_still_saves(photo_env, tmp_path):
    """⑪ 只配 media_ingest.enabled=true，经磁盘调用且**不传** media_vector
    → 磁盘层不得注入 default（该参数在 yaml 中无 default 键）⇒ 真实 ingest
    收到 None ⇒ 按配置产向量。

    真驱动 DiskExecutor：自建 registry（真键 "photo-server/ingest"，返回真
    ingest 函数）+ 自建 photo-server.yaml（directory: photos）。不复用
    test_disk_executor.py 的 mock_registry（纯 MagicMock ⇒ 真 ingest 永不执行）。
    """
    from niu_api.internal.disk_config import DiskConfig
    from niu_api.internal.disk_executor import DiskExecutor
    from niu_api.internal.disk_parser import DiskParser

    _set_media_ingest(photo_env["config_file"], True)

    config_dir = tmp_path / "disk_cfg"
    config_dir.mkdir()
    (config_dir / "photo-server.yaml").write_text(yaml.dump({
        "server": "photo-server",
        "directory": "photos",
        "description": "照片入库",
        "tools": [
            {
                "name": "ingest",
                "category": "write",
                "parameters": [
                    {"name": "path", "position": 1, "type": "string", "required": True},
                    {"name": "mode", "type": "string", "enum": ["copy", "move", "reference"], "default": "copy"},
                    {"name": "category", "type": "string"},
                    {"name": "action", "type": "string", "enum": ["", "start", "interact", "abort"], "default": ""},
                    # 与 config/disk/photo-server.yaml 同规则：只写 type，**不写 default**
                    {"name": "media_vector", "type": "boolean"},
                ],
            },
        ],
    }), encoding="utf-8")

    class _RealRegistry:
        """真 registry：'photo-server/ingest' 返回真 ingest 函数（目录路由键）。"""

        def get(self, name):
            if name == "photo-server/ingest":
                return niu_photo_server.ingest
            return None

    executor = DiskExecutor(DiskConfig(str(config_dir)), _RealRegistry())
    src = _make_photo(tmp_path)
    parsed = DiskParser().parse(f"/photos/ingest {src}")
    result = executor.execute(parsed)

    assert result.is_error is False, result.value
    assert result.value["status"] == "success"
    photo_env["encode"].assert_called_once()
    assert result.value["media_vector"]["saved"] is True
    assert media_vectors.count() == 1

    # 守仓库真实配置：photo-server.yaml 的 media_vector 参数不得带 default 键——
    # 磁盘层 _apply_defaults 会把"未传参"注入成显式 False ⇒ 配置开关被架空。
    # 上面自建 yaml 只守执行器路径，读不到仓库文件；此处读真仓库文件做内容级断言。
    repo_yaml = Path(__file__).resolve().parent.parent / "config" / "disk" / "photo-server.yaml"
    repo_cfg = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    ingest_tool = next(t for t in repo_cfg["tools"] if t["name"] == "ingest")
    mv_param = next(p for p in ingest_tool["parameters"] if p["name"] == "media_vector")
    assert "default" not in mv_param, (
        f"仓库 {repo_yaml} 给 media_vector 写了 default={mv_param.get('default')!r} ⇒ "
        "磁盘层会把未传参注入成显式 False，配置开关被架空"
    )
