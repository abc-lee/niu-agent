"""
E2E Test: Full Streaming Card Lifecycle — verify "生成中" indicator disappears

Simulates the complete streaming card lifecycle that the real app performs:
  Phase 1: Create streaming card with header subtitle "思考中..."
  Phase 2: Send card as message via im.v1.message.create
  Phase 3: Update element "md1" content (sequence=1)
  Phase 4: Update element "md1" content (sequence=2)
  Phase 5: Update element "md1" with final content (sequence=3)
  Phase 6: Finalize — flush last element (seq=4) + Settings API (seq=5) + UpdateCard (seq=6)
  Phase 7: Post-validate — UpdateCard still works after streaming ends

Run:
  cd REDACTED_USER_PATH/tools/ai-bot && python tests/test_v10_streaming_lifecycle.py

Prerequisites:
  ~/.niu/preferences.json contains feishu.app_id / app_secret / user_p2p_chat_id
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so lark_oapi from python/ is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import json
import time
import uuid as uuid_mod

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    CreateCardRequest,
    CreateCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
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


def ts() -> str:
    """Return current timestamp string for logging."""
    return time.strftime("%H:%M:%S", time.localtime())


def new_uuid(label: str) -> str:
    """Generate a unique UUID with a label prefix for traceability."""
    return f"{label}-{uuid_mod.uuid4().hex[:8]}"


def build_streaming_card_dict(subtitle: str = "思考中...", md_content: str = "...",
                              full_cardkit: bool = True) -> dict:
    """Build Card JSON 2.0 structure for streaming mode with header subtitle.

    Args:
        full_cardkit: If True, include streaming_config/update_multi (for CardKit API).
                      If False, omit these fields (for im.v1.message.create which
                      doesn't support them).
    """
    config = {
        "streaming_mode": True,
        "summary": {"content": ""},
    }
    if full_cardkit:
        config["update_multi"] = True
        config["streaming_config"] = {
            "config": {
                "print_frequency_ms": 800,
            },
        }

    return {
        "schema": "2.0",
        "header": {
            "title": {
                "content": "Niu助手",
                "tag": "plain_text",
            },
            "subtitle": {
                "content": subtitle,
                "tag": "plain_text",
            },
        },
        "config": config,
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": md_content,
                    "element_id": "md1",
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

def phase1_create_card(client: lark.Client, config: dict) -> str:
    """Phase 1: Create streaming card entity via CardKit. Returns card_id."""
    print(f"\n[{ts()}] ========== Phase 1: Create Streaming Card ==========")
    card_dict = build_streaming_card_dict(subtitle="思考中...", md_content="...", full_cardkit=True)
    card_json = json.dumps(card_dict, ensure_ascii=False)

    create_req = CreateCardRequest.builder() \
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(card_json)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.create(create_req)
    print(f"  CreateCard: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    if not resp.success():
        raise RuntimeError(f"CreateCard failed: code={resp.code}, msg={resp.msg}")
    card_id = resp.data.card_id
    print(f"  card_id={card_id}")
    return card_id


def phase2_send_message(client: lark.Client, config: dict, card_dict: dict) -> str:
    """Phase 2: Send card as inline message via im.v1.message.create. Returns message_id."""
    print(f"\n[{ts()}] ========== Phase 2: Send Card Message ==========")
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

    resp = client.im.v1.message.create(send_req)
    print(f"  SendMessage: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    if not resp.success():
        raise RuntimeError(f"SendMessage failed: code={resp.code}, msg={resp.msg}")
    message_id = resp.data.message_id
    print(f"  message_id={message_id}")
    return message_id


def phase3_update_element(client: lark.Client, card_id: str, element_id: str,
                          content_text: str, sequence: int, uuid_label: str) -> bool:
    """Update a single element's content via ContentCardElementRequest.

    The `content` field is a plain text string matching the markdown element's
    content field — the SDK's driver uses body.get("content") or "" which passes
    raw text, not a JSON-wrapped dict.
    """
    print(f"\n[{ts()}] ========== Update Element '{element_id}' (sequence={sequence}) ==========")
    print(f"  New content: {content_text[:60]}{'...' if len(content_text) > 60 else ''}")

    content_json = content_text

    req = ContentCardElementRequest.builder() \
        .card_id(card_id) \
        .element_id(element_id) \
        .request_body(
            ContentCardElementRequestBody.builder()
            .uuid(new_uuid(uuid_label))
            .content(content_json)
            .sequence(sequence)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card_element.content(req)
    print(f"  ContentCardElement: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    if not resp.success():
        print(f"  ERROR detail: {resp.raw.status_code} - {resp.raw.content if resp.raw else 'N/A'}")
    return resp.success()


def phase6_finalize(client: lark.Client, card_id: str, element_id: str,
                     final_md_content: str, sequence: int) -> bool:
    """Phase 6: Finalize streaming card.

    Three operations (matching SDK MarkdownStreamController pattern):
      1) ContentCardElementRequest: flush last element content update
      2) SettingsCardRequest: set streaming_mode=False (finish_streaming_card)
      3) UpdateCardRequest: update full card with subtitle="" and streaming_mode=False
    """
    print(f"\n[{ts()}] ========== Phase 6: Finalize Streaming Card ==========")

    # Step 6A-1: Flush last element content BEFORE closing streaming
    flush_content = final_md_content
    print(f"  [6A-1] ContentCardElementRequest: flush final content (sequence={sequence})")
    print(f"  [6A-1] Content: {flush_content[:60]}{'...' if len(flush_content) > 60 else ''}")

    flush_req = ContentCardElementRequest.builder() \
        .card_id(card_id) \
        .element_id(element_id) \
        .request_body(
            ContentCardElementRequestBody.builder()
            .uuid(new_uuid("finalize-flush"))
            .content(flush_content)
            .sequence(sequence)
            .build()
        ) \
        .build()

    flush_resp = client.cardkit.v1.card_element.content(flush_req)
    print(f"  [6A-1] Flush: success={flush_resp.success()}, code={flush_resp.code}, msg={flush_resp.msg}")
    if not flush_resp.success():
        print(f"  [6A-1] ERROR: raw_status={flush_resp.raw.status_code}, body={flush_resp.raw.content if flush_resp.raw else 'N/A'}")

    # Step 6A-2: Settings API — set streaming_mode=False
    settings_json = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
    print(f"  [6A-2] SettingsCardRequest: settings={settings_json} (sequence={sequence + 1})")

    settings_req = SettingsCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            SettingsCardRequestBody.builder()
            .uuid(new_uuid("finalize-settings"))
            .settings(settings_json)
            .sequence(sequence + 1)
            .build()
        ) \
        .build()

    settings_resp = client.cardkit.v1.card.settings(settings_req)
    print(f"  [6A-2] Settings: success={settings_resp.success()}, code={settings_resp.code}, msg={settings_resp.msg}")
    if not settings_resp.success():
        print(f"  [6A-2] ERROR: raw_status={settings_resp.raw.status_code}, body={settings_resp.raw.content if settings_resp.raw else 'N/A'}")

    # Step 6B: UpdateCard — remove subtitle ("思考中..." -> ""), streaming_mode=False
    final_card_dict = build_streaming_card_dict(subtitle="", md_content=final_md_content, full_cardkit=True)
    # Override streaming_mode to False in the final card, remove streaming-specific fields
    final_card_dict["config"]["streaming_mode"] = False
    final_card_dict["config"].pop("update_multi", None)
    final_card_dict["config"].pop("streaming_config", None)
    final_card_json = json.dumps(final_card_dict, ensure_ascii=False)

    print(f"  [6B] UpdateCardRequest: subtitle='', streaming_mode=False (sequence={sequence + 2})")

    update_req = UpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            UpdateCardRequestBody.builder()
            .card(
                Card.builder()
                .type("card_json")
                .data(final_card_json)
                .build()
            )
            .uuid(new_uuid("finalize-update"))
            .sequence(sequence + 2)
            .build()
        ) \
        .build()

    update_resp = client.cardkit.v1.card.update(update_req)
    print(f"  [6B] UpdateCard: success={update_resp.success()}, code={update_resp.code}, msg={update_resp.msg}")
    if not update_resp.success():
        print(f"  [6B] ERROR: raw_status={update_resp.raw.status_code}, body={update_resp.raw.content if update_resp.raw else 'N/A'}")

    all_ok = flush_resp.success() and settings_resp.success() and update_resp.success()
    print(f"  Phase 6 overall: success={all_ok}")
    return all_ok


def phase7_post_validate(client: lark.Client, card_id: str, sequence: int) -> bool:
    """Phase 7: Post-validate — UpdateCard should still work after streaming ends."""
    print(f"\n[{ts()}] ========== Phase 7: Post-Validate (card still updatable?) ==========")

    post_card_dict = build_streaming_card_dict(subtitle="已完成", md_content="查询结果如下：\n\n1. 第一条结果\n2. 第二条结果\n\n以上就是所有结果。\n\n[已验证：卡片在流式结束后仍可更新]", full_cardkit=True)
    post_card_dict["config"]["streaming_mode"] = False
    post_card_dict["config"].pop("update_multi", None)
    post_card_dict["config"].pop("streaming_config", None)
    post_card_json = json.dumps(post_card_dict, ensure_ascii=False)

    req = UpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            UpdateCardRequestBody.builder()
            .card(
                Card.builder()
                .type("card_json")
                .data(post_card_json)
                .build()
            )
            .uuid(new_uuid("post-validate"))
            .sequence(sequence)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.update(req)
    print(f"  Post-validate UpdateCard: success={resp.success()}, code={resp.code}, msg={resp.msg}")
    if not resp.success():
        print(f"  ERROR: raw_status={resp.raw.status_code}, body={resp.raw.content if resp.raw else 'N/A'}")
    return resp.success()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("E2E Test: Full Streaming Card Lifecycle")
    print("=" * 70)

    # Load config
    config = load_feishu_config()
    print(f"[CONFIG] app_id={config['app_id']}, chat_id={config['chat_id']}")

    # Build client
    client = build_client(config)

    # ---- Phase 1: Create streaming card (full CardKit JSON with streaming_config) ----
    card_dict = build_streaming_card_dict(subtitle="思考中...", md_content="...", full_cardkit=True)
    card_id = phase1_create_card(client, config)

    # ---- Phase 2: Send card message (simplified JSON for im.v1.message.create) ----
    # im.v1.message.create doesn't support streaming_config/update_multi,
    # so we use a simplified card dict for the message payload.
    msg_card_dict = build_streaming_card_dict(subtitle="思考中...", md_content="...", full_cardkit=False)
    message_id = phase2_send_message(client, config, msg_card_dict)

    # ---- Phase 3: Wait 3s, update element (sequence=1) ----
    print(f"\n[{ts()}] Waiting 3 seconds before first element update...")
    time.sleep(3)
    ok3 = phase3_update_element(
        client, card_id, "md1",
        content_text="让我查询一下...",
        sequence=1,
        uuid_label="phase3",
    )

    # ---- Phase 4: Wait 3s, update element (sequence=2) ----
    print(f"\n[{ts()}] Waiting 3 seconds before second element update...")
    time.sleep(3)
    ok4 = phase3_update_element(
        client, card_id, "md1",
        content_text="查询结果如下：\n\n1. 第一条结果\n2. 第二条结果",
        sequence=2,
        uuid_label="phase4",
    )

    # ---- Phase 5: Wait 3s, update element with final content (sequence=3) ----
    print(f"\n[{ts()}] Waiting 3 seconds before final element update...")
    time.sleep(3)
    ok5 = phase3_update_element(
        client, card_id, "md1",
        content_text="查询结果如下：\n\n1. 第一条结果\n2. 第二条结果\n\n以上就是所有结果。",
        sequence=3,
        uuid_label="phase5",
    )

    # ---- Phase 6: Wait 2s, finalize (flush seq=4, settings seq=5, update seq=6) ----
    print(f"\n[{ts()}] Waiting 2 seconds before finalize...")
    time.sleep(2)
    final_md = "查询结果如下：\n\n1. 第一条结果\n2. 第二条结果\n\n以上就是所有结果。"
    ok6 = phase6_finalize(client, card_id, "md1", final_md, sequence=4)

    # ---- Phase 7: Wait 5s, post-validate (sequence=7) ----
    print(f"\n[{ts()}] Waiting 5 seconds before post-validate...")
    time.sleep(5)
    ok7 = phase7_post_validate(client, card_id, sequence=7)

    # ---- Summary ----
    print("\n" + "=" * 70)
    print("LIFECYCLE TEST SUMMARY")
    print("=" * 70)
    print(f"  card_id:    {card_id}")
    print(f"  message_id: {message_id}")
    print()
    print(f"  Phase 3 (element update seq=1):    {'PASS' if ok3 else 'FAIL'}")
    print(f"  Phase 4 (element update seq=2):    {'PASS' if ok4 else 'FAIL'}")
    print(f"  Phase 5 (element update seq=3):    {'PASS' if ok5 else 'FAIL'}")
    print(f"  Phase 6 (finalize settings+update): {'PASS' if ok6 else 'FAIL'}")
    print(f"  Phase 7 (post-validate update):     {'PASS' if ok7 else 'FAIL'}")
    print()
    print("RESULT: Check your Feishu chat now. The card should show the final")
    print("content WITHOUT the \"生成中\" indicator.")
    print("If \"生成中\" still shows → there's a client-side rendering issue or missing step.")


if __name__ == "__main__":
    main()
