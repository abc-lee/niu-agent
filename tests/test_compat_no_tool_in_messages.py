import pathlib


def test_api_context_messages_excludes_tool_role():
    """前端获取消息列表时不应包含 role='tool' 的消息"""
    source = pathlib.Path("niu_api/compat.py").read_text(encoding="utf-8")
    lines = source.split("\n")
    in_endpoint = False
    has_tool_filter = False
    for _i, line in enumerate(lines):
        # Match the GET endpoint decorator (not /delete, /update, /add)
        if '@router.get("/api/context/messages")' in line:
            in_endpoint = True
        elif in_endpoint and line.strip().startswith("@router"):
            in_endpoint = False
        elif in_endpoint:
            if "tool" in line and ("!=" in line or "not" in line):
                has_tool_filter = True
    assert has_tool_filter, (
        "/api/context/messages 端点应过滤 role='tool' 的消息，不让前端看到"
    )
