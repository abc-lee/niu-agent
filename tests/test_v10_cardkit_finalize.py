"""
E2E Test: CardKit Finalize — 验证 BatchUpdateCard 终结流式卡片时是否需要同时更新卡片内容

假设1 测试：
  A) 只发 actions (streaming_mode:False)，不带 card 内容 → 飞书端"生成中"是否消失？
  B) 先 UpdateCard 更新内容，再 BatchUpdateCard 发 actions → 飞书端"生成中"是否消失？

运行方式：
  cd REDACTED_USER_PATH/tools/ai-bot && python tests/test_v10_cardkit_finalize.py

前置条件：
  ~/.niu/preferences.json 中已配置 feishu.app_id / app_secret / user_p2p_chat_id
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so 'lark_oapi' from python/ is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import json
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    CreateCardRequest,
    CreateCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_feishu_config() -> dict:
    prefs_path = Path.home() / ".niu" / "preferences.json"
    if not prefs_path.exists():
        raise RuntimeError(f"{prefs_path} not found")
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    feishu = prefs.get("feishu", {})
    app_id = feishu.get("app_id", "").strip()
    app_secret = feishu.get("app_secret", "").strip()
    chat_id = feishu.get("user_p2p_chat_id", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("feishu app_id/app_secret not configured in preferences.json")
    if not chat_id:
        raise RuntimeError("feishu user_p2p_chat_id not configured in preferences.json")
    return {"app_id": app_id, "app_secret": app_secret, "chat_id": chat_id}


def build_client(config: dict) -> lark.Client:
    return lark.Client.builder() \
        .app_id(config["app_id"]) \
        .app_secret(config["app_secret"]) \
        .log_level(lark.LogLevel.ERROR) \
        .build()


def build_streaming_card_dict(content: str, title: str = "Niu Agent Streaming Test") -> dict:
    """Build Card JSON 2.0 structure for streaming mode."""
    return {
        "schema": "2.0",
        "header": {
            "title": {
                "content": title,
                "tag": "plain_text",
            }
        },
        "config": {
            "streaming_mode": True,
            "summary": {"content": content[:50]},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                    "element_id": "md1",
                }
            ]
        },
    }


def create_and_send_card(client: lark.Client, config: dict, card_dict: dict,
                         title: str = "") -> tuple[str, str]:
    """Create card entity via CardKit, then send as inline card message.

    Returns:
        (card_id, message_id)
    """
    label = f"[{title}] " if title else ""

    # Step 1: Create card entity
    card_json = json.dumps(card_dict, ensure_ascii=False)
    create_req = CreateCardRequest.builder() \
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(card_json)
            .build()
        ) \
        .build()

    create_resp = client.cardkit.v1.card.create(create_req)
    if not create_resp.success():
        raise RuntimeError(f"{label}CreateCard failed: code={create_resp.code}, msg={create_resp.msg}")
    card_id = create_resp.data.card_id
    print(f"  {label}CreateCard: card_id={card_id}")

    # Step 2: Send as inline card message
    msg_content = json.dumps(card_dict, ensure_ascii=False)
    send_req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(config["chat_id"])
            .msg_type("interactive")
            .content(msg_content)
            .build()
        ) \
        .build()

    send_resp = client.im.v1.message.create(send_req)
    if not send_resp.success():
        raise RuntimeError(f"{label}SendMessage failed: code={send_resp.code}, msg={send_resp.msg}")
    message_id = send_resp.data.message_id
    print(f"  {label}SendMessage: message_id={message_id}")

    return card_id, message_id


def update_card_content(client: lark.Client, card_id: str, card_dict: dict,
                        sequence: int, uuid_label: str) -> bool:
    """Update card content via CardKit UpdateCard (PUT)."""
    card_json = json.dumps(card_dict, ensure_ascii=False)
    update_req = UpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            UpdateCardRequestBody.builder()
            .card(
                Card.builder()
                .type("card_json")
                .data(card_json)
                .build()
            )
            .uuid(uuid_label)
            .sequence(sequence)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.update(update_req)
    print(f"  UpdateCard: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    return resp.success()


def batch_update_finalize(client: lark.Client, card_id: str,
                          sequence: int, uuid_label: str,
                          include_card_update: bool = False,
                          final_content: str = "") -> bool:
    """Finalize streaming card via BatchUpdateCard.

    Args:
        include_card_update: If True, also update card content before finalizing
        final_content: The content to put in the card if include_card_update is True
    """
    # Optionally update card content first
    if include_card_update and final_content:
        final_card_dict = build_streaming_card_dict(final_content, title="Niu Agent Final")
        # UpdateCard with higher sequence
        ok = update_card_content(client, card_id, final_card_dict,
                                 sequence=sequence, uuid_label=f"{uuid_label}-content")
        if not ok:
            print("  WARNING: UpdateCard before BatchUpdateCard failed!")
        sequence += 1

    # BatchUpdateCard with streaming_mode=False action
    finalize_actions = json.dumps([
        {
            "action": "partial_update_setting",
            "params": {
                "settings": {
                    "config": {
                        "streaming_mode": False,
                    }
                }
            }
        }
    ])

    finalize_req = BatchUpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            BatchUpdateCardRequestBody.builder()
            .uuid(uuid_label)
            .sequence(sequence)
            .actions(finalize_actions)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.batch_update(finalize_req)
    print(f"  BatchUpdateCard: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    return resp.success()


# ---------------------------------------------------------------------------
# Test A: Only actions (streaming_mode:False), no card content update
# ---------------------------------------------------------------------------

def test_a_actions_only(client: lark.Client, config: dict) -> dict:
    """Test A: 只发 actions (streaming_mode:False)，不更新 card 内容"""
    print("\n" + "=" * 70)
    print("TEST A: BatchUpdateCard 只发 actions，不更新 card 内容")
    print("=" * 70)

    # 1. Create streaming card with initial content
    card_dict = build_streaming_card_dict(
        "这是测试A的初始内容（生成中...）",
        title="测试A - Actions Only"
    )
    card_id, message_id = create_and_send_card(client, config, card_dict, title="TestA")
    print(f"  卡片已发送，请在飞书中观察：应显示'生成中'状态")

    # 2. Update card content once (simulate streaming updates)
    time.sleep(2)
    updated_dict = build_streaming_card_dict(
        "这是测试A的更新内容（仍在生成中...）",
        title="测试A - Actions Only"
    )
    update_card_content(client, card_id, updated_dict, sequence=1, uuid_label="testA-update-1")

    # 3. Wait and then finalize with actions ONLY
    time.sleep(3)
    print("\n  >>> 即将执行 Test A: 只发 actions (streaming_mode:False)，不更新 card 内容 <<<")
    ok = batch_update_finalize(
        client, card_id,
        sequence=2, uuid_label="testA-finalize",
        include_card_update=False,
    )

    # 4. Wait for user observation
    print("\n  请在飞书中观察：")
    print("  - '生成中' 标记是否消失了？")
    print("  - 卡片内容是否还是旧的'仍在生成中'？")
    print("  - 卡片是否可交互（不再有loading动画）？")

    return {
        "test": "A",
        "card_id": card_id,
        "message_id": message_id,
        "finalize_success": ok,
        "method": "actions_only",
    }


# ---------------------------------------------------------------------------
# Test B: UpdateCard content + BatchUpdateCard actions together
# ---------------------------------------------------------------------------

def test_b_content_plus_actions(client: lark.Client, config: dict) -> dict:
    """Test B: 先 UpdateCard 更新内容，再 BatchUpdateCard 发 actions"""
    print("\n" + "=" * 70)
    print("TEST B: UpdateCard 更新内容 + BatchUpdateCard 发 actions")
    print("=" * 70)

    # 1. Create streaming card with initial content
    card_dict = build_streaming_card_dict(
        "这是测试B的初始内容（生成中...）",
        title="测试B - Content + Actions"
    )
    card_id, message_id = create_and_send_card(client, config, card_dict, title="TestB")
    print(f"  卡片已发送，请在飞书中观察：应显示'生成中'状态")

    # 2. Update card content once (simulate streaming updates)
    time.sleep(2)
    updated_dict = build_streaming_card_dict(
        "这是测试B的更新内容（仍在生成中...）",
        title="测试B - Content + Actions"
    )
    update_card_content(client, card_id, updated_dict, sequence=1, uuid_label="testB-update-1")

    # 3. Wait and then finalize: UpdateCard (final content) + BatchUpdateCard (actions)
    time.sleep(3)
    print("\n  >>> 即将执行 Test B: 先 UpdateCard 更新最终内容，再 BatchUpdateCard 发 actions <<<")
    ok = batch_update_finalize(
        client, card_id,
        sequence=2, uuid_label="testB-finalize",
        include_card_update=True,
        final_content="这是测试B的最终内容（生成完毕）",
    )

    # 4. Wait for user observation
    print("\n  请在飞书中观察：")
    print("  - '生成中' 标记是否消失了？")
    print("  - 卡片内容是否更新为'最终内容'？")
    print("  - 卡片是否可交互（不再有loading动画）？")

    return {
        "test": "B",
        "card_id": card_id,
        "message_id": message_id,
        "finalize_success": ok,
        "method": "content_plus_actions",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_feishu_config()
    print(f"[CONFIG] app_id={config['app_id']}, chat_id={config['chat_id']}")

    client = build_client(config)

    # Run Test A
    result_a = test_a_actions_only(client, config)

    # Pause for manual observation
    print("\n" + "-" * 70)
    print("Test A 完成，等待 8 秒后继续 Test B...")
    print("  （请在飞书中确认 Test A 卡片的状态）")
    time.sleep(8)

    # Run Test B
    result_b = test_b_content_plus_actions(client, config)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    print(f"Test A (actions only):")
    print(f"  card_id:       {result_a['card_id']}")
    print(f"  message_id:    {result_a['message_id']}")
    print(f"  API success:   {result_a['finalize_success']}")
    print()
    print(f"Test B (content + actions):")
    print(f"  card_id:       {result_b['card_id']}")
    print(f"  message_id:    {result_b['message_id']}")
    print(f"  API success:   {result_b['finalize_success']}")
    print()
    print("CONCLUSION GUIDE:")
    print("  如果 Test A 和 Test B 都能消除'生成中'：")
    print("    → 终结时只需 actions，不需要更新 card 内容")
    print()
    print("  如果 Test A 不能消除但 Test B 能消除：")
    print("    → 终结时必须同时更新 card 内容（UpdateCard + BatchUpdateCard）")
    print()
    print("  如果两者都不能消除'生成中'：")
    print("    → 可能需要其他机制（如 UpdateCard 直接设置 streaming_mode:False）")
    print()
    print("请到飞书中查看两张卡片的实际表现，得出结论。")


if __name__ == "__main__":
    main()
