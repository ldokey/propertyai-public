from __future__ import annotations
import json
from pathlib import Path
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture

def test_failure_and_normal_cleanup():
    normal=None
    with operator_fixture() as (normal,service,_):
        root=normal.cluster.root
        assert root.exists()
        assert service.reference_data()["units"]
    assert normal.cleanup_report["classification"]=="PASS"
    assert normal.cleanup_report["root_absent"] and not root.exists()
    bad=None
    try:
        with operator_fixture() as (bad,service,_):
            bad_root=bad.cluster.root
            assert bad_root.exists()
            raise RuntimeError("SYNTHETIC_TEST_FAILURE")
    except RuntimeError as exc:
        assert str(exc)=="SYNTHETIC_TEST_FAILURE"
    assert bad is not None and bad.cleanup_report["classification"]=="PASS"
    assert bad.cleanup_report["root_absent"] and not bad_root.exists()
    for fixture in (normal,bad):
        terminal=json.loads((fixture.evidence_root/"terminal.json").read_bytes())
        assert terminal["production_effect"] is False
        assert terminal["cleanup"]["classification"]=="PASS"
        assert terminal["owned_fixture_root"]==str(fixture.cluster.root)
