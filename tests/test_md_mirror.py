"""工程一：MD 镜像层单测（全 mock/tmp_path，零真实依赖）。"""

import json
import threading

import pytest

from agent.md_mirror import (
    TOOL_OUTPUT_HEAD_BYTES,
    TOOL_OUTPUT_MARKER,
    TOOL_OUTPUT_MAX_BYTES,
    TOOL_OUTPUT_TAIL_BYTES,
    append_record,
    format_message_record,
    truncate_tool_output,
)


class TestFormatMessageRecord:
    def test_user_record(self):
        block = format_message_record(
            msg_id="m1", created_at="2026-08-24T10:00:00", role="user", content="帮我查周报"
        )
        lines = block.split("\n")
        meta = json.loads(lines[0])
        assert meta == {"msg_id": "m1", "ts": "2026-08-24T10:00:00", "role": "user"}
        assert lines[1] == "帮我查周报"
        assert block.endswith("\n\n")  # 记录间空行分隔

    def test_assistant_with_tool_calls(self):
        calls = [{"name": "do_grep", "arguments": {"pattern": "周报"}}]
        block = format_message_record(
            msg_id="m2", created_at="t", role="assistant",
            content="我来搜索", tool_calls=calls,
        )
        meta = json.loads(block.split("\n")[0])
        assert meta["tool_calls"] == calls
        assert block.split("\n")[1] == "我来搜索"

    def test_tool_record_fenced_and_truncated(self):
        big = "y" * 3000
        block = format_message_record(
            msg_id="m3", created_at="t", role="tool", content=big,
            tool_call_id="call_x",
        )
        lines = block.split("\n")
        meta = json.loads(lines[0])
        assert meta["role"] == "tool" and meta["tool_call_id"] == "call_x"
        assert lines[1] == "```output"
        assert lines[3] == "```"
        assert TOOL_OUTPUT_MARKER in lines[2]

    def test_degraded_reason_in_meta(self):
        block = format_message_record(
            msg_id="m4", created_at="t", role="assistant",
            content="[系统繁忙]", degraded_reason="timeout",
        )
        assert json.loads(block.split("\n")[0])["degraded_reason"] == "timeout"

    def test_subagent_msg_supported(self):
        block = format_message_record(
            msg_id="m5", created_at="t", role="subagent_msg", content="子Agent结果"
        )
        assert json.loads(block.split("\n")[0])["role"] == "subagent_msg"

    def test_system_skipped(self):
        assert format_message_record(msg_id="m6", created_at="t", role="system", content="通知") is None

    def test_empty_content_ok(self):
        block = format_message_record(msg_id="m7", created_at="t", role="assistant", content="")
        assert block.split("\n")[1] == ""


class TestTruncateToolOutput:
    def test_short_text_unchanged(self):
        assert truncate_tool_output("你好") == "你好"

    def test_exactly_max_bytes_unchanged(self):
        # "a"*1999 + 一个三字节汉字 = 2002？不行，要恰好 2000：1997 个 a + 1 个三字节字
        text = "a" * 1997 + "好"
        assert len(text.encode("utf-8")) == 2000
        assert truncate_tool_output(text) == text

    def test_over_max_truncated_with_marker(self):
        text = "x" * 3000
        out = truncate_tool_output(text)
        raw = out.encode("utf-8")
        # 结构：前缀 + <已精简> + 后缀
        assert TOOL_OUTPUT_MARKER in out
        assert out.startswith("x" * 100)  # 前缀以 x 开头
        assert out.endswith("x" * 100)  # 后缀以 x 结尾
        assert len(out.split(TOOL_OUTPUT_MARKER)) == 2
        # 总字节 = 1200 + marker字节(11=3CJK×3B+尖括号2B) + 800 = 2011
        assert len(raw) == TOOL_OUTPUT_HEAD_BYTES + TOOL_OUTPUT_TAIL_BYTES + len(TOOL_OUTPUT_MARKER.encode("utf-8"))

    def test_cjk_boundary_no_mojibake(self):
        # 1200 字节切点落在三字节汉字中间：1199 个 'a' 后接 "好好好…"，切点在第一个"好"内部
        text = "a" * 1199 + "好" * 600
        out = truncate_tool_output(text)
        head = out.split(TOOL_OUTPUT_MARKER)[0]
        assert head == "a" * 1199  # 精确相等：1200 字节切点落在"好"内部，回退后恰为 1199 个 a
        assert "\ufffd" not in head  # 无替换符=无乱码（str 本身不可能非法，防 U+FFFD 注入）
        tail = out.split(TOOL_OUTPUT_MARKER)[1]
        assert "\ufffd" not in tail
        assert "好" in tail and tail != ""  # tail 非空且含汉字（防 _safe_decode_tail 退化空串）

    def test_tail_boundary_no_mojibake(self):
        # 尾窗起点 2749-800=1949 落在"好"内部（非字符边界）——显式触发 _safe_decode_tail 向后丢弃分支
        text = "好" * 900 + "b" * 49
        out = truncate_tool_output(text)
        assert "\ufffd" not in out
        assert out.endswith("b" * 49)
        assert "好" in out  # 头部窗口含完整汉字（900 好×3B=2700，头 1200B=400 个整"好"）

    def test_multibyte_exact_cut(self):
        # 切点恰好在字符边界（1200 = 400×3B）
        text = "好" * 700
        out = truncate_tool_output(text)
        assert "\ufffd" not in out
        assert TOOL_OUTPUT_MARKER in out


class TestAppendRecord:
    def test_creates_dir_and_appends(self, tmp_path):
        p = tmp_path / "md" / "f1.md"  # md 目录不存在，须自建
        assert append_record("block-a\n\n", str(p)) is True
        assert append_record("block-b\n\n", str(p)) is True
        assert p.read_text(encoding="utf-8") == "block-a\n\nblock-b\n\n"

    def test_failure_returns_false_not_raise(self, tmp_path):
        # 目标是目录 → open 必败 → 吞异常返回 False
        d = tmp_path / "adir"
        d.mkdir()
        assert append_record("x", str(d)) is False

    def test_concurrent_appends_no_interleave(self, tmp_path):
        # 块放大到 >64KB：O_APPEND 小写入近似原子，只有大块才能暴露无锁交叉撕裂（审查 B-P1）
        p = tmp_path / "f1.md"
        n = 8
        filler = "z" * 65536
        blocks = [f'{{"msg_id":"i{i}"}}\n{filler}{i}\n\n' for i in range(n)]

        def worker(b):
            append_record(b, str(p))

        threads = [threading.Thread(target=worker, args=(b,)) for b in blocks]
        [t.start() for t in threads]
        [t.join() for t in threads]
        text = p.read_text(encoding="utf-8")
        # 总长度守恒
        assert len(text.encode("utf-8")) == sum(len(b.encode("utf-8")) for b in blocks)
        # 每个 block 作为完整子串存在——撕裂即失败（锁存在的意义所在）
        for b in blocks:
            assert b in text


from agent.session import MessageStore


class TestAddMessageHook:
    async def test_mirror_written_after_commit(self, tmp_path):
        store = MessageStore(str(tmp_path / "messages.db"))
        await store.init_db()
        md = tmp_path / "f1.md"
        msg_id = await store.add_message(
            role="user", content="你好", md_path=str(md),
        )
        text = md.read_text(encoding="utf-8")
        assert msg_id in text
        assert '"role": "user"' in text  # json.dumps 默认带空格分隔

    async def test_system_not_mirrored(self, tmp_path):
        store = MessageStore(str(tmp_path / "messages.db"))
        await store.init_db()
        md = tmp_path / "f1.md"
        await store.add_message(role="system", content="通知", md_path=str(md))
        assert not md.exists()  # system 跳过 → 连文件都不建

    async def test_commit_failure_no_mirror(self, tmp_path):
        store = MessageStore(str(tmp_path / "messages.db"))
        await store.init_db()
        md = tmp_path / "f1.md"
        # tool_calls 不可序列化 → json.dumps 在 INSERT 前抛错 → 无 commit 即无镜像
        with pytest.raises(TypeError):
            await store.add_message(
                role="assistant", content="x", tool_calls=[{"bad": object()}],
                md_path=str(md),
            )
        assert not md.exists()

    async def test_mirror_failure_does_not_break_db(self, tmp_path):
        store = MessageStore(str(tmp_path / "messages.db"))
        await store.init_db()
        # md 路径指向一个已存在文件但不可写目录场景：直接指向目录
        bad = tmp_path / "badir"
        bad.mkdir()
        msg_id = await store.add_message(role="user", content="hi", md_path=str(bad))
        assert msg_id  # DB 写入成功返回 id
        # DB 里确实有这条
        msgs = await store.get_messages()
        assert any(m.content == "hi" for m in msgs)
