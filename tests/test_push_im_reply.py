"""push_im_reply 统一投递入口单测（mock 层，无真实 LLM / 网络 / 图谱写入）。

覆盖五条路径（对应 gateway.py push_im_reply 全部分支）：
1. should_push_im()=False（无 IM 标志）→ False，router/gateway 零调用
2. has_channel('im')=False → False，不投递
3. im_cid 非空 → route_out(reply, 'im', cid) → True（SEND 终结卡片）
4. force-only（im_cid 空）+ gateway 已连接 → send_sync('', reply, pop_reply_to=False) → True
5. force-only + gateway 未连接/None → router.push(reply, 'im', '') → True

mock 目标为消费方命名空间（项目实证教训：patch 目标必须是消费方命名空间）：
- get_channel_router 是 push_im_reply 函数体内局部 import → patch 'niu_api.channel.get_channel_router'
- get_im_gateway 是 gateway 模块级名字 → patch 'niu_api.channel.gateway.get_im_gateway'
"""
import unittest.mock as mock

import pytest

from niu_api.channel import gateway as gateway_mod


def _make_runner(should_push: bool = True, im_cid: str = "") -> mock.MagicMock:
    """构造 MagicMock runner：should_push_im / get_im_channel 按场景配置。"""
    runner = mock.MagicMock()
    runner.should_push_im.return_value = should_push
    runner.get_im_channel.return_value = im_cid
    return runner


def _make_router(has_im: bool = True) -> mock.MagicMock:
    """构造 router：has_channel 按场景配置，route_out/push 为 AsyncMock（函数内 await）。"""
    router = mock.MagicMock()
    router.has_channel.return_value = has_im
    router.route_out = mock.AsyncMock()
    router.push = mock.AsyncMock()
    return router


@pytest.mark.asyncio
async def test_no_flags_returns_false_without_any_delivery():
    """路径 1：无 IM 标志（should_push_im=False）→ False，router/gateway 零调用。"""
    runner = _make_runner(should_push=False)
    with mock.patch("niu_api.channel.get_channel_router") as get_router, \
            mock.patch.object(gateway_mod, "get_im_gateway") as get_gw:
        result = await gateway_mod.push_im_reply(runner, "reply text")
    assert result is False
    get_router.assert_not_called()
    get_gw.assert_not_called()


@pytest.mark.asyncio
async def test_no_im_channel_registered_returns_false():
    """路径 2：should_push_im=True 但 has_channel('im')=False → False，不投递。"""
    runner = _make_runner(should_push=True, im_cid="oc_cid")
    router = _make_router(has_im=False)
    with mock.patch("niu_api.channel.get_channel_router", return_value=router) as get_router, \
            mock.patch.object(gateway_mod, "get_im_gateway") as get_gw:
        result = await gateway_mod.push_im_reply(runner, "reply text")
    assert result is False
    get_router.assert_called_once_with()
    router.route_out.assert_not_awaited()
    router.push.assert_not_awaited()
    get_gw.assert_not_called()


@pytest.mark.asyncio
async def test_im_cid_nonempty_routes_out_send():
    """路径 3：im_cid 非空 → route_out(reply, 'im', cid) SEND 终结，返回 True。"""
    runner = _make_runner(should_push=True, im_cid="oc_abc123")
    router = _make_router(has_im=True)
    with mock.patch("niu_api.channel.get_channel_router", return_value=router), \
            mock.patch.object(gateway_mod, "get_im_gateway") as get_gw:
        result = await gateway_mod.push_im_reply(runner, "reply text")
    assert result is True
    router.route_out.assert_awaited_once_with("reply text", "im", "oc_abc123")
    router.push.assert_not_awaited()
    get_gw.assert_not_called()


@pytest.mark.asyncio
async def test_force_only_connected_gateway_sends_sync():
    """路径 4：force-only（im_cid 空）+ gateway 已连接 → send_sync('', reply, pop_reply_to=False)。"""
    runner = _make_runner(should_push=True, im_cid="")
    router = _make_router(has_im=True)
    gw = mock.MagicMock()
    gw.is_connected = True
    with mock.patch("niu_api.channel.get_channel_router", return_value=router), \
            mock.patch.object(gateway_mod, "get_im_gateway", return_value=gw):
        result = await gateway_mod.push_im_reply(runner, "reply text")
    assert result is True
    # 参数级断言：channel_id=''、pop_reply_to=False（SEND 终结流式卡，与 ChatQueue 分支 2 逐字对齐）
    gw.send_sync.assert_called_once_with("", "reply text", pop_reply_to=False)
    router.route_out.assert_not_awaited()
    router.push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway", [None, mock.MagicMock(is_connected=False)])
async def test_force_only_gateway_unavailable_pushes_independent(gateway):
    """路径 5：force-only + gateway 未连接/None → router.push(reply, 'im', '') 独立消息。"""
    runner = _make_runner(should_push=True, im_cid="")
    router = _make_router(has_im=True)
    with mock.patch("niu_api.channel.get_channel_router", return_value=router), \
            mock.patch.object(gateway_mod, "get_im_gateway", return_value=gateway):
        result = await gateway_mod.push_im_reply(runner, "reply text")
    assert result is True
    router.push.assert_awaited_once_with("reply text", "im", "")
    router.route_out.assert_not_awaited()
