import pathlib


def test_api_context_messages_visible_only():
    """前端历史滚动：/api/context/messages 必须走 visible_only（可见过滤下沉 SQL），
    limit 语义 = 可见消息条数——tool 密集段不得让分页整页滤后为空（spec 2026-09-06）"""
    source = pathlib.Path("niu_api/compat.py").read_text(encoding="utf-8")
    lines = source.split("\n")
    in_endpoint = False
    endpoint_body = []
    for line in lines:
        # Match the GET endpoint decorator (not /delete, /update, /add)
        if '@router.get("/api/context/messages")' in line:
            in_endpoint = True
        elif in_endpoint and line.strip().startswith("@router"):
            break
        elif in_endpoint:
            endpoint_body.append(line)
    body = "\n".join(endpoint_body)
    assert "visible_only=True" in body, (
        "/api/context/messages 端点必须调用 store.get_messages(..., visible_only=True)"
        "——可见过滤下沉 SQL，前端不得看到 role='tool' 消息"
    )
