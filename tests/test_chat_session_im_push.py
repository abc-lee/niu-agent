"""chat_session IM 推送闸门测试：异步子 Agent 续答（source=''）应回退广播到 IM；Electron 用户消息不推 IM。

真实逻辑在 niu_api.compat.chat_session 的锁释放后推送块。
本测试通过 AST 审查该推送块的结构（闸门条件、channel_id 来源、无 set_im_channel），
避免整端点运行触发真实 LLM。
"""
import ast
import inspect

from niu_api import compat


def _extract_push_block(source: str) -> ast.Try:
    """从 compat.py 源码提取 chat_session 的 IM 推送 try 块。

    特征：锁释放后、含 route_out 调用的 try 块。
    """
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_session"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            # 递归找 route_out 调用（可能被 await 包裹）
            if _contains_route_out(node):
                return node
    raise AssertionError("IM 推送块未找到")


def _contains_route_out(node: ast.AST) -> bool:
    """判断 AST 节点内是否含 route_out 调用（含 await 包裹）。"""
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Call):
            fn = stmt.func
            if isinstance(fn, ast.Name) and fn.id == "route_out":
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == "route_out":
                return True
    return False


def _push_block() -> ast.Try:
    return _extract_push_block(inspect.getsource(compat))


def test_push_gate_condition_is_source_not_electron():
    """推送闸门必须是 source != 'electron' 而非 if im_cid。"""
    block = _push_block()
    if_node = next(n for n in ast.walk(block) if isinstance(n, ast.If))
    cond_src = ast.unparse(if_node.test)
    assert "request.source" in cond_src, f"闸门未使用 source 判断, 实际: {cond_src}"
    assert "!=" in cond_src and "electron" in cond_src, f"闸门未排除 electron, 实际: {cond_src}"
    # 闸门不应再依赖 im_cid 非空（定时任务场景恒空导致静默丢弃）
    assert "im_cid" not in cond_src, f"闸门仍依赖 im_cid, 实际: {cond_src}"


def test_push_uses_get_im_channel_for_channel_id():
    """推送调 route_out 用 get_im_channel() 作 channel_id。"""
    block = _push_block()
    calls = [n for n in ast.walk(block) if isinstance(n, ast.Call)]
    route_call = next(
        c for c in calls
        if (isinstance(c.func, ast.Name) and c.func.id == "route_out")
        or (isinstance(c.func, ast.Attribute) and c.func.attr == "route_out")
    )
    args = [ast.unparse(a) for a in route_call.args]
    assert any("get_im_channel" in a for a in args), \
        f"route_out 第三参应为 get_im_channel(), 实际: {args}"


def test_no_set_im_channel_in_push_block():
    """推送块内绝不调用 set_im_channel（规则 4：子 Agent 通道返回不改变通道）。"""
    block = _push_block()
    for c in ast.walk(block):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name):
            assert c.func.id != "set_im_channel", "推送块不得调用 set_im_channel"
