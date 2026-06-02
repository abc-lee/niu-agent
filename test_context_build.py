#!/usr/bin/env python3
"""
测试脚本：模拟构建 LLM 上下文的过程，验证工具调用消息是否正确排列。

核心问题：
- 数据库中 tool_calls 和 tool_results 是 assistant 消息的字段
- OpenAI API 要求工具调用和结果是独立的 message
- 构建上下文时必须正确拆分

验证点：
1. 工具调用和工具结果是否是独立的 message？
2. tool message 是否紧跟在对应的 assistant tool_calls 消息后面？
3. tool_call_id 的对应关系是否正确？
4. 对话的顺序是否正确？
"""

import json
import sqlite3
import os
import sys

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

DB_PATH = os.path.expanduser("~/.niu/messages.db")


def load_messages_from_db(limit=None):
    """从数据库加载消息（按 rowid 排序，返回时间正序）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if limit is not None:
        cursor = conn.execute(
            "SELECT id, role, content, tool_calls, tool_results, tool_call_id, created_at, rowid "
            "FROM messages ORDER BY rowid DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = conn.execute(
            "SELECT id, role, content, tool_calls, tool_results, tool_call_id, created_at, rowid "
            "FROM messages ORDER BY rowid DESC"
        )
    rows = cursor.fetchall()
    conn.close()

    # 反转为时间正序
    messages = []
    for row in reversed(rows):
        tool_calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
        tool_results = json.loads(row["tool_results"]) if row["tool_results"] else []
        messages.append({
            "rowid": row["rowid"],
            "id": row["id"],
            "role": row["role"],
            "content": row["content"] or "",
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "tool_call_id": row["tool_call_id"] or "",
            "created_at": row["created_at"],
        })
    return messages


def build_openai_messages(db_messages):
    """
    模拟 context_manager.load_history() + agent_runner_loop 的消息构建逻辑。

    步骤1: context_manager.load_history() 从 DB 读取消息，转换为 dict 列表
    步骤2: agent_runner_loop 接收 history，构建最终的 messages 列表

    关键：DB 中 assistant 消息可能携带 tool_calls 字段，
    但 tool 消息是独立的行（role='tool'，有 tool_call_id）。
    """
    # === 步骤1: 模拟 context_manager.load_history() ===
    history = []
    for msg in db_messages:
        entry = {"role": msg["role"], "content": msg["content"] or ""}

        # 还原 tool_calls（assistant 消息可能携带工具调用）
        if msg["tool_calls"]:
            entry["tool_calls"] = msg["tool_calls"]

        # 还原 tool_call_id（tool 消息必须关联到对应的 tool_call）
        if msg["tool_call_id"]:
            entry["tool_call_id"] = msg["tool_call_id"]

        # 完全空的消息可以跳过
        if not msg["content"] and not msg["tool_calls"] and not msg["tool_call_id"]:
            continue

        history.append(entry)

    # === 步骤2: 模拟 agent_runner_loop 的 history 处理 ===
    # 代码来自 agent_loop.py 第 137-150 行
    messages = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and (content or msg.get("tool_calls")):
            entry = {"role": role, "content": content}
            # 还原 tool_calls（assistant 消息可能携带工具调用）
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]
            messages.append(entry)
        elif role == "tool" and msg.get("tool_call_id") and content is not None:
            # tool 消息必须有 tool_call_id 和 content，否则 OpenAI API 返回 400
            entry = {"role": role, "content": content, "tool_call_id": msg["tool_call_id"]}
            messages.append(entry)

    return messages


def validate_openai_messages(messages):
    """
    验证 OpenAI API 消息格式的正确性。

    规则：
    1. assistant 消息如果有 tool_calls，则 tool_calls 必须是列表
    2. 每个 tool_call 必须有 id、type、function 字段
    3. tool 消息必须紧跟在对应的 assistant(tool_calls) 消息后面
    4. tool 消息的 tool_call_id 必须匹配前面 assistant 的某个 tool_call.id
    5. 每个 assistant tool_call.id 必须有对应的 tool 消息
    6. 消息顺序必须正确：user → assistant(tool_calls) → tool → assistant → ...
    """
    errors = []
    warnings = []

    # 收集所有 assistant tool_call ids 和它们的位置
    assistant_tool_call_ids = {}  # tool_call_id -> message index
    pending_tool_calls = set()    # 已声明但未找到对应 tool 消息的 id

    for i, msg in enumerate(messages):
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            # 验证 tool_calls 格式
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if not tc_id:
                    errors.append(f"[MSG {i}] assistant tool_call 缺少 id 字段: {json.dumps(tc, ensure_ascii=False)[:100]}")
                else:
                    assistant_tool_call_ids[tc_id] = i
                    pending_tool_calls.add(tc_id)

                if not tc.get("type"):
                    errors.append(f"[MSG {i}] assistant tool_call 缺少 type 字段: id={tc_id}")
                if not tc.get("function", {}).get("name"):
                    errors.append(f"[MSG {i}] assistant tool_call 缺少 function.name: id={tc_id}")

        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if not tc_id:
                errors.append(f"[MSG {i}] tool 消息缺少 tool_call_id")
            else:
                # 检查是否有对应的 assistant tool_call
                if tc_id not in assistant_tool_call_ids:
                    errors.append(f"[MSG {i}] tool 消息的 tool_call_id={tc_id} 没有对应的 assistant tool_call")
                else:
                    # 检查顺序：tool 消息必须在 assistant 之后
                    assistant_idx = assistant_tool_call_ids[tc_id]
                    if i <= assistant_idx:
                        errors.append(f"[MSG {i}] tool 消息在 assistant(tool_call) 之前: "
                                     f"tool at {i}, assistant at {assistant_idx}, call_id={tc_id}")

                    # 检查中间是否有其他 user 消息（这会导致 API 报错）
                    for j in range(assistant_idx + 1, i):
                        if messages[j].get("role") == "user":
                            errors.append(f"[MSG {i}] tool 消息和 assistant(tool_call) 之间有 user 消息: "
                                         f"assistant at {assistant_idx}, user at {j}, tool at {i}, call_id={tc_id}")

                    # 标记已匹配
                    pending_tool_calls.discard(tc_id)

        elif role == "assistant" and not msg.get("tool_calls"):
            # 纯文本 assistant 消息，检查前面是否有未匹配的 tool_calls
            if pending_tool_calls:
                warnings.append(f"[MSG {i}] 纯文本 assistant 消息前有未匹配的 tool_calls: {pending_tool_calls}")

    # 检查所有 tool_calls 是否都有对应的 tool 消息
    if pending_tool_calls:
        errors.append(f"以下 tool_call_id 没有对应的 tool 消息: {pending_tool_calls}")

    return errors, warnings


def print_message_summary(messages, db_messages):
    """打印消息摘要，高亮工具调用相关消息"""
    print("=" * 80)
    print("消息列表（OpenAI API 格式）")
    print("=" * 80)

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant" and msg.get("tool_calls"):
            # assistant + tool_calls 消息
            tc_names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
            tc_ids = [tc.get("id", "?")[:20] for tc in msg["tool_calls"]]
            content_preview = content[:50].replace("\n", " ") if content else "(empty)"
            print(f"  [{i:3d}] ASSISTANT + tool_calls  content=\"{content_preview}\"")
            for j, (name, tid) in enumerate(zip(tc_names, tc_ids)):
                print(f"        tool_call[{j}]: id={tid}... name={name}")

        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")[:20]
            content_preview = content[:60].replace("\n", " ") if content else "(empty)"
            print(f"  [{i:3d}] TOOL  tool_call_id={tc_id}...  content=\"{content_preview}\"")

        elif role == "user":
            content_preview = content[:60].replace("\n", " ") if content else "(empty)"
            print(f"  [{i:3d}] USER  content=\"{content_preview}\"")

        elif role == "assistant":
            content_preview = content[:60].replace("\n", " ") if content else "(empty)"
            print(f"  [{i:3d}] ASSISTANT  content=\"{content_preview}\"")

        else:
            print(f"  [{i:3d}] {role.upper()}  {json.dumps(msg, ensure_ascii=False)[:80]}")

    print()


def print_db_raw(db_messages):
    """打印数据库原始消息，用于对比"""
    print("=" * 80)
    print("数据库原始消息（按 rowid 正序）")
    print("=" * 80)

    for msg in db_messages:
        role = msg["role"]
        rowid = msg["rowid"]
        content = msg["content"][:50].replace("\n", " ") if msg["content"] else "(empty)"
        tc = msg["tool_calls"]
        tr = msg["tool_results"]
        tci = msg["tool_call_id"]

        extra = ""
        if tc:
            tc_names = [t.get("function", {}).get("name", "?") for t in tc]
            extra += f" tool_calls=[{','.join(tc_names)}]"
        if tr:
            extra += f" tool_results(len={len(tr)})"
        if tci:
            extra += f" tool_call_id={tci[:20]}..."

        print(f"  rowid={rowid:3d}  role={role:10s}  content=\"{content}\"{extra}")

    print()


def check_db_structure(db_messages):
    """检查数据库中的消息结构是否合理"""
    print("=" * 80)
    print("数据库结构检查")
    print("=" * 80)

    issues = []
    warnings = []

    for i, msg in enumerate(db_messages):
        role = msg["role"]
        tc = msg["tool_calls"]
        tr = msg["tool_results"]
        tci = msg["tool_call_id"]

        # 检查1: assistant 消息不应该有 tool_results（tool_results 应该在 tool 消息中）
        if role == "assistant" and tr:
            issues.append(f"  [rowid={msg['rowid']}] assistant 消息有 tool_results 字段（应该在 tool 消息中）")

        # 检查2: tool 消息不应该有 tool_calls
        if role == "tool" and tc:
            issues.append(f"  [rowid={msg['rowid']}] tool 消息有 tool_calls 字段（应该在 assistant 消息中）")

        # 检查3: tool 消息必须有 tool_call_id
        if role == "tool" and not tci:
            issues.append(f"  [rowid={msg['rowid']}] tool 消息缺少 tool_call_id")

        # 检查4: assistant(tool_calls) 后面应该紧跟 tool 消息
        if role == "assistant" and tc:
            # 检查下一条消息
            if i + 1 < len(db_messages):
                next_msg = db_messages[i + 1]
                if next_msg["role"] == "user":
                    # 严重问题：用户中断了工具调用流程，tool 结果缺失
                    tc_ids = [t.get("id", "?")[:20] for t in tc]
                    issues.append(f"  [rowid={msg['rowid']}] assistant(tool_calls) 后面是 user 消息，"
                                 f"tool 结果缺失！tool_call_ids={tc_ids}")
                elif next_msg["role"] == "assistant":
                    # 可能是 LLM 连续调用了工具但没有持久化 tool 结果
                    tc_ids = [t.get("id", "?")[:20] for t in tc]
                    warnings.append(f"  [rowid={msg['rowid']}] assistant(tool_calls) 后面是 assistant 消息，"
                                   f"可能 tool 结果未持久化。tool_call_ids={tc_ids}")
                elif next_msg["role"] != "tool":
                    issues.append(f"  [rowid={msg['rowid']}] assistant(tool_calls) 后面不是 tool 消息，"
                                 f"而是 {next_msg['role']} (rowid={next_msg['rowid']})")
            else:
                issues.append(f"  [rowid={msg['rowid']}] assistant(tool_calls) 是最后一条消息，缺少 tool 结果")

        # 检查5: tool 消息前面应该是 assistant(tool_calls)
        if role == "tool":
            if i > 0:
                prev_msg = db_messages[i - 1]
                if prev_msg["role"] == "assistant" and prev_msg["tool_calls"]:
                    # 检查 tool_call_id 是否匹配
                    prev_tc_ids = {tc_item.get("id", "") for tc_item in prev_msg["tool_calls"]}
                    if tci not in prev_tc_ids:
                        # 可能是前一条 assistant 有多个 tool_calls，tool 消息可能跟在另一个 tool 后面
                        # 向前搜索最近的 assistant(tool_calls)
                        found = False
                        for j in range(i - 1, -1, -1):
                            if db_messages[j]["role"] == "assistant" and db_messages[j]["tool_calls"]:
                                ids = {tc_item.get("id", "") for tc_item in db_messages[j]["tool_calls"]}
                                if tci in ids:
                                    found = True
                                    break
                                break  # 只检查最近的 assistant(tool_calls)
                        if not found:
                            issues.append(f"  [rowid={msg['rowid']}] tool 消息的 tool_call_id={tci[:20]}... "
                                         f"不匹配前一条 assistant(tool_calls) 的 id")
                elif prev_msg["role"] == "tool":
                    # 连续的 tool 消息，检查是否属于同一个 assistant
                    pass  # 这是正常的（一个 assistant 可以有多个 tool_calls）
                else:
                    issues.append(f"  [rowid={msg['rowid']}] tool 消息前面不是 assistant(tool_calls)，"
                                 f"而是 {prev_msg['role']} (rowid={prev_msg['rowid']})")
            else:
                issues.append(f"  [rowid={msg['rowid']}] tool 消息是第一条消息（缺少 assistant 上下文）")

    if issues:
        print("发现以下问题：")
        for issue in issues:
            print(issue)
    else:
        print("数据库结构检查通过，未发现问题。")

    if warnings:
        print("\n发现以下警告：")
        for warn in warnings:
            print(warn)

    print()
    return issues, warnings


def main():
    print("=" * 80)
    print("LLM 上下文构建测试 — 验证工具调用消息排列")
    print("=" * 80)
    print(f"数据库: {DB_PATH}")
    print()

    # 1. 从数据库加载全部消息
    db_messages = load_messages_from_db(limit=None)
    print(f"从数据库加载了 {len(db_messages)} 条消息（全量，时间正序）")
    print()

    # 2. 打印数据库原始消息
    print_db_raw(db_messages)

    # 3. 检查数据库结构
    db_issues, db_warnings = check_db_structure(db_messages)

    # 4. 模拟构建 OpenAI messages
    openai_messages = build_openai_messages(db_messages)

    # 5. 打印 OpenAI messages
    print_message_summary(openai_messages, db_messages)

    # 6. 验证 OpenAI messages 格式
    print("=" * 80)
    print("OpenAI API 格式验证")
    print("=" * 80)

    errors, warnings = validate_openai_messages(openai_messages)

    if errors:
        print(f"\n发现 {len(errors)} 个错误：")
        for err in errors:
            print(f"  ERROR: {err}")
    else:
        print("\n格式验证通过，未发现错误。")

    if warnings:
        print(f"\n发现 {len(warnings)} 个警告：")
        for warn in warnings:
            print(f"  WARN: {warn}")

    # 7. 统计信息
    print()
    print("=" * 80)
    print("统计信息")
    print("=" * 80)

    role_counts = {}
    tool_call_count = 0
    tool_msg_count = 0
    for msg in openai_messages:
        role = msg.get("role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
        if role == "assistant" and msg.get("tool_calls"):
            tool_call_count += 1
        if role == "tool":
            tool_msg_count += 1

    print(f"  总消息数: {len(openai_messages)}")
    for role, count in sorted(role_counts.items()):
        print(f"  {role}: {count}")
    print(f"  assistant(tool_calls) 消息: {tool_call_count}")
    print(f"  tool 消息: {tool_msg_count}")

    # 8. 检查 tool_calls 和 tool 消息的配对
    print()
    print("=" * 80)
    print("tool_calls / tool 配对检查")
    print("=" * 80)

    # 收集所有 tool_call ids 和 tool message 的 tool_call_ids
    all_tc_ids = []
    all_tool_msg_ids = []
    for msg in openai_messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                all_tc_ids.append(tc.get("id", ""))
        if msg.get("role") == "tool":
            all_tool_msg_ids.append(msg.get("tool_call_id", ""))

    print(f"  assistant tool_call ids: {len(all_tc_ids)}")
    print(f"  tool message tool_call_ids: {len(all_tool_msg_ids)}")

    # 检查是否一一对应
    tc_id_set = set(all_tc_ids)
    tool_id_set = set(all_tool_msg_ids)

    missing_tool_msgs = tc_id_set - tool_id_set
    orphan_tool_msgs = tool_id_set - tc_id_set

    if missing_tool_msgs:
        print(f"  ERROR: {len(missing_tool_msgs)} 个 tool_call 没有对应的 tool 消息:")
        for tid in missing_tool_msgs:
            print(f"    - {tid}")
    if orphan_tool_msgs:
        print(f"  ERROR: {len(orphan_tool_msgs)} 个 tool 消息没有对应的 tool_call:")
        for tid in orphan_tool_msgs:
            print(f"    - {tid}")
    if not missing_tool_msgs and not orphan_tool_msgs:
        print("  配对检查通过：所有 tool_calls 都有对应的 tool 消息，反之亦然。")

    # 9. 最终结论
    print()
    print("=" * 80)
    print("最终结论")
    print("=" * 80)

    all_problems = errors + warnings + db_issues + db_warnings
    if not all_problems and not missing_tool_msgs and not orphan_tool_msgs:
        print("PASS: 消息构建逻辑正确，工具调用消息排列符合 OpenAI API 要求。")
    else:
        print(f"FAIL: 发现 {len(all_problems)} 个问题，需要修复。")
        if db_issues:
            print(f"  - 数据库结构问题: {len(db_issues)}")
        if db_warnings:
            print(f"  - 数据库结构警告: {len(db_warnings)}")
        if errors:
            print(f"  - OpenAI 格式错误: {len(errors)}")
        if warnings:
            print(f"  - OpenAI 格式警告: {len(warnings)}")
        if missing_tool_msgs:
            print(f"  - 缺失 tool 消息: {len(missing_tool_msgs)}")
        if orphan_tool_msgs:
            print(f"  - 孤立 tool 消息: {len(orphan_tool_msgs)}")


if __name__ == "__main__":
    main()
