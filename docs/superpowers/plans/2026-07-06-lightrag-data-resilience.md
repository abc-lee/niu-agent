# LightRAG 数据韧性外挂程序实施计划 (v6 用户决策驱动 + rfd 原生弹窗)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 LightRAG 数据层加外挂检测+修复，启动时检测损坏，弹窗让用户选"直接退出"或"尝试修复"，全程不改 LightRAG fork 源码、不改 nano-vectordb 安装包、不做自动备份。

**Architecture:**
- **外挂检测**：自己解析 vdb/kv_store/graphml 文件。vdb 文件格式实测验证：
  - `matrix` 字段：`base64(float32 bytes)` 一层编码
  - `data[i].vector` 字段：`base64(zlib(float16 bytes))` 三层编码（zlib magic header `789c`）
  - 检测项：JSON 完整性、字段齐全、`len(matrix_bytes) == 4 * embedding_dim * len(data)`（精确等于）、matrix reshape、vector 三层解码
- **外挂修复**：从损坏 vdb 自身的 `data` 字段读文本重建（matrix 损坏 data 完好是常见场景）。data 也损坏时 fallback 到 `kv_store_text_chunks.json`（只对 chunks 有效）。重建保留原 data 所有非 vector 字段，只重算 vector。vector 用 `base64(zlib(float16 bytes))` 三层编码。修复前把损坏 vdb 备份到 `.corrupt.bak`（保留现场，让用户事后查看）。
- **用户决策驱动**：启动时检测到损坏**不自动修复**，弹窗显示两个选项：
  - 选项 1「直接退出」：用户自己从备份恢复（备份是用户的事，程序不管）
  - 选项 2「尝试修复」：警告"修复未必成功，可能会丢失数据"，用户确认后才调 `repair_all`
- **不做备份**：删除 `full_backup` / `backup_all_vdbs` / `cleanup_corrupt_bak` / `backups/` 目录。正常运行不备份，备份是用户自己的事。
- **鸡生蛋解决**：`_embed_text` 不依赖 LightRAG 实例，直接用 `niu_api.internal.embedding.get_model()`（embedding 模型在 LightRAG eager init 之前预加载）。
- **弹窗 UI**：用 `rfd`（Rust File Dialog）的 `MessageDialog` 弹原生对话框——跨平台原生 GUI（macOS/Windows/Linux），比 iced 自绘弹窗简单。检测到损坏时在独立线程弹对话框，显示"是-尝试修复 / 否-退出"两个按钮 + 警告文字。**不**用 `launch_window("settings")` 那套 Electron 窗口（用户反馈弹不了窗），**不**用 iced splash 扩大窗口自绘告警（v5 方案太复杂）。

**Tech Stack:** Python 3.11+，numpy，zlib，base64，`niu_api.internal.embedding`（embedding 模型，预加载），Iced 0.13（launcher），pytest。

---

## v4 → v6 变更说明

v4 计划已实施 Task 1-6（commit `d4ec74cf` 到 `e5e7b46f`），但用户反馈三个问题：
1. **备份策略错**：每次启动先备份再检测，损坏数据会覆盖健康备份；保留 7 份全量备份磁盘占用过大（storage ~45MB，7 份 = 315MB，未来会到 GB 级）
2. **修复策略错**：检测到损坏应弹窗让用户选"退出"或"尝试修复"，不是自动修复
3. **弹窗实现错**：v5 用 iced splash 扩大窗口自绘告警太复杂，应该用 Rust 原生 GUI 弹窗（rfd）

v6 修正：
- **删除 Task 3（备份模块）**：`lightrag_backup.py` 整个模块删除，相关测试删除
- **改 Task 4（启动集成）**：Phase 1 只做检测（不备份不清理），Phase 2 不自动修复（等用户在弹窗选了"尝试修复"才调 repair_all）
- **改 Task 5（API）**：端点调 `run_repair_on_user_request`，只支持 `target=all`
- **改 Task 6（前端告警）**：用 `rfd::MessageDialog` 弹原生对话框，显示"是-尝试修复 / 否-退出"两按钮 + 警告文字，不用 iced 自绘告警
- **Task 1（检测）+ Task 2（修复）保持不变**

---

## 修改的文件

| 文件 | 改动 | 责任 |
|------|------|------|
| `niu_api/internal/lightrag_backup.py` | **删除整个文件** | v5 不做备份 |
| `tests/test_lightrag_backup.py` | **删除整个文件** | v5 不做备份 |
| `niu_api/internal/lightrag_manager.py` | Phase 1 删 cleanup+backup，只留 check_all；Phase 2 不自动修复，加 `run_repair_on_user_request` | 启动检测 + 用户触发修复 |
| `niu_api/__main__.py` | Phase 1 调用点更新（删 cleanup+backup）；Phase 2 改为不自动跑（等 API 触发） | 启动集成 |
| `launcher/src/main.rs` | splash 弹窗改"退出"+"尝试修复"两按钮；修复按钮带警告文字 | 前端告警 |
| `tests/test_lightrag_resilience_integration.py` | 改测试：Phase 1 只检测不备份；Phase 2 不自动修复 | 验证 |

---

## Task 1: 外挂检测——vdb 文件一致性检查（已完成，保持不变）

**状态**：已完成，commit `d4ec74cf`。v5 不动。

**文件**：
- `niu_api/internal/lightrag_integrity.py`（已存在）
- `tests/test_lightrag_integrity.py`（已存在，9 测试全过）

**v5 无改动**。检测逻辑（matrix float32 + vector zlib+float16 三层解码 + graphml 边引用校验）保持不变。

---

## Task 2: 外挂修复——从 vdb data 重建（已完成，保持不变）

**状态**：已完成，commit `ac120722`。v5 不动。

**文件**：
- `niu_api/internal/lightrag_repair.py`（已存在）
- `tests/test_lightrag_repair.py`（已存在，5 测试全过）

**v5 无改动**。修复逻辑（从 vdb data 字段重建保留元数据 + fallback kv_store_text_chunks + vector 三层编码 + 损坏 vdb 备份到 .corrupt.bak）保持不变。

**注意**：`repair_vdb` 里"备份到 .corrupt.bak"的逻辑保留——这是"修复前现场保留"，让用户事后能查看损坏数据，不是自动备份机制。

---

## Task 3: 删除备份模块（v5 新增）

**Files:**
- Delete: `niu_api/internal/lightrag_backup.py`
- Delete: `tests/test_lightrag_backup.py`

**背景**：v4 Task 3 实现了 `full_backup` / `backup_all_vdbs` / `cleanup_corrupt_bak`。v5 用户决策驱动设计不做自动备份——备份是用户自己的事。删除整个模块。

- [ ] **Step 1: 确认 lightrag_backup.py 当前内容**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && wc -l niu_api/internal/lightrag_backup.py tests/test_lightrag_backup.py
```

Expected: 两个文件都存在，行数 > 0

- [ ] **Step 2: grep 确认 lightrag_backup 的调用点**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && grep -rn "lightrag_backup\|full_backup\|backup_all_vdbs\|cleanup_corrupt_bak" --include="*.py" niu_api/ agent/ tests/
```

Expected: 只在 `lightrag_manager.py`（Task 4 集成点）和 `lightrag_backup.py` 自身有匹配。Task 4 会改 `lightrag_manager.py` 删掉这些调用，所以这里先确认调用点。

- [ ] **Step 3: 删除两个文件**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && rm niu_api/internal/lightrag_backup.py tests/test_lightrag_backup.py
```

- [ ] **Step 4: 同时改 `lightrag_manager.py` 删掉 cleanup/full_backup 调用（避免 NameError 中间状态）**

**关键**：Task 3 删 `lightrag_backup.py` 后，`lightrag_manager.py:991` 的 `from niu_api.internal.lightrag_backup import full_backup, cleanup_corrupt_bak` 会 ImportError，L996 的 `cleanup_corrupt_bak()` 和 L1002 的 `full_backup()` 会 NameError——程序启动崩。所以 Step 4 必须同时改 `run_resilience_phase1` 删掉这些调用，不能只注释 import。

读 `niu_api/internal/lightrag_manager.py` 找 `run_resilience_phase1`（约 L980-1020），把整个函数改为（直接做 Task 4 Step 4 的工作，避免中间 broken commit）：

```python
def run_resilience_phase1() -> dict:
    """Phase 1（LightRAG eager init 之前）：只做一致性检测。

    v6 修正：不做 cleanup / full_backup（备份是用户自己的事）。
    检测到损坏不自动修复，由 rfd 原生弹窗让用户选'退出'或'尝试修复'。

    Returns:
        {"check_ok": bool, "need_repair": bool, "check_result": dict}
    """
    global _integrity_result
    from niu_api.internal.lightrag_integrity import check_all

    # 只做检测，不动任何文件
    try:
        check_result = check_all()
    except Exception as e:
        logger.warning(f"[LightRAG] 一致性检测失败（不影响启动）: {e}")
        check_result = {"ok": True, "total_errors": 0, "error": str(e)}

    _integrity_result = check_result

    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"total_errors={check_result.get('total_errors', 0)}"
    )
    return {
        "check_ok": check_result.get("ok", True),
        "need_repair": not check_result.get("ok", True),
        "check_result": check_result,
    }
```

同时删掉 `run_resilience_phase2` 函数（如果存在），加 `run_repair_on_user_request`（Task 4 Step 5 的工作，提前到 Task 3 避免 Task 4 重复改）：

```python
def run_repair_on_user_request() -> dict:
    """用户在弹窗点'尝试修复'后调用（通过 /api/kg/lightrag/repair 触发）。

    v6: 不自动修复，等用户决策。用户确认后才调 repair_all。
    修复后主动调 get_lightrag() 触发重试初始化（不依赖后台线程兜底）。

    Returns:
        {"repaired": bool, "check_ok": bool, "repair_result": dict | None, "check_result": dict | None}
    """
    global _integrity_result
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all

    logger.warning("[LightRAG] 用户选择'尝试修复'，启动 repair_all")
    try:
        repair_result = repair_all()
        reset_init_state()
        # 修复后重跑 check_all 更新 _integrity_result
        check_result = check_all()
        _integrity_result = check_result
        # v6 改进 5：主动调 get_lightrag() 触发重试初始化（不依赖后台线程 60 秒兜底）
        try:
            get_lightrag()
        except Exception as e:
            logger.warning(f"[LightRAG] 修复后 get_lightrag 重试失败（不影响返回）: {e}")
        logger.info(f"[LightRAG] 修复完成: {repair_result}, 重检: ok={check_result.get('ok')}")
        return {
            "repaired": True,
            "check_ok": check_result.get("ok", True),
            "repair_result": repair_result,
            "check_result": check_result,
        }
    except Exception as e:
        logger.error(f"[LightRAG] 修复失败: {e}")
        return {
            "repaired": False,
            "check_ok": False,
            "repair_result": {"error": str(e)},
            "check_result": None,
        }
```

**注意**：`get_lightrag` 在同模块内已定义，直接调即可。`reset_init_state` 也在同模块内。

这样 Task 3 commit 后程序不会崩（`run_resilience_phase1` 已改为只 check_all，`run_resilience_phase2` 已删，`run_repair_on_user_request` 已加），Task 4 只需改 `__main__.py` 删 Phase 2 自动调用 + 改测试。

- [ ] **Step 5: 跑剩余韧性测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_integrity.py tests/test_lightrag_repair.py tests/test_lightrag_resilience_integration.py tests/test_lightrag_repair_api.py -v 2>&1 | tail -20
```

Expected: 备份测试已删除不跑；集成测试 `test_phase1_runs_cleanup_backup_check` 会 FAIL（因为 Phase 1 已不调 cleanup/full_backup，测试期望已过时）；其他测试 PASS。Task 4 会更新集成测试。

- [ ] **Step 6: 确认程序启动不崩（验证无 NameError 中间状态）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.internal.lightrag_manager import run_resilience_phase1, run_repair_on_user_request; print('import ok')"
```

Expected: `import ok`（无 NameError，程序启动不会崩）

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add -A
git commit -m "refactor(lightrag): 删除备份模块 + Phase 1 只检测 + 加 run_repair_on_user_request

v4 的 full_backup/backup_all_vdbs/cleanup_corrupt_bak 有两个问题：
1. 每次启动先备份再检测，损坏数据会覆盖健康备份
2. 保留 7 份全量备份磁盘占用过大（storage 45MB × 7 = 315MB，未来 GB 级）

v6 改为用户决策驱动：检测到损坏弹原生对话框让用户选'退出'或'尝试修复'。
备份是用户自己的事，程序不做自动备份。
repair_vdb 的'备份到 .corrupt.bak'逻辑保留（修复前现场保留）。

同步改 run_resilience_phase1 只做 check_all（删 cleanup+backup 调用，避免 NameError）。
删 run_resilience_phase2（不自动修复），加 run_repair_on_user_request（用户触发修复，
修复后主动调 get_lightrag 触发重试初始化）。"
```

---

## Task 4: 改 `__main__.py` 删 Phase 2 自动调用 + 更新集成测试（v6 简化）

**Files:**
- Modify: `niu_api/__main__.py`（Phase 2 自动调用删除）
- Test: `tests/test_lightrag_resilience_integration.py`

**背景**：Task 3 已改 `lightrag_manager.py`（删 `run_resilience_phase2`，加 `run_repair_on_user_request`，`run_resilience_phase1` 只检测）。Task 4 只需改 `__main__.py` 删 Phase 2 自动调用 + 更新集成测试。

- [ ] **Step 1: 改集成测试——Phase 1 只检测不备份**

读 `tests/test_lightrag_resilience_integration.py`，改 `test_phase1_runs_cleanup_backup_check` 为 `test_phase1_only_checks_no_backup_or_cleanup`：

```python
def test_phase1_only_checks_no_backup_or_cleanup(monkeypatch):
    """v6 Phase 1：只调 check_all，不调 cleanup/backup/repair（备份是用户的事）"""
    from niu_api.internal import lightrag_manager

    check_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: check_calls.append("check") or {"ok": True, "total_errors": 0})

    # 验证 lightrag_backup 模块不存在（已删除）
    try:
        import niu_api.internal.lightrag_backup  # noqa: F401
        assert False, "lightrag_backup 模块应已删除"
    except ImportError:
        pass  # 预期：模块已删除

    result = lightrag_manager.run_resilience_phase1()

    assert check_calls == ["check"]
    assert result["check_ok"] is True
    assert result["need_repair"] is False
```

- [ ] **Step 2: 改集成测试——Phase 2 不自动修复 + run_repair_on_user_request**

改 `test_phase2_repairs_when_needed` 和 `test_phase2_skips_when_healthy` 为：

```python
def test_phase2_does_not_auto_repair(monkeypatch):
    """v6 Phase 2：不自动修复，只记录 need_repair 状态等用户决策"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {})

    # v6 删除了 run_resilience_phase2（不自动修复）
    assert not hasattr(lightrag_manager, "run_resilience_phase2"), \
        "v6 应删除 run_resilience_phase2（不自动修复）"
    assert repair_calls == []


def test_run_repair_on_user_request_repairs_and_resets(monkeypatch):
    """v6: run_repair_on_user_request 用户点'尝试修复'后调 repair_all + reset_init_state + 重跑 check_all + get_lightrag"""
    from niu_api.internal import lightrag_manager

    repair_calls = []
    reset_calls = []
    check_calls = []
    get_lightrag_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all",
                        lambda: repair_calls.append("repair") or {"vdb_entities.json": {"status": "ok"}})
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state",
                        lambda: reset_calls.append("reset"))
    monkeypatch.setattr("niu_api.internal.lightrag_integrity.check_all",
                        lambda: check_calls.append("check") or {"ok": True, "total_errors": 0})
    # mock get_lightrag 避免真实初始化
    monkeypatch.setattr("niu_api.internal.lightrag_manager.get_lightrag",
                        lambda: get_lightrag_calls.append("get_lightrag") or None)

    result = lightrag_manager.run_repair_on_user_request()

    assert repair_calls == ["repair"]
    assert reset_calls == ["reset"]
    assert check_calls == ["check"]
    assert get_lightrag_calls == ["get_lightrag"]  # v6 改进 5：主动触发重试
    assert result["repaired"] is True
    assert result["check_ok"] is True
```

- [ ] **Step 3: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_resilience_integration.py -v
```

Expected: 部分 FAIL（`__main__.py` 还没改 Phase 2 调用——但 `__main__.py` 改动不影响测试，测试直接调 `lightrag_manager` 函数。实际上 Task 3 已让这些测试能过，Step 3 可能直接 PASS）

- [ ] **Step 4: 改 `__main__.py` 删 Phase 2 自动调用**

读 `niu_api/__main__.py` 找 Phase 1 + Phase 2 调用点（约 L185-230），改为：

**Phase 1**（LightRAG eager init 之前）保留（Task 3 已让 `run_resilience_phase1` 只检测）：
```python
    # Phase 1: LightRAG eager init 之前——只做检测
    try:
        from niu_api.internal.lightrag_manager import run_resilience_phase1
        phase1_result = run_resilience_phase1()
        logger.info(f"LightRAG Phase 1 检测: {phase1_result}")
    except Exception as e:
        logger.warning(f"LightRAG Phase 1 检测失败（不影响启动）: {e}")
        phase1_result = {"need_repair": False, "check_ok": True}
```

**Phase 2**（LightRAG eager init 之后）**删除自动调用**——改为只记录状态，等用户在弹窗触发 API：
```python
    # v6: Phase 2 不自动修复，等用户在 rfd 弹窗点'尝试修复'
    # phase1_result["need_repair"] 状态通过 get_lightrag_status() 的 integrity 字段暴露给 splash
    # 用户点'尝试修复'后，splash 调 /api/kg/lightrag/repair 触发 run_repair_on_user_request
    if phase1_result.get("need_repair"):
        logger.warning("[LightRAG] 检测到损坏，等待用户在 rfd 弹窗选择'退出'或'尝试修复'")
```

- [ ] **Step 5: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_resilience_integration.py -v
```

Expected: 5 PASS

- [ ] **Step 6: 跑全部韧性测试无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_integrity.py tests/test_lightrag_repair.py tests/test_lightrag_resilience_integration.py tests/test_lightrag_repair_api.py -v 2>&1 | tail -20
```

Expected: 全部 PASS（注意：test_lightrag_repair_api.py 的测试在 Task 5 会改，这里可能 FAIL，Task 5 会修）

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/__main__.py tests/test_lightrag_resilience_integration.py
git commit -m "refactor(__main__): v6 删 Phase 2 自动修复调用

Task 3 已改 lightrag_manager（run_resilience_phase1 只检测，删 run_resilience_phase2，
加 run_repair_on_user_request）。Task 4 改 __main__.py 删 Phase 2 自动调用，
改为只记录 need_repair 状态等用户在 rfd 弹窗决策。

更新集成测试：Phase 1 只检测不备份；run_repair_on_user_request 主动调 get_lightrag 触发重试。"
```

---

## Task 5: 修复 API 端点（已完成 + v5 小改）

**状态**：v4 已完成，commit `0ceb9a3f`。v5 小改：端点改调 `run_repair_on_user_request`（而不是直接调 `repair_all`）。

**Files:**
- Modify: `niu_api/kg_api.py`（`repair_lightrag_storage` 端点）
- Test: `tests/test_lightrag_repair_api.py`

**背景**：v4 端点直接调 `repair_all` + 手动重跑 `check_all`。v5 改为调 `run_repair_on_user_request`（封装了 repair_all + reset_init_state + 重跑 check_all）。

- [ ] **Step 1: 改测试——端点调 run_repair_on_user_request**

读 `tests/test_lightrag_repair_api.py`，改 3 个测试的 mock：

**当前测试**（v4，mock `repair_all` + `reset_init_state` + `check_all`）：
```python
def test_repair_endpoint_all_targets(monkeypatch):
    # ... mock repair_all + reset_init_state + check_all ...
    monkeypatch.setattr("niu_api.internal.lightrag_repair.repair_all", ...)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.reset_init_state", lambda: None)
```

**改为**（v5，mock `run_repair_on_user_request`）：
```python
def test_repair_endpoint_all_targets(monkeypatch):
    """v5: /api/kg/lightrag/repair 调 run_repair_on_user_request"""
    from niu_api import kg_api

    repair_calls = []
    monkeypatch.setattr("niu_api.internal.lightrag_manager.run_repair_on_user_request",
                        lambda: repair_calls.append("repair") or {
                            "repaired": True,
                            "check_ok": True,
                            "repair_result": {"vdb_entities.json": {"status": "ok"}},
                            "check_result": {"ok": True, "total_errors": 0},
                        })

    client = TestClient(kg_api.app)
    response = client.post("/api/kg/lightrag/repair", params={"target": "all"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert repair_calls == ["repair"]
    assert data["result"]["repaired"] is True
```

类似改 `test_repair_endpoint_specific_vdb` 和 `test_repair_endpoint_unknown_target`。

**注意**：v6 端点不再支持 `target=vdb_xxx.json` 单文件修复（简化为只有 `target=all`），因为 `run_repair_on_user_request` 内部调 `repair_all`。**删除** `test_repair_endpoint_specific_vdb` 测试（v6 只支持 all，单文件测试无意义），保留 `test_repair_endpoint_all_targets` 和 `test_repair_endpoint_unknown_target`，并加一个新测试 `test_repair_endpoint_rejects_vdb_target` 验证 `target=vdb_entities.json` 返回 400。

- [ ] **Step 2: 跑测试验证失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair_api.py -v
```

Expected: FAIL（端点还调 `repair_all`，没调 `run_repair_on_user_request`）

- [ ] **Step 3: 改 `kg_api.py` 端点调 `run_repair_on_user_request`**

读 `niu_api/kg_api.py` 找 `repair_lightrag_storage` 端点（约 L1084-1115），改为：

```python
@router.post("/lightrag/repair")
def repair_lightrag_storage(target: str = "all") -> dict:
    """修复 LightRAG 存储（用户在 splash 点'尝试修复'触发）。

    实际路径：/api/kg/lightrag/repair（router prefix=/api/kg + 端点 /lightrag/repair）

    v5: 调 run_repair_on_user_request（封装 repair_all + reset_init_state + 重跑 check_all）。
    v5 只支持 target=all（用户决策驱动，不分单文件修复）。

    Args:
        target: 只支持 "all"（其他值返回 400）
    """
    from fastapi import HTTPException
    from niu_api.internal.lightrag_manager import run_repair_on_user_request

    if target != "all":
        raise HTTPException(status_code=400, detail=f"v5 只支持 target=all，收到: {target}")

    result = run_repair_on_user_request()
    return {"status": "ok", "result": result}
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_lightrag_repair_api.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add niu_api/kg_api.py tests/test_lightrag_repair_api.py
git commit -m "refactor(kg_api): /api/kg/lightrag/repair 改调 run_repair_on_user_request

v4 端点直接调 repair_all + 手动重跑 check_all
v5 端点调 run_repair_on_user_request（封装 repair_all + reset_init_state + 重跑 check_all）
v5 只支持 target=all（用户决策驱动，不分单文件修复）"
```

---

## Task 6: 前端告警——rfd 原生弹窗"退出"+"尝试修复"两按钮（v6 简化）

**Files:**
- Modify: `launcher/Cargo.toml`（加 `rfd` 依赖）
- Modify: `launcher/src/main.rs`
- Test: 手动验证（Rust 测试不在 TDD 范围内）

**背景**：v5 用 iced splash 扩大窗口显示弹窗太复杂。v6 改用 Rust 原生弹窗 `rfd`（Rust File Dialog）的 `MessageDialog`——跨平台原生对话框，macOS/Windows/Linux 都支持，比 iced 自绘 UI 简单得多。

**关键**：检测到损坏时，在独立线程调 `rfd::MessageDialog::new().set_title(...).set_description(...).set_buttons(YesNo).show()` 弹原生对话框，根据用户选择（Yes=尝试修复 / No=退出）触发对应逻辑。

- [ ] **Step 1: 加 rfd 依赖到 Cargo.toml**

读 `launcher/Cargo.toml`，在 `[dependencies]` 末尾加：

```toml
rfd = "0.14"
```

- [ ] **Step 2: 读现有 splash 告警逻辑**

读 `launcher/src/main.rs` 的 `SplashMessage` enum + `Splash` struct + `update` 函数（L40-340 附近），理解 v4 已实现的 `alert: Option<LightragAlert>` + `StatusCheckResult` / `RepairLightrag` / `RepairResult` / `DismissAlert` message。

- [ ] **Step 3: 改 SplashMessage enum——删 DismissAlert，加 UserDialogChoice**

把 v4 的 `DismissAlert` 删掉（不再用 iced 自绘按钮），改为接收用户在原生弹窗的选择：

```rust
enum SplashMessage {
    Tick,
    WindowOpened(window::Id),
    HideDockIcon,
    StatusCheckResult(Result<LightragStatus, String>),
    UserDialogChoice(bool),  // true=尝试修复，false=退出（来自 rfd 原生弹窗）
    RepairResult(Result<String, String>),
    ExitApp,  // 实际退出程序
}
```

删除 `RepairLightrag`（不再用 iced 按钮触发）和 `DismissAlert`（不再用 iced "继续"按钮）。

- [ ] **Step 4: 改 StatusCheckResult 分支——弹 rfd 原生对话框**

在 `update` 函数的 `StatusCheckResult` 分支，检测到损坏时弹原生对话框（在独立线程跑，避免阻塞 iced executor）：

```rust
SplashMessage::StatusCheckResult(Ok(status)) => {
    if status.init_failed || status.integrity.as_ref().map_or(false, |i| !i.ok) {
        let total_errors = status.integrity.as_ref().map_or(0, |i| i.total_errors);
        let message = if status.init_failed {
            format!("LightRAG 初始化失败\n\n检测到数据损坏，请选择：\n\n是 - 尝试修复（修复未必成功，可能会丢失数据）\n否 - 直接退出（请自行从备份恢复）")
        } else {
            format!("检测到 {} 个数据一致性问题\n\n请选择：\n\n是 - 尝试修复（修复未必成功，可能会丢失数据）\n否 - 直接退出（请自行从备份恢复）", total_errors)
        };
        // 在独立线程弹 rfd 原生对话框，避免阻塞 iced
        let (tx, rx) = iced::futures::channel::oneshot::channel::<bool>();
        std::thread::spawn(move || {
            let choice = rfd::MessageDialog::new()
                .set_title("LightRAG 数据异常")
                .set_description(&message)
                .set_buttons(rfd::MessageButtons::YesNo)
                .set_level(rfd::MessageLevel::Warning)
                .show();
            let _ = tx.send(choice == rfd::MessageDialogResult::Yes);
        });
        return Task::perform(
            async move { rx.await.unwrap_or(false) },
            SplashMessage::UserDialogChoice,
        );
    }
    // v6: 健康则不弹窗，splash 正常继续启动（不设 alert 字段——v6 已删 alert 字段）
    Task::none()
}
```

**关键（v6 改进 3）**：同时删掉 v4 在 `StatusCheckResult` 分支里的 `window::resize(id, iced::Size::new(400.0, 160.0))` 调用（如果存在）。v6 用 rfd 原生弹窗（独立窗口），不需要扩大 iced splash 窗口。grep 确认：

```bash
cd REDACTED_USER_PATH/tools/ai-bot && grep -n "window::resize" launcher/src/main.rs
```

把所有 `window::resize(id, iced::Size::new(400.0, 160.0))` 和对应的缩回 `window::resize(id, iced::Size::new(280.0, 80.0))` 调用删掉（v6 不再用 iced 窗口尺寸变化显示告警）。

- [ ] **Step 5: 加 UserDialogChoice 分支——根据用户选择触发退出或修复**

```rust
SplashMessage::UserDialogChoice(try_repair) => {
    if try_repair {
        // 用户选"是"=尝试修复，调 /api/kg/lightrag/repair?target=all
        let (tx, rx) = iced::futures::channel::oneshot::channel::<Result<String, String>>();
        std::thread::spawn(move || {
            let result = reqwest::blocking::Client::new()
                .post("http://127.0.0.1:9876/api/kg/lightrag/repair?target=all")
                .timeout(std::time::Duration::from_secs(300))  // 修复可能要几分钟
                .send()
                .map_err(|e| e.to_string())
                .and_then(|resp| resp.text().map_err(|e| e.to_string()));
            let _ = tx.send(result);
        });
        return Task::perform(
            async move { rx.await.unwrap_or(Err("channel closed".into())) },
            SplashMessage::RepairResult,
        );
    } else {
        // 用户选"否"=退出
        return Task::done(SplashMessage::ExitApp);
    }
}

SplashMessage::ExitApp => {
    std::process::exit(0);
}
```

- [ ] **Step 6: 改 RepairResult 分支——修复后重查 status**

```rust
SplashMessage::RepairResult(Ok(_)) => {
    // 修复完成后重查 status，如果健康则关闭告警继续启动；如果仍损坏则再弹对话框
    let (tx, rx) = iced::futures::channel::oneshot::channel::<Result<LightragStatus, String>>();
    std::thread::spawn(move || {
        let result = reqwest::blocking::Client::new()
            .get("http://127.0.0.1:9876/api/kg/stats")
            .send()
            .map_err(|e| e.to_string())
            .and_then(|resp| resp.json::<LightragStatus>().map_err(|e| e.to_string()));
        let _ = tx.send(result);
    });
    return Task::perform(
        async move { rx.await.unwrap_or(Err("channel closed".into())) },
        SplashMessage::StatusCheckResult,
    );
}

SplashMessage::RepairResult(Err(e)) => {
    // 修复失败，弹原生对话框告诉用户
    let (tx, rx) = iced::futures::channel::oneshot::channel::<bool>();
    std::thread::spawn(move || {
        let choice = rfd::MessageDialog::new()
            .set_title("修复失败")
            .set_description(&format!("修复失败：{}\n\n请选择：\n\n是 - 重试\n否 - 退出", e))
            .set_buttons(rfd::MessageButtons::YesNo)
            .set_level(rfd::MessageLevel::Error)
            .show();
        let _ = tx.send(choice == rfd::MessageDialogResult::Yes);
    });
    return Task::perform(
        async move { rx.await.unwrap_or(false) },
        SplashMessage::UserDialogChoice,
    );
}
```

- [ ] **Step 7: 删 view 函数的告警分支**

v4 在 `view` 函数里有 `if let Some(alert) = &self.alert` 告警视图。v6 删掉这个分支（不再用 iced 自绘告警 UI，改用 rfd 原生弹窗）：

```rust
fn view(&self) -> Element<'_, SplashMessage> {
    // 只保留现有 splash 启动画面（280x80）
    // 删除 alert 告警分支
    // ...
}
```

同时可以删掉 `Splash` struct 的 `alert: Option<LightragAlert>` 字段和 `LightragAlert` struct（不再需要）。

- [ ] **Step 8: 编译验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher && cargo build 2>&1 | tail -10
```

Expected: 编译成功（rfd 0.14 + iced 0.13 兼容）

- [ ] **Step 9: 手动验证**

启动程序，模拟 LightRAG 初始化失败（临时把 vdb 文件改名），看是否弹出原生对话框（macOS 上是系统原生警告框），显示"是 - 尝试修复"/"否 - 退出"两个按钮。

- [ ] **Step 10: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add launcher/Cargo.toml launcher/src/main.rs
git commit -m "feat(launcher): v6 用 rfd 原生弹窗显示'退出'+'尝试修复'两按钮

v5 用 iced splash 扩大窗口自绘告警 UI 太复杂
v6 改用 rfd (Rust File Dialog) 的 MessageDialog——跨平台原生对话框

检测到损坏时在独立线程弹原生对话框：
- 标题'LightRAG 数据异常'
- 描述含损坏信息 + '是-尝试修复（修复未必成功，可能会丢失数据）/否-直接退出（请自行从备份恢复）'
- Warning 级别
- YesNo 按钮

用户选'是'调 /api/kg/lightrag/repair?target=all
用户选'否' std::process::exit(0) 直接退出
修复失败再弹原生对话框（Error 级别）让用户选重试或退出

删除 v4 的 alert 字段 + LightragAlert struct + view 告警分支（不再用 iced 自绘）。"
```

---

## Task 7: 端到端验证（v5 修正）

**Files:** 临时验证脚本

**目的**：验证 v5 用户决策驱动流程：检测到损坏 → 弹窗 → 用户选"尝试修复" → 修复成功。

- [ ] **Step 1: 模拟 vdb 损坏**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
cp ~/.niu/lightrag_storage/vdb_entities.json /tmp/vdb_entities.json.backup

python3 -c "
import json
with open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_entities.json') as f:
    raw = json.load(f)
raw['matrix'] = raw['matrix'][:1000]  # 截断 matrix
with open('REDACTED_USER_PATH/.niu/lightrag_storage/vdb_entities.json', 'w') as f:
    json.dump(raw, f)
"
# 注意：不要删 ~/.niu/last_region_sync.json——那是 region_sync 的状态文件，删了会触发全量重同步
```

- [ ] **Step 2: 启动程序，验证 rfd 原生弹窗**

```bash
./niu &
sleep 30
# 观察是否弹出系统原生警告框（macOS 是 NSAlert 风格，Windows 是 MessageBox，Linux 是 GTK Dialog）
# 对话框标题"LightRAG 数据异常"
# 描述含损坏信息 + "是-尝试修复（修复未必成功，可能会丢失数据）/否-直接退出（请自行从备份恢复）"
# 两个按钮：是 / 否
# 手动点"是"按钮
```

Expected: 弹出系统原生警告框，显示"是"/"否"两按钮 + 警告文字

- [ ] **Step 3: 点"尝试修复"后验证修复**

```bash
# 点"尝试修复"后，splash 调 /api/kg/lightrag/repair
# 等修复完成（~2 分钟，2333 条实体重新 embedding）
sleep 150
curl -s "http://localhost:9876/api/kg/stats" | python3 -m json.tool | grep -E "init_failed|integrity"
```

Expected: `init_failed: false`, `integrity.ok: true`

- [ ] **Step 4: 验证修复后程序正常启动**

修复完成后，rfd 弹窗关闭，`run_repair_on_user_request` 主动调 `get_lightrag()` 触发重试初始化，LightRAG 重新加载（用修复后的 vdb），程序正常启动。splash 窗口保持 280x80 显示启动画面，最终 splash 关闭进入主循环。

- [ ] **Step 5: 验证 .corrupt.bak 保留损坏现场**

```bash
ls ~/.niu/lightrag_storage/*.corrupt.bak
```

Expected: `vdb_entities.json.corrupt.bak` 存在（修复前保留的损坏现场）

- [ ] **Step 6: 恢复原始 vdb + 清理**

```bash
pgrep -f "niu_api" | xargs kill -TERM 2>/dev/null
sleep 5
cp /tmp/vdb_entities.json.backup ~/.niu/lightrag_storage/vdb_entities.json
rm -f ~/.niu/lightrag_storage/*.corrupt.bak
```

- [ ] **Step 7: 验证"退出"按钮**

再次模拟损坏启动，这次在 rfd 弹窗点"否"按钮，验证程序直接退出（`std::process::exit(0)`，不修复）。

---

## Self-Review

### 1. Spec coverage

用户需求："启动的时候自动检测，检测出现问题弹窗，显示两个选项。第一个选项是直接退出，要求用户自己从备份中恢复数据。第二个选项是尝试修复，但要告诉用户修复未必成功，也可能会丢失数据。备份和不备份是用户自己的事。"

5 个维度覆盖：
- ✅ 维度 1（故障检测）→ Task 1（已完成，保持不变）
- ✅ 维度 2（数据修复）→ Task 2（已完成，保持不变）+ Task 5（v5 改调 run_repair_on_user_request）
- ✅ 维度 3（写入原子性）→ 不改 nano-vectordb，外挂检测+用户触发修复兜底
- ✅ 维度 4（告警机制）→ Task 6（v5 弹窗"退出"+"尝试修复"两按钮）
- ✅ 维度 5（备份机制）→ **v5 删除备份模块**（备份是用户自己的事）

### 2. v4 → v6 变更

- ✅ **删除 Task 3（备份模块）**：`lightrag_backup.py` 整个删除
- ✅ **改 Task 4**：Phase 1 只检测（删 cleanup+backup）；Phase 2 不自动修复（删 `run_resilience_phase2`，加 `run_repair_on_user_request`）
- ✅ **改 Task 5**：端点调 `run_repair_on_user_request`，只支持 `target=all`
- ✅ **改 Task 6**：用 `rfd::MessageDialog` 弹原生对话框，显示"是-尝试修复 / 否-退出"两按钮 + 警告文字

### 2.1 v6 审查阻断+改进修复

- ✅ **阻断 1（Task 3+4 中间 commit NameError）** → Task 3 Step 4 改为同时改 `run_resilience_phase1` 删掉 cleanup/full_backup 调用 + 加 `run_repair_on_user_request`（不只注释 import），避免 broken commit
- ✅ **改进 1（测试名误导）** → Task 5 删除 `test_repair_endpoint_specific_vdb`，加 `test_repair_endpoint_rejects_vdb_target`
- ✅ **改进 2（self.alert=None 残留）** → Task 6 Step 4 删掉 `self.alert = None;`
- ✅ **改进 3（window::resize 残留）** → Task 6 Step 4 明确删 v4 的 `window::resize(400, 160)` 调用
- ✅ **改进 4（Task 7 描述过时）** → Task 7 Step 2/4/7 改为描述 rfd 原生弹窗行为
- ✅ **改进 5（修复后 LightRAG 重初始化触发）** → `run_repair_on_user_request` 末尾主动调 `get_lightrag()` 触发重试

### 3. Type consistency

- `run_resilience_phase1() -> dict` → Task 4 改（只检测）
- `run_repair_on_user_request() -> dict` → Task 4 新增（替代 `run_resilience_phase2`）
- `repair_lightrag_storage(target: str = "all") -> dict` → Task 5 改（调 `run_repair_on_user_request`）
- `SplashMessage::ExitAlert` / `ExitApp` → Task 6 新增（替代 `DismissAlert`）
- `_integrity_result: dict | None` → Task 4 保持（run_repair_on_user_request 重跑 check_all 更新它）

### 4. 关键设计决策

- **不做备份**：删除 `lightrag_backup.py`，备份是用户自己的事
- **不自动修复**：Phase 2 删除，等用户在 splash 选"尝试修复"才调 `run_repair_on_user_request`
- **弹窗在 splash iced 窗口里**：不用 `launch_window("settings")` 那套 Electron 窗口
- **修复前保留损坏现场**：`repair_vdb` 的 `.corrupt.bak` 逻辑保留（让用户事后查看损坏数据）
- **鸡生蛋解决**：`_embed_text` 用预加载 embedding 模型（Task 2 已实现）

---

## 执行交接

计划完成并保存到 `docs/superpowers/plans/2026-07-06-lightrag-data-resilience.md`。两种执行方式：

**1. Subagent-Driven（推荐）** - 每个 Task 派新子 Agent 实现，Task 之间审查

**2. Inline Execution** - 在当前会话里批量执行

要哪种？
