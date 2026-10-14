"""S3 原语单测：F4 媒体向量队列 v3.5 四原语（plan T1 测试清单，全部 tmp_path 队列文件）。

覆盖：
- f4_take_first_pending：文件缺失 / 空文件 / 仅空行与已完成行 → (None, None)（无活可干）；
  旧格式行（无 done 键）天然未完成（I-7 兼容）；尾行无换行 = 撕裂行，不是记录（跳过，不报坏行）；
  坏行（非 JSON / 非 dict / 缺必填键）→ F4LineCorruptError 携带行内容、原因与行号
  （line_index——v3.5：坏行可在文件任意位置）。
- f4_mark_line_done：标注行 = 原键保留 + done，其余行逐字节不变；撕裂尾行重写后原样拼回；
  标注/弃除最后一条完整行 + 撕裂尾行在场 ⇒ 拼合后收敛（完成行消失、无活可干，取舍见
  _f4_rewrite_line docstring）；越界 / 指向空行 → failed 不动文件；内容护栏不符 → failed
  文件不动；重写失败自动恢复原文（打桩 os.write 首次写失败，恢复写成功 ⇒ 文件内容 == 原文）；
  文件缺失 → "missing"（Chat 页面已删文件，I-3/口径丁 的竞态形态）。
- f4_drop_line：坏行夹在已完成行与未完成行之间，按行号弃除；撕裂尾行绝不被触及（触及 ⇒
  越界拒动）；重写失败恢复原文；文件缺失 → "missing"。
- f4_dismiss_if_all_done：全部记录行带完成标记（或无任何记录行）⇒ 删文件返 True；
  存在未完成行 ⇒ False 且文件原样；坏行不算完成 ⇒ 不删；文件缺失 ⇒ True。
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


def _rec_done(**over) -> str:
    base = {"entity_name": "x/2024/y.jpg", "file_path": "/tmp/x.jpg", "ts": "2026-01-01T00:00:00", "done": True}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


# ---------------------------------------------------------------------------
# f4_take_first_pending
# ---------------------------------------------------------------------------


def test_take_first_pending_missing_file(tmp_path):
    assert mdm.f4_take_first_pending(str(tmp_path / "nope.md")) == (None, None)


def test_take_first_pending_empty_file(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "")
    assert mdm.f4_take_first_pending(str(p)) == (None, None)


def test_take_first_pending_blank_and_done_only(tmp_path):
    """仅空行与已完成行 = 无活可干（完成行不是「活」，文件由 Chat 页面删，I-3/口径丁）。"""
    p = tmp_path / "q.md"
    _write(p, "\n" + _rec_done() + "\n\n")
    assert mdm.f4_take_first_pending(str(p)) == (None, None)


def test_take_first_pending_valid_record(tmp_path):
    p = tmp_path / "q.md"
    _write(p, _rec() + "\n")
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert idx == 0
    assert rec == json.loads(_rec())
    # 不删行、不改行（至少一次语义，D-21/I-5）：文件原样
    assert p.read_text(encoding="utf-8") == _rec() + "\n"


def test_take_first_pending_skips_blank_and_done_lines(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "\n" + _rec_done() + "\n\n" + _rec() + "\n")
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert rec == json.loads(_rec())
    assert idx == 3, "行号 = 行在文件全部行（含空行）中的下标"


def test_take_first_pending_torn_tail_is_not_a_record(tmp_path):
    """尾行无换行符 = O_APPEND 写了一半的撕裂行：不是记录，跳过，不报坏行。"""
    p = tmp_path / "q.md"
    _write(p, _rec() + "\n" + '{"entity_name": "torn_')  # 撕裂尾行
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert idx == 0
    assert rec == json.loads(_rec()), "首条完整记录应被取到，撕裂尾行不得冒充记录"


def test_take_first_pending_only_torn_tail(tmp_path):
    """仅有一条无换行尾行 ⇒ 无任何完整记录 ⇒ (None, None)。"""
    p = tmp_path / "q.md"
    _write(p, '{"entity_name": "torn_')
    assert mdm.f4_take_first_pending(str(p)) == (None, None)


def test_take_first_pending_old_format_line_without_done_is_pending(tmp_path):
    """I-7 兼容：旧格式三键行（无 done）天然未完成——排在完成行之后照样被取到。"""
    p = tmp_path / "q.md"
    _write(p, _rec_done() + "\n" + _rec() + "\n")
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert idx == 1
    assert rec == json.loads(_rec())


def test_take_first_pending_done_as_string_true_is_pending(tmp_path):
    """I-7 严格判定：``done`` 是字符串 ``"true"`` 而非布尔 ``True`` ⇒ 不算完成，必须按未完成取走
    （truthy 误判会让字符串完成行被跳过永不消费、完成比漂移）。将该行翻成布尔 true 后再取，
    必须跳过两条完成行、取到它们之后的未完成行（布尔行同样被跳过）。"""
    p = tmp_path / "q.md"
    string_done = _rec(entity_name="a", file_path="/x", done="true")
    bool_done = _rec_done(entity_name="b", file_path="/y")
    pending = _rec(entity_name="c", file_path="/z")
    _write(p, string_done + "\n" + bool_done + "\n" + pending + "\n")
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert idx == 0, "字符串 \"true\" 不算完成：首行必须按未完成取走"
    assert rec["entity_name"] == "a"
    _write(p, _rec_done(entity_name="a", file_path="/x") + "\n" + bool_done + "\n" + pending + "\n")
    idx, rec = mdm.f4_take_first_pending(str(p))
    assert idx == 2, "两条完成行都被跳过，取到的是它们之后的未完成行（布尔行同样被跳过）"
    assert rec["entity_name"] == "c"


def test_take_first_pending_non_json_line_raises_with_index(tmp_path):
    """坏行必须位于已完成行之后才会被扫描遇到（前面的未完成行会先被取走）。"""
    p = tmp_path / "q.md"
    _write(p, _rec_done() + "\n" + "这不是 JSON\n")
    with pytest.raises(mdm.F4LineCorruptError) as ei:
        mdm.f4_take_first_pending(str(p))
    assert ei.value.reason == "非 JSON"
    assert "这不是 JSON" in ei.value.line
    assert ei.value.line_index == 1, "坏行可在文件任意位置：行号必须携带"

def test_take_first_pending_missing_required_key_raises_with_index(tmp_path):
    p = tmp_path / "q.md"
    _write(p, _rec_done() + "\n" + json.dumps({"file_path": "/tmp/x.jpg", "ts": "2026-01-01T00:00:00"}, ensure_ascii=False) + "\n")
    with pytest.raises(mdm.F4LineCorruptError) as ei:
        mdm.f4_take_first_pending(str(p))
    assert "entity_name" in ei.value.reason, "缺必填键的原因必须点名缺失的键"
    assert ei.value.line_index == 1


def test_take_first_pending_non_dict_record_raises(tmp_path):
    p = tmp_path / "q.md"
    _write(p, json.dumps([1, 2, 3]) + "\n")
    with pytest.raises(mdm.F4LineCorruptError) as ei:
        mdm.f4_take_first_pending(str(p))
    assert ei.value.line_index == 0


# ---------------------------------------------------------------------------
# f4_mark_line_done
# ---------------------------------------------------------------------------


def test_mark_line_done_remainder_bytes_exact(tmp_path):
    """其余行逐字节不变；标注行 = 原键保留 + done。"""
    p = tmp_path / "q.md"
    head = _rec()
    second = _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg")
    _write(p, head + "\n" + second + "\n")
    assert mdm.f4_mark_line_done(1, queue_path=str(p)) == "marked"
    expected = json.loads(second)
    expected["done"] = True
    assert p.read_text(encoding="utf-8") == head + "\n" + json.dumps(expected, ensure_ascii=False) + "\n"


def test_mark_line_done_marks_second_line_keeps_head_done_line(tmp_path):
    p = tmp_path / "q.md"
    head = _rec_done()
    second = _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg")
    _write(p, head + "\n" + second + "\n")
    assert mdm.f4_mark_line_done(1, queue_path=str(p)) == "marked"
    expected = json.loads(second)
    expected["done"] = True
    lines = p.read_text(encoding="utf-8").split("\n")
    assert lines[0] == head, "已完成队首行逐字节保留"
    assert json.loads(lines[1]) == expected


def test_mark_line_done_torn_tail_preserved_after_mark(tmp_path):
    """标注非最后完整行后重写：撕裂尾行内容原样拼回（无尾换行），绝不吞掉写入方写了一半的行
    （目标行不是最后一行 ⇒ 拼合落在撕裂尾行自身，其内容逐字节保留）。"""
    p = tmp_path / "q.md"
    head = _rec()
    second = _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg")
    torn = '{"entity_name": "torn_'
    _write(p, head + "\n" + second + "\n" + torn)
    assert mdm.f4_mark_line_done(0, queue_path=str(p)) == "marked"
    marked_head = json.loads(head)
    marked_head["done"] = True
    assert p.read_text(encoding="utf-8") == json.dumps(marked_head, ensure_ascii=False) + "\n" + second + torn


def test_mark_line_done_torn_tail_joins_last_line_then_converges(tmp_path):
    """标注/弃除**最后一条完整行** + 撕裂尾行在场 ⇒ 重写结果与撕裂尾行拼成一条（整行变撕裂尾行），
    下轮收敛：完成行消失〔向量已入库、总数减 1、意图不受影响〕；弃除坏行形态下拼合落到前一行
    （如有，同样成撕裂尾行）——取舍见 ``_f4_rewrite_line`` docstring。"""
    # 形态 1：标注最后（唯一）完整行 + 撕裂尾行 ⇒ 拼合后无记录可扫，收敛为「无活可干」
    p = tmp_path / "q.md"
    head = _rec()
    torn = "TORN_PARTIAL"
    _write(p, head + "\n" + torn)
    assert mdm.f4_mark_line_done(0, queue_path=str(p)) == "marked"
    text = p.read_text(encoding="utf-8")
    assert text.endswith(torn) and "\n" not in text, "标注行与撕裂尾行拼合为整行撕裂尾行"
    assert mdm.f4_take_first_pending(str(p)) == (None, None), "收敛：无活可干，不再重复告警"
    # 形态 2：弃除队首坏行，拼合落到前一行（该完整行整体成撕裂尾行）⇒ 同样收敛
    p2 = tmp_path / "q2.md"
    pending = _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg")
    _write(p2, "这不是 JSON\n" + pending + "\n" + torn)
    assert mdm.f4_drop_line(0, queue_path=str(p2)) == "dropped"
    assert p2.read_text(encoding="utf-8") == pending + torn, "拼合落到前一行"
    assert mdm.f4_take_first_pending(str(p2)) == (None, None)


def test_mark_line_done_out_of_range_returns_failed(tmp_path):
    p = tmp_path / "q.md"
    original = _rec() + "\n"
    _write(p, original)
    assert mdm.f4_mark_line_done(5, queue_path=str(p)) == "failed"
    assert p.read_text(encoding="utf-8") == original, "越界必须不动文件"


def test_mark_line_done_empty_target_line_returns_failed(tmp_path):
    p = tmp_path / "q.md"
    original = "\n" + _rec() + "\n"
    _write(p, original)
    assert mdm.f4_mark_line_done(0, queue_path=str(p)) == "failed", "标注空行必须拒动"
    assert p.read_text(encoding="utf-8") == original


def test_mark_line_done_rewrite_failure_recovers_original(tmp_path, monkeypatch):
    """重写失败 ⇒ 自动恢复原文（与旧 f4_drop_prefix 的恢复语义同型）：首次写失败，
    恢复写成功 ⇒ 返回 failed 且文件内容逐字节 == 原文。"""
    p = tmp_path / "q.md"
    original = _rec() + "\n" + _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg") + "\n"
    _write(p, original)

    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("模拟首次重写失败")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    assert mdm.f4_mark_line_done(0, queue_path=str(p)) == "failed"
    assert p.read_text(encoding="utf-8") == original, "重写失败后必须恢复原文"


def test_mark_line_done_missing_file_returns_missing(tmp_path):
    assert mdm.f4_mark_line_done(0, queue_path=str(tmp_path / "nope.md")) == "missing"


def test_mark_line_done_expect_guard_mismatch_returns_failed_and_file_unchanged(tmp_path):
    p = tmp_path / "q.md"
    original = _rec() + "\n"
    _write(p, original)
    assert mdm.f4_mark_line_done(0, queue_path=str(p), expect=("another/entity.jpg", "/tmp/x.jpg")) == "failed"
    assert p.read_text(encoding="utf-8") == original, "内容护栏不符 ⇒ 文件不动"


# ---------------------------------------------------------------------------
# f4_drop_line
# ---------------------------------------------------------------------------


def test_drop_line_at_middle_position(tmp_path):
    """坏行夹在已完成行与未完成行之间：按行号弃除（v3.5：坏行不再限于队首）。"""
    p = tmp_path / "q.md"
    head = _rec_done()
    bad = "这不是 JSON"
    tail = _rec(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg")
    _write(p, head + "\n" + bad + "\n" + tail + "\n")
    assert mdm.f4_drop_line(1, queue_path=str(p)) == "dropped"
    assert p.read_text(encoding="utf-8") == head + "\n" + tail + "\n"


def test_drop_line_multiple_lines(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\nL3\nL4\n")
    assert mdm.f4_drop_line(1, str(p)) == "dropped"
    assert p.read_text(encoding="utf-8") == "L1\nL3\nL4\n"


def test_drop_line_all_lines_removed(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "L1\nL2\n")
    assert mdm.f4_drop_line(0, str(p)) == "dropped"
    assert mdm.f4_drop_line(0, str(p)) == "dropped"
    assert p.read_text(encoding="utf-8") == ""


def test_drop_line_out_of_range(tmp_path):
    p = tmp_path / "q.md"
    original = "L1\nL2\n"
    _write(p, original)
    assert mdm.f4_drop_line(5, str(p)) == "failed"
    assert p.read_text(encoding="utf-8") == original, "越界必须不动文件"


def test_drop_line_torn_tail_rejected(tmp_path):
    """尾行是撕裂的不完整行（无换行符）⇒ 行号触及它时越界拒动，文件原样。"""
    p = tmp_path / "q.md"
    original = "L1\nL2"  # L2 撕裂
    _write(p, original)
    assert mdm.f4_drop_line(1, str(p)) == "failed", "触及撕裂尾部不完整行必须拒动"
    assert p.read_text(encoding="utf-8") == original


def test_drop_line_rewrite_failure_recovers(tmp_path, monkeypatch):
    """重写失败 ⇒ 自动恢复原文：首次写失败，恢复写成功 ⇒ 文件内容逐字节 == 原文。"""
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
    assert mdm.f4_drop_line(0, str(p)) == "failed"
    assert p.read_text(encoding="utf-8") == original, "重写失败后必须恢复原文"


def test_drop_line_missing_file_returns_missing(tmp_path):
    assert mdm.f4_drop_line(0, str(tmp_path / "nope.md")) == "missing"


# ---------------------------------------------------------------------------
# f4_dismiss_if_all_done
# ---------------------------------------------------------------------------


def test_dismiss_all_done_removes_file(tmp_path):
    p = tmp_path / "q.md"
    _write(p, _rec_done() + "\n" + _rec_done(entity_name="x/2024/b.jpg", file_path="/tmp/b.jpg") + "\n")
    assert mdm.f4_dismiss_if_all_done(str(p)) is True
    assert not p.exists(), "全部记录行已完成 ⇒ 删文件"


def test_dismiss_with_pending_line_keeps_file_unchanged(tmp_path):
    p = tmp_path / "q.md"
    original = _rec_done() + "\n" + _rec() + "\n"
    _write(p, original)
    assert mdm.f4_dismiss_if_all_done(str(p)) is False, "存在未完成行 ⇒ 拒删"
    assert p.read_text(encoding="utf-8") == original, "文件保持原样"


def test_dismiss_with_bad_line_keeps_file(tmp_path):
    p = tmp_path / "q.md"
    original = _rec_done() + "\n" + "这不是 JSON\n"
    _write(p, original)
    assert mdm.f4_dismiss_if_all_done(str(p)) is False, "坏行不是「已完成」⇒ 不删（坏行先由工作者弃除路径处置）"
    assert p.read_text(encoding="utf-8") == original


def test_dismiss_missing_file_true(tmp_path):
    assert mdm.f4_dismiss_if_all_done(str(tmp_path / "nope.md")) is True


def test_dismiss_blank_and_done_only_removes_file(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "\n" + _rec_done() + "\n\n")
    assert mdm.f4_dismiss_if_all_done(str(p)) is True
    assert not p.exists(), "无任何记录行（仅空行与完成行）⇒ 视为已清，删文件"


def test_dismiss_empty_file_removed_true(tmp_path):
    p = tmp_path / "q.md"
    _write(p, "")
    assert mdm.f4_dismiss_if_all_done(str(p)) is True
    assert not p.exists()


def test_dismiss_inflight_writer_append_recheck_refuses_delete(tmp_path, monkeypatch):
    """竞态拦截（确定性注入，不用真并发/sleep）：页面此前观察到「全完成」；dismiss 复核之前，
    写入方已在同一 inode 追加了一条未完成行（真实竞态里锁使写入方的追加完成于页面的持锁重读之前
    ⇒ 重读内容必含该行）。此手法把该行注入 dismiss 复核的唯一行分类入口 _f4_split_records，与真实
    重读内容同构。若 dismiss 信任陈旧观察、跳过复核直接删文件，该行会随文件被删、静默丢未完成活。
    ⇒ 必须返回 False 且文件原样（False 路径无任何写/删动作）。"""
    p = tmp_path / "q.md"
    _write(p, _rec_done(entity_name="a", file_path="/x") + "\n")
    original = p.read_text(encoding="utf-8")
    real_split = mdm._f4_split_records

    def racing_split(content):
        lines, torn = real_split(content)
        return lines + [_rec(entity_name="b", file_path="/y")], torn  # 写入方刚追加的未完成行

    monkeypatch.setattr(mdm, "_f4_split_records", racing_split)
    assert mdm.f4_dismiss_if_all_done(str(p)) is False, "复核见到新追加的未完成行 ⇒ 拒删"
    assert p.exists() and p.read_text(encoding="utf-8") == original, "文件保持原样"
