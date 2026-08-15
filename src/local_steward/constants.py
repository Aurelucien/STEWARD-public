"""Frozen protocol constants."""

from pathlib import Path

SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "steward.toml"
SYSTEM_PROTECTED_PATHS = tuple(
    Path(value)
    for value in (
        "/",
        "/System",
        "/Library",
        "/Applications",
        "/bin",
        "/sbin",
        "/usr",
        "/private",
        "/Volumes",
    )
)
EXIT_SUCCESS = 0
EXIT_FINDINGS = 1
EXIT_CONFIGURATION = 2
EXIT_CAPABILITY = 3
EXIT_INCOMPLETE = 4
EXIT_PLAN_STALE = 5
EXIT_PARTIAL = 6
EXIT_POST_VERIFY = 7
EXIT_INTERNAL = 8
EXIT_CANCELLED = 9
