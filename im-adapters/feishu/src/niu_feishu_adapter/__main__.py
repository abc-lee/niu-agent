"""飞书 Adapter 入口 — python -m niu_feishu_adapter

退出码：0=正常, 1=瞬时错误(可重启), 2=永久错误(不重启)
"""
import asyncio
import os
import sys

from loguru import logger


def main():
    adapter_type = os.environ.get("NIU_IM_ADAPTER", "")
    if adapter_type != "feishu":
        logger.error(f"NIU_IM_ADAPTER={adapter_type}, expected 'feishu'")
        sys.exit(2)

    port_str = os.environ.get("NIU_GATEWAY_PORT", "")
    if not port_str:
        logger.error("Missing NIU_GATEWAY_PORT")
        sys.exit(2)
    try:
        gateway_port = int(port_str)
    except ValueError:
        logger.error(f"Invalid NIU_GATEWAY_PORT: {port_str}")
        sys.exit(2)

    app_id = os.environ.get("NIU_FEISHU_APP_ID", "")
    app_secret = os.environ.get("NIU_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.error("Missing NIU_FEISHU_APP_ID or NIU_FEISHU_APP_SECRET")
        sys.exit(2)

    push_chat_id = os.environ.get("NIU_FEISHU_USER_P2P_CHAT_ID", "")
    push_open_id = os.environ.get("NIU_FEISHU_USER_OPEN_ID", "")

    from niu_feishu_adapter.adapter import FeishuAdapter
    adapter = FeishuAdapter(
        gateway_port=gateway_port,
        app_id=app_id,
        app_secret=app_secret,
        push_chat_id=push_chat_id,
        push_open_id=push_open_id,
    )
    try:
        asyncio.run(adapter.run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"[FeishuAdapter] Fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
