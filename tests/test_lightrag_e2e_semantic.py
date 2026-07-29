"""端到端测试：真实数据完整跑 check → repair → check → 启动程序验证。

测试前提：~/.niu/lightrag_storage_backup_20260712_071242/ 存在（含 16 个僵尸脑区）。
"""
import hashlib
import shutil
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import pytest
import requests

BACKUP_DIR = Path.home() / ".niu/lightrag_storage_backup_20260712_071242"
STORAGE_DIR = Path.home() / ".niu/lightrag_storage"

# v8-Task 10: 3 真相源文件列表（铁律 1：repair 程序不写 3 真相源）
_TRUTH_FILES = [
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_llm_response_cache.json",
]


def _snapshot_truth() -> tuple[dict[str, float], dict[str, str]]:
    """快照 3 真相源 mtime + sha256。

    返回 (mtimes, hashes)，mtime 单位为秒。
    """
    mtimes: dict[str, float] = {}
    hashes: dict[str, str] = {}
    for fname in _TRUTH_FILES:
        p = STORAGE_DIR / fname
        if not p.exists():
            mtimes[fname] = 0.0
            hashes[fname] = ""
            continue
        mtimes[fname] = p.stat().st_mtime
        hashes[fname] = hashlib.sha256(p.read_bytes()).hexdigest()
    return mtimes, hashes


@pytest.fixture
def restore_real_data():
    """fixture：测试前恢复真实数据（含 16 个僵尸），测试后恢复测试前状态。

    使用 try/finally 保护：测试失败时也能恢复用户数据，避免污染真实环境。
    """
    # 保存当前状态
    snapshot = STORAGE_DIR.parent / f"lightrag_storage_e2e_snapshot_{int(time.time())}"
    if STORAGE_DIR.exists():
        shutil.copytree(STORAGE_DIR, snapshot)

    # 恢复 16 个僵尸脑区的真实数据
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    shutil.copytree(BACKUP_DIR, STORAGE_DIR)

    try:  # try/finally 保护：测试失败也确保恢复用户数据
        yield
    finally:
        # 测试后恢复，无论测试是否失败都执行
        shutil.rmtree(STORAGE_DIR, ignore_errors=True)
        if snapshot.exists():
            shutil.copytree(snapshot, STORAGE_DIR)
            shutil.rmtree(snapshot, ignore_errors=True)


def test_e2e_check_reports_zombies(restore_real_data):
    """阶段 1: 真实数据 check_all 应报告 16 个僵尸脑区"""
    from niu_api.internal.lightrag_integrity import check_all
    result = check_all()

    assert result["ok"] is False, "应该报告 ok=False（含 16 个僵尸脑区）"

    zombie_check = result["checks"].get("brainregion_semantic_zombie", {})
    zombie_errors = zombie_check.get("errors", [])
    assert len(zombie_errors) >= 16, f"应该报告至少 16 个僵尸脑区，实际 {len(zombie_errors)}"

    # 验证僵尸脑区名都是预期的 16 个之一（用集合断言避免关键词覆盖不全）
    EXPECTED_ZOMBIE_NAMES = {
        "智家全维资料脑区",
        "智家使用运维脑区",
        "智家打理相关脑区",
        "智家综合事务脑区",
        "家居智能应用脑区",
        "家居智能实践脑区",
        "家庭智能物联脑区",
        "家庭智能运维脑区",
        "个人智家档案库脑区",
        "个人智家运营脑区",
        "个人智用空间脑区",
        "个人智能库脑区",
        "智能家居内容脑区",
        "智能家居实践区脑区",
        "智能家居管理脑区",
        "居家智能脑区",
    }
    for err in zombie_errors:
        assert err["ref_key"] in EXPECTED_ZOMBIE_NAMES, (
            f"僵尸脑区名不在预期的 16 个之内: {err['ref_key']}"
        )


def test_e2e_repair_cleans_zombies(restore_real_data):
    """阶段 2: repair_all 应清理 16 个僵尸脑区"""
    from niu_api.internal.lightrag_repair import repair_all
    result = repair_all()

    zombie_repair = result.get("brainregion_zombies", {})
    assert zombie_repair["status"] == "ok"
    assert zombie_repair["cleaned_count"] >= 16


def test_e2e_zombies_cleaned_after_repair(restore_real_data):
    """阶段 3: repair 后 check_all 应清掉 16 个僵尸脑区（brainregion_semantic_zombie=0）。

    本次只保证 16 个僵尸脑区清理干净（brainregion_semantic_zombie=0），
    不保证整体 check_all ok=True。剩余 90 个非僵尸报错（7 个 source_id_mismatch +
    83 个 chunk_shared）是历史残留，待后续单独清理，不在本次修复范围
    （见 Task 8 Step 2 Expected 注释）。
    """
    from niu_api.internal.lightrag_integrity import check_all
    from niu_api.internal.lightrag_repair import repair_all

    repair_all()
    result = check_all()

    # 僵尸脑区 check 不再报错（本次修复的唯一硬性目标）
    zombie_check = result["checks"].get("brainregion_semantic_zombie", {})
    assert zombie_check.get("errors", []) == [], (
        f"16 个僵尸脑区应被清理干净，实际仍报错: {zombie_check.get('errors', [])}"
    )

    # 整体 ok 不一定是 True（剩余 90 个非僵尸报错是历史残留，不在本次修复范围）
    # 关键：brainregion_semantic_zombie 必须 0 errors


def test_e2e_program_starts_normally(restore_real_data):
    """阶段 4: 程序启动后 region_sync 不应卡 dissolve，不报僵尸脑区 warning"""
    from niu_api.internal.lightrag_repair import repair_all
    repair_all()  # 先清理

    # 启动 ./niu
    proc = subprocess.Popen(
        ["./niu"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd="REDACTED_USER_PATH/tools/ai-bot",
    )
    try:
        # 等 API ready
        for _ in range(60):
            try:
                r = requests.get("http://127.0.0.1:9876/health", timeout=2)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            pytest.fail("API 60 秒内未 ready，启动失败")

        # 再等 30 秒让 region_sync 跑完
        time.sleep(30)
        # 优雅停止逻辑移到 finally 块（确保异常时也能 shutdown + kill fallback，Bug J 修复）
    finally:
        # 优雅停止：先 SIGTERM，失败后 SIGKILL fallback（Bug J 修复）
        try:
            requests.post("http://127.0.0.1:9876/api/shutdown", timeout=5)
        except Exception:
            pass
        time.sleep(3)

        # 先 SIGTERM
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # SIGTERM 失败（进程不响应），用 SIGKILL fallback
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL 后仍不退出——极端情况，记录但不阻塞测试

        # 额外清理：杀残留子进程（Electron / niu-api / mcp 等）
        # 用 psutil 杀进程树（psutil 是项目硬依赖，测试环境必有）
        import signal

        import psutil
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.send_signal(signal.SIGTERM)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                parent.send_signal(signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except psutil.NoSuchProcess:
            pass  # 进程已退出

    # 读 stdout 日志
    raw = proc.stdout.read() if proc.stdout else b""
    output = raw.decode("utf-8", errors="replace")

    # 16 个僵尸脑区的特征标记（来自真实数据——description 含"被删除"且脑区名含"智家/家居/居家"）
    # 必须全部检查，避免只查"智家"漏掉"家居智能应用脑区"等
    ZOMBIE_MARKERS = [
        "被删除的重复脑区实体之一",
        "智家全维资料脑区",
        "智家使用运维脑区",
        "智家打理相关脑区",
        "智家综合事务脑区",
        "家居智能应用脑区",
        "家居智能实践脑区",
        "家庭智能物联脑区",
        "家庭智能运维脑区",
        "个人智家档案库脑区",
        "个人智家运营脑区",
        "个人智用空间脑区",
        "个人智能库脑区",
        "智能家居内容脑区",
        "智能家居实践区脑区",
        "智能家居管理脑区",
        "居家智能脑区",
    ]
    for marker in ZOMBIE_MARKERS:
        assert marker not in output, (
            f"启动日志里仍出现僵尸脑区标记: {marker}\n日志末尾:\n{output[-2000:]}"
        )
    # 不应该卡在 forced sync
    assert "activation_mgr still None" not in output, "启动后 activation_mgr 仍 None"


def test_e2e_repair_all_3_truth_sources_intact_via_http(restore_real_data):
    """v8-Task 10 e2e：启动 ./niu 后通过 HTTP repair_all，验证 repair 前后 3 真相源不变。

    关键修复点（与 niu 启动 mtime 变化区分）：
    - niu 启动时 RegionSync 会写 GraphML（脑区管理正常行为）—— 这是启动前后 mtime 变
    - repair 程序本身不写 3 真相源 —— 这是 repair 前后 mtime + sha256 不变
    所以快照点必须取在 repair 调用前后（不是 niu 启动前后）。

    流程：
    1. 启动 ./niu，等 RegionSync 跑完（sleep 30，GraphML mtime 稳定）
    2. repair 前快照 3 真相源（mtime + sha256）
    3. curl 触发 repair_all
    4. 等 repair 跑完（同步等 HTTP 响应）
    5. repair 后快照 3 真相源
    6. 断言 repair 前后 mtime + sha256 完全相同
    """
    # 先清理（fixture 恢复了 16 僵尸脑区，repair_all 清掉后再启动）
    from niu_api.internal.lightrag_repair import repair_all
    repair_all()

    # 1. 启动 ./niu
    proc = subprocess.Popen(
        ["./niu"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd="REDACTED_USER_PATH/tools/ai-bot",
    )
    try:
        # 等 API ready
        for _ in range(60):
            try:
                r = requests.get("http://127.0.0.1:9876/health", timeout=2)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            pytest.fail("API 60 秒内未 ready，启动失败")

        # 等 RegionSync 跑完（GraphML mtime 稳定）
        time.sleep(30)

        # 2. repair 前快照 3 真相源（这时 RegionSync 已写完 GraphML，mtime 稳定）
        mtimes_before, hashes_before = _snapshot_truth()

        # 3. curl 触发 repair_all（同步等响应）
        resp = requests.post(
            "http://127.0.0.1:9876/api/kg/lightrag/repair?target=all",
            timeout=600,
        )
        assert resp.status_code == 200, f"repair API 返回 {resp.status_code}: {resp.text[:200]}"

        # 4. repair 跑完后快照
        mtimes_after, hashes_after = _snapshot_truth()
    finally:
        # 优雅停止 niu
        try:
            requests.post("http://127.0.0.1:9876/api/shutdown", timeout=5)
        except Exception:
            pass
        time.sleep(3)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # 杀残留子进程
        import signal

        import psutil
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.send_signal(signal.SIGTERM)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                parent.send_signal(signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except psutil.NoSuchProcess:
            pass

    # 5. 断言 repair 前后 3 真相源 mtime + sha256 不变
    # 关键：这是 repair 前后比对，不是启动前后比对。
    # niu 启动后 RegionSync 写 GraphML 已在 step 2 快照前完成，
    # repair 本身不应再写 3 真相源。
    assert hashes_after == hashes_before, (
        f"3 真相源内容被 repair 修改:\n"
        f"  before: {hashes_before}\n"
        f"  after:  {hashes_after}"
    )
    assert mtimes_after == mtimes_before, (
        f"3 真相源 mtime 被 repair 修改:\n"
        f"  before: {mtimes_before}\n"
        f"  after:  {mtimes_after}"
    )
