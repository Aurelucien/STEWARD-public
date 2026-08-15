from local_steward.evidence import digest
from local_steward.snapshots import get_snapshot, validate_snapshot_evidence

from .test_snapshot_queries import snapshot_fixture


def evidence_for(config, snapshot):
    import json

    stored = get_snapshot(config, snapshot.snapshot_id)
    return json.loads((config.paths.evidence_dir / str(stored.evidence_relative_path)).read_text())


def test_intrinsic_snapshot_evidence_is_valid(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    result = validate_snapshot_evidence(evidence_for(config, snapshot))
    assert result.valid and result.snapshot_id == snapshot.snapshot_id


def test_intrinsic_validation_finds_entry_identity_and_digest_tampering(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    value = evidence_for(config, snapshot)
    value["payload"]["entries"][0]["entry_id"] = "0" * 64
    value["evidence_digest"] = digest(value)
    result = validate_snapshot_evidence(value)
    assert not result.valid and not result.entry_ids_valid


def test_intrinsic_validation_finds_bad_path_and_order(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    value = evidence_for(config, snapshot)
    value["payload"]["entries"].reverse()
    value["evidence_digest"] = digest(value)
    result = validate_snapshot_evidence(value)
    assert not result.valid and not result.entry_order_valid


def test_v1_evidence_missing_schema_version_is_stably_rejected(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    value = evidence_for(config, snapshot)
    value.pop("schema_version")
    value["evidence_digest"] = digest(value)

    result = validate_snapshot_evidence(value)

    assert not result.valid
    assert result.errors == (
        {
            "code": "EVIDENCE_SCHEMA_VERSION_INVALID",
            "message": "invalid or missing schema_version",
        },
    )
