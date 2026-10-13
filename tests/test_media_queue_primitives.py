"""S3 原语单测：F4 媒体向量队列的 `f4_take_head` / `f4_drop_prefix`。

覆盖（plan S3 步骤 4 点名分支）：
- f4_take_head：文件缺失 / 空文件 / 仅空行 → None；尾行无换行 = 撕裂行，
  不是记录（跳过，不报坏行）；坏行（非 JSON / 缺必填键）→ F4HeadCorruptError
  携带行内容与原因。
- f4_drop_prefix：删除后余下内容逐字节正确；越界返 0 不动文件；
  撕裂尾部不完整行拒删；重写失败自动恢复原文（打桩 os.write 首次写失败，
  恢复写成功 ⇒ 文件内容 == 原文）。
"""

import json
import os
from pathlib import Path

import pytest

import agent.md_mirror as mdm


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _rec(**over) -> str:
    base = {"entity_name": "x/2024/y.jpg", "file_path": "/tmp/x.jpg", "ts": "2026-01-01T00:00:00"}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


# ---------------------------------------------------------------------------
# f4_take_head
# ---------------------------------------------------------------------------


def test_take_head_missing_file(tmp_path):
    assert mdm.f4_take_head(str(tmp_path / "nope.md")) is None


def test_take_head_empty_file(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "")
    assert mdm.f4_take_head(str(p)) is None


def test_take_head_blank_lines_only(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "\n\n\n")
    assert mdm.f4_take_head(str(p)) is None


def test_take_head_valid_record(tmp_path):
    p = tmp_path / "q.md"
    _write(p, _rec() + "\n")
    rec = mdm.f4_take_head(str(p))
    assert rec == json.loads(_rec())
    # 不删行（至少一次语义，D-21）：文件原样
    assert p.read_text(encoding="utf-8") == _rec() + "\n"


def test_take_head_skips_blank_lines(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "\n\n" + _rec() + "\n")
    assert mdm.f4_take_head(str(p)) == json.loads(_rec())


def test_take_head_torn_tail_is_not_a_record(tmp_path):
    """尾行无换行符 = O_APPEND 写了一半的撕裂行：不是队首记录，跳过，不报坏行。"""
    p = tmp_path / "q.md"
    _write(p, _rec() + "\n" + '{"entity_name": "torn_')  # 撕裂尾行
    rec = mdm.f4_take_head(str(p))
    assert rec == json.loads(_rec()), "队首完整记录应被取到，撕裂尾行不得冒充队首"


def test_take_head_only_torn_tail(tmp_path):
    """仅有一条无换行尾行 ⇒ 无任何完整记录 ⇒ None。"""
    p = tmp_path / "q.md"
    _write(p, '{"entity_name": "torn_')
    assert mdm.f4_take_head(str(p)) is None


def test_take_head_non_json_head_raises(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "这不是 JSON\n")
    with pytest.raises(mdm.F4HeadCorruptError) as ei:
        mdm.f4_take_head(str(p))
    assert ei.value.reason == "非 JSON"
    assert "这不是 JSON" in ei.value.line


def test_take_head_missing_required_key_raises(tmp_path):
    p = tmp_path / "q.md"
    _write(p, json.dumps({"file_path": "/tmp/x.jpg", "ts": "2026-01-01T00:00:00"}, ensure_ascii=False) + "\n")
    with pytest.raises(mdm.F4HeadCorruptError) as ei:
        mdm.f4_take_head(str(p))
    assert "entity_name" in ei.value.reason, "缺必填键的原因必须点名缺失的键"


def test_take_head_non_dict_record_raises(tmp_path):
    p = tmp_path / "q.md"
    _write(p, json.dumps([1, 2, 3]) + "\n")
    with pytest.raises(mdm.F4HeadCorruptError):
        mdm.f4_take_head(str(p))


# ---------------------------------------------------------------------------
# f4_drop_prefix
# ---------------------------------------------------------------------------


def test_drop_prefix_remainder_bytes_exact(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\nL3\n")
    assert mdm.f4_drop_prefix(1, str(p)) == 1
    assert p.read_text(encoding="utf-8") == "L2\nL3\n"


def test_drop_prefix_multiple_lines(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\nL3\nL4\n")
    assert mdm.f4_drop_prefix(3, str(p)) == 3
    assert p.read_text(encoding="utf-8") == "L4\n"


def test_drop_prefix_all_lines_removes_file_content(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\n")
    assert mdm.f4_drop_prefix(2, str(p)) == 2
    assert p.read_text(encoding="utf-8") == ""


def test_drop_prefix_out_of_range_returns_zero(tmp_path):
    p = tmp_path / "q.md"
    original = "L1\nL2\n"
    _write(p, original)
    assert mdm.f4_drop_prefix(3, str(p)) == 0
    assert p.read_text(encoding="utf-8") == original, "越界必须不动文件"


def test_drop_prefix_invalid_n_returns_zero(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\n")
    assert mdm.f4_drop_prefix(0, str(p)) == 0
    assert mdm.f4_drop_prefix(-1, str(p)) == 0
    assert p.read_text(encoding="utf-8") == "L1\n"


def test_drop_prefix_torn_tail_rejected(tmp_path):
    """尾行是撕裂的不完整行（无换行符）⇒ 删除范围触及它时整次拒删。"""
    p = tmp_path / "q.md"
    original = "L1\nL2"  # L2 撕裂
    _write(p, original)
    assert mdm.f4_drop_prefix(2, str(p)) == 0, "触及撕裂尾部不完整行必须拒删"
    assert p.read_text(encoding="utf-8") == original


def test_drop_prefix_partial_drop_leaves_torn_tail_behind(tmp_path):
    """删前缀不触及尾行时照常删；撕裂尾行保留（重写后补了换行符）。"""
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\nL3_torn")
    assert mdm.f4_drop_prefix(1, str(p)) == 1
    assert p.read_text(encoding="utf-8") == "L2\nL3_torn\n"


def test_drop_prefix_rewrite_failure_recovers_original(tmp_path, monkeypatch):
    """重写失败 ⇒ 自动恢复原文（同 drop_f2_prefix 恢复语义）：首次写失败，
    恢复写成功 ⇒ 返回 0 且文件内容逐字节 == 原文。"""
    p = tmp_path / "q.md"
    original = "L1\nL2\nL3\n"
    _write(p, original)

    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("模拟首次重写失败")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    assert mdm.f4_drop_prefix(1, str(p)) == 0
    assert p.read_text(encoding="utf-8") == original, "重写失败后必须恢复原文"


def test_drop_prefix_missing_file_returns_zero(tmp_path):
    assert mdm.f4_drop_prefix(1, str(tmp_path / "nope.md")) == 0
