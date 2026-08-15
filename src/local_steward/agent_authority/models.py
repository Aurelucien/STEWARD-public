"""Immutable host-owned authority models for one STEWARD task."""

from dataclasses import dataclass
from enum import Enum


AUTHORITY_SCHEMA_NAME = "local_steward.authority_context"
AUTHORITY_SCHEMA_VERSION = 1


class RiskClass(str, Enum):
    HISTORICAL_READ = "HISTORICAL_READ"
    CURRENT_CONTENT_READ = "CURRENT_CONTENT_READ"
    CODE_WORKSPACE_READ = "CODE_WORKSPACE_READ"
    DERIVED_STATE_APPEND = "DERIVED_STATE_APPEND"
    RECOVERY_OR_ADMIN = "RECOVERY_OR_ADMIN"
    EXTERNAL_DISCLOSURE = "EXTERNAL_DISCLOSURE"
    USER_FILE_MUTATION = "USER_FILE_MUTATION"


class AuthoritySource(str, Enum):
    EXPLICIT_REQUEST = "EXPLICIT_REQUEST"
    HOST_APPROVAL = "HOST_APPROVAL"
    INHERITED_SAME_TASK = "INHERITED_SAME_TASK"


@dataclass(frozen=True, slots=True)
class AuthorityPath:
    scope_id: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class AuthorityAdmission:
    risk_class: RiskClass
    operations: tuple[str, ...]
    scope_ids: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    paths: tuple[AuthorityPath, ...] = ()
    external_destinations: tuple[str, ...] = ()
    user_file_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorityContext:
    schema_name: str
    schema_version: int
    authority_domain_digest: str
    task_identity_digest: str
    source: AuthoritySource
    admissions: tuple[AuthorityAdmission, ...]
    context_digest: str
