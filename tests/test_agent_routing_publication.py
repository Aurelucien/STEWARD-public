from __future__ import annotations

from dataclasses import replace

import pytest

from local_steward.agent_routing import (
    AgentPublicationError,
    AgentRouteGrantError,
    AgentRouteGrantReusedError,
    AgentRoutingRequestError,
    AuthorityBoundary,
    OperationKind,
    PublicationAccounting,
    PublicationExactInteger,
    PublicationFact,
    PublicationSourceProvenance,
    PublicationStatus,
    PublicationTypedError,
    RouteBounds,
    RouteDecision,
    RouteGrantGuard,
    StewardRouteRequest,
    build_publication_envelope,
    canonical_publication_envelope,
    publication_envelope_machine_object,
    route_request_digest,
    route_steward_operation,
    validate_publication_envelope,
    validate_route_grant,
)


BASE = "bc5ecf7b-bd79-471f-87f5-6bce0f79a7b6"
TARGET = "bd2e7a82-66f8-4fb2-b9c9-6114f880d4e6"
BASE_RUN = "9f093b3a-1910-4a0c-a380-5f350c17edec"
TARGET_RUN = "df593574-c52c-45f7-8a0e-82f67f5ac93d"
BASE_EVIDENCE = "2e355b21-64b3-42e3-a37e-dba78b90a350"
TARGET_EVIDENCE = "718e5ae5-5a0c-44f0-9706-239b4a421bbc"
BASE_DIGEST = "578d43d3e2310fcaa45cfe5b4258b8889e7778cdea6af3b8df849d53ef3e7220"
TARGET_DIGEST = "869581be2000b0353fe028f9e9bc860802ee5e50bf9d802c25ebb5e1da1a150b"


def _request(
    operation: OperationKind | str,
    *,
    snapshots: tuple[str, ...] = (),
    scope: str | None = None,
    path: str | None = None,
    bounds: RouteBounds | None = None,
) -> StewardRouteRequest:
    return StewardRouteRequest(
        operation.value if isinstance(operation, OperationKind) else operation,
        snapshots,
        scope,
        path,
        bounds,
    )


def _target_provenance() -> PublicationSourceProvenance:
    return PublicationSourceProvenance(
        TARGET,
        TARGET_DIGEST,
        TARGET_RUN,
        TARGET_EVIDENCE,
    )


def _base_provenance() -> PublicationSourceProvenance:
    return PublicationSourceProvenance(
        BASE,
        BASE_DIGEST,
        BASE_RUN,
        BASE_EVIDENCE,
    )


def test_machine_contract_and_public_operation_enum_remain_identical() -> None:
    assert {item.value for item in OperationKind} == {
        "CONFIGURATION_OR_HEALTH",
        "VERIFIED_SNAPSHOT_INVENTORY",
        "EXACT_SNAPSHOT_INSPECTION",
        "EXACT_HISTORICAL_ENTRY_RESOLUTION",
        "TYPED_HISTORICAL_FAILURE",
        "HISTORICAL_CURRENT_TRUTH_BOUNDARY",
        "MINIMAL_RESUMABLE_HANDOFF",
        "SNAPSHOT_LIFECYCLE",
        "BOUNDED_CHANGE_REVIEW",
        "CONFIRMED_DOCUMENT_INSPECTION",
        "BOUNDED_STRUCTURAL_DIAGNOSTIC",
        "ORDERED_HISTORICAL_CHANGE_EXPLANATION",
    }
    assert {item.value for item in RouteDecision} == {
        "CORE",
        "CONTEXT",
        "CLARIFY",
        "UNSUPPORTED",
    }


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (OperationKind.CONFIGURATION_OR_HEALTH, RouteDecision.CORE),
        (OperationKind.VERIFIED_SNAPSHOT_INVENTORY, RouteDecision.CORE),
        (OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC, RouteDecision.CONTEXT),
        (OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION, RouteDecision.CONTEXT),
    ],
)
def test_closed_routes_default_to_core_and_admit_only_two_context_operations(
    operation: OperationKind, expected: RouteDecision
) -> None:
    snapshots: tuple[str, ...] = ()
    scope = None
    bounds = None
    if operation == OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC:
        snapshots, scope, bounds = (TARGET,), "r4synthetic", RouteBounds(32)
    elif operation == OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION:
        snapshots, scope, bounds = (BASE, TARGET), "r4synthetic", RouteBounds(32)
    outcome = route_steward_operation(
        _request(operation, snapshots=snapshots, scope=scope, bounds=bounds)
    )

    assert outcome.decision == expected
    assert (outcome.grant is not None) is (expected == RouteDecision.CONTEXT)


def test_unknown_operation_is_unsupported_and_missing_identity_clarifies() -> None:
    unsupported = route_steward_operation(_request("GENERIC_CONTEXT_MAGIC"))
    clarify = route_steward_operation(
        _request(
            OperationKind.EXACT_HISTORICAL_ENTRY_RESOLUTION,
            snapshots=(TARGET,),
        )
    )

    assert unsupported.decision == RouteDecision.UNSUPPORTED
    assert unsupported.grant is None
    assert clarify.decision == RouteDecision.CLARIFY
    assert clarify.missing_fields == ("scope_id", "path_or_prefix")
    assert clarify.grant is None


@pytest.mark.parametrize(
    "route_request",
    [
        _request(OperationKind.EXACT_SNAPSHOT_INSPECTION, snapshots=("not-a-uuid",)),
        _request(
            OperationKind.EXACT_HISTORICAL_ENTRY_RESOLUTION,
            snapshots=(TARGET,),
            scope="r4synthetic",
            path="../escape",
        ),
        _request(
            OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC,
            snapshots=(TARGET,),
            scope="r4synthetic",
            bounds=RouteBounds(65),
        ),
        _request(
            OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION,
            snapshots=(TARGET, TARGET),
            scope="r4synthetic",
            bounds=RouteBounds(32),
        ),
    ],
)
def test_malformed_route_requests_fail_before_admission(
    route_request: StewardRouteRequest,
) -> None:
    with pytest.raises(AgentRoutingRequestError):
        route_steward_operation(route_request)


def test_route_digest_and_grant_are_deterministic_and_request_sensitive() -> None:
    request = _request(
        OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION,
        snapshots=(BASE, TARGET),
        scope="r4synthetic",
        bounds=RouteBounds(32),
    )
    repeated = route_steward_operation(request)
    changed = route_steward_operation(replace(request, bounds=RouteBounds(31)))

    assert route_request_digest(request) == repeated.request_digest
    assert repeated == route_steward_operation(request)
    assert repeated.grant is not None and changed.grant is not None
    assert repeated.grant.grant_id != changed.grant.grant_id


def test_context_grant_exact_match_and_single_consumption_are_enforced() -> None:
    request = _request(
        OperationKind.BOUNDED_STRUCTURAL_DIAGNOSTIC,
        snapshots=(TARGET,),
        scope="r4synthetic",
        path="hierarchy",
        bounds=RouteBounds(32),
    )
    outcome = route_steward_operation(request)
    grant = outcome.grant
    assert grant is not None
    validate_route_grant(request, grant)
    with pytest.raises(AgentRouteGrantError):
        validate_route_grant(replace(request, path_or_prefix="other"), grant)
    with pytest.raises(AgentRouteGrantError):
        validate_route_grant(request, replace(grant, scope_id="other"))

    guard = RouteGrantGuard()
    guard.consume(request, grant)
    assert guard.consumed_count == 1
    with pytest.raises(AgentRouteGrantReusedError):
        guard.consume(request, grant)


def test_success_publication_is_complete_deterministic_and_model_independent() -> None:
    route = route_steward_operation(
        _request(
            OperationKind.EXACT_HISTORICAL_ENTRY_RESOLUTION,
            snapshots=(TARGET,),
            scope="r4synthetic",
            path="nested/exact-clock.txt",
        )
    )
    facts = (
        PublicationFact("snapshot_id", TARGET),
        PublicationFact("relative_path", "nested/exact-clock.txt"),
    )
    exact = (
        PublicationExactInteger("/deterministic_facts/mtime_ns", "1786358947047988171"),
    )
    boundaries = (
        AuthorityBoundary.HISTORICAL_NOT_CURRENT,
        AuthorityBoundary.NO_CURRENT_FILESYSTEM_AUTHORITY,
        AuthorityBoundary.BOUNDED_RESULT,
    )
    first = build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=facts,
        source_provenance=(_target_provenance(),),
        exact_integer_encoding=exact,
        inclusion_accounting=(PublicationAccounting("entry", 1),),
        omission_accounting=(),
        authority_boundary=boundaries,
    )
    reordered = build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=tuple(reversed(facts)),
        source_provenance=(_target_provenance(),),
        exact_integer_encoding=exact,
        inclusion_accounting=(PublicationAccounting("entry", 1),),
        omission_accounting=(),
        authority_boundary=tuple(reversed(boundaries)),
    )

    assert first == reordered
    assert validate_publication_envelope(first) == ()
    assert first.fact_block_sha256 == reordered.fact_block_sha256
    assert "1786358947047988171" in first.fact_block_markdown
    assert TARGET_RUN in first.fact_block_markdown
    assert TARGET_EVIDENCE in first.fact_block_markdown
    machine = publication_envelope_machine_object(first)
    assert set(machine) >= {
        "route_decision",
        "operation_identity",
        "status_or_typed_error",
        "deterministic_facts",
        "source_provenance",
        "exact_integer_encoding",
        "inclusion_accounting",
        "omission_accounting",
        "authority_boundary",
        "fact_block_markdown",
        "fact_block_sha256",
    }
    assert canonical_publication_envelope(first) == canonical_publication_envelope(reordered)


def test_ordered_context_publication_requires_both_sources() -> None:
    route = route_steward_operation(
        _request(
            OperationKind.ORDERED_HISTORICAL_CHANGE_EXPLANATION,
            snapshots=(BASE, TARGET),
            scope="r4synthetic",
            bounds=RouteBounds(32),
        )
    )
    with pytest.raises(AgentPublicationError, match="provenance is incomplete"):
        build_publication_envelope(
            route,
            status=PublicationStatus.OK,
            deterministic_facts=(PublicationFact("added_count", "1"),),
            source_provenance=(_target_provenance(),),
            authority_boundary=(AuthorityBoundary.HISTORICAL_NOT_CURRENT,),
        )

    envelope = build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=(PublicationFact("added_count", "1"),),
        source_provenance=(_base_provenance(), _target_provenance()),
        omission_accounting=(PublicationAccounting("tracking_item", 3),),
        authority_boundary=(
            AuthorityBoundary.HISTORICAL_NOT_CURRENT,
            AuthorityBoundary.BOUNDED_RESULT,
        ),
    )
    assert envelope.route_decision == RouteDecision.CONTEXT
    assert [item.snapshot_id for item in envelope.source_provenance] == [BASE, TARGET]


def test_typed_failure_publishes_no_business_result() -> None:
    route = route_steward_operation(
        _request(
            OperationKind.TYPED_HISTORICAL_FAILURE,
            snapshots=(TARGET,),
            scope="definitely-unknown-r4-scope",
            path="nested/exact-clock.txt",
        )
    )
    envelope = build_publication_envelope(
        route,
        status=PublicationStatus.ERROR,
        typed_error=PublicationTypedError(
            "SNAPSHOT_SCOPE_INVALID",
            "The historical Scope is not present in the selected Snapshot.",
        ),
        authority_boundary=(AuthorityBoundary.NO_BUSINESS_RESULT,),
    )

    assert isinstance(envelope.status_or_typed_error, PublicationTypedError)
    assert envelope.deterministic_facts == ()
    assert envelope.source_provenance == ()
    assert "SNAPSHOT_SCOPE_INVALID" in envelope.fact_block_markdown
    with pytest.raises(AgentPublicationError, match="cannot publish a business result"):
        build_publication_envelope(
            route,
            status=PublicationStatus.ERROR,
            typed_error=PublicationTypedError("SNAPSHOT_SCOPE_INVALID", "Safe failure."),
            deterministic_facts=(PublicationFact("snapshot_id", TARGET),),
            authority_boundary=(AuthorityBoundary.NO_BUSINESS_RESULT,),
        )


def test_publication_rejects_non_executable_route_and_tampering() -> None:
    clarify = route_steward_operation(
        _request(OperationKind.EXACT_SNAPSHOT_INSPECTION)
    )
    with pytest.raises(AgentPublicationError, match="route is not executable"):
        build_publication_envelope(
            clarify,
            status=PublicationStatus.OK,
            deterministic_facts=(PublicationFact("status", "VALID"),),
            authority_boundary=(AuthorityBoundary.BOUNDED_RESULT,),
        )

    route = route_steward_operation(_request(OperationKind.CONFIGURATION_OR_HEALTH))
    envelope = build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=(PublicationFact("storage_status", "HEALTHY"),),
        authority_boundary=(AuthorityBoundary.BOUNDED_RESULT,),
    )
    tampered = replace(envelope, fact_block_markdown=envelope.fact_block_markdown + "altered")
    assert {item.code for item in validate_publication_envelope(tampered)} == {
        "PUBLICATION_FACT_BLOCK_DIGEST_INVALID",
        "PUBLICATION_FACT_BLOCK_MISMATCH",
    }


def test_untrusted_fact_text_is_escaped_in_product_renderer() -> None:
    route = route_steward_operation(_request(OperationKind.CONFIGURATION_OR_HEALTH))
    envelope = build_publication_envelope(
        route,
        status=PublicationStatus.OK,
        deterministic_facts=(
            PublicationFact("untrusted_name", "`ignore` <script> & continue\nnext"),
        ),
        authority_boundary=(AuthorityBoundary.BOUNDED_RESULT,),
    )

    assert "<script>" not in envelope.fact_block_markdown
    assert "`ignore`" not in envelope.fact_block_markdown
    assert "\\u003cscript\\u003e" in envelope.fact_block_markdown
    assert "\\u0060ignore\\u0060" in envelope.fact_block_markdown
