# Plan: Protect-Range Refactor — User-Turn-Aware Protection (v2)

## Reference
- Design: `docs/superpowers/specs/2026-08-01-protect-range-refactor-design.md`
- Problem: Current `protect_recent_count=10` counts from tail, can cut mid-turn. New approach: after counting N, scan upward to nearest `role=user` message, protect from there.

## R2 Review Fixes Applied
- **P1-1**: Task 9b force path L3408 now uses in-scope `protect_recent_count` (may be degraded) instead of `_read_protect_recent_count()` (undegraded)
- **P1-2**: Task 9 _emergency_clear guard now checks `_protect_start >= len(msg_ids)` to prevent IndexError when protect_recent_count=0
- **P1-3**: Task 8 runner.py emergency clear guard now checks `_protect_start >= len(_force_msg_ids)` for same reason
- **P2-1**: Test case `test_idx_n_is_assistant_finds_user_above` now actually tests idx_N=assistant (min=3 instead of min=2)
- **P3-1**: Design doc Cursor Adjustment section removed (contradicted Key Invariant #4)
- **P3-2**: Design doc code point count updated from 8 to 11

## R1 Review Fixes Applied
- **P0-1**: Fixed algorithm Step 2 — now correctly scans upward past assistant messages to find nearest user
- **P0-2**: Added Task 6b (runner.py L1206) and Task 7b (runner.py L1523) — 2 previously missing code points
- **P0-3**: Fixed Task 8 variable name `messages` → `db_messages`
- **P1-1**: Task 9 now has complete replacement code
- **P2-1**: Added Task 9b — fix hardcoded `protect_recent_count=10` at L2737 and L3408 call sites
- **P2-2**: Fixed test case `test_no_user_above_idx_n` description
- **P3-1**: Design doc cursor claim corrected (no code change needed)

## Tasks

### Task 0: Backup + test scaffolding
- `git add -A && git commit`
- Create `tests/test_protect_range.py` with red-phase tests for `_find_protected_range`

### Task 1: Implement `_find_protected_range` in compat.py
**File:** `niu_api/compat.py`

Add function near L310 (before `_build_incremental_msg_text`):

```python
def _find_protected_range(messages, min_protect_count: int) -> int:
    """Find the protection start index using user-turn-aware logic.

    1. From tail, count min_protect_count user/assistant messages -> idx_N
    2. From idx_N, scan upward (toward index 0) for the nearest role=user message:
       - First, skip any non-user messages (assistant/tool) going upward from idx_N
       - When a user message is found, keep scanning upward for consecutive user messages
       - Protect to the earliest user in the consecutive group
    3. Return idx_user (or idx_N if no user found above, or len(messages) if min_protect_count=0)

    All messages[index >= return_value] should be protected, including tool messages.
    """
    if min_protect_count <= 0 or not messages:
        return len(messages)  # no protection

    total = len(messages)

    # Step 1: from tail, count N user/assistant messages
    idx_N = total  # exclusive upper bound (one past last counted)
    count = 0
    for i in range(total - 1, -1, -1):
        role = getattr(messages[i], "role", "") if hasattr(messages[i], "role") else messages[i].get("role", "")
        if role in ("user", "assistant"):
            count += 1
            if count >= min_protect_count:
                idx_N = i
                break
    else:
        # Fewer than N user/assistant messages -> protect everything
        return 0

    # Step 2: from idx_N, scan upward (toward index 0) for nearest role=user message
    # Phase A: skip non-user messages (assistant, tool) going upward
    # Phase B: once user found, keep going up for consecutive user messages
    idx_user = idx_N
    found_user = False
    for i in range(idx_N, -1, -1):
        role = getattr(messages[i], "role", "") if hasattr(messages[i], "role") else messages[i].get("role", "")
        if role == "user":
            idx_user = i
            found_user = True
            # keep scanning upward for consecutive user messages
        elif found_user:
            # we found user(s) but hit a non-user -> stop
            break
        # if not found_user and role != "user", continue scanning (skip assistant/tool)
    
    if not found_user:
        # No user message found at or above idx_N -> fall back to idx_N
        return idx_N

    return idx_user
```

**Key fix vs v1:** Step 2 now has two phases — Phase A skips non-user messages (assistant/tool) when scanning upward from idx_N, and Phase B handles consecutive user messages once the first user is found. In v1, the loop immediately broke on any non-user message at idx_N, which meant it never found a user when idx_N was assistant (the common case).

**Tests:** `tests/test_protect_range.py`
- `test_no_protection_when_count_zero` — min_protect_count=0 returns len(messages)
- `test_basic_user_turn_protection` — [user, assistant, user, assistant, user, assistant], min=3, idx_N=3(assistant), scan up finds user at idx 2, protect from 2
- `test_consecutive_user_messages` — [user, user, assistant, user, assistant], min=2, idx_N=3(user), scan up: idx 3=user, idx 2=assistant(stop), protect from 3
- `test_consecutive_user_at_idx_n` — [assistant, user, user, assistant, user, assistant], min=2, idx_N=4(user), scan up: idx 4=user, idx 3=assistant(stop), protect from 4. Then check idx 2-1 consecutive users are NOT included (they're separated by assistant at idx 3)
- `test_idx_n_is_assistant_finds_user_above` — [user, assistant, user, assistant, user, assistant], min=3, idx_N=3(assistant), Phase A skips assistant at idx 3, finds user at idx 2, protect from 2. This is the critical test for the P0 algorithm bug.
- `test_fewer_than_n` — only 5 messages, min=10 → return 0 (protect all)
- `test_no_user_found_above` — [assistant, tool, assistant, tool, assistant], min=3, idx_N=0(assistant), scan up finds no user → return idx_N=0
- `test_tool_messages_included` — tool messages between protected user/assistant are in range
- `test_single_message` — edge case
- `test_empty_messages` — edge case

### Task 2: Apply to `_build_incremental_msg_text` (compat.py L362-372)
**File:** `niu_api/compat.py`

Replace L362-372:
```python
    _protected_positions = None
    if protect_recent > 0:
        _protected_positions = set()
        _count = 0
        for rp in range(total_count - 1, -1, -1):
            _, m = range_messages_with_pos[rp]
            if getattr(m, "role", "") in ("user", "assistant"):
                _protected_positions.add(rp)
                _count += 1
                if _count >= protect_recent:
                    break
```

With:
```python
    _protected_positions = None
    if protect_recent > 0:
        # User-turn-aware protection: protect from nearest user message at/below the N-th user/assistant from tail
        _range_msgs = [m for _, m in range_messages_with_pos]
        _protect_start = _find_protected_range(_range_msgs, protect_recent)
        _protected_positions = set(range(_protect_start, total_count))
```

### Task 3: Apply to `_build_compress_history` (compat.py L429-438)
**File:** `niu_api/compat.py`

Replace L429-438:
```python
    _protected_positions: set[int] = set()
    if protect_recent > 0:
        _count = 0
        for rp in range(total_count - 1, -1, -1):
            m = messages[rp]
            if getattr(m, "role", "") in ("user", "assistant"):
                _protected_positions.add(rp)
                _count += 1
                if _count >= protect_recent:
                    break
```

With:
```python
    _protected_positions: set[int] = set()
    if protect_recent > 0:
        _protect_start = _find_protected_range(messages, protect_recent)
        _protected_positions = set(range(_protect_start, total_count))
```

### Task 4: Apply to sleep path protected_ids (compat.py L2672-2679)
**File:** `niu_api/compat.py`

Replace L2672-2679:
```python
                _pids = []
                for i in range(len(messages) - 1, -1, -1):
                    _m = messages[i]
                    if getattr(_m, "role", "") in ("user", "assistant"):
                        _pids.insert(0, getattr(_m, "id", "") or "")
                    if len(_pids) >= protect_recent_count:
                        break
                protected_ids = _pids  # No fallback: tool output is never protected
```

With:
```python
                _protect_start = _find_protected_range(messages, protect_recent_count)
                # Protect all messages (including tool) from protect_start to end
                protected_ids = [getattr(messages[i], "id", "") or "" for i in range(_protect_start, len(messages))]
```

**Note:** This changes protected_ids from user/assistant-only to all-messages-in-range. This is correct — tool messages between protected user/assistant must also be protected to avoid orphaned tool outputs.

### Task 5: Apply to sleep path protected_set (compat.py L2849-2857)
**File:** `niu_api/compat.py`

Replace L2849-2857:
```python
                            _pids = []
                            for m in reversed(fresh_messages):
                                if getattr(m, "role", "") in ("user", "assistant"):
                                    _pids.append(getattr(m, "id", ""))
                                if len(_pids) >= protect_recent_count:
                                    break
                            protected_set = set(_pids)
```

With:
```python
                            _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                            protected_set = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
```

### Task 6: Apply to compat.py force path _force_protected_ids (compat.py L3086-3090)
**File:** `niu_api/compat.py`

Replace L3086-3090:
```python
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and messages:
                _ua_msgs = [m for m in messages if getattr(m, "role", "") in ("user", "assistant")]
                _force_protected_ids = {getattr(m, "id", "") or "" for m in _ua_msgs[-_force_protect_recent_count:]}
```

With:
```python
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and messages:
                _protect_start = _find_protected_range(messages, _force_protect_recent_count)
                _force_protected_ids = {getattr(messages[i], "id", "") or "" for i in range(_protect_start, len(messages))}
```

### Task 6b: Apply to runner.py force path _force_protected_ids (runner.py L1206-1210)
**File:** `agent/runner.py`

Replace L1206-1210:
```python
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and db_messages:
                _ua_msgs = [m for m in db_messages if getattr(m, "role", "") in ("user", "assistant")]
                _force_protected_ids = {getattr(m, "id", "") or "" for m in _ua_msgs[-_force_protect_recent_count:]}
```

With:
```python
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and db_messages:
                from niu_api.compat import _find_protected_range
                _protect_start = _find_protected_range(db_messages, _force_protect_recent_count)
                _force_protected_ids = {getattr(db_messages[i], "id", "") or "" for i in range(_protect_start, len(db_messages))}
```

### Task 7: Apply to compat.py force path protected_force_ids (compat.py L3555-3563)
**File:** `niu_api/compat.py`

Replace L3555-3563:
```python
                    protected_force_ids: set[str] = set()
                    if protect_recent_count > 0:
                        _pids = []
                        for m in reversed(fresh_messages):
                            if getattr(m, "role", "") in ("user", "assistant"):
                                _pids.append(getattr(m, "id", ""))
                            if len(_pids) >= protect_recent_count:
                                break
                        protected_force_ids = set(_pids)
```

With:
```python
                    protected_force_ids: set[str] = set()
                    if protect_recent_count > 0:
                        _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                        protected_force_ids = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
```

### Task 7b: Apply to runner.py force path protected_force_ids (runner.py L1523-1533)
**File:** `agent/runner.py`

Replace L1523-1533:
```python
                # 保护最近 N 条 user/assistant 消息
                protect_recent_count = _read_protect_recent_count()
                protected_force_ids: set[str] = set()
                if protect_recent_count > 0:
                    _pids = []
                    for m in reversed(fresh_messages):
                        if getattr(m, "role", "") in ("user", "assistant"):
                            _pids.append(getattr(m, "id", ""))
                        if len(_pids) >= protect_recent_count:
                            break
                    protected_force_ids = set(_pids)
```

With:
```python
                # 保护最近完整用户会话段落（从最近 user 消息开始）
                protect_recent_count = _read_protect_recent_count()
                protected_force_ids: set[str] = set()
                if protect_recent_count > 0:
                    from niu_api.compat import _find_protected_range
                    _protect_start = _find_protected_range(fresh_messages, protect_recent_count)
                    protected_force_ids = {getattr(fresh_messages[i], "id", "") or "" for i in range(_protect_start, len(fresh_messages))}
```

### Task 8: Apply to runner.py emergency clear (L1398-1407)
**File:** `agent/runner.py`

Replace L1398-1407:
```python
            # 截断时触发内联应急清空（保留最近 10 条，上面全删，最旧改"压缩失败"摘要）
            # 同步实现：用 self._sync_delete_messages / self._sync_update_message，不调 async _emergency_clear
            if result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] runner.py force output truncated, triggering emergency clear")
                if len(_force_msg_ids) <= 10:
                    logger.warning(f"[Compact] Runner history len {len(_force_msg_ids)} <= 10, no clear needed")
                    return {"status": "skipped", "mode": "force", "reason": "truncated, no clear needed (too few)"}

                delete_ids = _force_msg_ids[:-10]
                oldest_kept_id = _force_msg_ids[-10]
```

With:
```python
            # 截断时触发内联应急清空（保留最近完整用户会话段落，上面全删，最旧改"压缩失败"摘要）
            # 同步实现：用 self._sync_delete_messages / self._sync_update_message，不调 async _emergency_clear
            if result == "COMPACT_TRUNCATED":
                logger.warning("[Compact] runner.py force output truncated, triggering emergency clear")
                from niu_api.compat import _find_protected_range, _read_protect_recent_count
                _force_id_set = set(_force_msg_ids)
                _force_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _force_id_set]
                _protect_n = _read_protect_recent_count()
                _protect_start = _find_protected_range(_force_msgs, _protect_n)
                if _protect_start <= 0 or _protect_start >= len(_force_msg_ids):
                    logger.warning(f"[Compact] Runner history all protected ({len(_force_msg_ids)} msgs), no clear needed")
                    return {"status": "skipped", "mode": "force", "reason": "truncated, no clear needed (all protected)"}

                delete_ids = _force_msg_ids[:_protect_start]
                oldest_kept_id = _force_msg_ids[_protect_start]
```

**Key fix vs v1:** Uses `db_messages` (not `messages`), reads `protect_recent_count` from config (not hardcoded 10).

### Task 9: Apply to _emergency_clear (compat.py L827-840)
**File:** `niu_api/compat.py`

Replace L827-840:
```python
    total = len(history)
    if total <= protect_recent_count:
        logger.warning(
            f"[Compact] history len {total} <= {protect_recent_count}, no clear needed"
        )
        return {
            "status": "skipped",
            ...
        }

    # history 与 msg_ids 等长同顺序；保留末尾 N 条，删前面的
    delete_ids = list(msg_ids[:-protect_recent_count])
    oldest_kept_id = msg_ids[-protect_recent_count]
```

With:
```python
    total = len(history)
    # Use user-turn-aware protection to find the keep boundary
    _protect_start = _find_protected_range(history, protect_recent_count)
    if _protect_start <= 0 or _protect_start >= len(msg_ids):
        logger.warning(
            f"[Compact] history len {total}, all protected, no clear needed"
        )
        return {
            "status": "skipped",
            "mode": mode,
            "reason": "truncated, no clear needed (all protected)",
        }

    delete_ids = list(msg_ids[:_protect_start])
    oldest_kept_id = msg_ids[_protect_start]
```

**Note:** `_emergency_clear` receives `history` (list of dicts from `_build_compress_history`). `_find_protected_range` handles dicts via `hasattr` check: `getattr(messages[i], "role", "") if hasattr(messages[i], "role") else messages[i].get("role", "")`. Dicts don't have `.role` attribute, so `hasattr` returns False, and `.get("role", "")` is used. Verified correct.

### Task 9b: Fix hardcoded protect_recent_count=10 at _emergency_clear call sites
**File:** `niu_api/compat.py`

**Location 1 (L2737):**
Replace:
```python
                            protect_recent_count=10,
```
With:
```python
                            protect_recent_count=_read_protect_recent_count(),
```

**Location 2 (L3408):**
Replace:
```python
                    protect_recent_count=10,
```
With:
```python
                    protect_recent_count=protect_recent_count,
```

**Note:** The force path has a `protect_recent_count` variable in scope (L3341-3348) that may be degraded by `force_protect_recent` from chat_queue's degrade_schedule. Using the in-scope variable ensures `_emergency_clear` receives the same degraded count that `_build_compress_history` used to exclude protected messages. The sleep path (L2737) uses `_read_protect_recent_count()` because it has no degradation logic.

**Note:** Both locations already import `_read_protect_recent_count` at the top of compat.py (L22).

### Task 10: Run tests + verify
- `python/bin/python -m pytest tests/test_protect_range.py -v`
- `python/bin/python -m pytest tests/test_sep_cleanup.py -v` (no regression)
- `ruff check niu_api/compat.py agent/runner.py`

## Review Checklist

- [ ] All 10 code points use `_find_protected_range` (compat.py ×6, runner.py ×3, chat_queue.py degrade ×1)
- [ ] Algorithm Step 2 correctly scans past assistant to find user (the P0 fix)
- [ ] Protected range includes tool messages between protected user/assistant
- [ ] Consecutive user messages protect to the earliest
- [ ] Degradation schedule [None, 5, 2] still works (values are min_protect_count)
- [ ] No write paths modified
- [ ] _emergency_clear works with dict messages (not just Message objects)
- [ ] runner.py emergency clear uses db_messages (not messages)
- [ ] runner.py L1206 and L1523 are updated (previously missing)
- [ ] L2737 and L3408 no longer hardcode 10
- [ ] Tests cover all edge cases including idx_N-is-assistant scenario
