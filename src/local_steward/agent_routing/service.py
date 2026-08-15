"""Provider-free closed routing, grant admission, and deterministic publication."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from threading import Lock
from uuid import UUID

from .canonical import (
    canonical_publication_envelope,
    fact_block_digest,
    route_grant_digest,
    route_request_digest,
)
from .errors import (
    AgentPublicationError,
    AgentRouteGrantError,
    AgentRouteGrantReusedError,
    AgentRoutingError,
    AgentRoutingRequestError,
)
from .models import (
    MAX_PUBLICATION_ACCOUNTING_ITEMS,
    MAX_PUBLICATION_BOUNDARIES,
    MAX_PUBLICATION_FACT_BLOCK_BYTES,
    MAX_PUBLICATION_FACTS,
    MAX_PUBLICATION_PROVENANCE,
    MAX_PUBLICATION_VALUE_BYTES,
    MAX_ROUTE_LIMIT,
    MAX_ROUTE_OFFSET,
    MAX_ROUTE_OPERATION_LENGTH,
    MAX_ROUTE_PATH_LENGTH,
    MAX_ROUTE_SCOPE_LENGTH,
    PUBLICATION_SCHEMA_NAME,
    PUBLICATION_SCHEMA_VERSION,
    ROUTE_SCHEMA_NAME,
    ROUTE_SCHEMA_VERSION,
    AgentRoutingValidationViolation,
    AuthorityBoundary,
    OperationKind,
    PublicationAccounting,
    PublicationEnvelope,
    PublicationExactInteger,
    PublicationFact,
    PublicationPayload,
    PublicationSourceProvenance,
    PublicationStatus,
    PublicationTypedError,
    RouteBounds,
    RouteDecision,
    StewardRouteGrant,
    StewardRouteOutcome,
    StewardRouteRequest,
)


_OPERATION_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PUBLICATION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_CONTEXT_OPERATIONS = frozenset(
    {
        OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC,
        OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION,
    }
)

_HISTORICAL_OK_PROVENANCE_MINIMUM = {
    OperationKind.VERIFIED_SNAPSHOT_INVENTORY: 1,
    OperationKind.EXACT_SNAPSHOT_INSPECTION: 1,
    OperationKind.EXACT_HISTORICAL_ENTRY_RESOLUTION: 1,
    OperationKind.HISTORICAL_CURRENT_TRUTH_BOUNDARY: 1,
    OperationKind.MINIMAL_RESUMABLE_HANDOFF: 2,
    OperationKind.BOUNDED_CHANGE_REVIEW: 2,
    OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC: 1,
    OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION: 2,
}


@dataclass(frozen=True, slots=True)
class _RouteRequirements:
    snapshot_count: int | None = None
    scope: bool = False
    path: bool = False
    bounds: bool = False


_ROUTE_REQUIREMENTS = {
    OperationKind.CONFIGURATION_OR_HEALTH: _RouteRequirements(),
    OperationKind.VERIFIED_SNAPSHOT_INVENTORY: _RouteRequirements(),
    OperationKind.EXACT_SNAPSHOT_INSPECTION: _RouteRequirements(snapshot_count=1),
    OperationKind.EXACT_HISTORICAL_ENTRY_RESOLUTION: _RouteRequirements(
        snapshot_count=1, scope=True, path=True
    ),
    OperationKind.TYPED_HISTORICAL_FAILURE: _RouteRequirements(
        snapshot_count=1, scope=True, path=True
    ),
    OperationKind.HISTORICAL_CURRENT_TRUTH_BOUNDARY: _RouteRequirements(
        snapshot_count=1, scope=True, path=True
    ),
    OperationKind.MINIMAL_RESUMABLE_HANDOFF: _RouteRequirements(
        snapshot_count=2, scope=True
    ),
    OperationKind.SNAPSHOT_LIFECYCLE: _RouteRequirements(),
    OperationKind.BOUNDED_CHANGE_REVIEW: _RouteRequirements(
        snapshot_count=2, scope=True
    ),
    OperationKind.CONFIRMED_DOCUMENT_INSPECTION: _RouteRequirements(
        scope=True, path=True, bounds=True
    ),
    OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC: _RouteRequirements(
        snapshot_count=1, scope=True, bounds=True
    ),
    OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION: _RouteRequirements(
        snapshot_count=2, scope=True, bounds=True
    ),
}


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _validate_text(value: str | None, *, label: str, maximum: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise AgentRoutingRequestError(f"Agent route {label} is invalid")
    if any(ord(character) < 32 for character in value):
        raise AgentRoutingRequestError(f"Agent route {label} is invalid")


def _validate_path(value: str | None) -> None:
    _validate_text(value, label="path", maximum=MAX_ROUTE_PATH_LENGTH)
    if value is None:
        return
    path = PurePosixPath(value)
    if path.is_absolute() or value in (".", "..") or ".." in path.parts:
        raise AgentRoutingRequestError("Agent route path is invalid")


def _validate_bounds(value: RouteBounds | None) -> None:
    if value is None:
        return
    if not isinstance(value, RouteBounds):
        raise AgentRoutingRequestError("Agent route bounds are invalid")
    if type(value.limit) is not int or not 1 <= value.limit <= MAX_ROUTE_LIMIT:
        raise AgentRoutingRequestError("Agent route limit is invalid")
    if type(value.offset) is not int or not 0 <= value.offset <= MAX_ROUTE_OFFSET:
        raise AgentRoutingRequestError("Agent route offset is invalid")


def _validate_route_request(request: StewardRouteRequest) -> None:
    if not isinstance(request, StewardRouteRequest):
        raise AgentRoutingRequestError("Agent route request is invalid")
    operation = request.operation_kind
    if (
        not isinstance(operation, str)
        or not operation
        or len(operation) > MAX_ROUTE_OPERATION_LENGTH
        or not _OPERATION_PATTERN.fullmatch(operation)
    ):
        raise AgentRoutingRequestError("Agent route operation is invalid")
    if not isinstance(request.ordered_snapshot_ids, tuple):
        raise AgentRoutingRequestError("Agent route Snapshot identities are invalid")
    if any(not _canonical_uuid(item) for item in request.ordered_snapshot_ids):
        raise AgentRoutingRequestError("Agent route Snapshot identity is invalid")
    if len(set(request.ordered_snapshot_ids)) != len(request.ordered_snapshot_ids):
        raise AgentRoutingRequestError("Agent route Snapshot identities must be distinct")
    _validate_text(request.scope_id, label="Scope identity", maximum=MAX_ROUTE_SCOPE_LENGTH)
    _validate_path(request.path_or_prefix)
    _validate_bounds(request.bounds)


def _missing_fields(
    request: StewardRouteRequest, requirements: _RouteRequirements
) -> tuple[str, ...]:
    values: list[str] = []
    if (
        requirements.snapshot_count is not None
        and len(request.ordered_snapshot_ids) != requirements.snapshot_count
    ):
        values.append(f"ordered_snapshot_ids_exactly_{requirements.snapshot_count}")
    if requirements.scope and request.scope_id is None:
        values.append("scope_id")
    if requirements.path and request.path_or_prefix is None:
        values.append("path_or_prefix")
    if requirements.bounds and request.bounds is None:
        values.append("bounds")
    return tuple(values)


def _grant_for_request(
    request: StewardRouteRequest, operation: OperationKind, request_identity: str
) -> StewardRouteGrant:
    if request.scope_id is None or request.bounds is None:
        raise AgentRoutingRequestError("Context route admission is incomplete")
    grant = StewardRouteGrant(
        ROUTE_SCHEMA_VERSION,
        "",
        operation,
        request.ordered_snapshot_ids,
        request.scope_id,
        request.path_or_prefix,
        request.bounds,
        request_identity,
        False,
    )
    return replace(grant, grant_id=route_grant_digest(grant))


def route_steward_operation(request: StewardRouteRequest) -> StewardRouteOutcome:
    """Return one closed route decision without interpreting natural language."""
    _validate_route_request(request)
    request_identity = route_request_digest(request)
    try:
        operation = OperationKind(request.operation_kind)
    except ValueError:
        return StewardRouteOutcome(
            ROUTE_SCHEMA_NAME,
            ROUTE_SCHEMA_VERSION,
            RouteDecision.UNSUPPORTED,
            request.operation_kind,
            request_identity,
            (),
            None,
        )
    missing = _missing_fields(request, _ROUTE_REQUIREMENTS[operation])
    if missing:
        return StewardRouteOutcome(
            ROUTE_SCHEMA_NAME,
            ROUTE_SCHEMA_VERSION,
            RouteDecision.CLARIFY,
            operation.value,
            request_identity,
            missing,
            None,
        )
    decision = RouteDecision.CONTEXT if operation in _CONTEXT_OPERATIONS else RouteDecision.CORE
    grant = (
        _grant_for_request(request, operation, request_identity)
        if decision == RouteDecision.CONTEXT
        else None
    )
    return StewardRouteOutcome(
        ROUTE_SCHEMA_NAME,
        ROUTE_SCHEMA_VERSION,
        decision,
        operation.value,
        request_identity,
        (),
        grant,
    )


def validate_route_grant(
    request: StewardRouteRequest, grant: StewardRouteGrant
) -> None:
    """Fail closed unless a grant exactly matches one admitted Context request."""
    if not isinstance(grant, StewardRouteGrant):
        raise AgentRouteGrantError("Agent route grant is invalid")
    outcome = route_steward_operation(request)
    expected = outcome.grant
    if (
        outcome.decision != RouteDecision.CONTEXT
        or expected is None
        or grant.schema_version != ROUTE_SCHEMA_VERSION
        or grant.reusable
        or grant.grant_id != route_grant_digest(grant)
        or grant != expected
    ):
        raise AgentRouteGrantError("Agent route grant does not match the admitted operation")


class RouteGrantGuard:
    """Process-local single-consumption guard for operation-scoped Context grants."""

    def __init__(self) -> None:
        self._consumed: set[str] = set()
        self._lock = Lock()

    @property
    def consumed_count(self) -> int:
        with self._lock:
            return len(self._consumed)

    def consume(self, request: StewardRouteRequest, grant: StewardRouteGrant) -> None:
        validate_route_grant(request, grant)
        with self._lock:
            if grant.grant_id in self._consumed:
                raise AgentRouteGrantReusedError("Agent route grant has already been consumed")
            self._consumed.add(grant.grant_id)


def _validate_publication_key(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _PUBLICATION_KEY_PATTERN.fullmatch(value):
        raise AgentPublicationError(f"Agent publication {label} is invalid")


def _validate_publication_value(value: str, *, label: str) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PUBLICATION_VALUE_BYTES:
        raise AgentPublicationError(f"Agent publication {label} is invalid")


def _validate_publication_collections(payload: PublicationPayload) -> None:
    facts = payload.deterministic_facts
    provenance = payload.source_provenance
    exact = payload.exact_integer_encoding
    included = payload.inclusion_accounting
    omitted = payload.omission_accounting
    boundaries = payload.authority_boundary
    if len(facts) > MAX_PUBLICATION_FACTS:
        raise AgentPublicationError("Agent publication fact limit exceeded")
    if len(provenance) > MAX_PUBLICATION_PROVENANCE:
        raise AgentPublicationError("Agent publication provenance limit exceeded")
    if len(included) > MAX_PUBLICATION_ACCOUNTING_ITEMS or len(
        omitted
    ) > MAX_PUBLICATION_ACCOUNTING_ITEMS:
        raise AgentPublicationError("Agent publication accounting limit exceeded")
    if not boundaries or len(boundaries) > MAX_PUBLICATION_BOUNDARIES:
        raise AgentPublicationError("Agent publication authority boundary is invalid")

    fact_keys: set[str] = set()
    for fact in facts:
        if not isinstance(fact, PublicationFact):
            raise AgentPublicationError("Agent publication fact is invalid")
        _validate_publication_key(fact.key, label="fact key")
        _validate_publication_value(fact.value, label="fact value")
        if fact.key in fact_keys:
            raise AgentPublicationError("Agent publication fact key is duplicated")
        fact_keys.add(fact.key)

    provenance_ids: set[str] = set()
    for source in provenance:
        if (
            not isinstance(source, PublicationSourceProvenance)
            or not _canonical_uuid(source.snapshot_id)
            or not _DIGEST_PATTERN.fullmatch(source.snapshot_digest)
            or not _canonical_uuid(source.persistent_run_id)
            or not _canonical_uuid(source.evidence_id)
            or source.snapshot_id in provenance_ids
        ):
            raise AgentPublicationError("Agent publication source provenance is invalid")
        provenance_ids.add(source.snapshot_id)

    pointers: set[str] = set()
    for exact_integer in exact:
        if (
            not isinstance(exact_integer, PublicationExactInteger)
            or not exact_integer.json_pointer.startswith("/")
            or "//" in exact_integer.json_pointer
            or len(exact_integer.json_pointer) > MAX_ROUTE_PATH_LENGTH
            or not _DECIMAL_PATTERN.fullmatch(exact_integer.decimal_value)
            or exact_integer.json_pointer in pointers
        ):
            raise AgentPublicationError("Agent publication exact integer is invalid")
        pointers.add(exact_integer.json_pointer)

    for accounting, label in ((included, "inclusion"), (omitted, "omission")):
        categories: set[str] = set()
        for accounting_item in accounting:
            if not isinstance(accounting_item, PublicationAccounting):
                raise AgentPublicationError(f"Agent publication {label} accounting is invalid")
            _validate_publication_key(accounting_item.category, label=f"{label} category")
            if (
                type(accounting_item.count) is not int
                or accounting_item.count < 0
                or accounting_item.category in categories
            ):
                raise AgentPublicationError(f"Agent publication {label} accounting is invalid")
            categories.add(accounting_item.category)
    if any(not isinstance(item, AuthorityBoundary) for item in boundaries):
        raise AgentPublicationError("Agent publication authority boundary is invalid")


def _validate_publication_payload(payload: PublicationPayload) -> None:
    if not isinstance(payload, PublicationPayload):
        raise AgentPublicationError("Agent publication payload is invalid")
    if payload.route_decision not in (RouteDecision.CORE, RouteDecision.CONTEXT):
        raise AgentPublicationError("Agent publication route is not executable")
    if not _DIGEST_PATTERN.fullmatch(payload.operation_identity):
        raise AgentPublicationError("Agent publication operation identity is invalid")
    _validate_publication_collections(payload)
    if payload.status == PublicationStatus.ERROR:
        if not isinstance(payload.typed_error, PublicationTypedError):
            raise AgentPublicationError("Agent publication typed error is required")
        error = payload.typed_error
        if not _ERROR_CODE_PATTERN.fullmatch(error.code):
            raise AgentPublicationError("Agent publication typed error code is invalid")
        _validate_publication_value(error.message, label="typed error message")
        if (
            payload.deterministic_facts
            or payload.source_provenance
            or payload.exact_integer_encoding
            or payload.inclusion_accounting
            or payload.omission_accounting
        ):
            raise AgentPublicationError("Typed failure cannot publish a business result")
        if AuthorityBoundary.NO_BUSINESS_RESULT not in payload.authority_boundary:
            raise AgentPublicationError("Typed failure must declare no business result")
        return
    if payload.status != PublicationStatus.OK or payload.typed_error is not None:
        raise AgentPublicationError("Agent publication status is invalid")
    if not payload.deterministic_facts:
        raise AgentPublicationError("Agent publication deterministic facts are required")
    minimum = _HISTORICAL_OK_PROVENANCE_MINIMUM.get(payload.operation_kind, 0)
    if len(payload.source_provenance) < minimum:
        raise AgentPublicationError("Agent publication source provenance is incomplete")
    if minimum and AuthorityBoundary.HISTORICAL_NOT_CURRENT not in payload.authority_boundary:
        raise AgentPublicationError("Historical publication must declare its current-truth boundary")


def _quoted(value: str) -> str:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        rendered.replace("`", "\\u0060")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_publication_fact_block(payload: PublicationPayload) -> str:
    """Render one canonical user-visible fact block without model-authored text."""
    _validate_publication_payload(payload)
    lines = [
        "## STEWARD Deterministic Fact Block",
        "",
        f"- Route decision: `{payload.route_decision.value}`",
        f"- Operation kind: `{payload.operation_kind.value}`",
        f"- Operation identity: `{payload.operation_identity}`",
        f"- Status: `{payload.status.value}`",
        "",
    ]
    if payload.status == PublicationStatus.ERROR:
        error = payload.typed_error
        if error is None:
            raise AgentPublicationError("Agent publication typed error is required")
        lines.extend(
            [
                "### Typed error",
                "",
                f"- Code: `{error.code}`",
                f"- Message: {_quoted(error.message)}",
                "",
            ]
        )
    lines.extend(["### Deterministic facts", ""])
    lines.extend(
        f"- `{item.key}`: {_quoted(item.value)}"
        for item in sorted(payload.deterministic_facts, key=lambda value: value.key)
    )
    if not payload.deterministic_facts:
        lines.append("- None")
    lines.extend(["", "### Source provenance", ""])
    lines.extend(
        (
            f"- Snapshot `{item.snapshot_id}`; digest `{item.snapshot_digest}`; "
            f"persistent Run `{item.persistent_run_id}`; Evidence `{item.evidence_id}`"
        )
        for item in payload.source_provenance
    )
    if not payload.source_provenance:
        lines.append("- None")
    lines.extend(["", "### Exact integer encoding", ""])
    lines.extend(
        f"- `{item.json_pointer}`: `{item.decimal_value}`"
        for item in sorted(payload.exact_integer_encoding, key=lambda value: value.json_pointer)
    )
    if not payload.exact_integer_encoding:
        lines.append("- None")
    lines.extend(["", "### Inclusion accounting", ""])
    lines.extend(
        f"- `{item.category}`: {item.count}"
        for item in sorted(payload.inclusion_accounting, key=lambda value: value.category)
    )
    if not payload.inclusion_accounting:
        lines.append("- None")
    lines.extend(["", "### Omission accounting", ""])
    lines.extend(
        f"- `{item.category}`: {item.count}"
        for item in sorted(payload.omission_accounting, key=lambda value: value.category)
    )
    if not payload.omission_accounting:
        lines.append("- None")
    lines.extend(["", "### Authority boundary", ""])
    lines.extend(
        f"- `{item.value}`"
        for item in sorted(payload.authority_boundary, key=lambda value: value.value)
    )
    lines.append("")
    block = "\n".join(lines)
    if len(block.encode("utf-8")) > MAX_PUBLICATION_FACT_BLOCK_BYTES:
        raise AgentPublicationError("Agent publication fact block exceeds the product limit")
    return block


def build_publication_envelope(
    route: StewardRouteOutcome,
    *,
    status: PublicationStatus,
    deterministic_facts: tuple[PublicationFact, ...] = (),
    source_provenance: tuple[PublicationSourceProvenance, ...] = (),
    exact_integer_encoding: tuple[PublicationExactInteger, ...] = (),
    inclusion_accounting: tuple[PublicationAccounting, ...] = (),
    omission_accounting: tuple[PublicationAccounting, ...] = (),
    authority_boundary: tuple[AuthorityBoundary, ...],
    typed_error: PublicationTypedError | None = None,
) -> PublicationEnvelope:
    """Build and independently validate one complete deterministic publication."""
    if not isinstance(route, StewardRouteOutcome):
        raise AgentPublicationError("Agent publication route is invalid")
    try:
        operation = OperationKind(route.operation_kind)
    except ValueError as error:
        raise AgentPublicationError("Agent publication operation is unsupported") from error
    payload = PublicationPayload(
        route.decision,
        route.request_digest,
        operation,
        status,
        typed_error,
        tuple(sorted(deterministic_facts, key=lambda value: value.key)),
        source_provenance,
        tuple(sorted(exact_integer_encoding, key=lambda value: value.json_pointer)),
        tuple(sorted(inclusion_accounting, key=lambda value: value.category)),
        tuple(sorted(omission_accounting, key=lambda value: value.category)),
        tuple(sorted(authority_boundary, key=lambda value: value.value)),
    )
    block = render_publication_fact_block(payload)
    if status == PublicationStatus.OK:
        status_or_error: str | PublicationTypedError = status.value
    elif typed_error is not None:
        status_or_error = typed_error
    else:
        raise AgentPublicationError("Agent publication typed error is required")
    envelope = PublicationEnvelope(
        PUBLICATION_SCHEMA_NAME,
        PUBLICATION_SCHEMA_VERSION,
        payload.route_decision,
        payload.operation_identity,
        payload.operation_kind,
        status_or_error,
        payload.deterministic_facts,
        payload.source_provenance,
        payload.exact_integer_encoding,
        payload.inclusion_accounting,
        payload.omission_accounting,
        payload.authority_boundary,
        block,
        fact_block_digest(block),
    )
    violations = validate_publication_envelope(envelope)
    if violations:
        raise AgentPublicationError(
            "Agent publication envelope validation failed",
        )
    return envelope


def _payload_from_envelope(envelope: PublicationEnvelope) -> PublicationPayload:
    if isinstance(envelope.status_or_typed_error, PublicationTypedError):
        status = PublicationStatus.ERROR
        typed_error: PublicationTypedError | None = envelope.status_or_typed_error
    elif envelope.status_or_typed_error == PublicationStatus.OK.value:
        status = PublicationStatus.OK
        typed_error = None
    else:
        raise AgentPublicationError("Agent publication status is invalid")
    return PublicationPayload(
        envelope.route_decision,
        envelope.operation_identity,
        envelope.operation_kind,
        status,
        typed_error,
        envelope.deterministic_facts,
        envelope.source_provenance,
        envelope.exact_integer_encoding,
        envelope.inclusion_accounting,
        envelope.omission_accounting,
        envelope.authority_boundary,
    )


def validate_publication_envelope(
    envelope: PublicationEnvelope,
) -> tuple[AgentRoutingValidationViolation, ...]:
    """Return stable violations without repairing or partially publishing."""
    if not isinstance(envelope, PublicationEnvelope):
        return (AgentRoutingValidationViolation("PUBLICATION_TYPE_INVALID"),)
    codes: set[str] = set()

    def invalid(code: str) -> None:
        codes.add(code)

    if envelope.schema_name != PUBLICATION_SCHEMA_NAME:
        invalid("PUBLICATION_SCHEMA_NAME_INVALID")
    if envelope.schema_version != PUBLICATION_SCHEMA_VERSION:
        invalid("PUBLICATION_SCHEMA_VERSION_INVALID")
    try:
        payload = _payload_from_envelope(envelope)
        _validate_publication_payload(payload)
        expected_block = render_publication_fact_block(payload)
        if envelope.fact_block_markdown != expected_block:
            invalid("PUBLICATION_FACT_BLOCK_MISMATCH")
        if envelope.fact_block_sha256 != fact_block_digest(envelope.fact_block_markdown):
            invalid("PUBLICATION_FACT_BLOCK_DIGEST_INVALID")
        if len(canonical_publication_envelope(envelope)) > MAX_PUBLICATION_FACT_BLOCK_BYTES * 2:
            invalid("PUBLICATION_ENVELOPE_RESOURCE_LIMIT")
    except AgentRoutingError:
        invalid("PUBLICATION_PAYLOAD_INVALID")
    except Exception:
        invalid("PUBLICATION_CANONICAL_INVALID")
    return tuple(AgentRoutingValidationViolation(code) for code in sorted(codes))
