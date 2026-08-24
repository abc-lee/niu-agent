"""工程三 Task1：F3 梦境工作集原语单测（tmp_path，零真实依赖）。夹具由 format_message_record 真实输出派生，防手拼漂移。

日志断言用 loguru sink 捕获（md_mirror 用 loguru 而非 stdlib logging，pytest caplog 捕获不到——同 test_remove_outer_timeouts.py 模式）。
"""

import json
import os
import re

import pytest
from loguru import logger

import agent.md_mirror as mdm
from agent.md_mirror import (
    F3_MAX_BYTES_DEFAULT,
    append_record,
    build_f3_from_f2,
    drop_f2_prefix,
    format_message_record,
    truncate_relay_files,
)


def _capture_loguru(level="WARNING"):
    """loguru sink 捕获（md_mirror 用 loguru 而非 stdlib logging，caplog 捕获不到）。"""
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    return messages, sink_id


def _fill(p2, n_records=3, content_fn=None):
    """用真实格式化器向 p2 追加 n_records 条记录（meta+双行正文+空行=每条4行）；返回 ids。"""
    ids = []
    for i in range(n_records):
        role = "user" if i % 2 == 0 else "assistant"
        mid = f"id{i}"
        content = content_fn(i) if content_fn else f"内容{i}\n第二行"
        block = format_message_record(msg_id=mid, created_at="t", role=role, content=content)
        append_record(block, p2)
        ids.append(mid)
    return ids


def _read_ids(path):
    if not os.path.exists(path):
        return []
    return re.findall(r'"msg_id":\s*"([^"]+)"', open(path, encoding="utf-8").read())


def _lines_of(path):
    """文件行（剥离 split 尾部伪影），与边界值==read 显示行数约定一致。"""
    text = open(path, encoding="utf-8").read()
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _prefix_bytes(lines, n):
    """前 n 行（含行尾换行）的字节数。"""
    return sum(len(ln.encode("utf-8")) + 1 for ln in lines[:n])


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """HOME 指向空 tmp：_f3_max_bytes 不读真实 ~/.niu/preferences.json。"""
    home = tmp_path / "home"
    (home / ".niu").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def f3_env(tmp_path, monkeypatch):
    """双命名空间 patch agent.md_mirror 的 F2_PATH/F3_PATH → tmp（F1 由 conftest autouse 隔离）；返回 (p2, p3)。"""
    p2 = str(tmp_path / "f2.md")
    p3 = str(tmp_path / "f3.md")
    monkeypatch.setattr(mdm, "F2_PATH", p2)
    monkeypatch.setattr(mdm, "F3_PATH", p3)
    return p2, p3


class TestBuildF3FromF2:
    def test_missing_or_empty_f2(self, f3_env):
        p2, p3 = f3_env
        assert build_f3_from_f2() == 0                      # F2 不存在
        assert os.path.exists(p3) and open(p3, encoding="utf-8").read() == ""
        open(p2, "w", encoding="utf-8").close()              # F2 空文件
        assert build_f3_from_f2() == 0
        assert open(p3, encoding="utf-8").read() == ""

    def test_small_f2_copied_verbatim(self, f3_env):
        p2, p3 = f3_env
        _fill(p2, 3)
        src = open(p2, encoding="utf-8").read()
        assert build_f3_from_f2() == len(_lines_of(p2))     # 默认 64KB 预算全收
        assert open(p3, encoding="utf-8").read() == src     # 逐字节相同

    def test_over_budget_cuts_at_boundary(self, f3_env):
        p2, p3 = f3_env
        _fill(p2, 3)
        lines = _lines_of(p2)
        bounds = mdm.record_end_boundaries(lines)           # [4, 8, 12]
        budget = _prefix_bytes(lines, bounds[1])            # 恰好卡在第 2 条记录终点
        assert build_f3_from_f2(max_bytes=budget) == bounds[1]
        assert open(p3, encoding="utf-8").read() == "".join(ln + "\n" for ln in lines[:bounds[1]])

    def test_budget_mid_record_rounds_down(self, f3_env):
        p2, p3 = f3_env
        _fill(p2, 3)
        lines = _lines_of(p2)
        bounds = mdm.record_end_boundaries(lines)
        budget = (_prefix_bytes(lines, bounds[0]) + _prefix_bytes(lines, bounds[1])) // 2  # 卡在第 2 条记录中间
        assert build_f3_from_f2(max_bytes=budget) == bounds[0]   # 向下取整，不含残记录
        assert open(p3, encoding="utf-8").read() == "".join(ln + "\n" for ln in lines[:bounds[0]])

    def test_single_record_over_budget_soft_cap(self, f3_env):
        p2, p3 = f3_env
        _fill(p2, 1, content_fn=lambda i: "x" * 20000)      # 单记录远超预算
        lines = _lines_of(p2)
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert build_f3_from_f2(max_bytes=100) == len(lines)
        finally:
            logger.remove(sink_id)
        assert open(p3, encoding="utf-8").read() == open(p2, encoding="utf-8").read()  # F3 含该记录整体
        assert any("软上限" in m for m in msgs)

    def test_malformed_zero_boundaries(self, f3_env):
        p2, p3 = f3_env
        with open(p2, "w", encoding="utf-8") as f:
            f.write("没有元数据行\n垃圾\n")                   # 无 {"msg_id": 行 → 零边界
        assert build_f3_from_f2() == 0
        assert open(p3, encoding="utf-8").read() == ""

    def test_preferences_budget(self, f3_env, tmp_path):
        p2, _ = f3_env
        pref = tmp_path / "home" / ".niu" / "preferences.json"
        _fill(p2, 3)
        lines = _lines_of(p2)
        bounds = mdm.record_end_boundaries(lines)
        assert build_f3_from_f2() == len(lines)              # 无 preferences → 默认 64KB 全收
        mid = (_prefix_bytes(lines, bounds[0]) + _prefix_bytes(lines, bounds[1])) // 2
        pref.write_text(json.dumps({"context": {"dreamWorksetBytes": mid}}), encoding="utf-8")
        assert build_f3_from_f2() == bounds[0]               # 配置生效（非默认）
        for bad in (0, -1, "abc"):                           # 非法值回退默认
            pref.write_text(json.dumps({"context": {"dreamWorksetBytes": bad}}), encoding="utf-8")
            assert mdm._f3_max_bytes() == F3_MAX_BYTES_DEFAULT


class TestDropF2Prefix:
    def test_normal_drop(self, f3_env):
        p2, _ = f3_env
        _fill(p2, 3)
        lines0 = _lines_of(p2)
        assert drop_f2_prefix(8) == (8, "id1")               # 末删记录 msg_id
        assert open(p2, encoding="utf-8").read() == "".join(ln + "\n" for ln in lines0[8:])  # 剩余==原后缀

    def test_mid_record_snaps_down(self, f3_env):
        p2, _ = f3_env
        _fill(p2, 3)
        assert drop_f2_prefix(6) == (4, "id0")               # 6 落 id1 记录内 → 向下吸附，不多删
        assert _read_ids(p2) == ["id1", "id2"]

    def test_over_max_lines_rejected(self, f3_env):
        p2, _ = f3_env
        _fill(p2, 3)
        original = open(p2, encoding="utf-8").read()
        msgs, sink_id = _capture_loguru("WARNING")
        try:
            assert drop_f2_prefix(8, max_lines=4) == (0, "")
        finally:
            logger.remove(sink_id)
        assert any("超上限" in m for m in msgs)
        assert open(p2, encoding="utf-8").read() == original

    def test_invalid_n_rejected(self, f3_env):
        p2, _ = f3_env
        _fill(p2, 3)
        original = open(p2, encoding="utf-8").read()
        for bad_n in (0, -3, "abc", 999):                    # 0/负数/非 int/超总行数(max_lines=None)
            assert drop_f2_prefix(bad_n) == (0, ""), f"n={bad_n!r} 应拒绝"
            assert open(p2, encoding="utf-8").read() == original, f"n={bad_n!r} 不得动文件"

    def test_malformed_prefix_without_msg_id(self, f3_env):
        p2, _ = f3_env
        with open(p2, "w", encoding="utf-8") as f:
            f.write('{"msg_id":\nbodyA\n\n')                 # 畸形 meta：有边界前缀但无 msg_id 值
            f.write(format_message_record(msg_id="real1", created_at="t", role="user", content="bodyB"))
        original = open(p2, encoding="utf-8").read()
        assert drop_f2_prefix(3) == (0, "")
        assert open(p2, encoding="utf-8").read() == original

    def test_write_failure_restores(self, f3_env, monkeypatch):
        p2, _ = f3_env
        _fill(p2, 3)
        original = open(p2, encoding="utf-8").read()
        real = mdm._write_all
        calls = {"n": 0}

        def flaky(fd, data):
            # 第 1 次调用=重写剩余（中途失败）；第 2 次=恢复写——委托真实 _write_all 落盘
            # （纯 no-op mock 的"成功"不写字节，无法验证恢复；同 test_md_relay.py 在 os.write 层注入的思路）
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("boom")
            return real(fd, data)

        monkeypatch.setattr(mdm, "_write_all", flaky)
        assert drop_f2_prefix(8) == (0, "")
        assert open(p2, encoding="utf-8").read() == original  # F2 原文恢复

    def test_drop_single_record_empties(self, f3_env):
        p2, _ = f3_env
        _fill(p2, 1)
        lines = _lines_of(p2)
        assert drop_f2_prefix(len(lines)) == (len(lines), "id0")
        assert open(p2, encoding="utf-8").read() == ""


class TestTruncateRelayFilesF3:
    def test_no_arg_truncates_all_three(self, f3_env):
        p2, p3 = f3_env
        for p in (mdm.F1_PATH, p2, p3):                      # F1 已由 conftest autouse 指向 tmp
            with open(p, "w", encoding="utf-8") as f:
                f.write("data")
        truncate_relay_files()                               # 无参调用 → 清空 F1/F2/F3
        assert all(open(p, encoding="utf-8").read() == "" for p in (mdm.F1_PATH, p2, p3))
