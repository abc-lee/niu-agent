"""
E2E Test: Compare THREE methods to finalize a streaming card in Feishu.

Background:
  The lark_oapi SDK's MarkdownStreamController uses the `settings` API
  (cardkit_update_settings / PATCH /open-apis/cardkit/v1/cards/{card_id}/settings)
  to finalize, NOT BatchUpdateCard. Our previous v9 code used BatchUpdateCard with
  `partial_update_setting` action, which may be why the "generating" indicator
  never disappears.

Three test methods:
  Test A: BatchUpdateCard + partial_update_setting action (v9 approach -- suspected broken)
  Test B: Settings API with streaming_mode=False (SDK's official approach -- should work)
  Test C: UpdateCard (full card JSON with streaming_mode=False in config) -- alternative

Self-validation strategy:
  After finalizing with each method, attempt UpdateCardElementContent on the card.
  If streaming_mode was properly closed, the element update should either succeed
  or return a specific error code indicating streaming is closed.

  Also, after finalizing, attempt another UpdateCard with streaming_mode=False in
  the card config. If the card was already finalized, this should either succeed
  or return a no-op.

Run:
  cd REDACTED_USER_PATH/tools/ai-bot && python tests/test_v10_finalize_methods.py

Prerequisites:
  ~/.niu/preferences.json with feishu.app_id / app_secret / user_p2p_chat_id
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so 'lark_oapi' from python/ is importable
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
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    Card,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)


# ---------------------------------------------------------------------------
# Config & Client
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


# ---------------------------------------------------------------------------
# Card JSON Builder
# ---------------------------------------------------------------------------

def build_streaming_card_dict(content: str, title: str, *, streaming: bool = True) -> dict:
    """Build Card JSON 2.0 structure for streaming mode.

    Includes update_multi=True as required by API docs for streaming cards.
    """
    return {
        "schema": "2.0",
        "header": {
            "title": {
                "content": title,
                "tag": "plain_text",
            }
        },
        "config": {
            "streaming_mode": streaming,
            "update_multi": True,
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


# ---------------------------------------------------------------------------
# Card Operations
# ---------------------------------------------------------------------------

def create_card_entity(client: lark.Client, card_dict: dict, label: str = "") -> str:
    """Create card entity via CardKit. Returns card_id."""
    card_json = json.dumps(card_dict, ensure_ascii=False)
    req = CreateCardRequest.builder() \
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(card_json)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.create(req)
    if not resp.success():
        raise RuntimeError(f"[{label}] CreateCard failed: code={resp.code}, msg={resp.msg}")
    card_id = resp.data.card_id
    print(f"  CreateCard: OK, card_id={card_id}")
    return card_id


def send_card_message(client: lark.Client, config: dict, card_dict: dict, label: str = "") -> str:
    """Send card as inline card message via im.v1. Returns message_id."""
    msg_content = json.dumps(card_dict, ensure_ascii=False)
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(config["chat_id"])
            .msg_type("interactive")
            .content(msg_content)
            .build()
        ) \
        .build()

    resp = client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(f"[{label}] SendMessage failed: code={resp.code}, msg={resp.msg}")
    message_id = resp.data.message_id
    print(f"  SendMessage: OK, message_id={message_id}")
    return message_id


def update_element_content(client: lark.Client, card_id: str, element_id: str,
                           new_content: str, sequence: int, uuid_label: str,
                           label: str = "") -> dict:
    """Update element content via ContentCardElementRequest (PUT).

    Returns dict with success, code, msg.
    """
    req = ContentCardElementRequest.builder() \
        .card_id(card_id) \
        .element_id(element_id) \
        .request_body(
            ContentCardElementRequestBody.builder()
            .uuid(uuid_label)
            .content(new_content)
            .sequence(sequence)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card_element.content(req)
    result = {
        "success": resp.success(),
        "code": resp.code,
        "msg": resp.msg,
    }
    print(f"  UpdateElement ({label}): success={result['success']}, code={result['code']}, seq={sequence}")
    return result


def update_card_full(client: lark.Client, card_id: str, card_dict: dict,
                     sequence: int, uuid_label: str, label: str = "") -> dict:
    """Update card via UpdateCard (PUT full card JSON).

    Returns dict with success, code, msg.
    """
    card_json = json.dumps(card_dict, ensure_ascii=False)
    req = UpdateCardRequest.builder() \
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

    resp = client.cardkit.v1.card.update(req)
    result = {
        "success": resp.success(),
        "code": resp.code,
        "msg": resp.msg,
    }
    print(f"  UpdateCard ({label}): success={result['success']}, code={result['code']}, seq={sequence}")
    return result


# ---------------------------------------------------------------------------
# Three Finalize Methods
# ---------------------------------------------------------------------------

def finalize_batch_update_card(client: lark.Client, card_id: str,
                               sequence: int, uuid_label: str,
                               label: str = "") -> dict:
    """Test A: Finalize via BatchUpdateCard with partial_update_setting action.

    This is the v9 approach that we suspect is broken.
    """
    actions = json.dumps([
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

    req = BatchUpdateCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            BatchUpdateCardRequestBody.builder()
            .uuid(uuid_label)
            .sequence(sequence)
            .actions(actions)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.batch_update(req)
    result = {
        "success": resp.success(),
        "code": resp.code,
        "msg": resp.msg,
    }
    print(f"  Finalize (BatchUpdateCard, {label}): success={result['success']}, code={result['code']}")
    return result


def finalize_settings_api(client: lark.Client, card_id: str,
                          sequence: int, uuid_label: str,
                          label: str = "") -> dict:
    """Test B: Finalize via Settings API (PATCH .../settings).

    This is the SDK's official approach used by MarkdownStreamController.
    The `settings` field is a JSON STRING, not a dict.
    """
    settings_json = json.dumps({"config": {"streaming_mode": False}})

    req = SettingsCardRequest.builder() \
        .card_id(card_id) \
        .request_body(
            SettingsCardRequestBody.builder()
            .settings(settings_json)
            .uuid(uuid_label)
            .sequence(sequence)
            .build()
        ) \
        .build()

    resp = client.cardkit.v1.card.settings(req)
    result = {
        "success": resp.success(),
        "code": resp.code,
        "msg": resp.msg,
    }
    print(f"  Finalize (SettingsAPI, {label}): success={result['success']}, code={result['code']}")
    return result


def finalize_update_card(client: lark.Client, card_id: str,
                         sequence: int, uuid_label: str,
                         label: str = "") -> dict:
    """Test C: Finalize via UpdateCard with full card JSON where config.streaming_mode=False.

    This sends the complete card structure with streaming_mode turned off.
    """
    final_card_dict = build_streaming_card_dict(
        content="Finalized content",
        title=label,
        streaming=False,
    )
    return update_card_full(client, card_id, final_card_dict,
                            sequence=sequence, uuid_label=uuid_label,
                            label="finalize-UpdateCard")


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

def run_single_test(client: lark.Client, config: dict,
                    test_name: str, finalize_fn) -> dict:
    """Run one test: create -> send -> update element -> finalize -> validate.

    Args:
        test_name: e.g. "Test A"
        finalize_fn: function(client, card_id, sequence, uuid, label) -> dict

    Returns:
        dict with all results for summary.
    """
    print(f"\n{'=' * 70}")
    print(f"TEST: {test_name}")
    print(f"{'=' * 70}")

    # 1. Create card entity with streaming_mode=True
    card_dict = build_streaming_card_dict(
        content=f"{test_name}: initial content (streaming...)",
        title=test_name,
        streaming=True,
    )
    card_id = create_card_entity(client, card_dict, label=test_name)

    # 2. Send as inline card message
    message_id = send_card_message(client, config, card_dict, label=test_name)

    # 3. Wait 2s
    print("  Waiting 2s...")
    time.sleep(2)

    # 4. Update element content via ContentCardElementRequest (sequence=1)
    elem_result = update_element_content(
        client, card_id, "md1",
        new_content=f"{test_name}: updated content (still streaming...)",
        sequence=1, uuid_label=f"{test_name}-elem-1",
        label="streaming-update",
    )

    # 5. Wait 2s
    print("  Waiting 2s...")
    time.sleep(2)

    # 6. Finalize using the test method (sequence=2)
    seq_finalize = 2
    finalize_result = finalize_fn(
        client, card_id,
        sequence=seq_finalize,
        uuid_label=f"{test_name}-finalize",
        label=test_name,
    )

    # 7. Wait 3s for server-side propagation
    print("  Waiting 3s for finalization to propagate...")
    time.sleep(3)

    # 8. Post-finalize validation 1: Try UpdateCardElementContent again (sequence=3)
    post_elem_result = update_element_content(
        client, card_id, "md1",
        new_content=f"{test_name}: post-finalize content",
        sequence=3, uuid_label=f"{test_name}-post-elem-1",
        label="POST-FINALIZE",
    )

    # 9. Post-finalize validation 2: Try UpdateCard with streaming_mode=False (sequence=4)
    no_stream_card = build_streaming_card_dict(
        content=f"{test_name}: post-finalize UpdateCard",
        title=test_name,
        streaming=False,
    )
    post_update_result = update_card_full(
        client, card_id, no_stream_card,
        sequence=4, uuid_label=f"{test_name}-post-updatecard",
        label="POST-FINALIZE",
    )

    # Summary for this test
    print(f"\n  --- {test_name} Results ---")
    print(f"  CreateCard:    OK, card_id={card_id}")
    print(f"  SendMessage:   OK, message_id={message_id}")
    print(f"  UpdateElement: success={elem_result['success']}, code={elem_result['code']}")
    print(f"  Finalize:      success={finalize_result['success']}, code={finalize_result['code']}")
    print(f"  Post-validate UpdateElement: success={post_elem_result['success']}, code={post_elem_result['code']}")
    print(f"  Post-validate UpdateCard:    success={post_update_result['success']}, code={post_update_result['code']}")

    return {
        "test_name": test_name,
        "card_id": card_id,
        "message_id": message_id,
        "elem_success": elem_result["success"],
        "elem_code": elem_result["code"],
        "finalize_success": finalize_result["success"],
        "finalize_code": finalize_result["code"],
        "post_elem_success": post_elem_result["success"],
        "post_elem_code": post_elem_result["code"],
        "post_update_success": post_update_result["success"],
        "post_update_code": post_update_result["code"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_feishu_config()
    print(f"[CONFIG] app_id={config['app_id']}, chat_id={config['chat_id']}")

    client = build_client(config)

    # ---- Test A: BatchUpdateCard + partial_update_setting (v9 approach) ----
    result_a = run_single_test(
        client, config,
        test_name="Test A",
        finalize_fn=finalize_batch_update_card,
    )

    # Pause between tests
    print("\n" + "-" * 70)
    print("Test A done. Waiting 5s before Test B...")
    time.sleep(5)

    # ---- Test B: Settings API (SDK official approach) ----
    result_b = run_single_test(
        client, config,
        test_name="Test B",
        finalize_fn=finalize_settings_api,
    )

    # Pause between tests
    print("\n" + "-" * 70)
    print("Test B done. Waiting 5s before Test C...")
    time.sleep(5)

    # ---- Test C: UpdateCard with streaming_mode=False ----
    result_c = run_single_test(
        client, config,
        test_name="Test C",
        finalize_fn=finalize_update_card,
    )

    # ===================================================================
    # SUMMARY
    # ===================================================================
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print()

    method_labels = {
        "Test A": "BatchUpdateCard",
        "Test B": "Settings API",
        "Test C": "UpdateCard",
    }
    for r in [result_a, result_b, result_c]:
        name = r["test_name"]
        method = method_labels[name]
        fin_ok = "OK" if r["finalize_success"] else "FAIL"
        post_elem_ok = "OK" if r["post_elem_success"] else "FAIL"
        post_upd_ok = "OK" if r["post_update_success"] else "FAIL"
        print(f"  {name} ({method}): finalize={fin_ok}(code={r['finalize_code']}), "
              f"post_update_elem={post_elem_ok}(code={r['post_elem_code']}), "
              f"post_update_card={post_upd_ok}(code={r['post_update_code']})")

    print()
    print("CONCLUSION:")
    print()

    a_fin = result_a["finalize_success"]
    b_fin = result_b["finalize_success"]
    c_fin = result_c["finalize_success"]

    a_post = result_a["post_elem_success"]
    b_post = result_b["post_elem_success"]
    c_post = result_c["post_elem_success"]

    if b_fin and not a_fin:
        print("  - Test B (Settings API) works but Test A (BatchUpdateCard) fails")
        print("    => SDK's settings API is the correct way to finalize streaming cards")
        print("    => BatchUpdateCard partial_update_setting is NOT a valid finalize method")
    elif a_fin and b_fin:
        print("  - Both Test A and Test B finalize successfully")
        print("    => BatchUpdateCard is also a valid finalize method")
        if a_post and not b_post:
            print("    => But Settings API may close streaming more thoroughly (post-update fails)")
        elif b_post and not a_post:
            print("    => BatchUpdateCard may close streaming more thoroughly (post-update fails)")
        else:
            print("    => Both methods close streaming equally well")
    elif not a_fin and not b_fin and c_fin:
        print("  - Only Test C (UpdateCard) works")
        print("    => Full card update with streaming_mode=False is the reliable approach")
    elif not a_fin and not b_fin and not c_fin:
        print("  - NONE of the three methods finalize successfully")
        print("    => Need to investigate further — possibly a different API or timing issue")

    print()
    print("Detailed error codes (non-zero means API returned an error):")
    for r in [result_a, result_b, result_c]:
        print(f"  {r['test_name']}: finalize_code={r['finalize_code']}, "
              f"post_elem_code={r['post_elem_code']}, "
              f"post_update_code={r['post_update_code']}")

    print()
    print("Please verify in Feishu client:")
    print("  - Which cards still show the 'generating' indicator?")
    print("  - Which cards are interactive (no loading animation)?")


if __name__ == "__main__":
    main()
