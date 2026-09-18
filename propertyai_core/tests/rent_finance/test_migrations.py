from pathlib import Path
import os
import json
from propertyai_core.tests.rent_finance_staging import checked_bytes, require
from propertyai_core.tests.rent_finance_test_cluster import start_disposable_rent_postgres


def _fixture(scenario):
    binding_path = os.environ.get("PROPERTYAI_RENT_TEST_BINDING_FILE")
    binding_sha = os.environ.get("PROPERTYAI_RENT_TEST_BINDING_SHA256")
    require(bool(binding_path and binding_sha), "RENT_TEST_BINDING_NOT_PROVIDED")
    binding = json.loads(checked_bytes(Path(binding_path), binding_sha))
    stage = next(x for x in binding["staging"] if x["scenario"] == scenario)
    provenance = binding["provenance"]
    return start_disposable_rent_postgres(Path(stage["path"]), stage["sha256"], Path(provenance["path"]), provenance["sha256"], scenario=scenario)


def test_fresh_001_002_101_to_111():
    with _fixture("FRESH") as fixture:
        history = fixture.history()
        assert [r["version"] for r in history if r["version"] is not None] == ["20260904." + str(v) for v in range(101, 112)]
        assert all(r["success"] for r in history)
        assert [o["phase"] for o in fixture.observations if o["phase"].startswith("BOOTSTRAP_")] == ["BOOTSTRAP_001", "BOOTSTRAP_002"]
        tables = {r[0] for r in fixture.introspection()["tables"]}
        assert {"rent_contract", "rent_resident", "finance_movement_revision", "finance_receivable"} <= tables
    assert fixture.cleanup_report["classification"] == "PASS"
    assert fixture.cleanup_report["root_absent"]


def test_upgrade_108_to_111():
    with _fixture("UPGRADE_108") as fixture:
        baseline = next(o["history"] for o in fixture.observations if o["phase"] == "UPGRADE_BASELINE_108")
        final = fixture.history()
        assert final[:len(baseline)] == baseline
        fixture.assert_baseline_history(final)
        assert [o["argv"][-2] for o in fixture.observations if o["phase"] == "FLYWAY" and o["argv"][-1] == "migrate"] == ["-target=20260904.108", "-target=20260904.111"]
    assert fixture.cleanup_report["classification"] == "PASS"
    assert fixture.cleanup_report["root_absent"]

def test_101_to_108_history_checksums_unchanged():
    with _fixture("UPGRADE_108") as fixture:
        before=next(o["history"] for o in fixture.observations if o["phase"]=="UPGRADE_BASELINE_108")
        after=fixture.history()
        assert [x["version"] for x in before if x["version"] is not None]==[
            "20260904."+str(v) for v in range(101,109)]
        assert after[:len(before)]==before
        assert {x["version"]:x["checksum"] for x in before if x["version"]}==fixture.manifest["baseline_history_checksums"]
        assert all(x["success"] for x in before)
    assert fixture.cleanup_report["classification"]=="PASS"
    assert fixture.cleanup_report["root_absent"]

def test_failure_history_matches_real_schema():
    fixture=None
    try:
        with _fixture("FRESH") as fixture:
            snapshot=fixture.introspection()
            versions=[x["version"] for x in snapshot["history"] if x["version"] is not None]
            assert versions==["20260904."+str(v) for v in range(101,112)]
            assert {"rent_contract","finance_receivable"}<={x[0] for x in snapshot["tables"]}
            raise RuntimeError("SYNTHETIC_FAILURE_AFTER_REAL_SCHEMA_READ")
    except RuntimeError as exc:
        assert str(exc)=="SYNTHETIC_FAILURE_AFTER_REAL_SCHEMA_READ"
    assert fixture is not None
    assert fixture.cleanup_report["classification"]=="PASS"
    assert fixture.cleanup_report["root_absent"]
    terminal=json.loads((fixture.evidence_root/"terminal.json").read_bytes())
    assert terminal["primary_error"]=="SYNTHETIC_FAILURE_AFTER_REAL_SCHEMA_READ"
    assert any(x["phase"]=="FAILURE_DATABASE_STATE" for x in terminal["observations"])
