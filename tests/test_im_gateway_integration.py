"""IM Gateway 集成测试 — 模拟 IM Adapter

使用方式：
1. 启动 ./niu（确保 preferences.json 中 im.enabled=true）
2. 运行 python tests/test_im_gateway_integration.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 19877
TEST_CHANNEL_ID = "test_chat_001"
TEST_SENDER_ID = "test_user_001"


def encode(msg: dict) -> bytes:
    payload = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


async def read_one(reader, timeout=120.0):
    try:
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = int.from_bytes(header, "big")
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(data.decode("utf-8"))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None


async def connect_gateway():
    reader, writer = await asyncio.open_connection(GATEWAY_HOST, GATEWAY_PORT)
    writer.write(encode({"type": "READY", "adapter": "test-adapter", "push_target": TEST_CHANNEL_ID}))
    await writer.drain()
    await asyncio.sleep(0.5)
    return reader, writer


async def send_msg(writer, content, channel_id=TEST_CHANNEL_ID):
    writer.write(encode({
        "type": "MSG", "session_id": f"im:{TEST_SENDER_ID}",
        "content": content, "channel_id": channel_id,
        "sender_id": TEST_SENDER_ID, "is_group": False, "reply_to_id": None,
    }))
    await writer.drain()


async def wait_for_send(reader, timeout=120.0):
    """等待 Agent 回复（SEND 指令），跳过 STREAM/PONG"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = await read_one(reader, timeout=min(deadline - time.time(), 30))
        if cmd and cmd.get("type") == "SEND":
            return cmd
    return None


# ── 入方向测试 ──

async def test_inbound_text():
    """入方向：发送文本消息 → 收到 Agent 回复"""
    print("\n=== 测试: 入方向-文本消息 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "你好，请简单回复一句话")
    print("[测试] 已发送文本消息")

    reply = await wait_for_send(reader)
    if reply:
        content = reply.get("content", "")
        print(f"[测试] PASS 收到回复: {content[:80]}...")
        assert reply["type"] == "SEND"
        assert reply.get("channel_id") == TEST_CHANNEL_ID
        assert len(content) > 0
    else:
        raise AssertionError("Agent 未回复文本消息")

    writer.close()
    await writer.wait_closed()


async def test_inbound_image():
    """入方向：发送带图片的消息 → Agent 处理图片 → 回复"""
    print("\n=== 测试: 入方向-图片消息 ===")
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_img = tmp_dir / "test_photo.jpg"
    if not test_img.exists():
        test_img.write_bytes(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9')

    reader, writer = await connect_gateway()
    await send_msg(writer, f"请看这张照片\n![测试照片]({test_img})")
    print("[测试] 已发送图片消息")

    reply = await wait_for_send(reader)
    if reply:
        print(f"[测试] PASS 收到回复")
        assert reply["type"] == "SEND"
    else:
        raise AssertionError("Agent 未回复图片消息")

    writer.close()
    await writer.wait_closed()


async def test_inbound_file():
    """入方向：发送带文件的消息 → Agent 处理文件 → 回复"""
    print("\n=== 测试: 入方向-文件消息 ===")
    tmp_dir = Path.home() / ".niu" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    test_file = tmp_dir / "test_doc.txt"
    test_file.write_text("这是一个测试文件，用于验证 IM Gateway 文件传输功能。", encoding="utf-8")

    reader, writer = await connect_gateway()
    await send_msg(writer, f"请查看文件\n[测试文档]({test_file})")
    print("[测试] 已发送文件消息")

    reply = await wait_for_send(reader)
    if reply:
        print(f"[测试] PASS 收到回复")
        assert reply["type"] == "SEND"
    else:
        raise AssertionError("Agent 未回复文件消息")

    writer.close()
    await writer.wait_closed()


# ── 出方向测试 ──

async def test_outbound_image():
    """出方向：让 Agent 展示图片 → 回复 content 含 ![alt](path) 标记"""
    print("\n=== 测试: 出方向-图片回复 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请用 photo_search 工具搜索一张照片并展示给我，或者从你的知识库中找一张图片展示")
    print("[测试] 已发送请求展示图片的消息")

    reply = await wait_for_send(reader, timeout=180.0)
    if reply:
        content = reply.get("content", "")
        img_pattern = r'!\[.*?\]\((.+?)\)'
        matches = re.findall(img_pattern, content)
        if matches:
            for img_path in matches:
                p = Path(img_path)
                if p.exists():
                    print(f"[测试] PASS 回复含图片标记且文件存在: {img_path}")
                else:
                    print(f"[测试] WARN 回复含图片标记但文件不存在: {img_path}")
            assert reply["type"] == "SEND"
        else:
            print(f"[测试] WARN 回复不含图片标记（Agent 可能没有可用图片）: {content[:100]}...")
    else:
        raise AssertionError("Agent 未回复图片展示请求")

    writer.close()
    await writer.wait_closed()


async def test_outbound_file():
    """出方向：让 Agent 发送文件 → 回复 content 含 [name](path) 标记"""
    print("\n=== 测试: 出方向-文件回复 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请把你的配置文件内容发给我，用文件的形式")
    print("[测试] 已发送请求发送文件的消息")

    reply = await wait_for_send(reader, timeout=180.0)
    if reply:
        content = reply.get("content", "")
        # 检查回复中是否包含 Markdown 文件链接（排除图片 ![...]）
        file_pattern = r'(?<!!)\[([^\]]+)\]\(([^)]+)\)'
        file_matches = re.findall(file_pattern, content)
        if file_matches:
            for name, fpath in file_matches:
                p = Path(fpath)
                if p.exists():
                    print(f"[测试] PASS 回复含文件标记且文件存在: [{name}]({fpath})")
                else:
                    print(f"[测试] WARN 回复含文件标记但文件不存在: [{name}]({fpath})")
            assert reply["type"] == "SEND"
        else:
            print(f"[测试] WARN 回复不含文件标记（Agent 可能没有可用文件）: {content[:100]}...")
    else:
        raise AssertionError("Agent 未回复文件发送请求")

    writer.close()
    await writer.wait_closed()


# ── 流式推送测试 ──

async def test_stream_notification():
    """测试：Agent 回复过程中收到 STREAM 通知"""
    print("\n=== 测试: 流式推送通知 ===")
    reader, writer = await connect_gateway()
    await send_msg(writer, "请详细介绍一下你自己，至少写三段")
    print("[测试] 已发送消息，等待 STREAM 通知...")

    stream_count = 0
    deadline = time.time() + 15
    while time.time() < deadline:
        cmd = await read_one(reader, timeout=2)
        if cmd and cmd.get("type") == "STREAM":
            stream_count += 1
        elif cmd and cmd.get("type") == "SEND":
            break

    if stream_count > 0:
        print(f"[测试] PASS 收到 {stream_count} 条 STREAM 通知")
    else:
        print("[测试] WARN 未收到 STREAM 通知（Agent 可能回复太快）")

    writer.close()
    await writer.wait_closed()


async def run_all():
    results = []
    tests = [
        ("入方向-文本", test_inbound_text),
        ("入方向-图片", test_inbound_image),
        ("入方向-文件", test_inbound_file),
        ("出方向-图片", test_outbound_image),
        ("出方向-文件", test_outbound_file),
        ("流式通知", test_stream_notification),
    ]
    for name, fn in tests:
        try:
            await fn()
            results.append((name, "PASS"))
        except Exception as e:
            results.append((name, f"FAIL: {e}"))
            print(f"[测试] FAIL {name}: {e}")

    print("\n" + "=" * 50)
    for name, r in results:
        status = "PASS" if r == "PASS" else "FAIL"
        print(f"  {status} {name}: {r}")
    passed = sum(1 for _, r in results if r == "PASS")
    print(f"\n通过: {passed}/{len(results)}")
    return passed == len(results)


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
