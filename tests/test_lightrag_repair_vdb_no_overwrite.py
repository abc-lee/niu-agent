"""Probe 2: verify the fix for SkillSync vdb overwrite root cause.

Root cause (discovered): get_lightrag_for_repair() inside repair_all creates a
LightRAG instance and caches it to _rag_instance. That instance's NanoVectorDB
client loads from (empty) disk BEFORE repair rebuilds vdb files. After repair
writes 2 entities to disk, the in-memory client is still empty. When
run_repair_on_user_request calls get_lightrag(), it fast-paths the stale instance.
SkillSync's ainsert_custom_kg upserts to the empty in-memory client, and
_insert_done overwrites disk to just the skill entries — destroying the 2
repair-rebuilt entities. This is the actual "vdb 2211→6" bug.

Fix: run_repair_on_user_request now sets _rag_instance=None before get_lightrag()
after repair, forcing re-creation from the repair-rebuilt disk files.

This probe verifies: after repair_all + _rag_instance=None + get_lightrag(),
the in-memory vdb has the repair-rebuilt entities (NOT empty/stale).
"""
import pytest

pytest.skip("v8-Task 1 删除 get_lightrag_for_repair + repair_text_chunks 改 stub，依赖 stale instance 的 probe 需等 Task 4/9 重写", allow_module_level=True)

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from niu_api.internal.lightrag_repair import repair_all  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_lightrag_rebuild_from_truth import _make_synthetic_fixture  # noqa: E402


def test_probe_full_run_repair_preserves_vdb(tmp_path, monkeypatch):
    """Probe 2: verify fix — get_lightrag after repair loads repair-rebuilt vdb.

    Without the fix: in-memory vdb = 0 (stale instance from get_lightrag_for_repair).
    With the fix: in-memory vdb = 2 (fresh instance loaded from repair-rebuilt disk).
    """
    _make_synthetic_fixture(tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.embedding import get_model
    assert get_model() is not None

    import niu_api.internal.lightrag_manager as lightrag_manager
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)

    # Step 1: Run repair_all (this internally calls get_lightrag_for_repair which
    # caches a stale instance with empty in-memory vdb).
    result_repair = repair_all()
    assert not result_repair.get("_unrecoverable"), f"repair failed: {result_repair.get('_unrecoverable_reason')}"

    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    repair_count = len(vdb_e.get("data", []))
    print(f"\n[PROBE2] After repair_all: disk vdb_entities has {repair_count} entries")
    assert repair_count == 2, f"repair should rebuild 2 entities, got {repair_count}"

    # Step 2: WITHOUT the fix — verify the stale instance has empty in-memory vdb
    stale_rag = lightrag_manager._rag_instance
    assert stale_rag is not None, "repair_all should have cached a stale instance via get_lightrag_for_repair"
    stale_count = len(stale_rag.entities_vdb._client)
    print(f"[PROBE2] Stale instance (created during repair) in-memory vdb: {stale_count} entries")
    # This documents the bug: stale instance has 0 in-memory while disk has 2
    assert stale_count == 0, (
        f"Expected stale instance to have 0 in-memory (loaded before repair wrote disk), got {stale_count}"
    )

    # Step 3: WITH the fix — discard stale instance + get_lightrag re-creates from disk
    lightrag_manager._rag_instance = None  # THE FIX
    rag = lightrag_manager.get_lightrag()
    assert rag is not None, "LightRAG should init after repair"

    in_mem_after_init = len(rag.entities_vdb._client)
    print(f"[PROBE2] After fix (discard stale + re-init): in-memory vdb has {in_mem_after_init} entries")
    assert in_mem_after_init == 2, (
        f"After fix, in-memory vdb should have 2 (loaded from repair-rebuilt disk), got {in_mem_after_init}. "
        f"Without the fix, this would be 0 — causing SkillSync _insert_done to overwrite disk vdb to just skills."
    )

    # Step 4: Simulate SkillSync ainsert_custom_kg on the fresh instance.
    # With the fix, this upserts 1 skill into a 2-entity vdb → 3 entities on disk.
    # Without the fix, this would upsert 1 skill into a 0-entity vdb → 1 entity on disk (data loss).
    custom_kg = {
        "chunks": [{
            "content": "Skill: test-skill",
            "source_id": "skill://test-skill",
            "file_path": "custom_kg",
            "chunk_order_index": 0,
        }],
        "entities": [{
            "entity_name": "test-skill",
            "entity_type": "Skill",
            "description": "test skill description",
            "source_id": "skill://test-skill_test-skill",
            "file_path": "custom_kg",
        }],
        "relationships": [],
    }
    from niu_api.internal.lightrag_manager import call_async
    call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)
    call_async(rag._insert_done(), timeout=120)

    # Step 5: Read disk vdb_entities — should be 3 (2 repair + 1 skill), NOT 1.
    vdb_e_after = json.loads((tmp_path / "vdb_entities.json").read_text())
    disk_count = len(vdb_e_after.get("data", []))
    print(f"[PROBE2] After SkillSync inject + _insert_done: disk vdb_entities has {disk_count} entries")

    entity_names = {e.get("entity_name") for e in vdb_e_after.get("data", [])}
    print(f"[PROBE2] entity_names on disk: {entity_names}")

    assert "entity-a" in entity_names, "entity-a (from repair) must survive SkillSync (fix works)"
    assert "entity-b" in entity_names, "entity-b (from repair) must survive SkillSync (fix works)"
    assert "test-skill" in entity_names, "test-skill (newly injected) must be present"
    assert disk_count == 3, (
        f"Disk vdb should have 3 entries (2 from repair + 1 skill), got {disk_count}. "
        f"This confirms the fix: SkillSync no longer overwrites repair-rebuilt vdb."
    )
