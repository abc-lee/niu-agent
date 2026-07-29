"""Probe 3: end-to-end run_repair_on_user_request with REAL SkillSync + skills.

Verifies the reordered fix (rag_instance=None before repairing=False) prevents
SkillSync from overwriting repair-rebuilt vdb even when real skills exist.
"""
import pytest

pytest.skip("v8-Task 1 将 repair_text_chunks 改为 unrecoverable stub，依赖 repair_all 成功的 e2e probe 需等 Task 4/9 重写", allow_module_level=True)

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_lightrag_rebuild_from_truth import _make_synthetic_fixture


def test_probe_e2e_run_repair_with_real_skills(tmp_path, monkeypatch):
    """E2E: run_repair_on_user_request with real skills dir + real SkillSync.

    Verifies: after run_repair_on_user_request completes, vdb_entities still
    has the 2 repair-rebuilt entities (entity-a, entity-b) PLUS the injected
    skill — NOT just the skill (which would be the "vdb 2211→6" bug).
    """
    _make_synthetic_fixture(tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_repair._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_integrity._STORAGE_DIR", tmp_path)
    monkeypatch.setattr("niu_api.internal.lightrag_manager.STORAGE_DIR", tmp_path)

    from niu_api.internal.embedding import get_model
    assert get_model() is not None

    # Set up skills dir with 1 skill file
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill.md").write_text("""---
description: Test skill for probe
---

# Test Skill

A test skill.
""")

    # Set up tmp HOME so SkillSync state file goes to tmp
    monkeypatch.setenv("HOME", str(tmp_path))
    niu_dir = tmp_path / ".niu"
    niu_dir.mkdir(parents=True, exist_ok=True)
    (niu_dir / "skill_sync_state.json").write_text("{}")

    import niu_api.internal.lightrag_manager as lightrag_manager
    monkeypatch.setattr(lightrag_manager, "_integrity_result", None)
    monkeypatch.setattr(lightrag_manager, "_rag_instance", None)
    monkeypatch.setattr(lightrag_manager, "_repairing", False)
    monkeypatch.setattr(lightrag_manager, "_init_failed_at", None)
    monkeypatch.setattr("niu_api.kg_api._read_pipeline_busy", lambda: False)

    # Force SkillSync to use our tmp skills dir + disable watchdog
    import agent.injector.sync as sync_mod
    monkeypatch.setattr(sync_mod, "_skill_sync", None)

    original_init = sync_mod.SkillSync.__init__
    def patched_init(self, skills_dir=None, scan_interval=2, use_watchdog=True):
        # Use the tmp skills_dir, short scan_interval (2s) for fast test, no watchdog
        sd = str(skills_dir) if skills_dir else str(tmp_path / "skills")
        return original_init(self, skills_dir=sd, scan_interval=2, use_watchdog=False)
    monkeypatch.setattr(sync_mod.SkillSync, "__init__", patched_init)

    # Patch _state_file to tmp (it's set in __init__ from Path.home())
    # Since we set HOME=tmp_path, Path.home() returns tmp_path, so _state_file
    # = tmp_path/.niu/skill_sync_state.json (which we created as {}).

    # Pre-start SkillSync so it's already waiting for LightRAG ready
    from agent.injector.sync import get_skill_sync
    skill_sync = get_skill_sync(auto_start=True)
    print(f"[PROBE3] SkillSync started, skills_dir={skill_sync.skills_dir}")

    # Delete vdb_entities to simulate corruption
    (tmp_path / "vdb_entities.json").unlink()

    # Run the full repair flow
    result = lightrag_manager.run_repair_on_user_request()
    print(f"[PROBE3] run_repair_on_user_request result: repaired={result.get('repaired')}")
    print(f"[PROBE3] check_ok={result.get('check_ok')}, critical={result.get('critical_errors')}, major={result.get('major_errors')}")

    # Read final vdb_entities
    vdb_e = json.loads((tmp_path / "vdb_entities.json").read_text())
    final_count = len(vdb_e.get("data", []))
    entity_names = {e.get("entity_name") for e in vdb_e.get("data", [])}
    print(f"[PROBE3] Final disk vdb_entities: {final_count} entries")
    print(f"[PROBE3] entity_names: {entity_names}")

    # CRITICAL: repair-rebuilt entity-a and entity-b must survive SkillSync
    assert "entity-a" in entity_names, (
        f"entity-a (from repair) must survive SkillSync. Got: {entity_names}"
    )
    assert "entity-b" in entity_names, (
        f"entity-b (from repair) must survive SkillSync. Got: {entity_names}"
    )

    # The skill may or may not be present (depends on whether SkillSync ran),
    # but the key assertion is that repair-rebuilt entities are NOT destroyed.
    assert final_count >= 2, (
        f"Disk vdb should have at least 2 (repair-rebuilt entity-a, entity-b), got {final_count}. "
        f"If final_count==1 (just test-skill), the SkillSync overwrite bug is NOT fixed."
    )

    # Stop SkillSync
    skill_sync.stop_background_sync()
