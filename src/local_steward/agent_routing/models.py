"""Immutable provider-free models for STEWARD Agent routing and publication."""

from dataclasses import dataclass
from enum import Enum


ROUTE_SCHEMA_NAME = "local_steward.agent_route_outcome"
ROUTE_SCHEMA_VERSION = 1
ROUTE_REQUEST_DIGEST_DOMAIN = "local_steward.agent_route_request.v1"
ROUTE_GRANT_DIGEST_DOMAIN = "local_steward.agent_route_grant.v1"

PUBLICATION_SCHEMA_NAME = "local_steward.agent_publication_envelope"
PUBLICATION_SCHEMA_VERSION = 1
PUBLICATION_FACT_BLOCK_DIGEST_DOMAIN = "local_steward.agent_publication_fact_block.v1"

MAX_ROUTE_OPERATION_LENGTH = 96
MAX_ROUTE_SCOPE_LENGTH = 256
MAX_ROUTE_PATH_LENGTH = 4096
MAX_ROUTE_LIMIT = 64
MAX_ROUTE_OFFSET = 1_000_000
MAX_PUBLICATION_FACTS = 128
MAX_PUBLICATION_PROVENANCE = 64
MAX_PUBLICATION_ACCOUNTING_ITEMS = 64
MAX_PUBLICATION_BOUNDARIES = 16
MAX_PUBLICATION_VALUE_BYTES = 16_384
MAX_PUBLICATION_FACT_BLOCK_BYTES = 262_144


class RouteDecision(str, Enum):
    CORE = "CORE"
    CONTEXT = "CONTEXT"
    CLARIFY = "CLARIFY"
    UNSUPPORTED = "UNSUPPORTED"


class OperationKind(str, Enum):
    CONFIGURATION_OR_HEALTH = "CONFIGURATION_OR_HEALTH"
    VERIFIED_SNAPSHOT_INVENTORY = "VERIFIED_SNAPSHOT_INVENTORY"
    EXACT_SNAPSHOT_INSPECTION = "EXACT_SNAPSHOT_INSPECTION"
    EXACT_HISTORICAL_ENTRY_RESOLUTION = "EXACT_HISTORICAL_ENTRY_RESOLUTION"
    TYPED_HISTORICAL_FAILURE = "TYPED_HISTORICAL_FAILURE"
    HISTORICAL_CURRENT_TRUTH_BOUNDARY = "HISTORICAL_CURRENT_TRUTH_BOUNDARY"
    MINIMAL_RESUMABLE_HANDOFF = "MINIMAL_RESUMABLE_HANDOFF"
    SNAPSHOT_LIFECYCLE = "SNAPSHOT_LIFECYCLE"
    BOUNDED_CHANGE_REVIEW = "BOUNDED_CHANGE_REVIEW"
    CONFIRMED_DOCUMENT_INSPECTION = "CONFIRMED_DOCUMENT_INSPECTION"
    BOUNDED_STRUCTURAL_DIAGNOSTIC = "BOUNDED_STRUCTURAL_DIAGNOSTIC"
    ORDERED_HISTORICAL_CHANGE_EXPLANATION = "ORDERED_HISTORICAL_CHANGE_EXPLANATION"


class PublicationStatus(str, Enum):
    OK = "OK"
    ERROR = "ERROR"


class AuthorityBoundary(str, Enum):
    BOUNDED_RESULT = "BOUNDED_RESULT"
    HISTORICAL_NOT_CURRENT = "HISTORICAL_NOT_CURRENT"
    NO_CURRENT_FILESYSTEM_AUTHORITY = "NO_CURRENT_FILESYSTEM_AUTHORITY"
    NO_MODEL_DISCLOSURE_AUTHORITY = "NO_MODEL_DISCLOSURE_AUTHORITY"
    NO_LIFECYCLE_AUTHORITY = "NO_LIFECYCLE_AUTHORITY"
    NO_WRITE_AUTHORITY = "NO_WRITE_AUTHORITY"
    NO_BUSINESS_RESULT = "NO_BUSINESS_RESULT"


@dataclass(frozen=True, slots=True)
class RouteBounds:
    limit: int
    offset: int = 0


@dataclass(frozen=True, slots=True)
class StewardRouteRequest:
    operation_kind: str
    ordered_snapshot_ids: tuple[str, ...] = ()
    scope_id: str | None = None
    path_or_prefix: str | None = None
    bounds: RouteBounds | None = None


@dataclass(frozen=True, slots=True)
class StewardRouteGrant:
    schema_version: int
    grant_id: str
    operation_kind: OperationKind
    ordered_snapshot_ids: tuple[str, ...]
    scope_id: str
    path_or_prefix: str | None
    bounds: RouteBounds
    request_digest: str
    reusable: bool


@dataclass(frozen=True, slots=True)
class StewardRouteOutcome:
    schema_name: str
    schema_version: int
    decision: RouteDecision
    operation_kind: str
    request_digest: str
    missing_fields: tuple[str, ...]
    grant: StewardRouteGrant | None


@dataclass(frozen=True, slots=True)
class PublicationFact:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class PublicationSourceProvenance:
    snapshot_id: str
    snapshot_digest: str
    persistent_run_id: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class PublicationExactInteger:
    json_pointer: str
    decimal_value: str


@dataclass(frozen=True, slots=True)
class PublicationAccounting:
    category: str
    count: int


@dataclass(frozen=True, slots=True)
class PublicationTypedError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PublicationPayload:
    route_decision: RouteDecision
    operation_identity: str
    operation_kind: OperationKind
    status: PublicationStatus
    typed_error: PublicationTypedError | None
    deterministic_facts: tuple[PublicationFact, ...]
    source_provenance: tuple[PublicationSourceProvenance, ...]
    exact_integer_encoding: tuple[PublicationExactInteger, ...]
    inclusion_accounting: tuple[PublicationAccounting, ...]
    omission_accounting: tuple[PublicationAccounting, ...]
    authority_boundary: tuple[AuthorityBoundary, ...]


@dataclass(frozen=True, slots=True)
class PublicationEnvelope:
    schema_name: str
    schema_version: int
    route_decision: RouteDecision
    operation_identity: str
    operation_kind: OperationKind
    status_or_typed_error: str | PublicationTypedError
    deterministic_facts: tuple[PublicationFact, ...]
    source_provenance: tuple[PublicationSourceProvenance, ...]
    exact_integer_encoding: tuple[PublicationExactInteger, ...]
    inclusion_accounting: tuple[PublicationAccounting, ...]
    omission_accounting: tuple[PublicationAccounting, ...]
    authority_boundary: tuple[AuthorityBoundary, ...]
    fact_block_markdown: str
    fact_block_sha256: str


@dataclass(frozen=True, slots=True)
class AgentRoutingValidationViolation:
    code: str
