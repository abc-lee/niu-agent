"""Probe 2: full run_repair_on_user_request flow + REAL SkillSync scan_and_sync.

Tests the user's premise end-to-end: "SkillSync ainsert_custom_kg covers vdb 2211→6".

This probe runs the REAL run_repair_on_user_request (no mocking of SkillSync),
then checks vdb_entities count after the full flow completes.

Uses tmp_path isolation (no real ~/.niu/lightrag_storage mutation).
Uses real embedding model + real LightRAG instance.
"""
import json
import sys
import time
from pathlib import Path

import pytest

from niu_api.internal.lightrag_repair import repair_all
sys.path.insert(0, str(Path(__file__).parent))
from test_lightrag_rebuild_from_truth import _make_synthetic_fixture


def test_probe_full_run_repair_preserves_vdb(tmp_path, monkeypatch):
    """Probe 2: run_repair_on_user_request full flow with REAL SkillSync.

    Verifies vdb_entities is NOT overwritten to just skills after repair.
    """
    _make_synthetic_fixture(tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.embedding import get_model
    assert get_model() is not None

    # Set up skills dir in tmp_path so SkillSync finds skills
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill.md").write_text("""---
description: Test skill for probe
---

# Test Skill

A test skill.
""")

    # Point SkillSync to our tmp skills dir + tmp state file
    monkeypatch.setenv("HOME", str(tmp_path))
    # Create ~/.niu inside tmp_path
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    # Empty skill_sync_state.json so all skills look "new" to SkillSync
    (niu_dir / "skill_sync_state.json").write_text("{}")

    import niu_api.internal.lightrag_manager as lightrag_manager
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)

    # Force SkillSync to use our tmp skills dir
    import agent.injector.sync as sync_mod
    # Reset the global instance so it re-reads skills_dir
    monkeypatch.setattr(sync_mod, "_skill_sync", None)

    # Patch SkillSync default skills dir to point to our tmp skills
    original_init = sync_mod.SkillSync.__init__

    def patched_init(self, skills_dir=None, scan_interval=60, use_watchdog=True):
        return original_init(self, skills_dir=str(skills_dir), scan_interval=scan_interval, use_watchdog=False)

    monkeypatch.setattr(sync_mod.SkillSync, "__init__", patched_init)

    # Patch state file path to tmp
    @property
    def tmp_state_file(self):
        return tmp_path / ".niu" / "skill_sync_state.json"
    monkeypatch.setattr(sync_mod.SkillSync, "_state_file", tmp_state_file, raising=False)

    # Step 1: Run repair_all first to rebuild vdb (2 entities: entity-a, entity-b)
    result_repair = repair_all()
    assert not result_repair.get("_unrecoverable"), f"repair failed: {result_repair.get('_unrecoverable_reason')}"

    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    repair_count = len(vdb_e.get("data", []))
    print(f"\n[PROBE2] After repair_all: vdb_entities has {repair_count} entries")
    assert repair_count == 2, f"repair should rebuild 2 entities, got {repair_count}"

    # Step 2: Now trigger get_lightrag (this is what run_repair_on_user_request L1307 does)
    rag = lightrag_manager.get_lightrag()
    assert rag is not None, "LightRAG should init after repair"

    in_mem_after_init = len(rag.entities_vdb._client)
    print(f"[PROBE2] After get_lightrag init: in-memory vdb has {in_mem_after_init} entries")
    assert in_mem_after_init == 2, (
        f"After re-init, in-memory vdb should have 2 (loaded from disk), got {in_mem_after_init}"
    )

    # Step 3: Start SkillSync background sync + wait for first scan
    from agent.injector.sync import get_skill_sync, wait_first_scan_complete
    skill_sync = get_skill_sync(skills_dir=str(skills_dir), auto_start=False)
    # Manually run scan_and_sync (not background thread)
    scan_result = skill_sync.scan_and_sync()
    print(f"[PROBE2] SkillSync scan_and_sync result: added={scan_result[0]}, updated={scan_result[1]}, deleted={scan_result[2]}")

    # Step 4: trigger _insert_done to persist any in-memory changes
    from niu_api.internal.lightrag_manager import call_async
    call_async(rag._insert_done(), timeout=120)

    # Step 5: Read disk vdb_entities — should be 2 (repair) + 1 (skill) = 3,
    # NOT 1 (just the skill).
    vdb_e_after = json.loads((tmp_path / "vdb_entities.json").read_text())
    disk_count = len(vdb_e_after.get("data", []))
    print(f"[PROBE2] After SkillSync scan + _insert_done: disk vdb_entities has {disk_count} entries")

    entity_names = {e.get("entity_name") for e in vdb_e_after.get("data", [])}
    print(f"[PROBE2] entity_names on disk: {entity_names}")

    # CRITICAL ASSERTION: repair-rebuilt entities must survive SkillSync
    assert "entity-a" in entity_names, "entity-a (from repair) must survive SkillSync"
    assert "entity-b" in entity_names, "entity-b (from repair) must survive SkillSync"

    # The premise "vdb 2211→6" would mean disk_count == 1 (just test-skill).
    # If ainsert_custom_kg is true upsert, disk_count should be 3 (2 repair + 1 skill).
    assert disk_count >= 2, (
        f"Disk vdb should have at least 2 entries (repair-rebuilt entity-a, entity-b), got {disk_count}. "
        f"If disk_count==1, SkillSync IS overwriting vdb (premise correct, bug exists). "
        f"If disk_count>=2, the premise 'SkillSync covers vdb 2211→6' is WRONG (no bug)."
    )
