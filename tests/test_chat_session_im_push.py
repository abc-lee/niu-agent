"""chat_session IM 推送测试：统一入口 push_im_reply 收敛闸门，chat_error 无条件投递。

真实逻辑：
- niu_api.compat.chat_session 锁释放后的推送 try 块 = 无条件 `await push_im_reply(runner, full_reply)`
  （无 if 闸门包裹：chat_error 也投递——错误文案流式期已进卡必须终结，对齐 ChatQueue 分支 2）。
- 闸门（should_push_im）、channel_id 来源（get_im_channel）、force-only SEND 终结全部收敛于
  niu_api.channel.gateway.push_im_reply 单一入口（用户拍板：全局只有一个 IM 推送判定）。
本测试通过 AST 审查结构，避免整端点运行触发真实 LLM：
1. compat 推送块：无闸门内联、恰 1 处 push_im_reply(runner, full_reply)、无 set_im_channel/force、无条件投递。
2. push_im_reply 函数体：内含 should_push_im 单一入口调用。
"""
import ast
import inspect

from niu_api import compat


def _extract_push_block(source: str) -> ast.Try:
    """从 compat.py 源码提取 chat_session 的 IM 推送 try 块。

    特征：锁释放后、含 push_im_reply 调用（统一投递入口）的 try 块。
    """
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_session"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            # 递归找 push_im_reply 调用（可能被 await 包裹）
            if _contains_push_im_reply(node):
                return node
    raise AssertionError("IM 推送块未找到")


def _contains_call(node: ast.AST, name: str) -> bool:
    """判断 AST 节点内是否含指定函数名调用（Name id 或 Attribute attr，含 await 包裹）。"""
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Call):
            fn = stmt.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == name:
                return True
    return False


def _contains_push_im_reply(node: ast.AST) -> bool:
    """判断 AST 节点内是否含 push_im_reply 调用（统一投递入口）。"""
    return _contains_call(node, "push_im_reply")


def _push_block() -> ast.Try:
    return _extract_push_block(inspect.getsource(compat))


def test_push_uses_unified_gate_inside_push_im_reply():
    """闸门收敛于 push_im_reply 单一入口（用户拍板：全局只有一个 IM 推送判定）。

    ① compat 推送块内不得内联 route_out/should_push_im/get_im_channel/get_im_force；
    ② push_im_reply 函数体（gateway.py，AST 定位）内含 should_push_im 调用——单一入口收敛。
    """
    from niu_api.channel import gateway as gateway_mod

    block = _push_block()
    # ① compat 推送块零闸门/投递内联
    inner_names = set()
    for c in ast.walk(block):
        if isinstance(c, ast.Call):
            fn = c.func
            if isinstance(fn, ast.Name):
                inner_names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                inner_names.add(fn.attr)
    forbidden = {"route_out", "should_push_im", "get_im_channel", "get_im_force"}
    assert not (forbidden & inner_names), \
        f"compat 推送块不得内联闸门/投递逻辑（应收口 push_im_reply）, 实际调用: {sorted(inner_names)}"

    # ② push_im_reply 函数体含 should_push_im 单一入口
    tree = ast.parse(inspect.getsource(gateway_mod))
    fn_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "push_im_reply"
    )
    assert _contains_call(fn_node, "should_push_im"), "push_im_reply 函数体必须调用 should_push_im 单一入口"


def test_push_uses_get_im_channel_for_channel_id():
    """推送块恰有 1 处 push_im_reply 调用、参数为 (runner, full_reply)。

    channel_id 来源（get_im_channel）与投递形态（route_out/send_sync）已收敛于
    push_im_reply 内部，compat 侧只传 runner 与回复全文。
    """
    block = _push_block()
    calls = [
        c for c in ast.walk(block) if isinstance(c, ast.Call)
        and ((isinstance(c.func, ast.Name) and c.func.id == "push_im_reply")
             or (isinstance(c.func, ast.Attribute) and c.func.attr == "push_im_reply"))
    ]
    assert len(calls) == 1, f"推送块应恰有 1 处 push_im_reply 调用, 实际 {len(calls)}"
    args = [ast.unparse(a) for a in calls[0].args]
    assert args == ["runner", "full_reply"], \
        f"push_im_reply 参数应为 (runner, full_reply), 实际: {args}"


def test_no_set_im_channel_in_push_block():
    """推送块内绝不调用 set_im_channel / set_im_force（规则 5：子 Agent 返回不改变标志）。"""
    block = _push_block()
    for c in ast.walk(block):
        if isinstance(c, ast.Call):
            fn = c.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            assert name not in ("set_im_channel", "set_im_force"), \
                f"推送块不得调用 {name}"


def _parent_map(node: ast.AST) -> dict:
    """构建子节点 → 父节点映射（AST 无内置 parent 指针）。"""
    parents = {node: None}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def test_push_block_is_unconditional():
    """推送块内 push_im_reply 无 if 包裹（chat_error 无条件投递语义）。

    对齐 ChatQueue 分支 2：错误文案流式期已进卡（LLM 错误）必须终结；
    若回归为条件投递，异常回复会留下"思考中"不终结卡片。
    """
    block = _push_block()
    call = next(
        c for c in ast.walk(block) if isinstance(c, ast.Call)
        and ((isinstance(c.func, ast.Name) and c.func.id == "push_im_reply")
             or (isinstance(c.func, ast.Attribute) and c.func.attr == "push_im_reply"))
    )
    # 调用语句父节点链不得出现 ast.If
    parents = _parent_map(block)
    node = call
    while node is not None:
        assert not isinstance(node, ast.If), \
            f"push_im_reply 调用不得被 if 包裹（无条件投递）: {ast.unparse(node.test)}"
        node = parents[node]
    # 显式锁定：推送块内不得存在 test 为 'chat_error is None' 的判断（防语义退化）
    for n in ast.walk(block):
        if isinstance(n, ast.If) and "chat_error is None" in ast.unparse(n.test):
            raise AssertionError("推送块含 chat_error is None 条件判断——违反无条件投递语义")
