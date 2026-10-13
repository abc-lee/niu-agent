#!/usr/bin/env python3
"""媒体能力两态动态注入行为测试（D-29，spec v3.3）。

夹具配方沿用既有动态块测试先例：
- stub runner = NiuRunner.__new__ + 实例属性最小面（先例 test_compression_idle_system_fix.py
  的半真实配方；本文件只测「真实 _build_dynamic_block / _on_before_llm + 打桩的行生成器」）
- 行级字节稳定比较只比行方法返回值，不比含 Current Time 的整块（先例
  test_dynamic_block_park_reminder.py 的 R3-B 纪律：整块跨秒必 flaky）
- 开关两态切换 = monkeypatch 唯一读取模块的函数属性（调用时刻函数级 import 可见补丁）；
  热翻转全链路 = monkeypatch("niu_api.config.CONFIG_PATH") 指向临时配置文件（既有注入点）

行为级判据（不 grep 源文件）：注入输出随开关两态变化 / 读取异常兜底 / 空块边界 /
静态区翻转前后全等 / 多轮不叠加。
"""
import json

import pytest

from agent.runner import NiuRunner

OFF_TEXT = "媒体向量入库与查询能力未开启，如有需求，参照系统管理手册，与用户协商是否安装。"
ON_TEXT = "照片入库后，系统会异步计算向量并入库。你可以使用自然语言查找已入库的照片。举例:disk(\"/lightrag/search_media '<具体画面描述>'\")"


def _make_runner():
    """最小面 stub：跳过 __init__（副作用重）；行生成器打桩，只测两态注入与刷新链路。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner._active_dynamic_block = ""
    runner.default_model = "qwen-test"  # 非 claude ⇒ content 保持字符串
    runner.static_system_prompt = "STATIC_PROMPT_MARKER"
    runner.dynamic_system_prefix = "\n\nDISK_DESC_MARKER"
    runner._first_turn_extra_injection = ""
    runner._inject_dynamic_resources = lambda ctx: ("", {})
    runner._park_reminder_line = lambda: ""
    runner._daily_reminder_line = lambda: ""
    return runner


def _blocks(messages):
    return [
        m for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[系统动态信息]")
    ]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """临时 user-config：CONFIG_PATH 重定向（保持既有 monkeypatch 注入点生效）。"""
    path = tmp_path / "user-config.json"
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _isolate_real_memory(monkeypatch):
    """隔离真实 ~/.niu/memory.json（P3-2）：`_on_before_llm` 在 agent/runner.py 模块级调用点
    调 `_load_memory_for_prompt()`（非实例方法——patch 模块属性即覆盖裸名解析），不隔离会把
    开发者真实记忆带进测试消息，且 test_static_region_unchanged_across_flip 的两份静态区
    快照会依赖该文件在两次读取间未被并发改写（开发机 Niu 本体运行时可写）⇒ 理论假红。
    先例：tests/test_assemble_system_message_v2.py 的 patch.object(runner_mod, ...) 同配方。"""
    import agent.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_load_memory_for_prompt", lambda: "")


def _write_switch(config_file, enabled):
    config_file.write_text(
        json.dumps({"media_ingest": {"enabled": enabled}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_two_states_flip_and_single_block(config_file):
    """两态翻转：off ⇒ 未开启文本且无已开启文本；翻转后开 ⇒ 已开启文本；
    每轮恰一个动态块、旧块被移除不叠加；多轮历史不留媒体行。"""
    runner = _make_runner()
    _write_switch(config_file, False)

    msgs = [{"role": "system", "content": ""}, {"role": "user", "content": "第一轮输入"}]
    runner._on_before_llm(msgs, turn=1)
    blocks = _blocks(msgs)
    assert len(blocks) == 1, "每轮恰好一个动态块"
    assert OFF_TEXT in blocks[0]["content"]
    assert ON_TEXT not in blocks[0]["content"]

    # 免重启热翻转：改配置 ⇒ 下一轮 _on_before_llm 即呈新状态
    _write_switch(config_file, True)
    msgs.append({"role": "assistant", "content": "回复"})
    msgs.append({"role": "user", "content": "第二轮输入"})
    runner._on_before_llm(msgs, turn=2)
    blocks2 = _blocks(msgs)
    assert len(blocks2) == 1, "旧块被精确移除，不得叠加"
    assert ON_TEXT in blocks2[0]["content"]
    assert OFF_TEXT not in blocks2[0]["content"]
    # 历史不留媒体行：旧块已移除，仅存本轮块
    assert msgs[1]["content"] == "第一轮输入"


def test_read_failure_falls_back_to_off(config_file, monkeypatch):
    """读取异常（模拟模块 import 失败/读失败）⇒ 未开启态兜底、不抛、块其余内容完整。"""
    import niu_api.internal.media_ingest_config as mic

    def _boom():
        raise RuntimeError("config read boom")

    monkeypatch.setattr(mic, "media_ingest_enabled", _boom)
    runner = _make_runner()
    assert runner._media_ingest_prompt_line() == OFF_TEXT, "兜底 = 未开启态（与读取配方默认 False 一致）"

    # 全链路：_on_before_llm 不得因读取异常中断，块仍含未开启文本与时间行
    msgs = [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(msgs, turn=1)
    blocks = _blocks(msgs)
    assert len(blocks) == 1
    assert OFF_TEXT in blocks[0]["content"]
    assert "Current Time:" in blocks[0]["content"], "时间行不受读取异常影响"


def test_block_absent_never_creates_second_user_message():
    """空块边界：首条消息非 system ⇒ 无动态块，也绝不新造第二条 role=user 消息（消息序列原样）。"""
    runner = _make_runner()
    msgs = [{"role": "user", "content": "无 system 的退化形态"}, {"role": "assistant", "content": "x"}]
    runner._on_before_llm(msgs, turn=1)
    assert _blocks(msgs) == [], "退化形态无块 ⇒ 媒体行随块缺席"
    assert [m["role"] for m in msgs] == ["user", "assistant"], "不得新增任何消息"


def test_media_line_rides_block_even_when_other_lines_empty(config_file):
    """injection/例行/暂存全空时媒体行仍注入，且**行边界与位置被结构断言钉死**（P3-1/P3-3）：
    媒体行独立成一行（杀掉「删掉拼接前导 \n ⇒ 媒体句粘到上一行」变异）、位于 Current Time 行
    之前（杀掉「把媒体行挪到仪表盘之后 / injection 之前」变异；不引用仪表盘行，兼容其缺席）。
    必须挂 `config_file` 夹具（v1.1 P1 修订）：`_on_before_llm` 走真实行方法 ⇒ 不重定向配置会读真实 `~/.niu` 用户配置（假红/随环境漂移）。"""
    runner = _make_runner()  # 所有行生成器已打桩为空串
    _write_switch(config_file, False)  # 钉死「未开启」态，防真实配置漂移（v1.1：本测试是漏网夹具，补齐）
    _blocks_msgs = [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(_blocks_msgs, turn=1)
    blocks = _blocks(_blocks_msgs)
    assert len(blocks) == 1, "每轮恰好一个动态块"
    lines = blocks[0]["content"].splitlines()
    assert OFF_TEXT in lines, "媒体行独立成一行（拼接前导 \n 生效，未粘到上一行/框架头）"
    assert lines[0] == "[系统动态信息]", "框架头独占首行"
    assert "Current Time:" in lines[-1], "Current Time 恒为末行"
    assert lines.index(OFF_TEXT) < len(lines) - 1, "媒体行位于 Current Time 行之前（index 顺序比较）"


def test_static_region_unchanged_across_flip(config_file):
    """缓存前缀不变式：开关翻转前后 messages[0]（静态区）content 逐字节全等——
    媒体行只在动态块内，从不进静态区。"""
    runner = _make_runner()
    _write_switch(config_file, False)
    m1 = [{"role": "system", "content": ""}, {"role": "user", "content": "a"}]
    runner._on_before_llm(m1, turn=1)
    static_off = m1[0]["content"]

    _write_switch(config_file, True)
    m2 = [{"role": "system", "content": ""}, {"role": "user", "content": "b"}]
    runner._on_before_llm(m2, turn=1)
    assert m2[0]["content"] == static_off, "静态区字节翻转前后全等（缓存前缀不被打破）"


def test_two_state_text_content_guard():
    """内容守卫：两态文本不含「开关」字样、不点名配置键、不含 xxxxxx 占位；
    模块常量与用户原话逐字一致（防日后「好心」改写）。"""
    import agent.runner as runner_mod

    for text in (OFF_TEXT, ON_TEXT):
        assert "开关" not in text
        assert "media_ingest" not in text
        assert "xxxxxx" not in text
    assert runner_mod._MEDIA_PROMPT_OFF == OFF_TEXT
    assert runner_mod._MEDIA_PROMPT_ON == ON_TEXT
