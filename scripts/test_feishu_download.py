"""端到端测试：验证飞书图片下载完整路径

测试 SDK 的 download_resource_to_file 配合 message_id 参数能否成功下载图片。

用法:
    REDACTED_USER_PATH/tools/ai-bot/python/bin/python3 scripts/test_feishu_download.py

流程:
    1. 从 ~/.niu/preferences.json 读取飞书 app_id / app_secret
    2. 创建 FeishuChannel 实例并连接 WebSocket
    3. 注册 message 事件处理器
    4. 当收到带图片的消息时，打印 message_id / resources / 下载结果
    5. 等待 30 秒后断开

请在飞书上向机器人发送一张图片来触发测试。
"""

import asyncio
import json
import sys
import time
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────

PREFERENCES_PATH = Path.home() / ".niu" / "preferences.json"
DOWNLOAD_DIR = Path.home() / ".niu" / "tmp"
WAIT_SECONDS = 30


def load_feishu_credentials() -> tuple[str, str]:
    """从 preferences.json 读取飞书 app_id / app_secret"""
    if not PREFERENCES_PATH.exists():
        print(f"[ERROR] 配置文件不存在: {PREFERENCES_PATH}")
        sys.exit(1)

    with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
        prefs = json.load(f)

    feishu = prefs.get("feishu", {})
    app_id = feishu.get("app_id", "").strip()
    app_secret = feishu.get("app_secret", "").strip()

    if not app_id or not app_secret:
        print(f"[ERROR] 飞书 app_id / app_secret 缺失，请检查 {PREFERENCES_PATH}")
        sys.exit(1)

    return app_id, app_secret


async def main():
    # ── 1. 读取凭据 ──────────────────────────────────────────────────────────
    app_id, app_secret = load_feishu_credentials()
    print(f"[OK] app_id = {app_id[:8]}...")

    # ── 2. 修补 lark_oapi.ws.client 模块级 loop ─────────────────────────────
    # ws/client.py 在 import 时通过 asyncio.get_event_loop() 捕获当前 loop，
    # 如果已有 loop 在运行，WSClient.start() 的 loop.run_until_complete()
    # 会抛出 RuntimeError: This event loop is already running
    import lark_oapi.ws.client as _ws_client
    if _ws_client.loop.is_running():
        _ws_client.loop = asyncio.new_event_loop()
    print("[OK] ws.client loop patched")

    # ── 3. 创建 FeishuChannel 实例 ──────────────────────────────────────────
    from lark_oapi.channel import FeishuChannel
    from lark_oapi.channel.config import OutboundConfig, MarkdownConverter

    channel = FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        outbound=OutboundConfig(
            markdown_converter=MarkdownConverter(tag_md_mode="native")
        ),
    )
    print("[OK] FeishuChannel created")

    # ── 4. 注册 message 事件处理器 ──────────────────────────────────────────
    message_received = asyncio.Event()
    download_results = []

    def on_message(msg):
        """收到消息时的处理器 — 在 SDK 线程中调用"""
        print("\n" + "=" * 60)
        print(f"[MESSAGE] 收到消息")
        print(f"  msg.id          = {msg.id}")
        print(f"  msg.message_id  = {msg.message_id}")
        print(f"  msg.chat_id     = {msg.chat_id}")
        print(f"  msg.chat_type   = {msg.chat_type}")
        print(f"  msg.sender_id   = {msg.sender_id}")
        print(f"  msg.content_text= {msg.content_text[:100] if msg.content_text else '(empty)'}")
        print(f"  msg.resources   = {msg.resources}")
        print(f"  raw_content_type= {getattr(msg, 'raw_content_type', 'N/A')}")

        # 打印每个 resource 的详细信息
        for i, r in enumerate(msg.resources or []):
            rtype = getattr(r, 'type', '') if not isinstance(r, dict) else r.get('type', '')
            file_key = getattr(r, 'file_key', '') if not isinstance(r, dict) else r.get('file_key', '')
            file_name = getattr(r, 'file_name', '') if not isinstance(r, dict) else r.get('file_name', '')
            print(f"  resource[{i}]: type={rtype}, file_key={file_key}, file_name={file_name}")

        # 如果有图片/文件资源，尝试下载
        for r in msg.resources or []:
            rtype = getattr(r, 'type', '') if not isinstance(r, dict) else r.get('type', '')
            file_key = getattr(r, 'file_key', '') if not isinstance(r, dict) else r.get('file_key', '')
            file_name = getattr(r, 'file_name', '') if not isinstance(r, dict) else r.get('file_name', '')

            if rtype not in ('image', 'file') or not file_key:
                continue

            print(f"\n[DOWNLOAD] 尝试下载 {rtype}: file_key={file_key}")
            print(f"  message_id = {msg.id}")

            # 通过 channel.schedule() 在后台 loop 中执行异步下载
            future = channel.schedule(
                _download_and_report(file_key, rtype, file_name or "", message_id=msg.id)
            )
            try:
                result = future.result(timeout=15)
                download_results.append(result)
            except TimeoutError:
                print(f"  [FAIL] 下载超时 (15s)")
                download_results.append({"status": "timeout", "file_key": file_key})
            except Exception as e:
                print(f"  [FAIL] 下载异常: {e}")
                download_results.append({"status": "error", "file_key": file_key, "error": str(e)})

        # 标记已收到消息
        message_received.set()

    async def _download_and_report(file_key: str, rtype: str, file_name: str, *, message_id: str) -> dict:
        """执行下载并打印详细结果"""
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = file_name or f"{file_key}.bin"
        dest_file_name = f"test_download_{file_key}_{safe_name}"

        result_info = {
            "file_key": file_key,
            "resource_type": rtype,
            "message_id": message_id,
        }

        # ── 测试 A: 带 message_id 下载（消息资源端点） ─────────────────────
        print(f"\n  [Test A] download_resource_to_file(message_id={message_id})")
        try:
            path_a = await channel.download_resource_to_file(
                file_key,
                resource_type=rtype,
                message_id=message_id,
                dest_dir=DOWNLOAD_DIR,
                file_name=f"with_mid_{dest_file_name}",
            )
            print(f"  [Test A] SUCCESS: {path_a}")
            print(f"  [Test A] 文件大小: {path_a.stat().st_size if path_a.exists() else 'N/A'} bytes")
            result_info["with_message_id"] = {"status": "success", "path": str(path_a)}
        except Exception as e:
            print(f"  [Test A] FAIL: {type(e).__name__}: {e}")
            result_info["with_message_id"] = {"status": "fail", "error": str(e)}

        # ── 测试 B: 不带 message_id 下载（独立端点） ────────────────────────
        print(f"\n  [Test B] download_resource_to_file(message_id=None)")
        try:
            path_b = await channel.download_resource_to_file(
                file_key,
                resource_type=rtype,
                message_id=None,
                dest_dir=DOWNLOAD_DIR,
                file_name=f"no_mid_{dest_file_name}",
            )
            print(f"  [Test B] SUCCESS: {path_b}")
            print(f"  [Test B] 文件大小: {path_b.stat().st_size if path_b.exists() else 'N/A'} bytes")
            result_info["without_message_id"] = {"status": "success", "path": str(path_b)}
        except Exception as e:
            print(f"  [Test B] FAIL: {type(e).__name__}: {e}")
            result_info["without_message_id"] = {"status": "fail", "error": str(e)}

        # ── 测试 C: download_resource (返回 bytes) ──────────────────────────
        print(f"\n  [Test C] download_resource(message_id={message_id})")
        try:
            data = await channel.download_resource(
                file_key,
                resource_type=rtype,
                message_id=message_id,
            )
            if data is not None:
                print(f"  [Test C] SUCCESS: {len(data)} bytes")
                result_info["download_resource_bytes"] = {"status": "success", "size": len(data)}
            else:
                print(f"  [Test C] FAIL: 返回 None（API 成功但无 body）")
                result_info["download_resource_bytes"] = {"status": "none"}
        except Exception as e:
            print(f"  [Test C] FAIL: {type(e).__name__}: {e}")
            result_info["download_resource_bytes"] = {"status": "fail", "error": str(e)}

        return result_info

    channel.on("message", on_message)
    channel.on("error", lambda err: print(f"[ERROR] SDK error: {err}"))
    channel.on("reconnecting", lambda: print("[WARN] WebSocket reconnecting..."))
    channel.on("reconnected", lambda: print("[OK] WebSocket reconnected"))
    print("[OK] Event handlers registered")

    # ── 5. 连接 WebSocket ────────────────────────────────────────────────────
    print("\n[INFO] 正在连接 WebSocket...")
    try:
        await channel.connect_until_ready(timeout=30)
        print("[OK] WebSocket connected!")
    except Exception as e:
        print(f"[ERROR] WebSocket 连接失败: {type(e).__name__}: {e}")
        sys.exit(1)

    # ── 6. 等待消息 ─────────────────────────────────────────────────────────
    print(f"\n[INFO] 等待飞书消息（{WAIT_SECONDS}秒）...")
    print("[INFO] 请在飞书上向机器人发送一张图片来触发测试")
    print("-" * 60)

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        remaining = int(deadline - time.monotonic())
        if remaining % 10 == 0 and remaining > 0:
            print(f"[WAIT] 还剩 {remaining} 秒...", flush=True)
        try:
            await asyncio.wait_for(message_received.wait(), timeout=min(5, remaining + 1))
            print("[OK] 已收到消息，继续等待更多消息...")
            message_received.clear()
        except asyncio.TimeoutError:
            pass

    # ── 7. 断开连接并打印汇总 ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("[SUMMARY] 下载测试结果汇总:")
    if not download_results:
        print("  未收到任何带资源的消息，请确认已在飞书上发送图片")
    for i, result in enumerate(download_results):
        print(f"\n  下载 #{i + 1}:")
        if isinstance(result, dict):
            for k, v in result.items():
                print(f"    {k}: {v}")
        else:
            print(f"    {result}")

    print("\n[INFO] 正在断开连接...")
    try:
        await channel.disconnect()
        print("[OK] 已断开")
    except Exception as e:
        print(f"[WARN] 断开异常: {e}")

    print("\n[DONE] 测试结束")


if __name__ == "__main__":
    asyncio.run(main())
