"""Probe: verify SkillSync ainsert_custom_kg does NOT overwrite repair-rebuilt vdb.

Tests the user's premise: "SkillSync ainsert_custom_kg covers vdb 2211→6".

Uses tmp_path isolation (no real ~/.niu/lightrag_storage mutation).
Uses real embedding model + real LightRAG instance.
Reuses the proven fixture from test_lightrag_rebuild_from_truth.py.
"""
import json
import sys
from pathlib import Path

import pytest

from niu_api.internal.lightrag_repair import repair_all
# Import the proven fixture builder
sys.path.insert(0, str(Path(__file__).parent))
from test_lightrag_rebuild_from_truth import _make_synthetic_fixture


def test_probe_skillsync_does_not_overwrite_vdb(tmp_path, monkeypatch):
    """Probe: run repair_all → get_lightrag re-init → SkillSync-style ainsert_custom_kg
    → check vdb_entities still has the repair-rebuilt entities (not overwritten).

    This directly tests the premise "SkillSync covers vdb 2211→6".
    """
    _make_synthetic_fixture(tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)

    from niu_api.internal.embedding import get_model
    assert get_model() is not None

    # Step 1: repair_all rebuilds vdb_entities from GraphML (2 entities: entity-a, entity-b)
    result = repair_all()
    assert not result.get("_unrecoverable"), f"repair failed: {result.get('_unrecoverable_reason')}"

    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    repair_count = len(vdb_e.get("data", []))
    print(f"\n[PROBE] After repair_all: vdb_entities has {repair_count} entries")
    assert repair_count == 2, f"repair should rebuild 2 entities, got {repair_count}"

    # Step 2: simulate get_lightrag re-init (the run_repair_on_user_request L1307 path)
    import niu_api.internal.lightrag_manager as lm
    monkeypatch.setattr(lm, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(lm, "_rag_instance", None)
    monkeypatch.setattr(lm, "_repairing", False)
    monkeypatch.setattr(lm, "_init_failed_at", None)
    monkeypatch.setattr(lm, "_integrity_result", None)

    rag = lm.get_lightrag()
    assert rag is not None, "LightRAG should initialize after repair"

    # Check in-memory vdb count right after init (should be 2, loaded from disk)
    in_mem_count_after_init = len(rag.entities_vdb._client)
    print(f"[PROBE] After get_lightrag init: in-memory vdb has {in_mem_count_after_init} entries")
    assert in_mem_count_after_init == 2, (
        f"After re-init, in-memory vdb should have 2 (loaded from disk), got {in_mem_count_after_init}"
    )

    # Step 3: simulate SkillSync scan_and_sync injecting 1 skill via ainsert_custom_kg.
    custom_kg = {
        "chunks": [{
            "content": "Skill: test-skill\n\ntest skill content",
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
        "relationships": [{
            "src_id": "知识体系脑区",
            "tgt_id": "test-skill",
            "keywords": "belongs_to",
            "description": "test-skill belongs to knowledge system",
            "source_id": "skill://test-skill",
            "file_path": "custom_kg",
            "weight": 1.0,
        }],
    }

    from niu_api.internal.lightrag_manager import call_async
    call_async(rag.ainsert_custom_kg(custom_kg), timeout=120)

    # Step 4: check in-memory vdb count after ainsert_custom_kg
    in_mem_count_after_inject = len(rag.entities_vdb._client)
    print(f"[PROBE] After ainsert_custom_kg (1 skill): in-memory vdb has {in_mem_count_after_inject} entries")

    # Step 5: trigger _insert_done to persist to disk
    call_async(rag._insert_done(), timeout=120)

    # Step 6: read disk vdb_entities.json — should be 2 (repair) + 1 (skill) = 3,
    # NOT 1 (just the skill).
    vdb_e_after = json.loads((tmp_path / "vdb_entities.json").read_text())
    disk_count = len(vdb_e_after.get("data", []))
    print(f"[PROBE] After _insert_done: disk vdb_entities has {disk_count} entries")

    entity_names = {e.get("entity_name") for e in vdb_e_after.get("data", [])}
    print(f"[PROBE] entity_names on disk: {entity_names}")

    # The premise was "vdb 2211→6" (overwrite). If ainsert_custom_kg is true upsert,
    # we should see all 3 entities (entity-a, entity-b, test-skill), NOT just test-skill.
    assert "entity-a" in entity_names, "entity-a (from repair) must survive SkillSync inject"
    assert "entity-b" in entity_names, "entity-b (from repair) must survive SkillSync inject"
    assert "test-skill" in entity_names, "test-skill (newly injected) must be present"
    assert disk_count == 3, (
        f"Disk vdb should have 3 entries (2 from repair + 1 skill), got {disk_count}. "
        f"If this fails with disk_count=1, SkillSync IS overwriting vdb (premise correct). "
        f"If this passes, the premise 'SkillSync covers vdb 2211→6' is WRONG."
    )
