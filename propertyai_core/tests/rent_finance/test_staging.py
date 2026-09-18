from __future__ import annotations
from hashlib import sha256
import json,os,shutil,tempfile
from pathlib import Path
import pytest
from propertyai_core.tests.rent_finance_staging import checked_bytes,canonical,validate_staging_manifest,RentPrerequisiteError
from propertyai_core.tests.rent_finance_test_cluster import verify_flyway_provenance

def binding():
    path=Path(os.environ["PROPERTYAI_RENT_TEST_BINDING_FILE"])
    raw=checked_bytes(path,os.environ["PROPERTYAI_RENT_TEST_BINDING_SHA256"])
    return json.loads(raw)

def test_exact_input_manifest():
    value=binding()
    assert {x["scenario"] for x in value["staging"]}=={"FRESH","UPGRADE_108"}
    for record in value["staging"]:
        manifest=validate_staging_manifest(Path(record["path"]),record["sha256"])
        assert manifest["scenario"]==record["scenario"]
        assert len(manifest["ordered_staged_files"])==14
        assert [x["version"] for x in manifest["ordered_inputs"]][-4:]==[
            "20260904.108","20260904.109","20260904.110","20260904.111"]
        assert len(set(x["staged_relative_path"] for x in manifest["ordered_staged_files"]))==14

def test_flyway_provenance_required(monkeypatch):
    value=binding()["provenance"]
    receipt=verify_flyway_provenance(Path(value["path"]),value["sha256"])
    assert receipt["P1_FLYWAY_PROVENANCE"]=="PASS"
    monkeypatch.delenv("PROPERTYAI_FLYWAY_BIN",raising=False)
    with pytest.raises(RentPrerequisiteError,match="PROCESS_SCOPED_FLYWAY_BIN_NOT_BOUND"):
        verify_flyway_provenance(Path(value["path"]),value["sha256"])

def _copy_stage(record):
    root=Path(tempfile.mkdtemp(prefix="rent-stage-negative-",dir=os.environ["PROPERTYAI_RENT_TEST_EVIDENCE_ROOT"]))
    src=Path(record["path"]).parent
    dst=root/"stage"
    shutil.copytree(src,dst)
    return root,dst,dst/"finance_test_staging_manifest.json"

def test_no_cleaner_profile_substitution():
    record=next(x for x in binding()["staging"] if x["scenario"]=="FRESH")
    root,dst,path=_copy_stage(record)
    try:
        value=json.loads(path.read_bytes())
        value["profile_id"]="cleaner.finance.g1.p1"
        raw=canonical(value)
        path.chmod(0o600);path.write_bytes(raw)
        with pytest.raises(RentPrerequisiteError,match="CLEANER_OR_UNKNOWN_PROFILE_SUBSTITUTION"):
            validate_staging_manifest(path,sha256(raw).hexdigest())
    finally:shutil.rmtree(root)

def test_reject_missing_extra_duplicate_or_changed_migration():
    record=next(x for x in binding()["staging"] if x["scenario"]=="FRESH")
    for mutation in ("missing","extra","duplicate","changed"):
        root,dst,path=_copy_stage(record)
        try:
            value=json.loads(path.read_bytes())
            target=dst/value["ordered_staged_files"][12]["staged_relative_path"]
            if mutation=="missing":target.unlink()
            elif mutation=="extra":(dst/"db/v2_2_1/migration/V20260904.112__unapproved.sql").write_text("SELECT 1;")
            elif mutation=="changed":
                target.chmod(0o600);target.write_bytes(target.read_bytes()+b"\n-- drift\n")
            else:
                value["ordered_staged_files"][11]["staged_relative_path"]=value["ordered_staged_files"][12]["staged_relative_path"]
                raw=canonical(value);path.chmod(0o600);path.write_bytes(raw)
            expected_sha=sha256(raw).hexdigest() if mutation=="duplicate" else record["sha256"]
            with pytest.raises(RentPrerequisiteError):
                validate_staging_manifest(path,expected_sha)
        finally:shutil.rmtree(root)
