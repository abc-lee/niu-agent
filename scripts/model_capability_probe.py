#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型能力探测器 CLI 壳（组件 1）——主 Agent bash 调用入口。

用法：
    python/bin/python3 scripts/model_capability_probe.py --api-base URL --model MODEL \
        [--api-type anthropic] [--lightrag] [--api-key KEY]

参数：
    --api-base   API Base URL（必填，如 https://ark.cn-beijing.volces.com/api/plan/v3）
    --model      模型名（必填，不带 provider 前缀）
    --api-type   API 类型（openai/anthropic），缺省按 api-base 域名推导
    --lightrag   探测 lightrag_llm 场景（档案键后缀 |lightrag；api-key 缺省从
                 lightrag_llm 段读）
    --api-key    API Key（可选；缺省从 user-config.json 对应段读取——llm 段或
                 lightrag_llm 段，键名大小写归一）。本地模型（localhost/127.0.0.1）
                 免 key。apiKey 不 argv 明文必需——命令串不入消息历史明文。

stdout 契约：
    JSON（probe_status="ok"/"failed"/"partial"、档案路径、supported/unsupported
    摘要、ignores_unknown）——主 Agent 按 probe_status 区分成功/部分失败/完全失败
    如实汇报；stdout 不含 apiKey（脱敏）。

退出码：0 = 档案已更新；1 = 探测失败未覆盖旧档（主 Agent 可据此告知用户"保持旧档案"）。

bash timeout 预算（R5 补）：
    单场景探测 ≤ 11 次 × 10s ≈ 110s——主 Agent 显式传 timeout=120（bash 上限 300s 内）；
    双场景（llm + --lightrag）22 次 ≈ 220s——传 timeout=240 或分两次调用。
"""

import argparse
import json
import re
import sys
from pathlib import Path

# 项目根目录加入 sys.path（直接运行与本模块被测试 import 两种场景都可用）
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from niu_api.config import CONFIG_PATH  # noqa: E402
from niu_api.llm_proxy import get_llm_config  # noqa: E402
from niu_api.model_probe import (  # noqa: E402
    build_profile_key,
    default_profile_path,
    is_local_api_base,
    probe,
)


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数解析（独立函数供测试直接调用）。"""
    parser = argparse.ArgumentParser(
        prog="model_capability_probe",
        description=(
            "模型能力探测器：探测 reasoning_effort/thinking/response_format/tools "
            "支持档位，写入 ~/.niu/model_capabilities.json（退出码 0=档案已更新，"
            "1=探测失败未覆盖旧档）"
        ),
    )
    parser.add_argument("--api-base", required=True, help="API Base URL（必填）")
    parser.add_argument("--model", required=True, help="模型名，不带 provider 前缀（必填）")
    parser.add_argument("--api-type", default=None,
                        help="API 类型（openai/anthropic），缺省按 api-base 域名推导")
    parser.add_argument("--lightrag", action="store_true",
                        help="探测 lightrag_llm 场景（档案键后缀 |lightrag）")
    parser.add_argument("--api-key", default=None,
                        help="API Key（可选；缺省从 user-config.json 对应段读取；本地模型免 key）")
    return parser


def _sanitize(text: str) -> str:
    """脱敏：apiKey 不出现于 stdout（key=xxx / Bearer xxx 掩码）。"""
    text = re.sub(r"(?i)(api[_-]?key|key)=[^&\s\"']+", r"\1=***", text)
    text = re.sub(r"Bearer\s+[^\s]+", "Bearer ***", text)
    return text[:500]


def _read_user_config() -> dict:
    """读 user-config.json 全量数据（供 probe 取对应段 litellm_kwargs 等）。"""
    try:
        return json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置缺失时按空配置探测（仅 raw 候选）
        return {}


def _print_result(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    """CLI 主流程。返回退出码（0=档案已更新；1=探测失败未覆盖旧档）。"""
    args = build_parser().parse_args(argv)

    api_base = args.api_base
    api_type = args.api_type or "openai"
    profile_path = default_profile_path()

    # apiKey 缺省从 user-config.json 对应段读（get_llm_config 返回小写键——大小写归一）；
    # 本地模型（localhost/127.0.0.1）免 apiKey——不读配置、置空（对齐 _probe_llm is_local 豁免）
    api_key = args.api_key
    if api_key is None:
        if is_local_api_base(api_base):
            api_key = ""
        else:
            try:
                cfg = get_llm_config(use_lightrag_config=args.lightrag)
                api_key = cfg.get("apikey", "") or ""
            except Exception as e:  # noqa: BLE001 - 读配置失败 → 探测失败退出
                _print_result({
                    "probe_status": "failed",
                    "error": f"读取配置失败: {_sanitize(str(e))}",
                    "profile_path": str(profile_path),
                })
                return 1

    try:
        profile = probe(
            api_base=api_base,
            api_key=api_key,
            model=args.model,
            api_type=api_type,
            lightrag=args.lightrag,
            user_config=_read_user_config(),
            profile_path=profile_path,
        )
    except Exception as e:  # noqa: BLE001 - 探测异常 → 未覆盖旧档，退出 1
        _print_result({
            "probe_status": "failed",
            "error": f"探测失败: {_sanitize(str(e))}",
            "profile_path": str(profile_path),
        })
        return 1

    _print_result({
        "probe_status": profile["probe_status"],
        "profile_path": str(profile_path),
        "profile_key": build_profile_key(api_base, args.model, args.lightrag),
        "ignores_unknown": profile["ignores_unknown"],
        "reasoning_effort": profile["reasoning_effort"],
        "thinking": profile["thinking"],
        "response_format": profile["response_format"],
        "tools": profile["tools"],
    })
    return 0 if profile["probe_status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
