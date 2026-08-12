"""飞书 Adapter 单元测试（v14 流式卡片修复）

覆盖：
1. _on_stream 累积全文（飞书要求每次传元素累计全文，前缀一致→打字机续打）
2. _truncate_card_text 字节守卫（CJK 3B/字，30KB 上限）
3. _on_send ask_finalize 状态判重（终结记标记 / 问题消息不清标记 / route_out 重复跳过+清标记）
4. _on_send F3 前缀补全（流式中断 best-effort）
5. _on_send 媒体回退保留（pending_files / failed_images）
6. IMGateway.send_sync（同步 SEND 桥：pop_reply_to / ask_finalize 透传）
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

# conftest 只加 repo root；niu_feishu_adapter 包在 im-adapters/feishu/src（R11-B-P3）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "im-adapters" / "feishu" / "src"))


def _encode(msg: dict) -> bytes:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


async def _read_one(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict | None:
    try:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = int.from_bytes(header, "big")
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(data.decode("utf-8"))
    except (TimeoutError, asyncio.IncompleteReadError):
        return None


# ── _truncate_card_text 字节守卫 ──

def test_truncate_card_text_byte_guard():
    from niu_feishu_adapter.adapter import _truncate_card_text
    # 短文本不截断
    short = "你好，世界" * 100  # 600 bytes
    assert _truncate_card_text(short) == short
    # CJK 长文本：30000 字节截断（3B/字 × 10000 字），必须 ≤ 29500 + 后缀
    long_cjk = "你" * 20000  # 60000 bytes
    out = _truncate_card_text(long_cjk)
    suffix = "\n\n...[内容已截断]"
    assert len(out.encode("utf-8")) <= 29500 + len(suffix.encode("utf-8"))
    assert out.endswith(suffix)
    # 截断不切坏 UTF-8 字符（errors='ignore' 兜底）
    assert out.encode("utf-8").decode("utf-8") == out


# ── _on_stream 累积全文 ──

@pytest.mark.asyncio
async def test_on_stream_accumulates_full_text(monkeypatch):
    """飞书契约：每次 update 传累计全文（非增量），建卡=首 chunk，后续=截断累计全文"""
    from niu_feishu_adapter.adapter import FeishuAdapter, _truncate_card_text
    import niu_feishu_adapter.feishu_api as api

    created = []
    updates = []

    async def fake_create(client, receive_id, content, reply_to_id=None):
        created.append((receive_id, content, reply_to_id))
        return "card1", "msg1"

    async def fake_update(client, card_id, content, seq):
        updates.append((card_id, content, seq))
        return None

    monkeypatch.setattr(api, "create_card", fake_create)
    monkeypatch.setattr(api, "update_card_element", fake_update)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"

    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "你好", "reply_to_id": None})
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "，世界", "reply_to_id": None})
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "！", "reply_to_id": None})

    # 建卡：内容=首 chunk（字节截断版）
    assert created == [("ch1", "你好", None)]
    # 第 2+ 次 update：content == _truncate_card_text(accumulated)，seq 严格递增
    assert [u[0] for u in updates] == ["card1", "card1"]
    assert updates[0][1] == _truncate_card_text("你好，世界")
    assert updates[0][2] == 2
    assert updates[1][1] == _truncate_card_text("你好，世界！")
    assert updates[1][2] == 3
    # accumulated 保留 raw 全文
    state = adapter._card_states["ch1"]
    assert state.accumulated == "你好，世界！"
    assert state.last_content == "你好，世界！"


@pytest.mark.asyncio
async def test_on_stream_empty_keepalive_only_seq(monkeypatch):
    """空内容 = 信号通知：只递增 seq，不建卡不更新"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    updates = []

    async def fake_update(client, card_id, content, seq):
        updates.append(seq)
        return None

    monkeypatch.setattr(api, "update_card_element", fake_update)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    state = CardState("card1", "ch1")
    state.seq = 5
    adapter._card_states["ch1"] = state
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "", "reply_to_id": None})
    assert state.seq == 6
    assert updates == []
    # 无 state 时空内容直接忽略
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch2", "content": ""})
    assert "ch2" not in adapter._card_states


# ── _on_stream 死卡错误码 pop + 重建 ──

@pytest.mark.asyncio
@pytest.mark.parametrize("dead_code", [300309, 200850, 200740, 200750])
async def test_on_stream_dead_card_pop_and_rebuild(monkeypatch, dead_code):
    """死卡错误码（streaming closed/timeout/实体不存在/过期）：
    先 finalize_card 纯终结旧卡（4 参 + 完整卡片 JSON + subtitle 清空）→ pop → 重建新卡
    （种子 = 旧 accumulated 已含当前 chunk 单次，无 double-append；seq=1；message_id 字段）"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState, _truncate_card_text
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    created = []

    async def fake_update(client, card_id, content, seq):
        return dead_code  # 参数化返回 pop 集错误码

    async def fake_finalize(client, card_id, final_json, seq):
        finalized.append((client, card_id, final_json, seq))
        return True

    async def fake_create(client, receive_id, content, reply_to_id=None):
        created.append((receive_id, content, reply_to_id))
        return "card2", "msg2"

    monkeypatch.setattr(api, "update_card_element", fake_update)
    monkeypatch.setattr(api, "finalize_card", fake_finalize)
    monkeypatch.setattr(api, "create_card", fake_create)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    old = CardState("card1", "ch1", "om_reply")
    old.accumulated = "前文"
    old.seq = 3
    adapter._card_states["ch1"] = old

    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "新chunk", "reply_to_id": None})

    # 旧卡终结：finalize_card 以 (client, card_id, 含 subtitle:"" 的完整卡片 JSON, seq) 被调用
    assert len(finalized) == 1
    _, fin_card_id, fin_json, fin_seq = finalized[0]
    assert fin_card_id == "card1"
    fin_card = json.loads(fin_json)
    assert fin_card["header"]["subtitle"]["content"] == ""  # 清空"思考中..."
    assert fin_card["config"]["streaming_mode"] is False
    assert fin_card["body"]["elements"][0]["content"] == _truncate_card_text("前文新chunk")
    assert fin_seq == 5  # update 用 seq=4（3+1），finalize 前再 += 1 → 5（严格递增防 300317）
    # 重建：内容 = 旧 accumulated（已含当前 chunk）单次——无 double-append
    assert created == [("ch1", _truncate_card_text("前文新chunk"), "om_reply")]  # 携带旧 reply_to_id
    new_state = adapter._card_states["ch1"]
    assert new_state.card_id == "card2"
    assert new_state.message_id == "msg2"
    assert new_state.accumulated == "前文新chunk"  # 单次（非 "前文新chunk新chunk"）
    assert new_state.seq == 1  # 新卡序号从 1 重启
    assert new_state.last_content == "前文新chunk"


@pytest.mark.asyncio
async def test_on_stream_dead_card_rebuild_failure_drops_state(monkeypatch):
    """重建 create_card 失败（返回 falsy）：旧卡终结后 _card_states 无该 receive_id（不留死 state，
    下个 chunk 走正常建卡重试——镜像既有建卡失败检查）"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []

    async def fake_update(client, card_id, content, seq):
        return 300309

    async def fake_finalize(client, card_id, final_json, seq):
        finalized.append(card_id)
        return True

    async def fake_create_fail(client, receive_id, content, reply_to_id=None):
        return "", None  # create_card 内部消化异常转 falsy 返回

    monkeypatch.setattr(api, "update_card_element", fake_update)
    monkeypatch.setattr(api, "finalize_card", fake_finalize)
    monkeypatch.setattr(api, "create_card", fake_create_fail)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    old = CardState("card1", "ch1")
    old.accumulated = "前文"
    old.seq = 1
    adapter._card_states["ch1"] = old

    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "新chunk", "reply_to_id": None})

    assert finalized == ["card1"]  # 旧卡已 best-effort 终结
    assert "ch1" not in adapter._card_states  # 旧卡终结后无该 receive_id（不留 card_id="" 死 state）


@pytest.mark.asyncio
async def test_on_stream_transient_error_keeps_state(monkeypatch):
    """瞬时/可重试错误（None=成功或异常、300120/300317/200860 不在 pop 集）——保留 state 下次重试"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    async def fake_update(client, card_id, content, seq):
        return 300120  # 服务端内部错误——不落入 pop 集

    monkeypatch.setattr(api, "update_card_element", fake_update)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    state = CardState("card1", "ch1")
    state.accumulated = "前文"
    state.seq = 1
    adapter._card_states["ch1"] = state

    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "新chunk", "reply_to_id": None})

    assert adapter._card_states["ch1"] is state  # state 保留（不误杀活卡）
    assert state.accumulated == "前文新chunk"  # 累积不丢
    assert state.last_content == "前文新chunk"


@pytest.mark.asyncio
async def test_on_stream_new_card_discards_ask_finalized(monkeypatch):
    """v11 + ImplReviewR3-P1：建新卡清标记、**保留内容记录**（2c 多轮拼接需跨卡累积）"""
    from niu_feishu_adapter.adapter import FeishuAdapter
    import niu_feishu_adapter.feishu_api as api

    async def fake_create(client, receive_id, content, reply_to_id=None):
        return "card1", "msg1"

    monkeypatch.setattr(api, "create_card", fake_create)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"
    adapter._ask_finalized.add("ch1")  # 上一轮 ask_user 终结残留标记
    adapter._ask_finalized_content["ch1"] = "a1"  # 上一轮终结内容（供多轮拼接）
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "新回复", "reply_to_id": None})
    assert "ch1" not in adapter._ask_finalized  # 建卡 discard 标记（新卡出现，后续 SEND 走 state 分支）
    assert adapter._ask_finalized_content.get("ch1") == "a1"  # 记录保留（round2 finalize 拼接 a1+a2）


# ── _on_send ask_finalize 状态判重 ──

def test_build_final_body_total_budget():
    """R5-P2 + ImplReview-P2-1：多段+图 / 单段超长 终结卡片 JSON 总字节 ≤ 30000（元素开销计入预算）"""
    import json as _json
    from niu_feishu_adapter.adapter import FeishuAdapter
    build = FeishuAdapter._build_final_body

    def _wrap(elements):
        return {
            "schema": "2.0",
            "header": {"title": {"content": "Niu助手", "tag": "plain_text"},
                       "subtitle": {"content": "", "tag": "plain_text"}},
            "config": {"streaming_mode": False, "update_multi": True},
            "body": {"elements": elements},
        }

    # 5 个超长 CJK 段 + 3 张图（最坏多段场景）
    big = "你" * 15000  # 45KB
    imgs = [{"img_key": "img_" + "x" * 24, "alt": "照片"},
            {"img_key": "img_" + "y" * 24, "alt": "照片2"},
            {"img_key": "img_" + "z" * 24, "alt": "照片3"}]
    elements = build("[PHOTO_SEP]".join([big] * 5), imgs)
    total = len(_json.dumps(_wrap(elements), ensure_ascii=False).encode("utf-8"))
    assert total <= 30000, f"multi-seg card {total}B > 30000B"

    # 单段超长 + 无图
    elements2 = build("你" * 40000, [])
    total2 = len(_json.dumps(_wrap(elements2), ensure_ascii=False).encode("utf-8"))
    assert total2 <= 30000, f"single-seg card {total2}B > 30000B"


@pytest.mark.asyncio
async def test_on_send_ask_finalize_flow(monkeypatch):
    """ask_user 终结流：
    ①有 state + ask_finalize → 终结 accumulated + 记标记
    ②问题 SEND 无 state + ask_finalize → send_markdown 不清标记
    ③route_out 重复 SEND 无 state + 标记在 + 非 ask_finalize → 跳过 + discard
    ④下一轮无流式 SEND（标记已清）→ send_markdown 兜底不丢
    """
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    sent = []

    async def fake_finalize(self, state, content):
        finalized.append((state.receive_id, content))

    async def fake_send_markdown(client, target, content):
        sent.append(("md", target, content))
        return True

    def fake_extract(content):
        return []

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)
    monkeypatch.setattr(api, "send_markdown", fake_send_markdown)
    monkeypatch.setattr(api, "extract_md_refs", fake_extract)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"

    # ① ask_user 终结：有 state + ask_finalize=True → 终结 accumulated + 记标记 + 记终结内容
    state = CardState("card1", "ch1")
    state.accumulated = "累积的回复内容"
    adapter._card_states["ch1"] = state
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    assert finalized == [("ch1", "累积的回复内容")]
    assert "ch1" in adapter._ask_finalized
    assert adapter._ask_finalized_content.get("ch1") == "累积的回复内容"
    assert "ch1" not in adapter._card_states

    # ② 问题独立消息：无 state + ask_finalize=True → send_markdown，不清标记
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 请确认", "ask_finalize": True})
    assert sent == [("md", "ch1", "❓ 请确认")]
    assert "ch1" in adapter._ask_finalized  # 标记保留（供 route_out 判重）

    # ③ route_out 真重复 SEND（content == 终结内容，2b 场景）：无 state + 标记在 + 非 ask_finalize → 跳过 + discard
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "累积的回复内容"})
    assert sent == [("md", "ch1", "❓ 请确认")]  # 无新增（不重复）
    assert "ch1" not in adapter._ask_finalized  # 标记清除（本轮结束）
    assert "ch1" not in adapter._ask_finalized_content

    # ④ 下一轮无流式 SEND：标记已清 → send_markdown 兜底（不丢）
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "兜底内容"})
    assert sent[-1] == ("md", "ch1", "兜底内容")


@pytest.mark.asyncio
async def test_on_send_ask_finalize_fallback_not_skipped(monkeypatch):
    """ImplReview-P2-2：marker 在 + 非 ask_finalize + content ≠ 终结内容（runner return_value 兜底文本）→ 不跳过，send_markdown 正常发"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    sent = []

    async def fake_finalize(self, state, content):
        finalized.append((state.receive_id, content))

    async def fake_send_markdown(client, target, content):
        sent.append(("md", target, content))
        return True

    def fake_extract(content):
        return []

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)
    monkeypatch.setattr(api, "send_markdown", fake_send_markdown)
    monkeypatch.setattr(api, "extract_md_refs", fake_extract)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"

    # ask_user 终结（记录终结内容 part1）+ 问题消息
    state = CardState("card1", "ch1")
    state.accumulated = "part1"
    adapter._card_states["ch1"] = state
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 确认?", "ask_finalize": True})
    assert "ch1" in adapter._ask_finalized

    # 用户回答后 turn 以 return_value 兜底结束（CONTEXT_OVERFLOW/STOPPED/错误）：route_out SEND 带兜底文本 ≠ part1
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "[上下文超限] 已保留部分内容"})
    # 兜底文本必须正常发出（pre-patch 行为），不得被判重吞掉
    assert sent[-1] == ("md", "ch1", "[上下文超限] 已保留部分内容")
    assert "ch1" not in adapter._ask_finalized  # 兜底发送后清标记（防跨轮残留 R6-P2）


@pytest.mark.asyncio
async def test_on_send_ask_finalize_multi_round_concat(monkeypatch):
    """ImplReviewFix-P2-2 + ImplReviewR3-P1：多轮 ask_user（2c）——终结内容跨卡拼接（建卡保留记录），
    route_out 整轮 a1+a2 才跳过；兜底 ≠ 拼接 → 正常发"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    sent = []

    async def fake_finalize(self, state, content):
        finalized.append((state.receive_id, content))

    async def fake_send_markdown(client, target, content):
        sent.append(("md", target, content))
        return True

    async def fake_create(client, receive_id, content, reply_to_id=None):
        return f"card{len(finalized) + 1}", "msg"

    def fake_extract(content):
        return []

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)
    monkeypatch.setattr(api, "send_markdown", fake_send_markdown)
    monkeypatch.setattr(api, "create_card", fake_create)
    monkeypatch.setattr(api, "extract_md_refs", fake_extract)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"

    # 轮 1：ask_user 终结卡 A（a1）+ 问题消息
    s1 = CardState("card1", "ch1")
    s1.accumulated = "a1"
    adapter._card_states["ch1"] = s1
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 第一问?", "ask_finalize": True})

    # 用户回答 → 新流式 chunk → _on_stream 建卡 B（生产路径：标记 discard、内容记录保留 'a1'）
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "a2", "reply_to_id": None})
    assert "ch1" not in adapter._ask_finalized  # 建卡清标记
    assert adapter._ask_finalized_content.get("ch1") == "a1"  # 记录保留供拼接

    # 轮 2：ask_user 终结卡 B（accumulated 'a2' → finalize 'a2'）+ 问题消息（记录拼接 a1+a2）
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 第二问?", "ask_finalize": True})
    assert adapter._ask_finalized_content.get("ch1") == "a1a2"  # 拼接记录

    # route_out 整轮 a1+a2（无 state + 标记在 + 非 ask_finalize）→ 与拼接相等 → 跳过 + 双清
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "a1a2"})
    assert sent == [("md", "ch1", "❓ 第一问?"), ("md", "ch1", "❓ 第二问?")]  # 无新增（不重复）
    assert "ch1" not in adapter._ask_finalized
    assert "ch1" not in adapter._ask_finalized_content

    # 非重复兜底（≠ 拼接）→ 正常发
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "[停止] 任务中断"})
    assert sent[-1] == ("md", "ch1", "[停止] 任务中断")


@pytest.mark.asyncio
async def test_on_stream_cross_turn_record_residue(monkeypatch):
    """ImplReviewR4-P1：跨轮记录残留——turn1 state 分支终结后记录残留，turn2 新回合建卡（标记不在）→ pop 记录，
    防 'a1'+'b1' 误拼接判重 miss 整轮重复"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    sent = []

    async def fake_finalize(self, state, content):
        finalized.append((state.receive_id, content))

    async def fake_send_markdown(client, target, content):
        sent.append(("md", target, content))
        return True

    async def fake_create(client, receive_id, content, reply_to_id=None):
        return f"card{len(finalized) + 1}", "msg"

    def fake_extract(content):
        return []

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)
    monkeypatch.setattr(api, "send_markdown", fake_send_markdown)
    monkeypatch.setattr(api, "create_card", fake_create)
    monkeypatch.setattr(api, "extract_md_refs", fake_extract)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    adapter._push_chat_id = "ch1"

    # ── turn1：ask_user 终结卡 A（记录 'a1'）+ 流式建卡 B（标记清、记录保留）→ route_out state 分支终结卡 B（记录 'a1' 残留）──
    sA = CardState("card1", "ch1")
    sA.accumulated = "a1"
    adapter._card_states["ch1"] = sA
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 问?", "ask_finalize": True})
    assert adapter._ask_finalized_content.get("ch1") == "a1"

    # 用户回答 → 流式建卡 B（2c 延续：标记在 → 保留记录 'a1'）
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "a2", "reply_to_id": None})
    assert "ch1" not in adapter._ask_finalized
    assert adapter._ask_finalized_content.get("ch1") == "a1"

    # route_out state 分支终结卡 B（accumulated 'a2'）——正常终结不记标记，记录 'a1' 残留
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "a1a2"})
    assert finalized[-1] == ("ch1", "a2")  # F3 未触发（'a1a2' 非 'a2' 前缀）→ 用 accumulated
    assert adapter._ask_finalized_content.get("ch1") == "a1"  # 残留
    assert "ch1" not in adapter._ask_finalized

    # ── turn2（新回合）：流式建卡 C——标记不在 → pop 跨轮残留记录 ──
    await adapter._on_stream({"type": "STREAM", "channel_id": "ch1", "content": "b1", "reply_to_id": None})
    assert "ch1" not in adapter._ask_finalized_content  # 残留记录已清（防 'a1'+'b1' 误拼接）

    # turn2 ask_user 终结卡 C（记录 'b1'）→ route_out content 'b1' == 记录 → 跳过（不重复）
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "", "ask_finalize": True})
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "❓ 第二问?", "ask_finalize": True})
    assert adapter._ask_finalized_content.get("ch1") == "b1"  # 干净拼接（无 'a1' 污染）
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "b1"})
    assert sent == [("md", "ch1", "❓ 问?"), ("md", "ch1", "❓ 第二问?")]  # route_out 判重跳过，无重复
    assert "ch1" not in adapter._ask_finalized
    assert "ch1" not in adapter._ask_finalized_content


@pytest.mark.asyncio
async def test_on_send_f3_prefix_recovery(monkeypatch):
    """F3（best-effort）：state 终结时 cmd.content 以 accumulated 为前缀且明显更长 → 用 cmd.content 补全（流式中断）"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState

    finalized = []

    async def fake_finalize(self, state, content):
        finalized.append((state.receive_id, content))

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    state = CardState("card1", "ch1")
    state.accumulated = "前缀"  # 流式中断：accumulated 只是完整回复的前缀
    adapter._card_states["ch1"] = state
    full = "前缀" + "很长的剩余内容" * 50
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": full})
    assert finalized[0][1] == full  # F3 命中 → 补全

    # 正常流：cmd.content == accumulated（相等 → 不触发 F3，用 accumulated）
    state2 = CardState("card2", "ch2")
    state2.accumulated = "正常回复"
    adapter._card_states["ch2"] = state2
    await adapter._on_send({"type": "SEND", "channel_id": "ch2", "content": "正常回复"})
    assert finalized[1] == ("ch2", "正常回复")

    # 卡 B 场景：accumulated 是整轮的后缀（非前缀）→ F3 不触发，用 accumulated（卡 A 内容不重复）
    state3 = CardState("card3", "ch3")
    state3.accumulated = "第二轮回答"
    adapter._card_states["ch3"] = state3
    await adapter._on_send({"type": "SEND", "channel_id": "ch3", "content": "第一轮内容+第二轮回答"})
    assert finalized[2] == ("ch3", "第二轮回答")


@pytest.mark.asyncio
async def test_on_send_media_fallback_preserved(monkeypatch):
    """媒体回退保留：pending_files → send_file_message；failed_images → upload_image + send_image_message"""
    from niu_feishu_adapter.adapter import FeishuAdapter, CardState
    import niu_feishu_adapter.feishu_api as api

    finalized = []
    file_sent = []
    imgs_sent = []

    async def fake_finalize(self, state, content):
        finalized.append(1)

    async def fake_send_file(client, receive_id, file_key, filename):
        file_sent.append((receive_id, file_key, filename))
        return True

    def fake_upload_image(app_id, app_secret, path):
        return f"img_key_for_{path}"

    async def fake_send_image(client, receive_id, image_key):
        imgs_sent.append((receive_id, image_key))
        return True

    monkeypatch.setattr(FeishuAdapter, "_do_finalize", fake_finalize)
    monkeypatch.setattr(api, "send_file_message", fake_send_file)
    monkeypatch.setattr(api, "upload_image", fake_upload_image)
    monkeypatch.setattr(api, "send_image_message", fake_send_image)

    adapter = FeishuAdapter(gateway_port=0, app_id="a", app_secret="s")
    state = CardState("card1", "ch1")
    state.accumulated = "含媒体回复"
    state.pending_files = [{"file_key": "fk1", "filename": "报告.pdf"}]
    state.pending_images = [{"img_key": None, "path": "/tmp/x.png", "failed": True}]
    adapter._card_states["ch1"] = state
    await adapter._on_send({"type": "SEND", "channel_id": "ch1", "content": "含媒体回复"})
    assert finalized == [1]
    assert file_sent == [("ch1", "fk1", "报告.pdf")]
    assert imgs_sent == [("ch1", "img_key_for_/tmp/x.png")]


# ── IMGateway.send_sync（同步 SEND 桥） ──

@pytest.mark.asyncio
async def test_send_sync_send_command():
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.sleep(0.1)
        gw.send_sync("ch1", "reply text")
        cmd = await _read_one(reader)
        assert cmd["type"] == "SEND"
        assert cmd["channel_id"] == "ch1"
        assert cmd["content"] == "reply text"
        assert cmd["ask_finalize"] is False
        assert cmd["reply_to_id"] == ""
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


@pytest.mark.asyncio
async def test_send_sync_pop_reply_to_and_ask_finalize():
    """pop_reply_to=False 保留 reply_to（终结+问题连续两条复用）；ask_finalize 透传；默认 pop_reply_to=True 发送后弹出"""
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    await gw.start_server()
    port = gw._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.sleep(0.1)
        with gw._lock:
            gw._reply_to_ids["ch1"] = "reply123"
        # ① 终结：pop_reply_to=False → 保留 reply_to，透传 ask_finalize=True
        gw.send_sync("ch1", "", pop_reply_to=False, ask_finalize=True)
        cmd1 = await _read_one(reader)
        assert cmd1["reply_to_id"] == "reply123"
        assert cmd1["ask_finalize"] is True
        with gw._lock:
            assert gw._reply_to_ids.get("ch1") == "reply123"  # 未弹
        # ② 问题消息：仍保留 reply_to（群聊回复串联）
        gw.send_sync("ch1", "❓ 问题", pop_reply_to=False, ask_finalize=True)
        cmd2 = await _read_one(reader)
        assert cmd2["reply_to_id"] == "reply123"
        assert cmd2["content"] == "❓ 问题"
        # ③ 默认 pop_reply_to=True → 发送后弹出
        gw.send_sync("ch1", "x")
        cmd3 = await _read_one(reader)
        assert cmd3["reply_to_id"] == "reply123"
        assert cmd3["ask_finalize"] is False
        with gw._lock:
            assert "ch1" not in gw._reply_to_ids
    finally:
        writer.close()
        await writer.wait_closed()
        await gw.stop()


def test_send_sync_not_connected_noop():
    """未连接时 send_sync no-op（不抛异常，不发指令）"""
    from niu_api.channel.gateway import IMGateway
    gw = IMGateway(channel_router=None, port=0)
    gw.send_sync("ch1", "text", ask_finalize=True)  # 未连接 → 直接返回
    assert len(gw._send_buffer) == 0
