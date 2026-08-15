"""Construction, loading and exact admission checks for host-owned authority."""

from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path
from typing import Any

from ..agent_session import StewardSession
from ..evidence import canonical_json
from .errors import StewardAuthorityContextError, StewardAuthorityRequiredError
from .models import (
    AUTHORITY_SCHEMA_NAME,
    AUTHORITY_SCHEMA_VERSION,
    AuthorityAdmission,
    AuthorityContext,
    AuthorityPath,
    AuthoritySource,
    RiskClass,
)


def _digest(value: dict[str, Any]) -> str:
    return sha256(canonical_json(value)).hexdigest()


def _admission_payload(admission: AuthorityAdmission) -> dict[str, Any]:
    return {
        "risk_class": admission.risk_class.value,
        "operations": list(admission.operations),
        "scope_ids": list(admission.scope_ids),
        "snapshot_ids": list(admission.snapshot_ids),
        "run_ids": list(admission.run_ids),
        "paths": [
            {"scope_id": item.scope_id, "relative_path": item.relative_path}
            for item in admission.paths
        ],
        "external_destinations": list(admission.external_destinations),
        "user_file_ids": list(admission.user_file_ids),
    }


def create_authority_context(
    session: StewardSession,
    *,
    task_identity: str,
    source: AuthoritySource,
    admissions: tuple[AuthorityAdmission, ...],
) -> AuthorityContext:
    """Create a context from host facts; tool arguments cannot call this constructor."""
    if not isinstance(session, StewardSession):
        raise StewardAuthorityContextError("authority requires one admitted STEWARD session")
    if (
        not isinstance(task_identity, str)
        or not task_identity
        or len(task_identity.encode("utf-8")) > 1024
        or any(ord(character) < 32 for character in task_identity)
    ):
        raise StewardAuthorityContextError("authority task identity is invalid")
    if not isinstance(source, AuthoritySource) or not admissions:
        raise StewardAuthorityContextError("authority source and admissions are required")
    for admission in admissions:
        if not isinstance(admission, AuthorityAdmission) or not admission.operations:
            raise StewardAuthorityContextError("authority admission is invalid")
        if len(set(admission.operations)) != len(admission.operations):
            raise StewardAuthorityContextError("authority operations must be unique")
        if admission.risk_class == RiskClass.CURRENT_CONTENT_READ and not admission.paths:
            raise StewardAuthorityContextError("current-content authority requires exact paths")
        if admission.risk_class == RiskClass.DERIVED_STATE_APPEND and not admission.scope_ids:
            raise StewardAuthorityContextError("derived-state authority requires exact Scopes")
        if admission.risk_class == RiskClass.RECOVERY_OR_ADMIN and not admission.run_ids:
            raise StewardAuthorityContextError("recovery authority requires exact Runs")
        if (
            admission.risk_class == RiskClass.EXTERNAL_DISCLOSURE
            and not admission.external_destinations
        ):
            raise StewardAuthorityContextError("external authority requires exact destinations")
        if admission.risk_class == RiskClass.USER_FILE_MUTATION and not admission.user_file_ids:
            raise StewardAuthorityContextError("mutation authority requires exact file identities")
    task_digest = _digest(
        {
            "domain": "local_steward.authority_task.v1",
            "task_identity": task_identity,
            "authority_domain_digest": session.identity.authority_domain_digest,
        }
    )
    payload = {
        "schema_name": AUTHORITY_SCHEMA_NAME,
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "authority_domain_digest": session.identity.authority_domain_digest,
        "task_identity_digest": task_digest,
        "source": source.value,
        "admissions": [_admission_payload(item) for item in admissions],
    }
    return AuthorityContext(
        AUTHORITY_SCHEMA_NAME,
        AUTHORITY_SCHEMA_VERSION,
        session.identity.authority_domain_digest,
        task_digest,
        source,
        admissions,
        _digest({"domain": "local_steward.authority_context.v1", **payload}),
    )


def authority_context_machine_object(context: AuthorityContext) -> dict[str, Any]:
    return {
        "schema_name": context.schema_name,
        "schema_version": context.schema_version,
        "authority_domain_digest": context.authority_domain_digest,
        "task_identity_digest": context.task_identity_digest,
        "source": context.source.value,
        "admissions": [_admission_payload(item) for item in context.admissions],
        "context_digest": context.context_digest,
    }


def load_authority_context(path: Path, session: StewardSession) -> AuthorityContext:
    """Load one host-owned JSON context and verify its canonical digest and domain."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        admissions = tuple(
            AuthorityAdmission(
                RiskClass(item["risk_class"]),
                tuple(item["operations"]),
                tuple(item.get("scope_ids", ())),
                tuple(item.get("snapshot_ids", ())),
                tuple(item.get("run_ids", ())),
                tuple(AuthorityPath(**entry) for entry in item.get("paths", ())),
                tuple(item.get("external_destinations", ())),
                tuple(item.get("user_file_ids", ())),
            )
            for item in value["admissions"]
        )
        context = AuthorityContext(
            value["schema_name"],
            value["schema_version"],
            value["authority_domain_digest"],
            value["task_identity_digest"],
            AuthoritySource(value["source"]),
            admissions,
            value["context_digest"],
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise StewardAuthorityContextError("host authority context is unavailable") from error
    payload = authority_context_machine_object(context)
    supplied = payload.pop("context_digest")
    expected = _digest({"domain": "local_steward.authority_context.v1", **payload})
    if (
        context.schema_name != AUTHORITY_SCHEMA_NAME
        or context.schema_version != AUTHORITY_SCHEMA_VERSION
        or not hmac.compare_digest(
            context.authority_domain_digest, session.identity.authority_domain_digest
        )
        or not hmac.compare_digest(supplied, expected)
    ):
        raise StewardAuthorityContextError("host authority context failed verification")
    return context


def require_authority(
    context: AuthorityContext,
    session: StewardSession,
    risk_class: RiskClass,
    operation: str,
    *,
    scope_id: str | None = None,
    snapshot_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    path: AuthorityPath | None = None,
    external_destination: str | None = None,
    user_file_id: str | None = None,
) -> AuthorityAdmission:
    """Require one exact admission; no request field can widen the context."""
    if not hmac.compare_digest(
        context.authority_domain_digest, session.identity.authority_domain_digest
    ):
        raise StewardAuthorityContextError("authority context belongs to another session")
    if risk_class == RiskClass.CURRENT_CONTENT_READ and (scope_id is None or path is None):
        raise StewardAuthorityRequiredError("current-content read requires one exact path")
    if risk_class == RiskClass.DERIVED_STATE_APPEND and scope_id is None:
        raise StewardAuthorityRequiredError("derived-state append requires one exact Scope")
    if risk_class == RiskClass.RECOVERY_OR_ADMIN and run_id is None:
        raise StewardAuthorityRequiredError("recovery requires one exact Run")
    if risk_class == RiskClass.EXTERNAL_DISCLOSURE and external_destination is None:
        raise StewardAuthorityRequiredError("external disclosure requires one exact destination")
    if risk_class == RiskClass.USER_FILE_MUTATION and user_file_id is None:
        raise StewardAuthorityRequiredError("user-file mutation requires one exact file")
    for admission in context.admissions:
        if admission.risk_class != risk_class or operation not in admission.operations:
            continue
        if scope_id is not None and scope_id not in admission.scope_ids:
            continue
        if not snapshot_ids and admission.snapshot_ids:
            continue
        if (
            snapshot_ids
            and admission.snapshot_ids
            and not set(snapshot_ids).issubset(admission.snapshot_ids)
        ):
            continue
        if run_id is not None and run_id not in admission.run_ids:
            continue
        if path is not None and path not in admission.paths:
            continue
        if (
            external_destination is not None
            and external_destination not in admission.external_destinations
        ):
            continue
        if user_file_id is not None and user_file_id not in admission.user_file_ids:
            continue
        return admission
    raise StewardAuthorityRequiredError(
        f"host authority does not admit {risk_class.value}:{operation} for the resolved object"
    )
