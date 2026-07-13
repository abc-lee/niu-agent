"""端到端测试：真实数据完整跑 check → repair → check → 启动程序验证。

测试前提：~/.niu/lightrag_storage_backup_20260712_071242/ 存在（含 16 个僵尸脑区）。
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

BACKUP_DIR = Path.home() / ".niu/lightrag_storage_backup_20260712_071242"
STORAGE_DIR = Path.home() / ".niu/lightrag_storage"


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
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all

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
        # 用 psutil 杀进程树（如果可用），否则用 pkill fallback
        import signal
        try:
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
        except ImportError:
            # psutil 不可用，用 pkill 兜底（只杀 niu 和 Electron，不杀其他）
            subprocess.run(["pkill", "-9", "-f", "niu"], check=False, timeout=10)
            subprocess.run(["pkill", "-9", "-f", "Electron"], check=False, timeout=10)

    # 读 stdout 日志
    output = proc.stdout.read().decode("utf-8", errors="replace")

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
