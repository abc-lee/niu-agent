"""pytest fixtures"""
import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path（跨平台，不硬编码绝对路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def temp_db():
    """Create temporary database for testing"""
    db_path = tempfile.mktemp(suffix=".db")
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def mock_config():
    """Mock configuration for testing"""
    return {
        "apikey": "test-api-key",
        "apibase": "https://api.anthropic.com",
        "model": "claude-3-5-sonnet-20241022",
        "context_win": 200000,
        "max_retries": 3
    }

# ============================================================================
# E2E / Integration 测试保护
# ============================================================================
# 默认不收集 e2e/integration 测试（会操作真实用户数据 ~/.niu/）
# 只有显式加 --run-e2e 参数才收集

def pytest_addoption(parser):
    """添加 --run-e2e 命令行参数"""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="收集并运行 e2e/integration 测试（会操作真实用户数据 ~/.niu/）"
    )


def pytest_collection_modifyitems(config, items):
    """默认跳过标记为 e2e 的测试"""
    if config.getoption("--run-e2e"):
        return  # 显式要求跑 e2e，不跳过

    skip_e2e = pytest.mark.skip(reason="需要 --run-e2e 参数才运行（会操作真实用户数据）")
    for item in items:
        if "e2e" in item.keywords or "integration" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(autouse=True)
def _isolate_md_mirror(tmp_path, monkeypatch):
    """MD 镜像路径全局指向临时文件——防止任何测试污染用户真实 ~/.niu/md/F1。"""
    import agent.md_mirror as mdm
    import agent.session as sess
    fake = str(tmp_path / "isolated_f1.md")
    monkeypatch.setattr(mdm, "F1_PATH", fake)
    if hasattr(mdm, "F2_PATH"):
        monkeypatch.setattr(mdm, "F2_PATH", str(tmp_path / "isolated_f2.md"))
    if hasattr(mdm, "F3_PATH"):
        monkeypatch.setattr(mdm, "F3_PATH", str(tmp_path / "isolated_f3.md"))
    if hasattr(sess, "F1_PATH"):
        monkeypatch.setattr(sess, "F1_PATH", fake)
    yield


@pytest.fixture(autouse=True)
def _isolate_niu_log_dir(tmp_path, monkeypatch):
    """修复报告日志目录全局指向临时目录——防止任何测试把 repair-report-*.json
    写进用户真实 ~/.niu/logs/（与 _isolate_md_mirror 同范式）。
    需要特定路径的测试可在用例内 monkeypatch.setenv 覆盖本 fixture。"""
    monkeypatch.setenv("NIU_LOG_DIR", str(tmp_path / "isolated-niu-logs"))
    yield


# ============================================================================
# ~/.niu 真实数据的全局隔离（与 _isolate_md_mirror / _isolate_niu_log_dir 同范式）
# ============================================================================

_REAL_NIU_DIFF_LINES: list[str] = []


def _real_niu_fingerprints() -> dict:
    """真实 ~/.niu 路径的指纹快照（md5 / mtime_ns / size，只读不改）。
    md5 只对 ≤ 32MB 的文件计算，更大的以 mtime_ns + size 作指纹。"""
    niu = Path.home() / ".niu"
    snaps: dict = {}

    def _snap(p: Path) -> None:
        if not p.is_file():
            return
        st = p.stat()
        snap = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "md5": None}
        if st.st_size <= 32 * 1024 * 1024:
            snap["md5"] = hashlib.md5(p.read_bytes()).hexdigest()
        snaps[str(p)] = snap

    for rel in ("config/user-config.json", "preferences.json", "work/photos.db"):
        _snap(niu / rel)
    lrs = niu / "lightrag_storage"
    if lrs.is_dir():
        for p in sorted(lrs.rglob("*")):
            _snap(p)
    return snaps


@pytest.fixture(scope="session", autouse=True)
def _guard_real_niu_data():
    """会话级「真实用户数据被改」探针（只报不杀，永不 fail 用例）：跑前扫一遍、
    跑后再扫一遍，有 diff 时由 pytest_terminal_summary 打印醒目汇总行（含具体
    路径与前后指纹）。若是测试所为＝隔离还是有漏洞（请看路径归属）；若本机
    Niu 正在干活＝正常（运行中的 Niu 会正常写这些文件）。"""
    global _REAL_NIU_DIFF_LINES
    _REAL_NIU_DIFF_LINES = []
    before = _real_niu_fingerprints()
    yield
    after = _real_niu_fingerprints()
    for path in sorted(set(before) | set(after)):
        b, a = before.get(path), after.get(path)
        if b == a:
            continue
        if a is None:
            _REAL_NIU_DIFF_LINES.append(f"  - {path}（跑前: {b}）")
        elif b is None:
            _REAL_NIU_DIFF_LINES.append(f"  + {path}（跑后: {a}）")
        else:
            _REAL_NIU_DIFF_LINES.append(f"  ~ {path}\n      跑前: {b}\n      跑后: {a}")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """真实 ~/.niu 数据被改时的醒目汇总（只报不杀，不影响 pass/fail）。"""
    if not _REAL_NIU_DIFF_LINES:
        return
    tr = terminalreporter
    tr.write_sep("=", "真实 ~/.niu 用户数据被改（只报不杀）")
    tr.write_line("若是测试所为＝隔离还是有漏洞（请看路径归属）；若本机 Niu 正在干活＝正常。")
    for line in _REAL_NIU_DIFF_LINES:
        tr.write_line(line)


@pytest.fixture(autouse=True)
def _isolate_niu_config(tmp_path, monkeypatch):
    """niu_api.config.CONFIG_PATH 全局重定向到 tmp 副本——防止任何测试经
    llama_config 的「缺段补写」（_materialize_default_section）把缺省段写进
    用户真实 user-config.json（与 _isolate_md_mirror 同范式）。
    真实文件存在 ⇒ 拷贝内容过去（读取看到真实内容）；不存在 ⇒ 指向同样不
    存在的 tmp 路径（保留「缺失」语义，断言缺失的用例不假绿）。
    HOME 同时指到 tmp 目录：llama_config / embedding_artifacts / media_artifacts
    都以 expanduser / Path.home 现算 ~/.niu/...，布局文件、mmproj 家目录路径、
    家目录状态文件全部落 tmp，不落到真实 ~/.niu。另清 llama_config._warned_reasons
    与 embedding_artifacts 的布局缓存 / 告警标记，防跨用例状态串味。
    用例自己再 monkeypatch CONFIG_PATH / HOME 不受影响（monkeypatch 叠加，以用例为准）。"""
    import niu_api.config as cfg_mod
    from niu_api.internal import embedding_artifacts
    from niu_api.internal import llama_config

    real = Path(cfg_mod.CONFIG_PATH)
    fake = tmp_path / "isolated-niu" / "config" / "user-config.json"
    fake.parent.mkdir(parents=True, exist_ok=True)
    if real.exists():
        fake.write_bytes(real.read_bytes())
    # 真实缺失 ⇒ fake 也不存在 ⇒ 保留「缺失」语义

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(fake))

    # 全仓 grep 唯一在顶层 `from niu_api.config import CONFIG_PATH` 的模块
    # （scripts/model_capability_probe.py:43，独立探测脚本）：取值在 import 期
    # 绑定，模块级 patch 罩不住 ⇒ 一并 patch 它的属性（仅当它已被 import）。
    probe = sys.modules.get("scripts.model_capability_probe")
    if probe is not None:
        monkeypatch.setattr(probe, "CONFIG_PATH", str(fake), raising=False)

    llama_config._warned_reasons.clear()
    embedding_artifacts._LAYOUT_CACHE = None
    embedding_artifacts._LAYOUT_CACHE_KEY = None
    embedding_artifacts._WARNED_REASONS.clear()
    yield


@pytest.fixture(autouse=True)
def _isolate_import_time_niu_paths(tmp_path, monkeypatch):
    """「import 期就把 ~/.niu 路径绑死成模块常量」的全局隔离——_isolate_niu_config 的
    结构性补强（同范式）。

    为什么需要：HOME 重定向（_isolate_niu_config）只对「调用期现算」的路径有效
    （expanduser / Path.home 在函数里）。一批模块在 import 期就把 ~/.niu 路径求值
    成模块级常量（STORAGE_DIR / _db_path / _USER_AGENTS_DIR / CONFIG_PATH /
    MEDIA_QUEUE_PATH…），import 一旦发生过，重定向 HOME 再也罩不住它们——昨夜
    测试污染真实 ~/.niu 的事故正是这类「import 期绑定」常量经手写入的。
    本 fixture 双管齐下：
    ① 设 NIU_STORAGE_DIR env 指向 tmp——给「尚未 import」的模块兜底（lightrag_manager
       的 STORAGE_DIR 在 import 期读该 env）；
    ② 对已在 sys.modules 里的模块逐一 monkeypatch.setattr 模块常量指向 tmp 路径；
       未 import 的模块不强行 import（保持轻量，import 期 env 兜底接住）。

    纳入的常量（全仓 grep `^\w+.*=.*expanduser|Path\.home` 的模块级赋值 + 顶层
    re-import 绑定，逐条复核过消费点）：
    - lightrag_manager.STORAGE_DIR（import 期读 NIU_STORAGE_DIR env 或 Path.home）
    - lightrag_integrity._STORAGE_DIR / lightrag_repair._STORAGE_DIR（Path.home 现算后
      绑死；repair 的 run_full_repair 运行期会把这个值反向同步到另两个模块，三处
      同指 tmp 保持一致）
    - db_monitor._db_path（messages.db 路径，import 期 expanduser）
    - subagent._USER_AGENTS_DIR（~/.niu/agents）
    - ha_watcher.watcher.CONFIG_PATH（~/.niu/ha-config.json；包 __init__ 只重导出
      函数、不重导出该常量，patch watcher 模块即覆盖）
    - md_mirror.MD_DIR / MEDIA_QUEUE_PATH（~/.niu/md/…；F1/F2/F3 由专用
      _isolate_md_mirror 覆盖，此处只补 MD_DIR 与媒体队列文件）
    - media_worker.F4_DEFAULT_QUEUE（顶层 `from agent.md_mirror import MEDIA_QUEUE_PATH
      as F4_DEFAULT_QUEUE` 的 re-import 绑定——patch md_mirror 属性够不到它，须单独
      patch，与 _isolate_md_mirror 同时 patch sess.F1_PATH 同理）

    不纳入（调用期现算 expanduser / Path.home，_isolate_niu_config 的 HOME 重定向
    已覆盖）：compat 的 .startup_stage、__main__ 的 .startup_error、http_log_api /
    gateway 的日志目录、embedding_artifacts 布局文件、media_artifacts 的 models 目录、
    media_vectors（另有 NIU_MEDIA_STORE env 接缝）、region_manager 的 last_region_sync.json、
    brain_region_prompt 的 memory.json。lightrag_manager._raw_storage_dir 仅是
    import 期 env 的原始记录、无下游消费点，不 patch。

    tmp 布局镜像真实 ~/.niu（tmp/.niu/{lightrag_storage,agents,md,…}），使
    「路径里含 .niu」的断言（如 test_lightrag_manager 的 STORAGE_DIR 位置断言）
    在隔离下仍成立；真实 ~/.niu 指纹由会话级 _guard_real_niu_data 兜底核验。"""
    niu_root = tmp_path / ".niu"
    storage = niu_root / "lightrag_storage"
    agents_dir = niu_root / "agents"
    md_dir = niu_root / "md"
    for d in (storage, agents_dir, md_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 尚未 import 的模块兜底：lightrag_manager import 期读该 env 求值 STORAGE_DIR
    monkeypatch.setenv("NIU_STORAGE_DIR", str(storage))

    queue_file = md_dir / "media_vector_queue.md"
    _PATCH_TARGETS = (
        ("niu_api.internal.lightrag_manager", "STORAGE_DIR", storage),
        ("niu_api.internal.lightrag_integrity", "_STORAGE_DIR", storage),
        ("niu_api.internal.lightrag_repair", "_STORAGE_DIR", storage),
        ("niu_api.db_monitor", "_db_path", str(niu_root / "messages.db")),
        ("agent.subagent", "_USER_AGENTS_DIR", str(agents_dir)),
        ("niu_api.internal.ha_watcher.watcher", "CONFIG_PATH", str(niu_root / "ha-config.json")),
        ("agent.md_mirror", "MD_DIR", str(md_dir)),
        ("agent.md_mirror", "MEDIA_QUEUE_PATH", str(queue_file)),
        ("niu_api.internal.media_worker", "F4_DEFAULT_QUEUE", str(queue_file)),
    )
    for modname, attr, value in _PATCH_TARGETS:
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)
    yield


@pytest.fixture(autouse=True)
def _reset_main_agent_owner():
    """主 Agent 守卫全局态隔离：每个用例前后把 `agent.runner._active_main_owner` 置 None。

    守卫是进程级全局（P2a I-1）：某用例泄漏 owner（异常路径未释放）会让后续用例
    被误拒。前置清一次防上游泄漏，后置清一次防本用例污染下游。
    只经 `sys.modules` 取已导入的模块——不在 fixture 里主动 import agent.runner，
    避免改变其它用例的 import 时机。
    """
    mod = sys.modules.get("agent.runner")
    if mod is not None:
        mod._active_main_owner = None
    yield
    mod = sys.modules.get("agent.runner")
    if mod is not None:
        mod._active_main_owner = None
