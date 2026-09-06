#!/usr/bin/env python3
"""
harness.py — entity-extractor「查重空转死循环」行为级复现 harness（TDD：红基线 / 绿验证）

背景（2026-09-06 raw_http 000094-000253 实证）：entity-extractor 用 glm-5.3-flash
跑 126 轮、6.4M prompt tokens，只产出 1 次 lightrag_insert；从 ~137 号请求起同一话题
的 5 组 search_entities query 轮转锁死（note摘要功能 / 教训白读 / 使用率压缩线 /
规则提示词搭车 / fold_tool_output），直到用户插话才 @end。根因=判断链无出口。

本 harness 不依赖 agent 框架（无 SSE/DB），直接模拟 agent_loop：
  - system = config/agents/entity-extractor.md 剥 frontmatter 后的 body（修复对象，只读）
  - user   = compat.py _call_entity_extractor_on_f1 同款 task 文本（路径换成隔离 F1）
  - tools  = md frontmatter mcpToolFilter 白名单 6 个 lightrag 工具（真实实现，跑在
             隔离图谱目录的子进程里）+ read stub（带行号分页，对齐真实 read 输出格式）
  - LLM    = litellm.completion 直调，参数组装照 agent/generic/litellm_adapter.py
             build_base_params/assemble_request_params 通道惯例（thinking/reasoning_effort
             经 extra_body，其余 litellm_kwargs 顶层 + drop_params）

用法:
  python3 harness.py                 # 跑一次当前提示词，打印每轮日志 + 结果 JSON（红基线用）
  python3 harness.py --expect-end    # 绿判定：ended_with_end 且 turns<=20 且 insert_count>=1 → exit 0
  python3 harness.py --max-turns 40  # 轮数上限（默认 25；样本不够锁死时加大）
  python3 harness.py --full-f1       # 用完整 720 行 F1（含程序化入库流程段），而非 5 组精选样本
  python3 harness.py --keep-incident-doc  # 不回滚事故文档（默认回滚=复现事故前图谱状态）

隔离铁律:
  - 所有 lightrag 读写只发生在 worker 子进程 + NIU_STORAGE_DIR 指向的临时目录；
    生产图谱 ~/.niu/lightrag_storage 仅作复制源（只读）
  - config/agents/entity-extractor.md 是修复对象——本文件只读不改

图谱状态回滚（默认开启）:
  事故文档 doc-f091...d4ad 的 insert 发生在事故中途（11:56），它创建的折叠家族实体
  让"现在的图谱"对旧提示词也显得可收敛——不复现。故每次跑前在隔离副本里级联删除
  该文档，把图谱恢复到事故开始（11:45）时的状态；红/绿两轮必须用同一图谱状态，
  唯一变量才是提示词。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# ============== 路径常量 ==============

REPO = Path("/Users/lilei/tools/ai-bot")
NIU_HOME = Path.home() / ".niu"
CONFIG_PATH = NIU_HOME / "config" / "user-config.json"
STORAGE_SRC = NIU_HOME / "lightrag_storage"
PROMPT_MD = REPO / "config" / "agents" / "entity-extractor.md"
LIGHTRAG_SRC = REPO / "mcp-servers" / "lightrag-server" / "src"

# 事故日 raw_http：entity-extractor 的 read 分页输出在 *request* 文件的 messages 里
# （response 文件只有 LLM 自己的 tool_calls 回复）。三个 read 页 = F1 全文 720 行。
RAW_HTTP_DIR = NIU_HOME / "logs" / "raw_http" / "20260906"
F1_PAGES = ((95, 3), (96, 5), (97, 7))  # (seq, tool role message index)

# md frontmatter mcpToolFilter 白名单（lightrag-server）
WHITELIST = [
    "lightrag_insert",
    "lightrag_search_entities",
    "lightrag_document_status",
    "lightrag_list_entities",
    "lightrag_get_graph",
    "lightrag_get_entity_info",
]

# 隔离临时目录（每次跑前重建；保留在 /tmp 便于事后检查）
TMP_BASE = Path("/tmp/niu_entity_harness")

# 事故唯一一次 insert 产生的文档（raw_http 000221，2026-09-06 11:56:25 本地创建）。
# 其 LightRAG 管道在 11:56:54-56 创建了 fold_tool_output/折叠规则/折叠工具使用教训/摘要功能
# 等 14 实体 + 27 关系——即事故前半程（11:45-11:56）搜索查无精确命中而空转的内容。
# 复现事故前图谱状态 = 在隔离副本里级联删除该文档（--keep-incident-doc 可关闭回滚）。
INCIDENT_DOC_ID = "doc-f09119cb6468aed6d74c5cee12b9d4ad"

# read stub 字符预算分页（对齐生产：事故 F1 720 行 → 页边界 301/504/720；B∈[28565,286xx] 均复现）
READ_PAGE_BUDGET = 28600
WORKER_TIMEOUT_S = 3600  # worker 子进程总超时（insert 会真触发 LightRAG 管道，可能慢）

# F1 精选样本：事故中锁死的话题全部来自文件尾段（L556-720）。
# 行号基于从 raw_http 恢复的 720 行全文（1 起）。
SAMPLE_RANGES = [
    (556, 602, "c: 智慧民生项目总结（assistant 转述图谱查询结果——图谱已覆盖话题，查重命中后应跳过）"),
    (603, 612, "b: [定时任务] 咖啡机提醒 + assistant 回复（例行/程序化消息，判断链第①步跳过）"),
    (613, 633, "a1: 用户批评「读出来就折叠=白读」+ assistant 教训归纳（事故 query 家族「教训/白读/重新调用」候选）"),
    (634, 650, "a2: 动态块使用率/强制压缩线消息 + assistant 确认新折叠规则（事故 query 家族「使用率/压缩线/提示词/搭车」候选）"),
    (651, 720, "a3: 不可再生数据讨论 + fold_tool_output note 参数功能请求 + 菜谱例子（主锁死候选：「note/摘要功能」query 家族）"),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ============== 配置装载 ==============

def load_llm_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["llm"]
    for key in ("apiKey", "apiBase", "model"):
        if not cfg.get(key):
            raise SystemExit(f"user-config.json llm.{key} 为空")
    return cfg


def derive_provider_prefix(api_base: str | None, model: str, api_type: str | None = None) -> str:
    """照 litellm_adapter.py _derive_provider_prefix（L546）：volces.com → volcengine/ 路由。"""
    base = (api_base or "").lower()
    if "volces.com" in base:
        return f"volcengine/{model}"
    if "api.anthropic.com" in base:
        return f"anthropic/{model}"
    if api_type == "anthropic":
        return f"anthropic/{model}"
    return f"openai/{model}"


def build_request_params(cfg: dict, messages: list, tools: list, temperature: float) -> dict:
    """litellm.completion 参数组装——照 litellm_adapter.py chat()（L999-1046）+
    build_base_params/assemble_request_params 的通道惯例，非流式版：
      - thinking / reasoning_effort → extra_body（litellm 白名单碰不到的唯一可靠通道）
      - 其余 litellm_kwargs（除 thinking/sticky_session_headers）→ 顶层 update + drop_params=True
    """
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


# ============== 提示词装载（只读）==============

def load_prompt() -> tuple[str, float]:
    """entity-extractor.md → (剥 frontmatter 后的 system body, temperature)。"""
    text = PROMPT_MD.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit("entity-extractor.md 未找到 frontmatter")
    fm, body = m.group(1), text[m.end():]
    tm = re.search(r"^temperature:\s*([0-9.]+)", fm, re.M)
    return body.strip(), (float(tm.group(1)) if tm else 0.3)


# ============== F1 样本恢复（raw_http → 隔离 tmp）==============

def reconstruct_f1_full() -> list[str]:
    """从事故日 raw_http 的 read 分页输出恢复 F1_extract_source.md 全文（720 行）。

    read 输出格式：首行 [FILE] Showing N lines from line X (total T lines)，
    随后每行 {lineno}|{content}，截断页尾带 [Truncated at line N. Use offset=N+1 ...]。
    """
    pages: list[tuple[int, str]] = []
    for seq, idx in F1_PAGES:
        path = RAW_HTTP_DIR / f"{seq:06d}_request.json"
        if not path.is_file():
            raise SystemExit(f"raw_http 文件缺失: {path}")
        d = json.loads(path.read_text(encoding="utf-8"))
        content = d["messages"][idx]["content"]
        lines = content.split("\n")
        if not lines[0].startswith("[FILE] Showing"):
            raise SystemExit(f"{path.name} msg[{idx}] 不是 read 输出: {lines[0][:80]!r}")
        for ln in lines[1:]:
            if ln.startswith("[Truncated at line"):
                continue
            m = re.match(r"^(\d+)\|(.*)$", ln)
            if not m:
                raise SystemExit(f"{path.name} 无法解析的行: {ln[:80]!r}")
            pages.append((int(m.group(1)), m.group(2)))
    pages.sort(key=lambda t: t[0])
    nums = [n for n, _ in pages]
    if nums != list(range(1, len(nums) + 1)):
        raise SystemExit(f"F1 行号不连续（{len(pages)} 行，期望 1..720）")
    return [p for _, p in pages]


def build_f1_sample(tmp: Path, full: bool = False) -> tuple[Path, int, list[str]]:
    """写 F1 到 <tmp>/F1_extract_source.md，返回 (路径, 行数, 组标签)。"""
    full_lines = reconstruct_f1_full()
    if full:
        selected, labels = full_lines, ["完整 720 行（含程序化入库流程段）"]
    else:
        # 锚点校验：行号若因日志重放漂移，宁可失败也不静默取错内容
        assert full_lines[555].startswith('{"msg_id"'), "L556 不是元数据行（样本锚点漂移）"
        assert "[定时任务]" in full_lines[603], "L604 不是咖啡机提醒（样本锚点漂移）"
        assert "折叠" in full_lines[613], "L614 不是「读出来就折叠」批评（样本锚点漂移）"
        assert "fold_tool_output" in "".join(full_lines[665:720]), "L666-720 缺 note 功能请求段（样本锚点漂移）"
        selected = []
        for start, end, _ in SAMPLE_RANGES:
            selected.extend(full_lines[start - 1:end])
        labels = [label for _, _, label in SAMPLE_RANGES]
    path = tmp / "F1_extract_source.md"
    path.write_text("\n".join(selected), encoding="utf-8")
    return path, len(selected), labels


# ============== read stub（worker 用）==============

# read schema 与生产逐字一致（raw_http 20260906/000094_request.json tools[read]，865 chars）
READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read",
        "description": (
            "Reads a file from the local filesystem. You can access any file directly by using this tool. "
            "By default, it reads up to 500 lines starting from the beginning of the file. You can optionally "
            "specify a line offset and limit (especially handy for long files). Results are returned with line "
            "numbers starting at 1. A negative offset anchors at the end of the file and reads the last "
            "min(|offset|, limit) lines (e.g. offset=-50 reads the last 50 lines; offset=-50 with limit=10 reads "
            "the last 10 lines). Pages are capped by a character budget, so a page may contain fewer lines than "
            "requested. Lines are kept whole whenever possible; only a single line exceeding the page budget is "
            "cut mid-line and marked [TRUNCATED] (a tail page keeps the end of that line). If output ends with a "
            "truncation marker, continue with the indicated offset/limit to read the remaining lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file to read"},
                "offset": {"type": "integer", "default": 1,
                           "description": "The line number to start reading from (1-based). A negative value "
                                          "anchors at the end of the file: it reads the last min(|offset|, limit) "
                                          "lines (e.g. offset=-50 = last 50 lines; with limit=10 = last 10 lines). "
                                          "Only provide if the file is too large to read at once."},
                "limit": {"type": "integer", "default": 500,
                          "description": "The number of lines to read. With a negative offset it shrinks the tail "
                                         "window from the older end (offset=-50 limit=10 = last 10 lines). Only "
                                         "provide if the file is too large to read at once. Maximum 500."},
            },
            "required": ["file_path"],
        },
    },
}


def read_stub(file_path: str, offset: int = 1, limit: int = 500) -> str:
    """对齐真实 read：[FILE] 头 + {lineno}|{content}（行号 1 起）+ 截断标记。

    分页语义与生产一致（schema 逐字复刻自 raw_http 000094）：
    - 正 offset：从该行起读，最多 limit 行且受字符预算 READ_PAGE_BUDGET 限制
      （事故 F1 720 行 → 页边界 301/504/720，B=28600 复现该分页）；
    - 负 offset：锚定文件尾，读最后 min(|offset|, limit) 行；
    - limit 上限 500。
    """
    p = os.path.expanduser(file_path)
    if not os.path.isfile(p):
        return f"[ERROR] 文件不存在: {file_path}"
    lines = Path(p).read_text(encoding="utf-8").split("\n")
    total = len(lines)
    offset = int(offset or 1)
    limit = max(1, min(int(limit or 500), 500))
    if offset < 0:
        window = min(abs(offset), limit)
        start = max(1, total - window + 1)
        end = total
    else:
        start = max(1, offset)
        # 字符预算分页：整行保留，加下一行会超预算即停（至少含 1 行）
        n = 0
        acc = 0
        for i in range(start, min(total, start + limit - 1) + 1):
            cost = len(f"{i}|{lines[i - 1]}")
            if n > 0 and acc + cost > READ_PAGE_BUDGET:
                break
            acc += cost
            n += 1
        end = start + n - 1
    page = lines[start - 1: end]
    out = [f"[FILE] Showing {len(page)} lines from line {start} (total {total} lines)"]
    out += [f"{i}|{ln}" for i, ln in enumerate(page, start=start)]
    if end < total:
        out.append(f"[Truncated at line {end}. Use offset={end + 1} to read more.]")
    return "\n".join(out)


# ============== worker（子进程：模拟 agent_loop）==============

def build_tools(lts) -> list:
    """白名单 6 个 lightrag schema（TOOL_SCHEMAS 是 input_schema 形态 → 转 OpenAI parameters）+ read stub。"""
    tools = []
    for name in WHITELIST:
        s = lts.TOOL_SCHEMAS[name]
        tools.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("input_schema") or s.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    tools.append(READ_SCHEMA)
    return tools


def execute_tool(name: str, args: dict, lts) -> str:
    if name == "read":
        try:
            return read_stub(str(args.get("file_path", "")),
                             int(args.get("offset") or 1), int(args.get("limit") or 500))
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
    if name in WHITELIST:
        try:
            result = lts.call_tool(name, args)  # call_tool 按函数签名过滤未知参数（同生产 ToolRegistry）
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
    if len(s) > 80:
        s = s[:77] + "..."
    return f"{tc.function.name}({s})"


def worker(f1_path: str, max_turns: int) -> None:
    # NIU_STORAGE_DIR 必须在 import lightrag 模块前就位（lightrag_manager.py L65-66 import 时读）。
    # 父进程已通过 subprocess env 传入；这里再断言一次双保险。
    if not os.environ.get("NIU_STORAGE_DIR", "").strip():
        raise SystemExit("worker: NIU_STORAGE_DIR 未设置（隔离图谱目录）")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    sys.path.insert(0, str(LIGHTRAG_SRC))
    sys.path.insert(0, str(REPO))

    import litellm
    import niu_lightrag_server as lts  # noqa: E402  (env 就位后才可 import)

    cfg = load_llm_config()
    body, temperature = load_prompt()
    tools = build_tools(lts)
    task = (f"本次待提炼内容在文件 `{f1_path}` 中。请按你的输入规范用 read 工具分段读取并提炼入库，"
            f"完成后输出 @end 和 processed_line 行号。")
    messages = [
        {"role": "system", "content": body},
        {"role": "user", "content": task},
    ]

    log(f"[worker] system={len(body)} chars, temperature={temperature}, "
        f"model={cfg['model']} (route {derive_provider_prefix(cfg.get('apiBase'), cfg['model'], cfg.get('type'))})")
    log(f"[worker] 隔离图谱: {os.environ['NIU_STORAGE_DIR']}")

    tool_counts: Counter = Counter()
    insert_count = 0
    ended_with_end = False
    processed_line = None
    final_text = ""
    error = None
    turns = 0
    t_start = time.time()

    for turn in range(1, max_turns + 1):
        params = build_request_params(cfg, messages, tools, temperature)
        resp = None
        for attempt in range(3):
            try:
                resp = litellm.completion(**params)
                break
            except Exception as e:
                log(f"[turn {turn}] LLM 调用失败（第 {attempt + 1} 次）: {e}")
                if attempt == 2:
                    error = f"LLM 调用连续失败: {e}"
                else:
                    time.sleep(5)
        if resp is None:
            break
        turns = turn

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            log(f"[turn {turn}] TOOL " + "; ".join(summarize_tc(tc) for tc in tool_calls))
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = execute_tool(name, args, lts)
                tool_counts[name] += 1
                if name == "lightrag_insert":
                    insert_count += 1
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            # 无工具调用的纯文本 = agent_loop 终止轮：@end+processed_line 才算成功退出
            final_text = msg.content or ""
            log(f"[turn {turn}] TEXT: {final_text[:200].strip()!r}")
            ended_with_end = ("@end" in final_text) and bool(re.search(r"processed_line=\d+", final_text))
            m = re.search(r"processed_line=(\d+)", final_text)
            if m:
                processed_line = int(m.group(1))
            break

    result = {
        "turns": turns,
        "max_turns": max_turns,
        "tool_counts": dict(tool_counts),
        "search_count": tool_counts.get("lightrag_search_entities", 0),
        "insert_count": insert_count,
        "ended_with_end": ended_with_end,
        "processed_line": processed_line,
        "final_text": final_text[:500],
        "elapsed_s": round(time.time() - t_start, 1),
        "error": error,
    }
    # 结果 JSON 独占 stdout（进度全走 stderr）——main 取最后一行非空行解析
    print(json.dumps(result, ensure_ascii=False), flush=True)


def rollback_worker() -> None:
    """子进程：在隔离副本里级联删除事故文档，恢复事故前图谱状态。结果 JSON 走 stdout。"""
    if not os.environ.get("NIU_STORAGE_DIR", "").strip():
        raise SystemExit("rollback worker: NIU_STORAGE_DIR 未设置")
    sys.path.insert(0, str(LIGHTRAG_SRC))
    sys.path.insert(0, str(REPO))

    import niu_lightrag_server as lts  # noqa: E402  (env 就位后才可 import)

    del_result = lts.call_tool("lightrag_delete_document", {"doc_id": INCIDENT_DOC_ID})
    status = del_result.get("status") if isinstance(del_result, dict) else str(del_result)
    # 删除后验证：折叠家族实体应查无精确命中（只剩 09-02 折叠功能测试的弱相关实体）
    verify = lts.call_tool(
        "lightrag_search_entities",
        {"query": "fold_tool_output 折叠规则 摘要功能 note参数", "keywords": ["fold_tool_output", "折叠规则"], "top_k": 8},
    )
    # lightrag_search_entities 返回 {"status": "ok", "data": [entities]}（或 no_results）
    names = []
    if isinstance(verify, dict):
        for e in verify.get("data", []) or []:
            n = e.get("entity_name") if isinstance(e, dict) else str(e)
            if n:
                names.append(n)
    gone = not any(t in n for n in names for t in ("fold_tool_output", "折叠规则", "摘要功能"))
    log(f"[rollback] delete status={status}; 验证搜索命中 {len(names)} 实体: {names}")
    print(json.dumps({
        "deleted": status == "success" or status == "ok",
        "delete_result": del_result if isinstance(del_result, dict) else str(del_result),
        "verify_entities": names,
        "fold_entities_gone": gone,
    }, ensure_ascii=False, default=str), flush=True)


# ============== main（编排）==============

def prepare_isolated_storage(tmp: Path) -> int:
    """rm -rf + mkdir + 从生产图谱复制全部文件（生产目录只读，仅作复制源）。"""
    if tmp.exists():
        shutil.rmtree(tmp)
    dst = tmp / "lightrag_storage"
    dst.mkdir(parents=True)
    n = 0
    for f in sorted(STORAGE_SRC.iterdir()):
        if f.is_file():
            shutil.copy2(f, dst / f.name)
            n += 1
    return n


def run_worker(f1_path: str, max_turns: int, tmp: Path) -> dict:
    env = dict(os.environ)
    env["NIU_STORAGE_DIR"] = str(tmp / "lightrag_storage")
    pp = [str(REPO), str(LIGHTRAG_SRC)]
    if os.environ.get("PYTHONPATH"):
        pp.append(os.environ["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pp)

    cmd = [sys.executable, str(Path(__file__).resolve()), "--_worker", f1_path, str(max_turns)]
    log(f"[main] worker: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=None,  # stderr 直通显示每轮进度
        text=True, bufsize=1,
    )
    try:
        out, _ = proc.communicate(timeout=WORKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return {"error": f"worker 超时 {WORKER_TIMEOUT_S}s（可能卡在 LightRAG 管道/LLM）"}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return {"error": f"worker 无 stdout（exit={proc.returncode}）"}
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as e:
        return {"error": f"worker stdout 末行不是 JSON: {e}: {lines[-1][:200]}"}
    if proc.returncode != 0:
        result.setdefault("worker_exit", proc.returncode)
    return result


def run_rollback(storage_dir: Path) -> dict:
    """子进程回滚隔离副本到事故前图谱状态（级联删除事故文档 + 验证折叠家族实体已消失）。"""
    env = dict(os.environ)
    env["NIU_STORAGE_DIR"] = str(storage_dir)
    pp = [str(REPO), str(LIGHTRAG_SRC)]
    if os.environ.get("PYTHONPATH"):
        pp.append(os.environ["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pp)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_rollback"],
        env=env, stdout=subprocess.PIPE, text=True, timeout=600,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return {"error": f"rollback worker 无 stdout（exit={proc.returncode}）"}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as e:
        return {"error": f"rollback stdout 末行不是 JSON: {e}: {lines[-1][:200]}"}


def main() -> None:
    ap = argparse.ArgumentParser(description="entity-extractor 查重空转死循环复现 harness")
    ap.add_argument("--expect-end", action="store_true",
                    help="绿判定：ended_with_end 且 turns<=20 且 insert_count>=1 → exit 0，否则 exit 1")
    ap.add_argument("--max-turns", type=int, default=25, help="轮数上限（默认 25）")
    ap.add_argument("--full-f1", action="store_true", help="用完整 720 行 F1 而非 5 组精选样本")
    ap.add_argument("--keep-incident-doc", action="store_true",
                    help="不回滚事故文档（默认回滚=复现事故前图谱状态）")
    ap.add_argument("--tmp-base", type=str, default=str(TMP_BASE),
                    help=f"隔离临时目录基址（默认 {TMP_BASE}；并行跑时用不同值避免互踩）")
    ap.add_argument("--_worker", nargs=2, metavar=("F1_PATH", "MAX_TURNS"), help=argparse.SUPPRESS)
    ap.add_argument("--_rollback", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._rollback:
        rollback_worker()
        return
    if args._worker:
        worker(args._worker[0], int(args._worker[1]))
        return

    t0 = time.time()
    cfg = load_llm_config()
    log("=== entity-extractor harness ===")
    log(f"LLM: {cfg['model']} @ {cfg['apiBase']} (reasoning_effort={cfg.get('reasoning_effort')}, "
        f"litellm_kwargs={json.dumps(cfg.get('litellm_kwargs'), ensure_ascii=False)})")

    tmp_base = Path(args.tmp_base)
    n_files = prepare_isolated_storage(tmp_base)
    log(f"隔离图谱: {tmp_base / 'lightrag_storage'}（复制 {n_files} 个文件自 {STORAGE_SRC}）")

    if args.keep_incident_doc:
        graph_state = "current（含事故文档，未回滚）"
        log("图谱状态: current —— --keep-incident-doc，跳过事故文档回滚")
    else:
        rb = run_rollback(tmp_base / "lightrag_storage")
        if rb.get("error"):
            log(f"VERDICT: ERROR — 事故文档回滚失败: {rb['error']}")
            sys.exit(3)
        graph_state = ("pre-incident（已级联删除事故文档，折叠家族实体已移除）"
                       if rb.get("fold_entities_gone") else
                       f"current?（回滚后验证未确认折叠实体消失: {rb.get('verify_entities')}）")
        log(f"图谱状态: pre-incident —— 已级联删除 {INCIDENT_DOC_ID}；"
            f"验证搜索命中 {len(rb.get('verify_entities', []))} 个弱相关实体（精确命中应已消失）")

    f1_path, f1_lines, labels = build_f1_sample(tmp_base, full=args.full_f1)
    log(f"F1 样本: {f1_path}（{f1_lines} 行，自 raw_http 20260906 恢复）")
    for lab in labels:
        log(f"  - {lab}")

    body, temperature = load_prompt()
    log(f"提示词: {PROMPT_MD}（body {len(body)} chars, temperature={temperature}，只读不改）")
    log(f"MAX_TURNS={args.max_turns}")
    log("--- 每轮进度见上方 stderr ---")

    result = run_worker(str(f1_path), args.max_turns, tmp_base)
    result["graph_state"] = graph_state
    log("=== 结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    log(f"总耗时 {round(time.time() - t0, 1)}s（含图谱复制与 worker）")

    if result.get("error"):
        log(f"VERDICT: ERROR — {result['error']}")
        sys.exit(3)

    if args.expect_end:
        reasons = []
        if not result["ended_with_end"]:
            reasons.append(f"未以 @end+processed_line 结束（turns={result['turns']}/{args.max_turns}，"
                           f"search_count={result['search_count']}）")
        if result["turns"] > 20:
            reasons.append(f"轮数 {result['turns']} > 20")
        if result["insert_count"] < 1:
            reasons.append(f"insert_count={result['insert_count']} < 1（样本含新事实，应至少入库 1 次）")
        if reasons:
            log("VERDICT: RED — " + "；".join(reasons))
            sys.exit(1)
        log(f"VERDICT: GREEN — turns={result['turns']}, inserts={result['insert_count']}, "
            f"processed_line={result['processed_line']}")
        sys.exit(0)

    # 默认模式：只跑并打印结果（红基线用），恒 exit 0
    spin = result["search_count"] >= 8 and not result["ended_with_end"]
    log(f"VERDICT: 空转特征{'成立' if spin else '未显现'}"
        f"（search={result['search_count']}, turns={result['turns']}/{args.max_turns}, "
        f"@end={result['ended_with_end']}）")


if __name__ == "__main__":
    main()
