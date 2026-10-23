"""工程二 Task1：relay 原语单测（tmp_path，零真实依赖）。夹具由 format_message_record 真实输出派生，防手拼漂移。

轮末口径（plan §1 / spec §3.1）：F1→F2 relay 只切 **strict_turn_ends**——文件末尾没有后继
user 的段在 F1 里不算完整轮，整轮留 F1 等下次；F2 侧（切 F3 / drop）才用 all_turn_ends。
"""

import agent.md_mirror as mdm
from agent.md_mirror import (
    all_turn_ends,
    append_record,
    format_message_record,
    record_end_boundaries,
    relay_processed_prefix,
    snap_to_boundary,
    strict_turn_ends,
    truncate_relay_files,
)
from loguru import logger


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（md_mirror 用 loguru 而非 stdlib logging，caplog 捕获不到）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _make_f1(tmp_path, roles=("user", "assistant", "user"), name="f1.md"):
    """用真实格式化器构造记录（正文两行 ⇒ meta+2 行正文+空行 = 每条 4 行）。返回 (p1, p2, ids)。"""
    p1 = tmp_path / name
    p2 = tmp_path / "f2.md"
    ids = []
    for i, role in enumerate(roles):
        mid = f"id{i}"
        block = format_message_record(msg_id=mid, created_at="t", role=role, content=f"内容{i}\n第二行")
        append_record(block, str(p1))
        ids.append(mid)
    return str(p1), str(p2), ids


def _append_f1(p1, roles, start):
    """向既有 F1 追加记录（id 从 start 起算），模拟对话继续镜像。"""
    for i, role in enumerate(roles):
        block = format_message_record(msg_id=f"id{start + i}", created_at="t", role=role,
                                      content=f"内容{start + i}\n第二行")
        assert append_record(block, p1)


def _read_ids(path):
    import os, re
    if not os.path.exists(path):
        return []
    return re.findall(r'"msg_id":\s*"([^"]+)"', open(path, encoding="utf-8").read())


def _split(content):
    """按 relay 内部同法剥离 split 尾部伪影。"""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class TestRecordEndBoundaries:
    def test_three_records(self):
        # 每条记录 4 行（meta/body/body/blank）→ 记录起点 [0,4,8] → 终点 [4,8,12]
        lines = ('{"msg_id":"a"}\nbody\nbody\n\n' '{"msg_id":"b"}\nbody\nbody\n\n' '{"msg_id":"c"}\nbody\nbody\n\n').split("\n")
        lines = lines[:-1]  # 模拟 relay 内的伪影剥离
        assert mdm._record_starts(lines) == [0, 4, 8]
        assert record_end_boundaries(lines) == [4, 8, 12]

    def test_empty_and_garbage(self):
        assert record_end_boundaries([]) == []
        assert record_end_boundaries(["垃圾", ""]) == []
        assert mdm._record_starts(["垃圾", ""]) == []

    def test_prefix_match_without_parsable_json_is_body(self):
        """`{"msg_id":` 开头但解析失败（工具回显/cat F1）⇒ 不是记录起点，按正文处理。"""
        lines = _split('{"msg_id":\nbodyA\n\n{"msg_id":"b"}\nx\n\n')
        assert mdm._record_starts(lines) == [3]
        assert record_end_boundaries(lines) == [6]

    def test_record_meta_parses_or_none(self):
        assert mdm._record_meta('{"msg_id":"a","role":"user"}') == {"msg_id": "a", "role": "user"}
        assert mdm._record_meta('{"msg_id":') is None            # 撕裂/回显行
        assert mdm._record_meta('{"msg_id": 1, "role": "user"}') == {"msg_id": 1, "role": "user"}
        assert mdm._record_meta('{"role":"user"}') is None       # 无 msg_id 键
        assert mdm._record_meta('{"msg_id": [1,2]}') == {"msg_id": [1, 2]}   # 值类型不限：dict 含 msg_id 即收
        assert mdm._record_meta('[{"msg_id":"a"}]') is None      # 顶层非 dict
        assert mdm._record_meta("垃圾") is None


class TestTurnEnds:
    """轮末两套口径（plan §1）：strict=F1（要后继 user），all=F2（末段也算完整轮）。"""

    FIVE = ("user", "assistant", "user", "assistant", "user")     # 起点 [0,4,8,12,16]

    def test_strict_excludes_open_final_turn(self, tmp_path):
        p1, _, _ = _make_f1(tmp_path, self.FIVE)
        lines = _split(open(p1, encoding="utf-8").read())
        assert strict_turn_ends(lines) == [8, 16]                # 末条 user 无后继 → 不算轮末
        assert all_turn_ends(lines) == [8, 16, 20]               # 末轮末 = len(lines)

    def test_single_user_turn(self, tmp_path):
        p1, _, _ = _make_f1(tmp_path, ("user", "assistant"))
        lines = _split(open(p1, encoding="utf-8").read())
        assert strict_turn_ends(lines) == []                     # F1 里这一轮还没结束
        assert all_turn_ends(lines) == [8]

    def test_no_user_no_turn_ends(self, tmp_path):
        p1, _, _ = _make_f1(tmp_path, ("assistant", "tool", "assistant"))
        lines = _split(open(p1, encoding="utf-8").read())
        assert strict_turn_ends(lines) == [] and all_turn_ends(lines) == []

    def test_missing_role_is_not_user(self):
        lines = _split('{"msg_id":"a"}\nbody\n\n{"msg_id":"b"}\nbody\n\n')
        assert strict_turn_ends(lines) == [] and all_turn_ends(lines) == []

    def test_empty_msg_id_record_still_counts(self):
        """msg_id 为空仍是记录（回填路径），role=user 照样开轮。"""
        lines = _split('{"msg_id":"","role":"user"}\nbody\n\n{"msg_id":"b","role":"assistant"}\nbody\n\n')
        assert mdm._record_starts(lines) == [0, 3]
        assert all_turn_ends(lines) == [6]

    def test_forged_meta_line_does_not_split_turn(self):
        """assistant 正文里混进 `{"msg_id":` 回显：解析失败 → 不切轮（plan R-5 既有面）。"""
        lines = _split(
            '{"msg_id": "u0", "role": "user"}\n提问\n\n'
            '{"msg_id": "a0", "role": "assistant"}\ncat F1 回显\n{"msg_id":\n伪记录\n\n'
            '{"msg_id": "u1", "role": "user"}\n下一次\n\n'
        )
        assert mdm._record_starts(lines) == [0, 3, 8]
        assert record_end_boundaries(lines) == [3, 8, 11]
        assert strict_turn_ends(lines) == [8]
        assert all_turn_ends(lines) == [8, 11]

    def test_turn_ends_ascending_and_within_file(self, tmp_path):
        p1, _, _ = _make_f1(tmp_path, self.FIVE)
        lines = _split(open(p1, encoding="utf-8").read())
        for ends in (strict_turn_ends(lines), all_turn_ends(lines)):
            assert ends == sorted(set(ends)) and all(0 < e <= len(lines) for e in ends)


class TestSnapToBoundary:
    def test_exact_mid_over_below(self):
        b = [4, 8, 12]
        assert snap_to_boundary(8, b, 0) == 8
        assert snap_to_boundary(6, b, 0) == 4      # 记录中部 → 吸附下行
        assert snap_to_boundary(999, b, 0) == 12   # 越界 → 末条
        assert snap_to_boundary(6, b, 8) is None   # 低于进度 → None


class TestRelayProcessedPrefix:
    def test_cut_at_last_complete_turn_end(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path)                       # user/assistant/user
        assert relay_processed_prefix(8, p1, p2) == 8
        assert _read_ids(p2) == ids[:2]                        # 完整轮才搬
        assert _read_ids(p1) == ["id2"]                        # 未结束的尾轮留 F1

    def test_over_report_snaps_down_to_turn_end(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path, ("user", "assistant", "user", "assistant", "user"))
        msgs, sink_id = _capture_loguru("INFO")
        try:
            assert relay_processed_prefix(10, p1, p2) == 8     # 10 落在次轮内部 → 向下吸附到首轮末
        finally:
            logger.remove(sink_id)
        assert _read_ids(p2) == ids[:2] and _read_ids(p1) == ids[2:]
        assert any("过报告吸附" in m for m in msgs)

    def test_unfinished_tail_leaves_nothing_relayed(self, tmp_path):
        p1, p2, _ = _make_f1(tmp_path, ("user", "assistant"))
        msgs, sink_id = _capture_loguru("INFO")
        try:
            assert relay_processed_prefix(999, p1, p2) == 0    # 吸不到轮末 → 整轮留 F1
        finally:
            logger.remove(sink_id)
        assert any("吸不到完整轮末" in m for m in msgs)
        assert not _read_ids(p2) and _read_ids(p1) == ["id0", "id1"]

    def test_report_inside_first_turn_blocks_all(self, tmp_path):
        """6 落在首轮内部：向下没有轮末 → 一条都不搬（绝不多删、绝不撕轮）。"""
        p1, p2, _ = _make_f1(tmp_path)
        assert relay_processed_prefix(6, p1, p2) == 0
        assert not _read_ids(p2) and _read_ids(p1) == ["id0", "id1", "id2"]

    def test_all_cut_then_empty(self, tmp_path):
        """轮末恰在文件倒数第二条边界：搬完后 F1 只剩未结束的尾轮。"""
        p1, p2, ids = _make_f1(tmp_path, ("user", "assistant", "user", "assistant", "user"))
        assert relay_processed_prefix(16, p1, p2) == 16
        assert _read_ids(p2) == ids[:4] and _read_ids(p1) == ["id4"]

    def test_rewrite_failure_restores_f1(self, tmp_path, monkeypatch):
        p1, p2, ids = _make_f1(tmp_path)
        calls = {"n": 0}
        real_write = __import__("os").write

        def flaky_write(fd, data):
            calls["n"] += 1
            # 第 1 次 os.write 是 _append_under_lock 写 F2（放行）；第 2 次起是重写 F1：首次成功半途、之后失败，恢复写放行
            if calls["n"] == 2:
                mid = len(data) // 2
                return real_write(fd, data[:mid])
            if calls["n"] == 3:
                raise OSError("simulated mid-write failure")
            return real_write(fd, data)

        monkeypatch.setattr("agent.md_mirror.os.write", flaky_write)
        assert relay_processed_prefix(8, p1, p2) == 0
        # F1 原文完整恢复
        assert _read_ids(p1) == ["id0", "id1", "id2"]

    def test_min_progress_blocks_regression(self, tmp_path):
        p1, p2, _ = _make_f1(tmp_path)
        assert relay_processed_prefix(8, p1, p2, min_progress=12) == 0
        assert not _read_ids(p2)

    def test_garbage_file_returns_zero(self, tmp_path):
        p1 = tmp_path / "f1.md"; p1.write_text("没有元数据行\n", encoding="utf-8")
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert relay_processed_prefix(1, str(p1), str(tmp_path / "f2.md")) == 0
        finally:
            logger.remove(sink_id)
        assert any("无记录可吸附" in m for m in msgs)

    def test_missing_f1_returns_zero(self, tmp_path):
        assert relay_processed_prefix(3, str(tmp_path / "nope.md"), str(tmp_path / "f2.md")) == 0

    def test_invalid_args_return_zero(self, tmp_path):
        p1, p2, _ = _make_f1(tmp_path)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            for bad in ("abc", -1, None):
                assert relay_processed_prefix(bad, p1, p2) == 0, f"{bad!r} 应拒绝"
        finally:
            logger.remove(sink_id)
        assert sum("无效参数" in m for m in msgs) == 3
        assert _read_ids(p1) == ["id0", "id1", "id2"]

    def test_sequential_relays_append_order(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path, ("user", "assistant", "user", "assistant", "user"))
        assert relay_processed_prefix(999, p1, p2) == 16       # 搬前两个完整轮
        _append_f1(p1, ("assistant", "user"), 5)               # 对话继续：id5(assistant)、id6(user)
        assert relay_processed_prefix(999, p1, p2) == 8        # 又凑出一个完整轮（id4+id5）
        assert _read_ids(p2) == ids[:4] + ["id4", "id5"]
        assert _read_ids(p1) == ["id6"]                        # 尾轮仍留 F1

    def test_truncate_relay_files(self, tmp_path):
        a = tmp_path / "a.md"; b = tmp_path / "b.md"
        a.write_text("data", encoding="utf-8"); b.write_text("data", encoding="utf-8")
        truncate_relay_files(str(a), str(b))
        assert a.read_text(encoding="utf-8") == "" and b.read_text(encoding="utf-8") == ""
        truncate_relay_files(str(tmp_path / "g1.md"), str(tmp_path / "g2.md"))  # 不存在不抛
