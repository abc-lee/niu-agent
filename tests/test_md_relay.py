"""工程二 Task1：relay 原语单测（tmp_path，零真实依赖）。夹具由 format_message_record 真实输出派生，防手拼漂移。"""

from agent.md_mirror import (
    format_message_record,
    append_record,
    record_end_boundaries,
    snap_to_boundary,
    relay_processed_prefix,
    truncate_relay_files,
)


def _make_f1(tmp_path, n_records=3):
    """用真实格式化器构造 n_records 条记录（meta+双行正文+空行=每条4行）。返回 (p1, p2, ids)。"""
    p1 = tmp_path / "f1.md"
    p2 = tmp_path / "f2.md"
    ids = []
    for i in range(n_records):
        role = "user" if i % 2 == 0 else "assistant"
        mid = f"id{i}"
        block = format_message_record(msg_id=mid, created_at="t", role=role, content=f"内容{i}\n第二行")
        append_record(block, str(p1))
        ids.append(mid)
    return str(p1), str(p2), ids


def _read_ids(path):
    import os, re
    if not os.path.exists(path):
        return []
    return re.findall(r'"msg_id":\s*"([^"]+)"', open(path, encoding="utf-8").read())


class TestRecordEndBoundaries:
    def test_three_records(self):
        # 每条记录 4 行（meta/body/body/blank）→ 边界 [4,8,12]
        lines = ('{"msg_id":"a"}\nbody\nbody\n\n' '{"msg_id":"b"}\nbody\nbody\n\n' '{"msg_id":"c"}\nbody\nbody\n\n').split("\n")
        lines = lines[:-1]  # 模拟 relay 内的伪影剥离
        assert record_end_boundaries(lines) == [4, 8, 12]

    def test_empty_and_garbage(self):
        assert record_end_boundaries([]) == []
        assert record_end_boundaries(["垃圾", ""]) == []


class TestSnapToBoundary:
    def test_exact_mid_over_below(self):
        b = [4, 8, 12]
        assert snap_to_boundary(8, b, 0) == 8
        assert snap_to_boundary(6, b, 0) == 4      # 记录中部 → 吸附下行
        assert snap_to_boundary(999, b, 0) == 12   # 越界 → 末条
        assert snap_to_boundary(6, b, 8) is None   # 低于进度 → None


class TestRelayProcessedPrefix:
    def test_cut_two_of_three(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path)
        cut = relay_processed_prefix(8, p1, p2)
        assert cut == 8
        assert _read_ids(p2) == ["id0", "id1"]
        assert _read_ids(p1) == ["id2"]

    def test_mid_record_snap_does_not_tear(self, tmp_path):
        p1, p2, _ = _make_f1(tmp_path)
        assert relay_processed_prefix(6, p1, p2) == 4  # 6 落在 id1 记录内 → 吸附 4
        assert _read_ids(p2) == ["id0"]
        assert _read_ids(p1) == ["id1", "id2"]

    def test_all_cut_then_empty(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path)
        cut = relay_processed_prefix(999, p1, p2)
        assert cut == 12
        assert _read_ids(p1) == []
        assert _read_ids(p2) == ids

    def test_min_progress_blocks_regression(self, tmp_path):
        p1, p2, _ = _make_f1(tmp_path)
        assert relay_processed_prefix(4, p1, p2, min_progress=8) == 0

    def test_garbage_file_returns_zero(self, tmp_path):
        p1 = tmp_path / "f1.md"; p1.write_text("没有元数据行\n", encoding="utf-8")
        assert relay_processed_prefix(1, str(p1), str(tmp_path / "f2.md")) == 0

    def test_missing_f1_returns_zero(self, tmp_path):
        assert relay_processed_prefix(3, str(tmp_path / "nope.md"), str(tmp_path / "f2.md")) == 0

    def test_sequential_relays_append_order(self, tmp_path):
        p1, p2, ids = _make_f1(tmp_path)
        relay_processed_prefix(4, p1, p2)
        relay_processed_prefix(999, p1, p2)
        assert _read_ids(p2) == ids

    def test_truncate_relay_files(self, tmp_path):
        a = tmp_path / "a.md"; b = tmp_path / "b.md"
        a.write_text("data", encoding="utf-8"); b.write_text("data", encoding="utf-8")
        truncate_relay_files(str(a), str(b))
        assert a.read_text(encoding="utf-8") == "" and b.read_text(encoding="utf-8") == ""
        truncate_relay_files(str(tmp_path / "g1.md"), str(tmp_path / "g2.md"))  # 不存在不抛
