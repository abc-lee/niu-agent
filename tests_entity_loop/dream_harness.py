#!/usr/bin/env python3
"""
dream_harness.py — dream-evolver「查重空转死循环」验证 harness（TDD：绿验证）

背景（2026-09-06）：entity-extractor 两次空转（fields 纯名查重无描述→死循环），
已修。dream-evolver 提示词有同类隐患：4 处教 fields=["entity_name","entity_type"]
纯名检查（L172/L224/L511/L545），且无「查过即终局」收敛条款——精加工任务若对同一
实体反复 search/get_entity_info 交替也会空转。修复=加「收敛铁律」+ 3 处收敛措辞
（最小干预，不改工作流）。本 harness 验证修复后 dream 不空转。

与 entity harness 差异：
  - agent 配置 = config/agents/dream-evolver.md（frontmatter mcpToolFilter 14 工具）
  - 输入 = ~/.niu/md/F3_dream_workset.md 精选样本（今日真实工作集，81 行）
  - 非 lightrag 工具 stub：edit/write/read/bash（bash 只读命令放行，写操作拒绝）
  - 绿判定：有限轮结束（有 @end/covered_all/纯文本收尾）+ 图谱写操作 ≥1
    （dream 精加工必须产出 insert/edit/relation，否则是空转）
  - 隔离铁律同 entity：NIU_STORAGE_DIR 指向临时图谱副本，生产图谱仅复制源

用法:
  python3 dream_harness.py                 # 跑一次，打印每轮日志 + 结果 JSON
  python3 dream_harness.py --expect-end    # 绿判定：有限轮 + 有写操作 → exit 0
  python3 dream_harness.py --max-turns 30  # 轮数上限（默认 25）
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/Users/lilei/tools/ai-bot")
PROMPT_MD = REPO / "config" / "agents" / "dream-evolver.md"
PROD_STORAGE = Path.home() / ".niu" / "lightrag_storage"
F3_SOURCE = Path.home() / ".niu" / "md" / "F3_dream_workset.md"
WORKDIR_F3 = "/Users/lilei/.niu/md/F3_dream_workset.md"  # task 消息里给模型的路径


def log(msg: str) -> None:
    print(msg, flush=True)


def load_llm_config() -> dict:
    cfg = json.loads((Path.home() / ".niu" / "config" / "user-config.json").read_text(encoding="utf-8"))
    return cfg["llm"]


def derive_provider_prefix(api_base: str | None, model: str, api_type: str | None = None) -> str:
    """照 litellm_adapter.py _derive_provider_prefix：volces.com → volcengine/ 路由。"""
    base = (api_base or "").lower()
    if "volces.com" in base:
        return f"volcengine/{model}"
    if "api.anthropic.com" in base:
        return f"anthropic/{model}"
    if api_type == "anthropic":
        return f"anthropic/{model}"
    return f"openai/{model}"


def build_request_params(cfg: dict, messages: list, tools: list, temperature: float) -> dict:
    """litellm.completion 参数组装——照 litellm_adapter.py 通道惯例（非流式版）。"""
    litellm_kwargs = cfg.get("litellm_kwargs") or {}
    params: dict = {
        "model": derive_provider_prefix(cfg.get("apiBase"), cfg["model"], cfg.get("type")),
        "api_base": cfg.get("apiBase") or None,
        "api_key": cfg.get("apiKey") or None,
        "max_tokens": cfg.get("max_tokens"),
        "timeout": 600,
        "temperature": temperature,
        "stream": False,
        "messages": messages,
        "tools": tools,
    }
    for k, v in litellm_kwargs.items():
        if k not in ("thinking", "sticky_session_headers"):
            params[k] = v
    if litellm_kwargs:
        params["drop_params"] = True
    extra: dict = {}
    effort = cfg.get("reasoning_effort")
    if effort and effort != "none":
        extra["reasoning_effort"] = effort
    if litellm_kwargs.get("thinking"):
        extra["thinking"] = litellm_kwargs["thinking"]
    if extra:
        params["extra_body"] = {**extra, **(litellm_kwargs.get("extra_body") or {})}
    return params


def load_prompt() -> tuple[str, float]:
    """dream-evolver.md → (剥 frontmatter 后的 system body, temperature)。"""
    text = PROMPT_MD.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise SystemExit("dream-evolver.md 未找到 frontmatter")
    fm, body = m.group(1), m.group(2)
    t = re.search(r"^temperature:\s*([\d.]+)", fm, re.M)
    temp = float(t.group(1)) if t else 0.3
    # 工具白名单
    wl = re.search(r"mcpToolFilter:\n\s*lightrag-server:\n((?:\s+- \S+\n)+)", fm)
    whitelist = re.findall(r"-\s+(\S+)", wl.group(1)) if wl else []
    return body, temp, whitelist


# 非 lightrag 工具 stub（frontmatter allowBaseTools: read/write/edit/bash）
def stub_edit(file_path: str, old_string: str = "", new_string: str = "") -> str:
    # skill 修改：验证 harness 里禁真改文件，返回成功占位（不污染）
    return json.dumps({"status": "ok", "message": "[stub] edit 已跳过（harness 不落盘）"}, ensure_ascii=False)


def stub_write(file_path: str, content: str = "") -> str:
    return json.dumps({"status": "ok", "message": "[stub] write 已跳过（harness 不落盘）"}, ensure_ascii=False)


def stub_read(file_path: str, offset: int = 1, limit: int = 500) -> str:
    p = Path(file_path)
    if not p.exists():
        return json.dumps({"status": "error", "message": f"文件不存在: {file_path}"}, ensure_ascii=False)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, int(offset) - 1)
    chunk = lines[start:start + int(limit)]
    body = "\n".join(f"{i + 1}|{ln}" for i, ln in enumerate(chunk, start=start + 1))
    if start + len(chunk) < len(lines):
        body += f"\n[Truncated at line {start + len(chunk)}. Use offset={start + len(chunk) + 1} to read more.]"
    return body


def stub_bash(command: str) -> str:
    # 只放行只读命令；写操作拒绝（防 harness 污染）
    if re.search(r"rm\s|mv\s|>|>>|mkfs|dd\s", command):
        return json.dumps({"status": "error", "message": "[stub] 写命令已拒绝（harness）"}, ensure_ascii=False)
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        return json.dumps({"status": "ok", "stdout": r.stdout[:500], "stderr": r.stderr[:200]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def build_tools(whitelist, lts) -> list:
    tools = []
    for name in whitelist:
        s = lts.TOOL_SCHEMAS.get(name)
        if not s:
            log(f"[warn] 白名单工具 {name} 不在 TOOL_SCHEMAS")
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    stub_specs = [
        ("read", "Read a file (offset/limit for paging).",
         {"type": "object", "properties": {
             "file_path": {"type": "string"},
             "offset": {"type": "integer"},
             "limit": {"type": "integer"}},
          "required": ["file_path"]}),
        ("write", "Write a file (stub in harness).",
         {"type": "object", "properties": {
             "file_path": {"type": "string"},
             "content": {"type": "string"}},
          "required": ["file_path", "content"]}),
        ("edit", "Edit a file (stub in harness).",
         {"type": "object", "properties": {
             "file_path": {"type": "string"},
             "old_string": {"type": "string"},
             "new_string": {"type": "string"}},
          "required": ["file_path", "old_string", "new_string"]}),
        ("bash", "Run a shell command (read-only in harness).",
         {"type": "object", "properties": {
             "command": {"type": "string"}},
          "required": ["command"]}),
    ]
    for name, desc, params in stub_specs:
        tools.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return tools


def execute_tool(name: str, args: dict, lts, whitelist) -> str:
    if name == "read":
        return stub_read(str(args.get("file_path", "")), int(args.get("offset") or 1), int(args.get("limit") or 500))
    if name == "edit":
        return stub_edit(**args)
    if name == "write":
        return stub_write(**args)
    if name == "bash":
        return stub_bash(str(args.get("command", "")))
    if name in whitelist:
        try:
            result = lts.call_tool(name, args)
        except Exception as e:
            result = {"status": "error", "message": str(e)}
        return json.dumps(result, ensure_ascii=False, default=str)
    return json.dumps({"status": "error", "message": f"工具 {name} 不在白名单"}, ensure_ascii=False)


def summarize_tc(tc) -> str:
    try:
        a = json.loads(tc.function.arguments or "{}")
    except Exception:
        a = {}
    s = json.dumps(a, ensure_ascii=False)
    if len(s) > 100:
        s = s[:97] + "..."
    return f"{tc.function.name}({s})"


def build_f3_sample(tmp: Path) -> tuple[Path, int]:
    """F3 工作集精选样本——取今日真实 F3 前若干条含实体/纠错信号的记录。"""
    lines = F3_SOURCE.read_text(encoding="utf-8", errors="replace").splitlines()
    # 保留完整记录（到下一个 {"msg_id" 前），最多 40 行
    keep, count = [], 0
    for ln in lines:
        keep.append(ln)
        if ln.startswith('{"msg_id"'):
            count += 1
            if count >= 3:  # 3 条完整会话足够触发精加工
                break
    path = tmp / "F3_dream_workset.md"
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return path, len(keep)


def worker(tmp_str: str, max_turns: int) -> None:
    import litellm
    import niu_lightrag_server
    lts = niu_lightrag_server

    tmp = Path(tmp_str)
    storage_dir = tmp / "lightrag_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    for f in PROD_STORAGE.glob("*"):
        if f.is_file():
            shutil.copy2(f, storage_dir / f.name)

    body, temp, whitelist = load_prompt()
    cfg = load_llm_config()
    f3_path, f3_lines = build_f3_sample(tmp)
    tools = build_tools(whitelist, lts)
    task = f"本次待精加工的对话记录在文件 `{WORKDIR_F3}` 中，请按你的工作流程读取并处理，完成后输出 @end。"
    messages = [
        {"role": "system", "content": body},
        {"role": "user", "content": task},
    ]
    from collections import Counter
    tool_counts = Counter()
    write_ops = 0
    ended = False
    final_text = ""
    for turn in range(1, max_turns + 1):
        try:
            params = build_request_params(cfg, messages, tools, temp)
            resp = litellm.completion(**params)
        except Exception as e:
            log(f"[turn {turn}] LLM 错误: {e}")
            break
        msg = resp.choices[0].message
        tcs = getattr(msg, "tool_calls", None) or []
        if not tcs:
            text = (msg.content or "").strip()
            final_text = text[:500]
            log(f"[turn {turn}] TEXT: {text[:300]}")
            if "@end" in text:
                ended = True
            break
        # 执行工具
        log(f"[turn {turn}] tool_calls: {len(tcs)}")
        for tc in tcs:
            fn = tc.function
            try:
                args = json.loads(fn.arguments or "{}")
            except Exception:
                args = {}
            name = fn.name
            tool_counts[name] += 1
            if name in ("lightrag_insert_entity", "lightrag_insert_relation",
                        "lightrag_edit_entity", "lightrag_edit_relation",
                        "lightrag_delete_entity", "lightrag_delete_relation",
                        "lightrag_merge_entities"):
                write_ops += 1
            log(f"    {summarize_tc(tc)}")
            result = execute_tool(name, args, lts, whitelist)
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [{"id": tc.id, "type": "function",
                                             "function": {"name": name, "arguments": fn.arguments}}]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        # 若一轮超多工具且纯查询无写，打印警示
        if turn >= 8 and write_ops == 0 and tool_counts["lightrag_search_entities"] > 10:
            log(f"[warn] 疑似空转：{turn} 轮仍零写操作，search {tool_counts['lightrag_search_entities']} 次")

    out = {
        "turns": turn,
        "max_turns": max_turns,
        "tool_counts": dict(tool_counts),
        "write_ops": write_ops,
        "ended_with_end": ended,
        "final_text": final_text,
    }
    log("=== 结果 ===")
    log(json.dumps(out, ensure_ascii=False, indent=2))
    with open(tmp / "result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-end", action="store_true", help="绿判定：有限轮 + 有写操作")
    ap.add_argument("--max-turns", type=int, default=25)
    args = ap.parse_args()

    cfg = load_llm_config()
    tmp = Path(tempfile.mkdtemp(prefix="niu_dream_harness_"))
    os.environ["NIU_STORAGE_DIR"] = str(tmp / "lightrag_storage")
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "mcp-servers" / "lightrag-server" / "src"))

    t0 = time.time()
    worker(str(tmp), args.max_turns)

    elapsed = time.time() - t0
    res = json.loads((tmp / "result.json").read_text(encoding="utf-8"))
    log(f"总耗时 {elapsed:.1f}s")
    if args.expect_end:
        ok = res["ended_with_end"] and res["turns"] <= args.max_turns - 1 and res["write_ops"] >= 1
        verdict = "GREEN" if ok else "RED"
        log(f"VERDICT: {verdict} — turns={res['turns']}, write_ops={res['write_ops']}, ended={res['ended_with_end']}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
