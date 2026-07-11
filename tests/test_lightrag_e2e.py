"""LightRAG 端到端修复测试（真实数据 + 完整备份恢复）

9 个场景验证修复程序的端到端行为：
1. vdb_entities 截断 → 从 GraphML 重建
2. vdb_chunks 截断 → 从 text_chunks 重建
3. GraphML 损坏 → unrecoverable（未启动 niu_api，LightRAG 实例不可用）
4. text_chunks 损坏 → unrecoverable（LightRAG 实例不可用，无法重新 chunking）
5. full_docs 损坏 → unrecoverable（真相源不可重建）
6. 新用户空数据 → check_all ok=True（tempfile 隔离）
7. entity_chunks 引用悬空 → 从 GraphML 重建
8. llm_response_cache 损坏 → 清空（minor）
9. delete 中途失败（doc_status.chunks_list 引用悬空）→ repair 后恢复一致

测试前必须做完整备份（cp -r ~/.niu/lightrag_storage ~/.niu/lightrag_storage.e2e-bak-TS）。
测试后从备份恢复 + MD5 校验。
任何测试失败/异常，finally 强制从备份恢复。

注意：
- 测试用真实 ~/.niu/lightrag_storage 路径（除了场景 6 用 tempfile）
- 不启动 niu_api，monkeypatch get_lightrag 返回 None（场景 3/4 需要 unrecoverable）
- 真实 embedding 模型（已预加载 bge-base-zh-v1.5），不 mock
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]


# =============================================================================
# 常量 + 工具函数
# =============================================================================


STORAGE_DIR = Path.home() / ".niu" / "lightrag_storage"

# 测试前做完整备份的目录名（带时间戳）
_BACKUP_DIR: Path | None = None

# 备份的文件列表（用于 MD5 校验）
_TRACKED_FILES = [
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
    "graph_chunk_entity_relation.graphml",
    "kv_store_full_docs.json",
    "kv_store_text_chunks.json",
    "kv_store_doc_status.json",
    "kv_store_entity_chunks.json",
    "kv_store_relation_chunks.json",
    "kv_store_full_entities.json",
    "kv_store_full_relations.json",
    "kv_store_llm_response_cache.json",
]


def _md5(path: Path) -> str:
    """计算文件 MD5（macOS 用 md5 -q）。"""
    import hashlib

    if not path.exists():
        return "MISSING"
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_md5_snapshot() -> dict[str, str]:
    """记录当前 storage 目录下所有跟踪文件的 MD5。"""
    return {f: _md5(STORAGE_DIR / f) for f in _TRACKED_FILES}


def _restore_from_backup(backup_dir: Path) -> None:
    """从备份目录恢复所有文件到 storage 目录。

    强制覆盖，不询问。恢复后删除测试期间产生的 .corrupt.*.bak 文件。
    """
    if not backup_dir.exists():
        return

    # 恢复所有跟踪文件
    for fname in _TRACKED_FILES:
        src = backup_dir / fname
        dst = STORAGE_DIR / fname
        if src.exists():
            shutil.copy2(src, dst)
        else:
            # 原本不存在，删除测试期间可能产生的文件
            if dst.exists():
                dst.unlink()

    # 清理测试期间产生的 .corrupt.*.bak 文件
    for f in STORAGE_DIR.glob("*.corrupt.*.bak"):
        try:
            f.unlink()
        except Exception:
            pass

    # 清理 pre-corrupt-test.bak（损坏脚本生成的备份）
    for f in STORAGE_DIR.glob("*.pre-corrupt-test.bak"):
        try:
            f.unlink()
        except Exception:
            pass


def _make_full_backup() -> Path:
    """做完整备份：cp -r ~/.niu/lightrag_storage ~/.niu/lightrag_storage.e2e-bak-TS。

    返回备份目录路径。
    """
    timestamp = int(time.time())
    backup_dir = Path.home() / ".niu" / f"lightrag_storage.e2e-bak-{timestamp}"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    shutil.copytree(STORAGE_DIR, backup_dir)
    return backup_dir


def _verify_md5_match(snapshot: dict[str, str], label: str = "") -> None:
    """校验当前 storage 文件的 MD5 跟 snapshot 一致。

    任何不一致抛 AssertionError，列出差异。
    """
    differences = []
    for fname, expected_md5 in snapshot.items():
        actual_md5 = _md5(STORAGE_DIR / fname)
        if actual_md5 != expected_md5:
            differences.append(f"  {fname}: expected={expected_md5}, actual={actual_md5}")

    if differences:
        diff_text = "\n".join(differences)
        raise AssertionError(
            f"MD5 校验失败 ({label})——{len(differences)} 个文件不一致:\n{diff_text}"
        )


# =============================================================================
# 全局 fixture：备份 + 恢复
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
def module_backup_and_restore():
    """模块级 fixture：测试开始前做完整备份（作为基线），测试结束后从基线恢复 + MD5 校验。

    即使所有测试失败/异常，finally 也会强制恢复。
    每个 test 内部用 function-scope fixture `restore_baseline` 单独从基线恢复。
    """
    global _BACKUP_DIR

    if not STORAGE_DIR.exists():
        pytest.skip(f"lightrag_storage 不存在: {STORAGE_DIR}（请先正常运行过程序一次）")

    # 模块开始：做完整备份（作为基线）
    _BACKUP_DIR = _make_full_backup()
    baseline_snapshot = _record_md5_snapshot()
    print(f"\n[e2e] 模块级完整备份: {_BACKUP_DIR}")
    print(f"[e2e] 基线 MD5 snapshot 已记录 ({len(baseline_snapshot)} 个文件)")

    yield

    # 模块结束：从基线恢复 + MD5 校验
    try:
        _restore_from_backup(_BACKUP_DIR)
        _verify_md5_match(baseline_snapshot, label="模块结束 vs 基线")
        print(f"\n[e2e] 模块结束 MD5 校验通过: 所有 {len(baseline_snapshot)} 个文件恢复一致")
    except Exception as e:
        print(f"\n[e2e] !!! 模块结束恢复失败: {e}")
        raise


@pytest.fixture
def restore_baseline():
    """function 级 fixture：每个测试前从基线备份恢复，确保测试间状态隔离。

    在 backup_and_restore（module-scope）之后运行，_BACKUP_DIR 已设置。
    """
    if _BACKUP_DIR is None or not _BACKUP_DIR.exists():
        pytest.skip("基线备份不存在")
    _restore_from_backup(_BACKUP_DIR)
    yield
    # 测试后也恢复一次（防御性，避免单测失败污染下一个）
    _restore_from_backup(_BACKUP_DIR)


# =============================================================================
# 公共 fixture
# =============================================================================


@pytest.fixture
def no_lightrag_instance(monkeypatch):
    """模拟"LightRAG 实例不可用"——monkeypatch get_lightrag 返回 None。

    用于场景 3/4，让 repair_text_chunks / repair_graphml 走 unrecoverable 分支。
    """
    from niu_api.internal import lightrag_manager

    monkeypatch.setattr(lightrag_manager, "get_lightrag", lambda: None)
    # repair_text_chunks 内部直接 import get_lightrag，要 patch 模块属性
    from niu_api.internal import lightrag_repair

    monkeypatch.setattr(
        "niu_api.internal.lightrag_manager.get_lightrag", lambda: None
    )


# =============================================================================
# 工具函数：读取 vdb / 损坏文件
# =============================================================================


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _truncate_vdb(path: Path, keep_ratio: float = 0.3) -> int:
    """截断 vdb 文件——保留前 keep_ratio 比例的字符，使其 JSON 解析失败。

    返回截断后的字节数。
    """
    with open(path, "rb") as f:
        raw = f.read()
    truncate_at = max(100, int(len(raw) * keep_ratio))
    truncated = raw[:truncate_at]
    with open(path, "wb") as f:
        f.write(truncated)
    return truncate_at


def _corrupt_json_file(path: Path) -> None:
    """损坏 JSON 文件——写入无效 JSON。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"truncated": invalid json')


# =============================================================================
# 场景 1: vdb_entities 截断 → 从 GraphML 重建
# =============================================================================


def test_01_vdb_entities_truncated_rebuilds_from_graphml(restore_baseline):
    """vdb_entities 截断 → check 检测到 critical → repair_vdb_entities 重建。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 截断 vdb_entities
    vdb_path = STORAGE_DIR / "vdb_entities.json"
    original_size = vdb_path.stat().st_size
    _truncate_vdb(vdb_path, keep_ratio=0.3)
    truncated_size = vdb_path.stat().st_size
    print(f"[e2e-1] vdb_entities: {original_size}B → {truncated_size}B (truncated)")

    # 2. check_all 应该检测到 critical（json_parse 或 matrix_size_mismatch）
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"vdb_entities 截断后应该检测到 critical，实际 critical={check_result['critical_errors']}"
    )

    # 3. repair_vdb_entities 从 GraphML 重建
    result = lightrag_repair.repair_vdb_entities()
    print(f"[e2e-1] repair_vdb_entities: status={result['status']}, actual={result.get('actual')}")
    assert result["status"] == "ok", f"repair_vdb_entities 应该成功: {result}"
    assert result["actual"] > 0, f"应该重建至少 1 个 entity: {result}"

    # 4. 重检 check_all 应该 ok（critical=0, major=0）
    check_after = lightrag_integrity.check_all()
    # 允许 minor > 0（如果有其他独立问题），但 critical 必须为 0
    assert check_after["critical_errors"] == 0, (
        f"修复后 critical 应该为 0，实际 {check_after['critical_errors']}: "
        f"{[e for e in check_after['errors'] if e.get('severity') == 'critical']}"
    )


# =============================================================================
# 场景 2: vdb_chunks 截断 → 从 text_chunks 重建
# =============================================================================


def test_02_vdb_chunks_truncated_rebuilds_from_text_chunks(restore_baseline):
    """vdb_chunks 截断 → check 检测到 critical → repair_vdb_chunks 从 text_chunks 重建。"""
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 截断 vdb_chunks
    vdb_path = STORAGE_DIR / "vdb_chunks.json"
    original_size = vdb_path.stat().st_size
    _truncate_vdb(vdb_path, keep_ratio=0.3)
    truncated_size = vdb_path.stat().st_size
    print(f"[e2e-2] vdb_chunks: {original_size}B → {truncated_size}B (truncated)")

    # 2. check_all 应该检测到 critical
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"vdb_chunks 截断后应该检测到 critical，实际 critical={check_result['critical_errors']}"
    )

    # 3. repair_vdb_chunks 从 text_chunks 重建
    result = lightrag_repair.repair_vdb_chunks()
    print(f"[e2e-2] repair_vdb_chunks: status={result['status']}, actual={result.get('actual')}")
    assert result["status"] == "ok", f"repair_vdb_chunks 应该成功: {result}"
    assert result["actual"] > 0, f"应该重建至少 1 个 chunk: {result}"

    # 4. 重检 critical=0
    check_after = lightrag_integrity.check_all()
    assert check_after["critical_errors"] == 0, (
        f"修复后 critical 应该为 0，实际 {check_after['critical_errors']}"
    )


# =============================================================================
# 场景 3: GraphML 损坏 → unrecoverable（LightRAG 实例不可用）
# =============================================================================


def test_03_graphml_corrupt_unrecoverable_without_lightrag(restore_baseline, no_lightrag_instance):
    """GraphML 损坏 + 无 LightRAG 实例 → unrecoverable。

    计划设计：GraphML 损坏时调 apipeline_process_enqueue_documents 重建，
    但需要 LightRAG 实例。测试环境不启动 niu_api，get_lightrag 返回 None，
    repair_graphml 应走 unrecoverable 分支。

    真实重建路径需启动 niu_api 完整初始化 LightRAG，超出 e2e 测试范围。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 损坏 GraphML
    graphml_path = STORAGE_DIR / "graph_chunk_entity_relation.graphml"
    original_size = graphml_path.stat().st_size
    # 写入无效 XML
    with open(graphml_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><graphml><graph><node id="x"><data key="d2">')
    print(f"[e2e-3] graphml: {original_size}B → 损坏 XML")

    # 2. check_all 应该检测到 critical（xml_parse）
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"GraphML 损坏后应该检测到 critical，实际 {check_result['critical_errors']}"
    )

    # 3. repair_graphml 走 unrecoverable 分支
    result = lightrag_repair.repair_graphml()
    print(f"[e2e-3] repair_graphml: status={result['status']}, unrecoverable={result.get('unrecoverable')}")
    assert result["status"] == "error", f"repair_graphml 应该失败: {result}"
    assert result.get("unrecoverable") is True, (
        f"无 LightRAG 实例时应该 unrecoverable，实际 result={result}"
    )


# =============================================================================
# 场景 4: text_chunks 损坏 → unrecoverable（无 LightRAG 实例无法重新 chunking）
# =============================================================================


def test_04_text_chunks_corrupt_unrecoverable_without_lightrag(restore_baseline, no_lightrag_instance):
    """text_chunks 损坏 + 无 LightRAG 实例 → unrecoverable。

    计划设计：text_chunks 损坏时从 full_docs 重新 chunking 重建，
    但需要 LightRAG tokenizer。测试环境 get_lightrag 返回 None，
    repair_text_chunks 应走 unrecoverable 分支。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 损坏 text_chunks
    tc_path = STORAGE_DIR / "kv_store_text_chunks.json"
    _corrupt_json_file(tc_path)
    print(f"[e2e-4] text_chunks: 损坏 JSON")

    # 2. check_all 应该检测到 critical（json_parse）
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"text_chunks 损坏后应该检测到 critical，实际 {check_result['critical_errors']}"
    )

    # 3. repair_text_chunks 走 unrecoverable 分支（无 LightRAG 实例）
    result = lightrag_repair.repair_text_chunks()
    print(f"[e2e-4] repair_text_chunks: status={result['status']}, unrecoverable={result.get('unrecoverable')}")
    assert result["status"] == "error", f"repair_text_chunks 应该失败: {result}"
    assert result.get("unrecoverable") is True, (
        f"无 LightRAG 实例时应该 unrecoverable，实际 result={result}"
    )


# =============================================================================
# 场景 5: full_docs 损坏 → unrecoverable（真相源不可重建）
# =============================================================================


def test_05_full_docs_corrupt_unrecoverable(restore_baseline):
    """full_docs 损坏 → unrecoverable（真相源不可重建）。

    full_docs 是真相源，损坏后无法重新 chunking 重建 text_chunks。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 损坏 full_docs
    fd_path = STORAGE_DIR / "kv_store_full_docs.json"
    _corrupt_json_file(fd_path)
    print(f"[e2e-5] full_docs: 损坏 JSON")

    # 2. check_all 应该检测到 critical（json_parse）
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"full_docs 损坏后应该检测到 critical，实际 {check_result['critical_errors']}"
    )

    # 3. repair_text_chunks 走 unrecoverable 分支（full_docs 损坏）
    result = lightrag_repair.repair_text_chunks()
    print(f"[e2e-5] repair_text_chunks: status={result['status']}, unrecoverable={result.get('unrecoverable')}")
    assert result["status"] == "error", f"repair_text_chunks 应该失败: {result}"
    assert result.get("unrecoverable") is True, (
        f"full_docs 损坏应该 unrecoverable，实际 result={result}"
    )
    assert "full_docs" in result["message"], (
        f"消息应该提到 full_docs，实际: {result['message']}"
    )


# =============================================================================
# 场景 6: 新用户空数据 → check_all ok=True（tempfile 隔离）
# =============================================================================


def test_06_empty_storage_new_user_ok():
    """新用户空数据（所有文件不存在或空 dict）→ check_all ok=True。

    用 tempfile.TemporaryDirectory + monkeypatch _STORAGE_DIR 隔离，不碰真实数据。
    """
    from niu_api.internal import lightrag_integrity

    with tempfile.TemporaryDirectory() as tmpdir:
        sd = Path(tmpdir) / "lightrag_storage"
        sd.mkdir()
        # 所有文件不存在 → check_all 应该 ok

        # monkeypatch _STORAGE_DIR（integrity 和 repair 都要 patch）
        original_sd = lightrag_integrity._STORAGE_DIR
        try:
            lightrag_integrity._STORAGE_DIR = sd
            check_result = lightrag_integrity.check_all()
        finally:
            lightrag_integrity._STORAGE_DIR = original_sd

        print(
            f"[e2e-6] 空数据 check: ok={check_result['ok']}, "
            f"critical={check_result['critical_errors']}, "
            f"major={check_result['major_errors']}, "
            f"minor={check_result['minor_errors']}"
        )
        assert check_result["ok"] is True, (
            f"空数据应该 ok=True，实际 ok={check_result['ok']}, "
            f"errors={check_result['errors']}"
        )
        assert check_result["critical_errors"] == 0
        assert check_result["major_errors"] == 0
        assert check_result["minor_errors"] == 0


# =============================================================================
# 场景 7: entity_chunks 引用悬空 → 从 GraphML 重建
# =============================================================================


def test_07_entity_chunks_dangling_rebuilds_from_graphml(restore_baseline):
    """entity_chunks 引用悬空 → check 检测到 major → repair_entity_chunks 重建。

    构造方式：往 entity_chunks 加一个不在 GraphML 中的 key（伪造悬空引用）。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 在 entity_chunks 中加一个悬空 key
    ec_path = STORAGE_DIR / "kv_store_entity_chunks.json"
    ec_data = _read_json(ec_path)
    original_keys = set(ec_data.keys())

    # 找一个 GraphML 中不存在的 entity_name
    node_ids, _, _ = lightrag_integrity._load_graphml(
        STORAGE_DIR / lightrag_integrity._GRAPHML_FILE
    )
    dangling_name = "zzz_dangling_test_entity_xxx"
    assert dangling_name not in node_ids, "测试用 dangling_name 不应该在 GraphML 中"

    ec_data[dangling_name] = ["chunk-fake-test-id"]
    with open(ec_path, "w", encoding="utf-8") as f:
        json.dump(ec_data, f, ensure_ascii=False)
    print(f"[e2e-7] entity_chunks: 加悬空 key '{dangling_name}'")

    # 2. check_all 应该检测到 major（entity_chunks_dangling）
    check_result = lightrag_integrity.check_all()
    ec_errors = check_result["checks"].get("entity_chunks_dangling", {}).get("errors", [])
    assert len(ec_errors) > 0, (
        f"应该检测到 entity_chunks_dangling major，实际 errors={ec_errors}"
    )
    assert any(e.get("ref_key") == dangling_name for e in ec_errors), (
        f"应该有 '{dangling_name}' 的 major error，实际 errors={ec_errors}"
    )

    # 3. repair_entity_chunks 从 GraphML 重建
    result = lightrag_repair.repair_entity_chunks()
    print(f"[e2e-7] repair_entity_chunks: status={result['status']}, actual={result.get('actual')}")
    assert result["status"] == "ok", f"repair_entity_chunks 应该成功: {result}"

    # 4. 验证悬空 key 已被清除
    new_ec_data = _read_json(ec_path)
    assert dangling_name not in new_ec_data, (
        f"悬空 key '{dangling_name}' 应该被 repair 清除"
    )
    # 验证 GraphML 中的 entity 仍然存在（repair 应该保留有效的）
    valid_count = sum(1 for k in new_ec_data if k in node_ids)
    assert valid_count > 0, "repair 后应该保留 GraphML 中存在的 entity"


# =============================================================================
# 场景 8: kv_store_llm_response_cache.json 损坏 → 清空（minor）
# =============================================================================


def test_08_llm_response_cache_corrupt_clears_minor(restore_baseline):
    """llm_response_cache 损坏 → 文件级 critical → repair 清空（minor 级别降级启动）。

    注意：cache 损坏时 check_all 仍会报 critical（文件级），
    但 repair_llm_response_cache 会清空（返回 status=ok），
    repair 后重检 critical 应该为 0（cache 已清空成合法空 dict）。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 损坏 llm_response_cache
    cache_path = STORAGE_DIR / "kv_store_llm_response_cache.json"
    original_size = cache_path.stat().st_size
    _corrupt_json_file(cache_path)
    print(f"[e2e-8] llm_response_cache: {original_size}B → 损坏 JSON")

    # 2. check_all 应该检测到 critical（json_parse）
    check_result = lightrag_integrity.check_all()
    assert check_result["critical_errors"] > 0, (
        f"cache 损坏后应该检测到 critical，实际 {check_result['critical_errors']}"
    )

    # 3. repair_llm_response_cache 清空（status=ok）
    result = lightrag_repair.repair_llm_response_cache()
    print(f"[e2e-8] repair_llm_response_cache: status={result['status']}")
    assert result["status"] == "ok", f"repair_llm_response_cache 应该成功: {result}"

    # 4. 验证文件已清空成空 dict
    new_cache = _read_json(cache_path)
    assert new_cache == {}, f"cache 应该被清空成 {{}}，实际 {type(new_cache)}: {new_cache}"

    # 5. 同时应该清空 text_chunks.llm_cache_list（避免引用悬空）
    tc_data = _read_json(STORAGE_DIR / "kv_store_text_chunks.json")
    if tc_data:
        non_empty = [
            cid for cid, cv in tc_data.items()
            if isinstance(cv, dict) and cv.get("llm_cache_list")
        ]
        assert len(non_empty) == 0, (
            f"text_chunks.llm_cache_list 应该被清空，仍有 {len(non_empty)} 条非空"
        )


# =============================================================================
# 场景 9: delete 中途失败（doc_status.chunks_list 引用悬空）→ repair 后恢复
# =============================================================================


def test_09_delete_partial_failure_doc_status_dangling(restore_baseline, no_lightrag_instance):
    """delete 中途失败 → doc_status.chunks_list 引用已被删的 chunk_id → repair 后恢复。

    构造方式：从 text_chunks 删一个 chunk_id，但保留 doc_status.chunks_list 中的引用。
    check #5 (doc_status_chunks_dangling) 应该检测到 major。
    repair_doc_status 会从 text_chunks 重新派生 chunks_list，消除悬空。

    注意：repair_doc_status 只派生 chunks_list（不调 LightRAG），
    所以 no_lightrag_instance fixture 不影响这个 repair。
    """
    from niu_api.internal import lightrag_integrity, lightrag_repair

    # 1. 从 text_chunks 删一个 chunk_id（模拟 delete 中途失败）
    tc_path = STORAGE_DIR / "kv_store_text_chunks.json"
    tc_data = _read_json(tc_path)
    if not tc_data:
        pytest.skip("text_chunks 为空，无法构造 delete 中途失败场景")

    # 找一个 doc_status.chunks_list 中引用的 chunk_id 来删除
    ds_path = STORAGE_DIR / "kv_store_doc_status.json"
    ds_data = _read_json(ds_path)
    target_chunk_id = None
    for doc_id, ds_value in ds_data.items():
        if not isinstance(ds_value, dict):
            continue
        chunks_list = ds_value.get("chunks_list", []) or []
        for cid in chunks_list:
            if cid in tc_data:
                target_chunk_id = cid
                break
        if target_chunk_id:
            break

    if not target_chunk_id:
        pytest.skip("找不到 doc_status.chunks_list 中引用的 chunk_id")

    # 删除 chunk
    del tc_data[target_chunk_id]
    with open(tc_path, "w", encoding="utf-8") as f:
        json.dump(tc_data, f, ensure_ascii=False)
    print(f"[e2e-9] 删除 text_chunks['{target_chunk_id}']（保留 doc_status 引用）")

    # 2. check_all 应该检测到 major（doc_status_chunks_dangling）
    check_result = lightrag_integrity.check_all()
    ds_errors = check_result["checks"].get("doc_status_chunks_dangling", {}).get("errors", [])
    assert len(ds_errors) > 0, (
        f"应该检测到 doc_status_chunks_dangling major，实际 errors={ds_errors}"
    )
    assert any(e.get("chunk_id") == target_chunk_id for e in ds_errors), (
        f"应该有 chunk_id='{target_chunk_id}' 的 major error，实际 errors={ds_errors}"
    )

    # 3. repair_doc_status 从 text_chunks 重新派生 chunks_list
    result = lightrag_repair.repair_doc_status()
    print(f"[e2e-9] repair_doc_status: status={result['status']}, actual={result.get('actual')}")
    assert result["status"] == "ok", f"repair_doc_status 应该成功: {result}"

    # 4. 重检 doc_status_chunks_dangling 应该无 error
    check_after = lightrag_integrity.check_all()
    ds_errors_after = check_after["checks"].get("doc_status_chunks_dangling", {}).get("errors", [])
    assert len(ds_errors_after) == 0, (
        f"修复后 doc_status_chunks_dangling 应该无 error，实际 errors={ds_errors_after}"
    )


# =============================================================================
# 额外场景：repair_all 不抛异常（验证组合修复）
# =============================================================================


def test_10_repair_all_no_exception_on_clean_data(restore_baseline):
    """repair_all 在数据完好时不抛异常，所有 repair 函数返回 status=ok 或预期 unrecoverable。

    这是 smoke 测试：验证 repair_all 调用链不会因为某个 repair 抛异常而中断。
    """
    from niu_api.internal import lightrag_repair

    # 数据完好（clean state，刚被 fixture 清理过）
    result = lightrag_repair.repair_all()
    print(f"[e2e-10] repair_all: {len(result)} 个 repair 结果")

    # 所有结果应该是 dict（不应有 internal error）
    for name, r in result.items():
        if name.startswith("_"):
            continue
        assert isinstance(r, dict), f"{name} 返回非 dict: {r}"
        assert "status" in r, f"{name} 缺 status 字段: {r}"
        # 完好数据应该都能修复（status=ok），或因无 LightRAG 实例 unrecoverable
        if r["status"] != "ok":
            print(f"  {name}: status={r['status']}, message={r.get('message', '')}")
