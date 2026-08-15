from pathlib import Path


def write_config(root: Path, text: str | None = None) -> Path:
    config = root / "config" / "steward.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        text
        or """schema_version = 1
project_name = "Test"
[paths]
data_dir = "data"
cache_dir = "data/cache"
evidence_dir = "data/evidence"
quarantine_dir = "data/quarantine"
[[scopes]]
scope_id = "managed"
role = "managed_root"
path = "~/managed-test"
enabled = true
follow_directory_symlinks = false
allow_cross_mount = false
""",
        encoding="utf-8",
    )
    return config
