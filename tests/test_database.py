from pathlib import Path

from local_steward.database import connect, initialize, validate_schema


def test_schema_initialization_has_required_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    initialize(path, "test", "2026-01-01T00:00:00.000000Z")
    connection = connect(path)
    try:
        validate_schema(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()
