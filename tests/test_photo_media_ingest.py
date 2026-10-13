"""S3：入库唯一队列写入点（照片）— 写入时机 / 三键行格式 / 失败不阻断 / 开关零成本。

全 mock 面（不写真库、不加载重型模型、不触网）：
- 照片库：`NIU_DB_PATH` 指向临时目录，并复位模块级连接缓存
  `niu_photo_server._conn` / `_db_path_failed`（get_connection 只在
  `_conn is None` 时重连，不复位会指向真实 `~/.niu/work/photos.db`）。
- 工作区：`WORKSPACE_PATH` 指向临时目录。
- 人脸：打桩 `detect_faces` → []（不构造 FaceAnalysis，先例
  test_photo_server_model_path.py）。
- sync_photo_to_kg：打桩（真调会触网）；三种 status（success/skipped/error）
  由用例按需设定。
- 队列文件：`agent.md_mirror.MEDIA_QUEUE_PATH` 重定向到临时路径（写入点
  函数级 import 在调用时现读模块属性 ⇒ 补丁生效）。
- 编码面（AC-5）：`encode_media`/`encode_batch` 打桩成 `AssertionError`——
  入库路径任何一处编码调用都会炸掉本用例；入库成功 ⇒ 编码只可能发生在
  后台工作者里。
- 配置：`media_ingest_enabled` 现读 `niu_api.config.CONFIG_PATH` ⇒
  monkeypatch 该常量指向临时 user-config.json。

用例：
① 开关开 + sync success ⇒ 队列恰一行，三键格式（entity_name = 落盘后路径
   归一化 stem，不得用入参路径的 stem；file_path = 落盘后绝对路径；ts 可解析），
   media_queue == {"queued": True}，编码桩零调用。
② sync skipped ⇒ 同样写一行（视同成功，D-20）。
③ sync error ⇒ 不写队列行；queued is False，reason 提及图谱同步状态。
④ append_record 打桩失败 ⇒ 入库仍 success + 告警 + queued is False
   （D-7 精神：队列失败不阻断入库）。
⑤ 开关关 ⇒ 不写队列行、返回体不含 media_queue 键（与 v2.x 之前逐字节一致，AC-2）。
⑥ 目录模式 3 张照片各自 sync 成功 ⇒ 队列 3 行（逐文件各写一行）。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 让 niu_photo_server 模块可导入（其源码在 mcp-servers/photo-server/src 下）
_SRC = os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "photo-server", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import agent.md_mirror as mdm  # noqa: E402
import niu_photo_server  # noqa: E402
import niu_api.internal.llama_server as _llama  # noqa: E402

# 约定集合：ingest_photo 成功返回体的既有 10 个字段（v3.0 只新增 media_queue，
# 且仅开关开时出现——AC-2：开关关时返回体与 v2.x 之前逐字节一致）。
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


def _queue_lines(queue_file: Path) -> list[dict]:
    """读队列文件全部行（逐行 JSON 解析；无文件 → 空列表）。"""
    if not queue_file.exists():
        return []
    lines = [ln for ln in queue_file.read_text(encoding="utf-8").split("\n") if ln.strip()]
    return [json.loads(ln) for ln in lines]


@pytest.fixture()
def photo_env(tmp_path, monkeypatch):
    """全套 mock 面：临时工作区/照片库/队列文件 + 人脸与 KG 同步打桩。"""
    _reset_photo_module_state()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_file = tmp_path / "photos.db"
    queue_file = tmp_path / "md" / "media_vector_queue.md"
    config_file = tmp_path / "user-config.json"
    config_file.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setenv("NIU_DB_PATH", str(db_file))
    monkeypatch.setenv("WORKSPACE_PATH", str(workspace))
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(config_file))
    monkeypatch.setattr(mdm, "MEDIA_QUEUE_PATH", str(queue_file))

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
        MagicMock(return_value={"status": "success", "kg_entities": []}),
    )

    # AC-5：入库路径零编码——任何编码调用立即炸掉用例
    monkeypatch.setattr(_llama, "encode_media", MagicMock(side_effect=AssertionError("入库路径不得调用 encode_media")))
    monkeypatch.setattr(_llama, "encode_batch", MagicMock(side_effect=AssertionError("入库路径不得调用 encode_batch")))

    yield {
        "workspace": workspace,
        "db_file": db_file,
        "queue_file": queue_file,
        "config_file": config_file,
    }
    _reset_photo_module_state()


def _set_media_ingest(config_file: Path, enabled: bool):
    config_file.write_text(
        json.dumps({"media_ingest": {"enabled": enabled}}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# ①–⑥ 队列写入点断言（直调 ingest_photo / 公开入口 ingest）
# ---------------------------------------------------------------------------


def test_01_config_on_sync_success_writes_one_line(photo_env, tmp_path):
    """① 开关开 + sync success ⇒ 队列恰一行，三键格式正确，queued=True，编码零调用。

    源文件名与落盘名刻意不同（copy 模式按 EXIF 时间重命名）——若实现误用
    入参路径的 stem，本用例必红（错键陷阱的守门）。
    """
    _set_media_ingest(photo_env["config_file"], True)
    src = _make_photo(tmp_path, "My Photo.JPG")
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success"

    landing = r["file_path"]
    assert landing != str(src), "copy 模式应已重命名"

    lines = _queue_lines(photo_env["queue_file"])
    assert len(lines) == 1, "sync 成功应恰写一行队列记录"
    rec = lines[0]
    assert set(rec) == {"entity_name", "file_path", "ts"}, "队列行恰三键（D-24）"
    assert rec["entity_name"] == _expected_entity(landing), "绑定键 = 落盘后路径归一化 stem"
    source_stem = Path(str(src).replace("\\", "/").lower()).stem
    assert rec["entity_name"] != source_stem, "不得用入参路径的 stem"
    assert rec["file_path"] == landing, "file_path = 落盘后绝对路径"
    datetime.fromisoformat(rec["ts"])  # ts 可解析（入队时刻，仅诊断）

    assert r["media_queue"] == {"queued": True}
    assert not _llama.encode_media.called and not _llama.encode_batch.called


def test_02_sync_skipped_also_writes(photo_env, tmp_path, monkeypatch):
    """② sync skipped（已同步、节点已在图谱）⇒ 视同成功，同样写一行（D-20）。"""
    _set_media_ingest(photo_env["config_file"], True)
    monkeypatch.setattr(
        niu_photo_server, "sync_photo_to_kg",
        lambda *args, **kwargs: {"status": "skipped", "reason": "already synced", "kg_entities": []},
    )
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success"
    assert r["media_queue"] == {"queued": True}
    lines = _queue_lines(photo_env["queue_file"])
    assert len(lines) == 1
    assert lines[0]["entity_name"] == _expected_entity(r["file_path"])


def test_03_sync_error_writes_nothing(photo_env, tmp_path, monkeypatch):
    """③ sync error ⇒ 不写队列行；queued is False，reason 提及图谱同步状态。"""
    _set_media_ingest(photo_env["config_file"], True)
    monkeypatch.setattr(
        niu_photo_server, "sync_photo_to_kg",
        lambda *args, **kwargs: {"status": "error", "reason": "lightrag down", "kg_entities": []},
    )
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success", "图谱同步失败不阻断照片入库"
    assert _queue_lines(photo_env["queue_file"]) == [], "sync error 不写队列行"
    assert r["media_queue"]["queued"] is False
    assert "error" in r["media_queue"]["reason"], "reason 必须可辨图谱同步状态"


def test_04_append_failure_does_not_block(photo_env, tmp_path, monkeypatch):
    """④ 队列写入失败（append_record 返 False）⇒ 入库仍 success + 告警 + queued is False。"""
    _set_media_ingest(photo_env["config_file"], True)
    monkeypatch.setattr(mdm, "append_record", lambda block, md_path=None: False)
    src = _make_photo(tmp_path)

    from loguru import logger
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        r = niu_photo_server.ingest_photo(str(src))
    finally:
        logger.remove(sink_id)

    assert r["status"] == "success", "队列写入失败不得阻断入库（D-7 精神）"
    assert _queue_lines(photo_env["queue_file"]) == []
    assert r["media_queue"]["queued"] is False
    assert "队列文件写入失败" in r["media_queue"]["reason"]
    assert any("Media queue append failed" in m for m in messages), "必须记 WARNING"


def test_05_switch_off_zero_trace(photo_env, tmp_path):
    """⑤ 开关关 ⇒ 不写队列行、返回体不含 media_queue 键（AC-2：与 v2.x 前逐字节一致）。"""
    src = _make_photo(tmp_path)
    r = niu_photo_server.ingest_photo(str(src))
    assert r["status"] == "success"
    assert set(r) == BASE_FIELDS, "开关关时返回体不含 media_queue"
    assert not photo_env["queue_file"].exists(), "开关关时队列文件不被创建/触碰"


def test_06_directory_mode_writes_per_file(photo_env, tmp_path):
    """⑥ 目录模式 3 张照片、各自 sync 成功 ⇒ 队列 3 行（逐文件各写一行，无会话级记录）。"""
    _set_media_ingest(photo_env["config_file"], True)
    d = tmp_path / "photos"
    d.mkdir()
    for i in range(3):
        _make_photo(d, f"P_{i}.jpg")

    r = niu_photo_server.ingest(str(d), action="start")
    assert r["status"] == "success"
    assert r["photos"] == 3
    assert len(r["details"]["photos"]) == 3

    lines = _queue_lines(photo_env["queue_file"])
    assert len(lines) == 3, "逐文件各写一行队列记录"
    # 逐张绑定键断言：目录层整批错绑（3 张→3 个不同错键）时行数不变、照样绿
    for entry in r["details"]["photos"]:
        assert entry["result"]["media_queue"] == {"queued": True}
    for rec in lines:
        assert set(rec) == {"entity_name", "file_path", "ts"}
        assert rec["entity_name"] == _expected_entity(rec["file_path"])
    stems = [rec["entity_name"] for rec in lines]
    assert len(set(stems)) == 3, "三张照片落到三个不同的实体键"
    assert not _llama.encode_media.called and not _llama.encode_batch.called
