"""
Test CardKit API: create streaming card, send via im.v1, update, finalize.

Reads feishu config from ~/.niu/preferences.json.

Key finding: im.v1.message.create does NOT support card_id reference format
(e.g. {"type":"card_id","card_id":"xxx"}). The card must be sent as inline
card JSON in the content field.
"""

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


def build_streaming_card_dict(content: str, element_id: str = "md1") -> dict:
    """Build Card JSON 2.0 structure for streaming mode."""
    return {
        "schema": "2.0",
        "header": {
            "title": {
                "content": "Niu Agent Streaming Test",
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
                    "element_id": element_id,
                }
            ]
        },
    }


def main():
    config = load_feishu_config()
    print(f"[CONFIG] app_id={config['app_id']}, chat_id={config['chat_id']}")

    # Create lark client
    client = lark.Client.builder() \
        .app_id(config["app_id"]) \
        .app_secret(config["app_secret"]) \
        .log_level(lark.LogLevel.ERROR) \
        .build()

    # =========================================================================
    # Step 1: Create streaming card entity via CardKit
    # =========================================================================
    print("\n[STEP 1] Create streaming card entity (content='测试1')")
    card_dict = build_streaming_card_dict("测试1")
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
    print(f"  success={create_resp.success()}, code={create_resp.code}, msg={create_resp.msg}")
    if not create_resp.success():
        print(f"  FAILED: {create_resp.msg}")
        return
    card_id = create_resp.data.card_id
    print(f"  card_id={card_id}")

    # =========================================================================
    # Step 2: Send card message via im.v1 (inline card JSON in content)
    # =========================================================================
    print("\n[STEP 2] Send card message to chat_id={}".format(config["chat_id"]))
    # NOTE: card_id reference format ({"type":"card_id","card_id":"xxx"}) does NOT work
    # with im.v1.message.create. Must use inline card JSON.
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
    print(f"  success={send_resp.success()}, code={send_resp.code}, msg={send_resp.msg}")
    if not send_resp.success():
        print(f"  FAILED: {send_resp.msg}")
        return
    message_id = send_resp.data.message_id
    print(f"  message_id={message_id}")

    # Wait 2 seconds
    print("\n  Waiting 2 seconds...")
    time.sleep(2)

    # =========================================================================
    # Step 3: Update card content via CardKit UpdateCard (PUT) with sequence
    # =========================================================================
    print("\n[STEP 3] Update card content to '测试2' (sequence=1)")
    updated_card_dict = build_streaming_card_dict("测试2")
    updated_card_json = json.dumps(updated_card_dict, ensure_ascii=False)

    update_req = UpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            UpdateCardRequestBody.builder()
            .card(
                Card.builder()
                .type("card_json")
                .data(updated_card_json)
                .build()
            )
            .uuid("test-update-001")
            .sequence(1)
            .build()
        ) \
        .build()

    update_resp = client.cardkit.v1.card.update(update_req)
    print(f"  success={update_resp.success()}, code={update_resp.code}, msg={update_resp.msg}")
    if not update_resp.success():
        print(f"  FAILED: {update_resp.msg}")

    # Wait 2 seconds
    print("\n  Waiting 2 seconds...")
    time.sleep(2)

    # =========================================================================
    # Step 4: Finalize streaming card via BatchUpdateCard
    # =========================================================================
    print("\n[STEP 4] Finalize streaming card (sequence=2, streaming_mode=false)")
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
            .uuid("test-finalize-001")
            .sequence(2)
            .actions(finalize_actions)
            .build()
        ) \
        .build()

    finalize_resp = client.cardkit.v1.card.batch_update(finalize_req)
    print(f"  success={finalize_resp.success()}, code={finalize_resp.code}, msg={finalize_resp.msg}")
    if not finalize_resp.success():
        print(f"  FAILED: {finalize_resp.msg}")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("[SUMMARY]")
    print(f"  Create card:   card_id={card_id}")
    print(f"  Send message:  message_id={message_id}")
    print(f"  Update card:   success={update_resp.success()}")
    print(f"  Finalize card: success={finalize_resp.success()}")
    print()
    print("[KEY FINDING] card_id reference format does NOT work with")
    print("im.v1.message.create. Must send inline card JSON in content field.")
    print("The card_id from CreateCard is used for subsequent UpdateCard/")
    print("BatchUpdateCard calls.")


if __name__ == "__main__":
    main()