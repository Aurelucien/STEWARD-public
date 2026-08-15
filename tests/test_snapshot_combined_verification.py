from local_steward.snapshots import verify_snapshot

from .test_snapshot_queries import snapshot_fixture


def test_combined_snapshot_verification_is_valid(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    result = verify_snapshot(config, snapshot.snapshot_id)
    assert result.status == "VALID" and result.evidence_valid and result.run_consistent


def test_combined_verification_detects_incomplete_entry_index(tmp_path) -> None:
    config, snapshot = snapshot_fixture(tmp_path)
    connection = __import__("sqlite3").connect(config.paths.data_dir / "state.db")
    connection.execute("DELETE FROM snapshot_entries WHERE snapshot_id=?", (snapshot.snapshot_id,))
    connection.commit()
    connection.close()
    result = verify_snapshot(config, snapshot.snapshot_id)
    assert result.status == "INCOMPLETE" and not result.entry_count_consistent
