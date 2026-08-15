"""Typer command surface; policy is implemented in dedicated modules."""

import json
import shutil
import traceback
import os
from pathlib import Path
from typing import Any, Callable, cast
from uuid import UUID

import typer

from .config import discover_config, load_config, project_root_for
from .constants import EXIT_CAPABILITY, PROJECT_ROOT
from .database import SCHEMA_VERSION
from .doctor import run_doctor
from .document_observation import DocumentInspectionRequest, inspect_document
from .errors import (
    ConfigurationError,
    DuplicateAnalysisError,
    GrowthError,
    InitializationError,
    SnapshotBudgetError,
    SnapshotNotFoundError,
    StewardError,
    StructureError,
)
from .models import (
    CapabilityStatus,
    CommandEnvelope,
    GrowthRank,
    ScopeRole,
    SnapshotVerificationResult,
    StewardConfig,
    StructureRank,
)
from .output import envelope, error_envelope, json_text, safe_text, to_jsonable
from .runs import create_run, evidence_records, get_run, list_runs, transition_run
from .storage import (
    initialize_storage,
    migrate_storage,
    rebuild_index,
    storage_status,
    verify_evidence_report,
)
from .models import RunStatus
from .errors import RebuildConfirmationError, StorageMigrationConfirmationError
from .scan_budget import make_budget
from .payload_hashing import default_payload_hash_policy
from .snapshots import (
    _verified_snapshot_detail,
    _verified_snapshot_entries,
    create_snapshot,
    list_snapshots,
    verify_snapshot,
)
from .snapshot_acquisition import (
    SnapshotAcquisitionRequest,
    acquire_snapshot,
    recover_snapshot_acquisition,
    snapshot_acquisition_status,
)
from .snapshot_refresh import (
    MAX_REFRESH_ENTRIES,
    SnapshotChangeReviewRequest,
    SnapshotRefreshRequest,
    refresh_snapshot,
    review_snapshot_changes,
)
from .snapshot_diff import compute_verified_snapshot_diff
from .change_semantics import change_events_from_snapshot_diff, summarize_change_events
from .resources import observe_resources
from .runtime_capabilities import inspect_runtime_capabilities
from .system_status import build_system_status_review
from .snapshot_relation_query import query_verified_snapshot_relations
from .models import RelationKind
from .snapshot_duplicate_query import query_verified_snapshot_duplicates
from .storage_query import query_verified_snapshot_growth, query_verified_snapshot_structure
from .observation_projection import (
    ObservationProjectionError,
    ObservationProjectionInvariantError,
    ObservationProjectionRequestError,
    build_pair_tracking_projection,
    build_snapshot_diagnostic_projection,
    machine_object,
)
from .observation_projection.json_input import (
    decode_pair_tracking_request,
    decode_projection_policy,
    decode_snapshot_diagnostic_request,
    load_json_object,
)

app = typer.Typer(add_completion=False, help="Local System Steward foundation CLI.")
config_app = typer.Typer(help="Configuration operations.")
scopes_app = typer.Typer(help="Scope operations.")
storage_app = typer.Typer(help="Derived SQLite index operations.")
runs_app = typer.Typer(help="Persistent Run operations.")
evidence_app = typer.Typer(help="Immutable evidence operations.")
snapshots_app = typer.Typer(help="Filesystem snapshot operations.")
resources_app = typer.Typer(help="One-shot operating-system resource observation.")
runtime_app = typer.Typer(help="Installed dependency profiles and operation capabilities.")
documents_app = typer.Typer(help="Confirmed provider-free current-document inspection.")
projection_app = typer.Typer(
    help="Read-only Observation Projection machine JSON; complete request/policy files required."
)
app.add_typer(config_app, name="config")
app.add_typer(scopes_app, name="scopes")
app.add_typer(storage_app, name="storage")
app.add_typer(runs_app, name="runs")
app.add_typer(evidence_app, name="evidence")
app.add_typer(snapshots_app, name="snapshots")
app.add_typer(resources_app, name="resources")
app.add_typer(runtime_app, name="runtime")
app.add_typer(documents_app, name="documents")
app.add_typer(projection_app, name="projection")


def _context(ctx: typer.Context) -> dict[str, Any]:
    value = ctx.find_root().obj
    return value if isinstance(value, dict) else {}


@app.callback()
def callback(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="Path to one TOML configuration."),
    output_format: str = typer.Option(
        "human", "--format", case_sensitive=False, help="human or json"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Limit human output."),
    no_progress: bool = typer.Option(False, "--no-progress", help="Reserved stable interface."),
    debug: bool = typer.Option(False, "--debug", help="Show diagnostics on errors."),
) -> None:
    """Store frozen global protocol options."""
    if output_format not in {"human", "json"}:
        raise typer.BadParameter("must be human or json", param_hint="--format")
    ctx.obj = {
        "config": config,
        "format": output_format,
        "quiet": quiet,
        "no_progress": no_progress,
        "debug": debug,
    }


def _emit(
    ctx: typer.Context, payload: CommandEnvelope, human: Callable[[], str], *, exit_code: int = 0
) -> None:
    options = _context(ctx)
    if options.get("format") == "json":
        typer.echo(json_text(payload))
    else:
        typer.echo(human())
    if exit_code:
        raise typer.Exit(exit_code)


def _failure(ctx: typer.Context, command: str, error: StewardError) -> None:
    options = _context(ctx)
    if options.get("format") == "json":
        typer.echo(json_text(error_envelope(command, error)))
    else:
        typer.echo(f"Error [{error.code}]: {safe_text(error)}", err=True)
        if options.get("debug"):
            typer.echo(traceback.format_exc(), err=True)
    raise typer.Exit(error.exit_code)


def _run(
    ctx: typer.Context,
    command: str,
    action: Callable[[], tuple[CommandEnvelope, Callable[[], str], int]],
) -> None:
    try:
        payload, human, exit_code = action()
        _emit(ctx, payload, human, exit_code=exit_code)
    except StewardError as error:
        _failure(ctx, command, error)
    except typer.Exit:
        raise
    except Exception as error:  # Normal CLI boundary: never expose traceback by default.
        internal = StewardError(f"unexpected internal error: {type(error).__name__}")
        _failure(ctx, command, internal)


@runtime_app.command("status")
def runtime_status(ctx: typer.Context) -> None:
    """Report installed runtime profiles without loading optional backends."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = inspect_runtime_capabilities()
        payload = envelope("runtime.status", "OK", {"runtime_capabilities": report})

        def human() -> str:
            profiles = cast(dict[str, dict[str, object]], report["profiles"])
            lines = ["Runtime Capabilities:"]
            lines.extend(
                f"{name}: {profile['status']} ({profile['install_target']})"
                for name, profile in profiles.items()
            )
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "runtime.status", action)


def _config(ctx: typer.Context) -> StewardConfig:
    return load_config(_context(ctx).get("config"))


_PROJECTION_SOURCE_CODES = frozenset(
    {
        "SNAPSHOT_MISSING",
        "SNAPSHOT_REPOSITORY_INVALID",
        "SNAPSHOT_REUSE_SOURCE_INVALID",
        "PAIR_TEMPORAL_INVALID",
        "PAIR_DIRECTION_INVALID",
    }
)


def _projection_failure(error: Exception) -> tuple[str, str, int]:
    """Map Projection boundary failures to the frozen CLI error categories."""
    if isinstance(error, ObservationProjectionRequestError):
        if error.code in _PROJECTION_SOURCE_CODES:
            return error.code, "SOURCE_VALIDATION", 3
        return error.code, "CLI_REQUEST_VALIDATION", 2
    if isinstance(error, ObservationProjectionInvariantError):
        return error.code, "PROJECTION_INVARIANT", 4
    if isinstance(error, ObservationProjectionError):
        return error.code, "PROJECTION_INVARIANT", 4
    if isinstance(error, StewardError):
        return error.code, "CLI_REQUEST_VALIDATION", 2
    return "PROJECTION_INTERNAL_ERROR", "INTERNAL_INTEGRATION", 4


def _projection_json_output(projection: object, *, pretty: bool) -> str:
    """Render the existing complete Projection machine facts once for the CLI."""
    try:
        facts = getattr(projection, "facts")
        digest = getattr(projection, "projection_digest")
        result = {**machine_object(facts), "projection_digest": digest}
        return json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise ObservationProjectionInvariantError("CANONICAL_INVARIANT_VIOLATION") from error


def _run_projection(action: Callable[[], object], *, pretty: bool) -> None:
    """Emit one Projection JSON object or one structured error object."""
    try:
        typer.echo(_projection_json_output(action(), pretty=pretty))
    except Exception as error:  # Deliberate CLI boundary: do not expose a traceback.
        code, category, exit_code = _projection_failure(error)
        typer.echo(
            json.dumps(
                {"status": "error", "code": code, "category": category, "context": {}},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            err=True,
        )
        raise typer.Exit(exit_code) from None


@projection_app.command("diagnose")
def projection_diagnose(
    ctx: typer.Context,
    request_json: Path = typer.Option(
        ..., "--request-json", help="Complete Snapshot Diagnostic request JSON."
    ),
    policy_json: Path = typer.Option(
        ..., "--policy-json", help="Complete resolved Projection policy JSON."
    ),
    pretty: bool = typer.Option(
        False, "--pretty", help="Format the same machine JSON with indentation."
    ),
) -> None:
    """Emit read-only Projection machine JSON; no live scan or file action."""

    def action() -> object:
        request = decode_snapshot_diagnostic_request(load_json_object(request_json))
        policy = decode_projection_policy(load_json_object(policy_json))
        return build_snapshot_diagnostic_projection(_config(ctx), request, policy)

    _run_projection(action, pretty=pretty)


@projection_app.command("track")
def projection_track(
    ctx: typer.Context,
    request_json: Path = typer.Option(
        ..., "--request-json", help="Complete Pair Tracking request JSON."
    ),
    policy_json: Path = typer.Option(
        ..., "--policy-json", help="Complete resolved Projection policy JSON."
    ),
    pretty: bool = typer.Option(
        False, "--pretty", help="Format the same machine JSON with indentation."
    ),
) -> None:
    """Emit read-only Projection machine JSON; no live scan or file action."""

    def action() -> object:
        request = decode_pair_tracking_request(load_json_object(request_json))
        policy = decode_projection_policy(load_json_object(policy_json))
        return build_pair_tracking_projection(_config(ctx), request, policy)

    _run_projection(action, pretty=pretty)


def _summary(config: StewardConfig) -> dict[str, Any]:
    counts = {role.value: sum(scope.role == role for scope in config.scopes) for role in ScopeRole}
    return {
        "config_path": str(config.source_path),
        "project_name": config.project_name,
        "schema_version": config.schema_version,
        "counts": counts,
        "paths": to_jsonable(config.paths),
    }


def _storage_status_human(result: dict[str, Any]) -> str:
    """Render concise storage and Snapshot integrity facts without expanding items."""
    diagnostics = result.get("historical_evidence_diagnostics", [])
    lines = [
        f"Storage Status: {result['storage_status']}",
        f"Historical Evidence Diagnostics: {len(diagnostics)}",
        "",
        "Snapshot Storage",
    ]
    snapshot = result.get("snapshot_integrity")
    if not isinstance(snapshot, dict):
        lines.append("Status: UNAVAILABLE")
        lines.append("Issues: snapshot integrity check unavailable")
        return "\n".join(lines)
    labels = (
        ("Status", "status"),
        ("Snapshot Evidence", "snapshot_evidence_count"),
        ("Indexed Snapshots", "indexed_snapshot_count"),
        ("Indexed Entry Groups", "indexed_entry_group_count"),
        ("Indexed Entries", "indexed_entry_count"),
        ("Related Runs", "run_count"),
        ("Healthy Snapshots", "healthy_snapshot_count"),
        ("Degraded Snapshots", "degraded_snapshot_count"),
        ("Invalid Snapshots", "invalid_snapshot_count"),
        ("Orphan Evidence", "orphan_evidence_count"),
        ("Missing Evidence", "missing_evidence_count"),
        ("Duplicate Snapshot IDs", "duplicate_snapshot_id_count"),
        ("Duplicate Snapshot Runs", "duplicate_run_snapshot_count"),
        ("Duplicate Evidence Indexes", "duplicate_evidence_index_count"),
        ("Orphan Entries", "orphan_entry_count"),
        ("Cross-reference Entries", "cross_reference_entry_count"),
    )
    lines.extend(f"{label}: {snapshot[key]}" for label, key in labels)
    issues = snapshot["issues"]
    if not issues:
        lines.append("Issues: none")
    else:
        lines.append("Issues:")
        for issue in issues[:10]:
            identity = " ".join(
                value
                for value in (
                    issue.get("snapshot_id", ""),
                    issue.get("evidence_id", ""),
                    issue.get("persistent_run_id", ""),
                    issue.get("evidence_relative_path", ""),
                )
                if value
            )
            lines.append(
                f"- {safe_text(issue['code'])}{f' {safe_text(identity)}' if identity else ''}"
            )
        if len(issues) > 10:
            lines.append(f"- {len(issues) - 10} additional Snapshot issues")
    if diagnostics:
        lines.append("Historical Diagnostics:")
        for diagnostic in diagnostics[:10]:
            identity = " ".join(
                value
                for value in (
                    diagnostic.get("snapshot_id", ""),
                    diagnostic.get("persistent_run_id", ""),
                )
                if value
            )
            lines.append(
                f"- {safe_text(diagnostic['code'])}{f' {safe_text(identity)}' if identity else ''}"
            )
    return "\n".join(lines)


def _evidence_verify_human(results: list[Any], snapshot: dict[str, Any]) -> str:
    """Render per-Run verification with a concise Snapshot Evidence summary."""
    lines = [
        "\n".join(f"{item.run_id}: {item.status}" for item in results) or "No evidence.",
        "",
        "Snapshot Evidence",
        f"Snapshot Evidence Count: {snapshot['evidence_count']}",
        f"Valid Snapshot Evidence: {snapshot['valid_count']}",
        f"Invalid Snapshot Evidence: {snapshot['invalid_count']}",
        f"Snapshot Run Conflicts: {snapshot['run_missing_count'] + snapshot['run_invalid_count']}",
        f"Duplicate Snapshot IDs: {snapshot['duplicate_snapshot_id_count']}",
        f"Duplicate Snapshot Runs: {snapshot['duplicate_run_count']}",
    ]
    issues = snapshot["issues"]
    if not issues:
        lines.append("Snapshot Issues: none (VALID)")
        return "\n".join(lines)
    lines.append("Snapshot Issues:")
    for issue in issues[:10]:
        identity = " ".join(
            value
            for value in (
                issue.get("snapshot_id", ""),
                issue.get("evidence_id", ""),
                issue.get("persistent_run_id", ""),
                issue.get("evidence_relative_path", ""),
            )
            if value
        )
        lines.append(f"- {safe_text(issue['code'])}{f' {safe_text(identity)}' if identity else ''}")
    if len(issues) > 10:
        lines.append(f"- {len(issues) - 10} additional Snapshot issues")
    return "\n".join(lines)


@app.command()
def init(
    ctx: typer.Context,
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace only a regular configuration file."
    ),
) -> None:
    """Create the default local configuration and internal directories."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        explicit = _context(ctx).get("config")
        target = discover_config(explicit)
        root = project_root_for(target, explicit is not None)
        if target.exists() or target.is_symlink():
            if not overwrite:
                raise InitializationError(f"configuration already exists: {target}")
            if target.is_symlink() or not target.is_file():
                raise InitializationError(
                    "refusing to overwrite a non-regular or symbolic-link configuration target"
                )
        for path in (
            root / "config",
            root / "data",
            root / "data/cache",
            root / "data/evidence",
            root / "data/quarantine",
        ):
            path.mkdir(parents=True, exist_ok=True)
        example = root / "config" / "steward.example.toml"
        if not example.is_file():
            example = PROJECT_ROOT / "config" / "steward.example.toml"
        shutil.copyfile(example, target)
        result = {
            "config_path": str(target),
            "created_directories": [
                str(root / item)
                for item in ("config", "data", "data/cache", "data/evidence", "data/quarantine")
            ],
        }
        payload = envelope("init", "OK", result)
        return payload, lambda: f"Initialized configuration: {safe_text(target)}", 0

    _run(ctx, "init", action)


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the single configured TOML file."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        config = _config(ctx)
        result = _summary(config)
        payload = envelope("config.validate", "OK", result, warnings=list(config.warnings))
        counts = result["counts"]

        def human() -> str:
            return "\n".join(
                (
                    "Config: VALID",
                    "Schema Version: 1",
                    f"Managed Roots: {counts['managed_root']}",
                    f"Reference Roots: {counts['reference_root']}",
                    f"Excluded Roots: {counts['excluded_root']}",
                )
            )

        return payload, human, 0

    _run(ctx, "config.validate", action)


@scopes_app.command("list")
def scopes_list(ctx: typer.Context) -> None:
    """List configured scopes without recursive directory access."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        config = _config(ctx)
        entries: list[dict[str, Any]] = []
        for scope in config.scopes:
            exists = scope.normalized_path.is_dir()
            readable = exists and os.access(scope.normalized_path, os.R_OK)
            writable = exists and os.access(scope.normalized_path, os.W_OK)
            entries.append(
                {
                    "scope_id": scope.scope_id,
                    "role": scope.role.value,
                    "raw_path": scope.raw_path,
                    "normalized_path": str(scope.normalized_path),
                    "enabled": scope.enabled,
                    "follow_directory_symlinks": scope.follow_directory_symlinks,
                    "allow_cross_mount": scope.allow_cross_mount,
                    "exists": exists,
                    "readable": readable,
                    "writable": writable,
                }
            )
        payload = envelope("scopes.list", "OK", {"scopes": entries}, warnings=list(config.warnings))

        def human() -> str:
            blocks = []
            for entry in entries:
                blocks.append(
                    "\n".join(f"{key}: {safe_text(value)}" for key, value in entry.items())
                )
            return "\n\n".join(blocks) or "No scopes configured."

        return payload, human, 0

    _run(ctx, "scopes.list", action)


@storage_app.command("init")
def storage_init(ctx: typer.Context) -> None:
    """Initialize the explicit, rebuildable current-schema SQLite index."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        initialize_storage(_config(ctx))
        payload = envelope("storage.init", "OK", {"storage_status": "HEALTHY"})
        return payload, lambda: "Storage: INITIALIZED", 0

    _run(ctx, "storage.init", action)


@storage_app.command("status")
def storage_status_command(ctx: typer.Context) -> None:
    """Report index and ledger consistency without repairing either."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        result = to_jsonable(storage_status(_config(ctx)))
        payload = envelope("storage.status", "OK", result)
        return (
            payload,
            lambda: _storage_status_human(result),
            0 if result["storage_status"] in {"HEALTHY", "DEGRADED"} else 4,
        )

    _run(ctx, "storage.status", action)


@storage_app.command("rebuild-index")
def storage_rebuild(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Confirm replacement of derived index."),
) -> None:
    """Rebuild only the SQLite derived index from valid immutable evidence."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        if not yes:
            raise RebuildConfirmationError("storage rebuild-index requires --yes")
        rebuild_index(_config(ctx))
        payload = envelope(
            "storage.rebuild-index", "OK", {"storage_status": "HEALTHY", "rebuilt": True}
        )
        return payload, lambda: "Storage index: REBUILT", 0

    _run(ctx, "storage.rebuild-index", action)


@storage_app.command("migrate")
def storage_migrate(ctx: typer.Context, yes: bool = typer.Option(False, "--yes")) -> None:
    """Explicitly migrate a supported legacy derived index to the current schema."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        if not yes:
            raise StorageMigrationConfirmationError("storage migrate requires --yes")
        current = migrate_storage(_config(ctx))
        payload = envelope(
            "storage.migrate",
            "OK",
            {"already_current": current, "schema_version": SCHEMA_VERSION},
        )
        return payload, lambda: "Storage: ALREADY_CURRENT" if current else "Storage: MIGRATED", 0

    _run(ctx, "storage.migrate", action)


def _run_result(record: object) -> dict[str, Any]:
    return cast(dict[str, Any], to_jsonable(record))


def _snapshot_verification_human(verification: SnapshotVerificationResult) -> str:
    value = _run_result(verification)
    return (
        f"Snapshot ID: {value['snapshot_id']}\n"
        f"Verification Status: {value['status']}\n"
        f"Evidence ID: {value['evidence_id']}\n"
        f"Persistent Run ID: {value['persistent_run_id']}\n"
        f"Evidence Present: {value['evidence_present']}\n"
        f"Evidence Valid: {value['evidence_valid']}\n"
        f"Index Present: {value['index_present']}\n"
        f"Index Consistent: {value['index_consistent']}\n"
        f"Run Present: {value['run_present']}\n"
        f"Run Consistent: {value['run_consistent']}\n"
        f"Errors: {len(value['errors'])}\n"
        f"Warnings: {len(value['warnings'])}"
    )


def _verification_exit_code(status: str) -> int:
    return 0 if status == "VALID" else 4 if status == "INCOMPLETE" else 5


def _verification_failure(
    command: str, verification: SnapshotVerificationResult
) -> tuple[CommandEnvelope, Callable[[], str], int]:
    value = _run_result(verification)
    payload = envelope(
        command,
        str(value["status"]),
        {"verification": value},
        errors=list(value["errors"]),
        warnings=list(value["warnings"]),
    )
    return (
        payload,
        lambda: _snapshot_verification_human(verification),
        _verification_exit_code(str(value["status"])),
    )


def _snapshot_id(value: str) -> str:
    """Accept only the UUID form used by persisted Snapshot identities."""
    try:
        return str(UUID(value))
    except ValueError as error:
        raise SnapshotNotFoundError(f"invalid snapshot ID: {value}") from error


def _relation_snapshot_id(value: str) -> str:
    """Relations use the frozen domain error rather than a generic CLI failure."""
    try:
        return str(UUID(value))
    except ValueError as error:
        from .errors import RelationError

        raise RelationError("RELATION_INVALID: snapshot IDs must be UUIDs") from error


def _duplicate_snapshot_id(value: str) -> str:
    """Duplicate analysis uses its frozen domain error for invalid identities."""
    try:
        return str(UUID(value))
    except ValueError as error:
        raise DuplicateAnalysisError("DUPLICATE_INVALID: snapshot ID must be a UUID") from error


def _structure_snapshot_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise StructureError("STRUCTURE_INVALID: snapshot ID must be a UUID") from error


def _growth_snapshot_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise GrowthError("GROWTH_INVALID: snapshot IDs must be UUIDs") from error


def _structure_rank(value: str | None) -> StructureRank | None:
    if value is None:
        return None
    try:
        return StructureRank(value)
    except ValueError as error:
        raise StructureError("STRUCTURE_INVALID: unsupported structure rank") from error


def _growth_rank(value: str | None) -> GrowthRank | None:
    if value is None:
        return None
    try:
        return GrowthRank(value)
    except ValueError as error:
        raise GrowthError("GROWTH_INVALID: unsupported growth rank") from error


def _relation_kind(value: str | None) -> RelationKind | None:
    if value is None:
        return None
    try:
        return RelationKind(value)
    except ValueError as error:
        from .errors import RelationError

        raise RelationError("RELATION_INVALID: unsupported relation kind") from error


@runs_app.command("create")
def runs_create(ctx: typer.Context, kind: str = typer.Option(..., "--kind")) -> None:
    """Create a Run and its immutable run.created evidence."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        record = create_run(_config(ctx), kind)
        result = {"run": _run_result(record)}
        payload = envelope("runs.create", "OK", result)
        return (
            payload,
            lambda: (
                f"Run ID: {record.run_id}\nKind: {record.run_kind}\nStatus: {record.status.value}\nCreated At: {record.created_at}\nEvidence Sequence: {record.last_sequence}\nEvidence Digest: {record.last_evidence_digest}"
            ),
            0,
        )

    _run(ctx, "runs.create", action)


@runs_app.command("list")
def runs_list(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status"),
    kind: str | None = typer.Option(None, "--kind"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List Runs by newest creation time."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        selected = RunStatus(status) if status else None
        records = list_runs(_config(ctx), selected, kind, limit)
        result = {"runs": [_run_result(record) for record in records]}
        payload = envelope("runs.list", "OK", result)
        return (
            payload,
            lambda: (
                "\n".join(
                    f"{record.run_id} {record.run_kind} {record.status.value}" for record in records
                )
                or "No runs."
            ),
            0,
        )

    _run(ctx, "runs.list", action)


@runs_app.command("show")
def runs_show(ctx: typer.Context, run_id: str) -> None:
    """Show one Run and evidence summaries, not full payloads."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        config = _config(ctx)
        record = get_run(config, run_id)
        evidence = evidence_records(config, run_id)
        result = {"run": _run_result(record), "evidence": [_run_result(item) for item in evidence]}
        payload = envelope("runs.show", "OK", result)
        return (
            payload,
            lambda: (
                f"Run ID: {record.run_id}\nStatus: {record.status.value}\nEvidence: {len(evidence)}"
            ),
            0,
        )

    _run(ctx, "runs.show", action)


@runs_app.command("cancel")
def runs_cancel(
    ctx: typer.Context, run_id: str, reason: str = typer.Option(..., "--reason")
) -> None:
    """Cancel an eligible Run through the centralized state machine."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        record = transition_run(_config(ctx), run_id, RunStatus.CANCELLED, reason)
        result = {
            "run": _run_result(record),
            "already_cancelled": record.status == RunStatus.CANCELLED and record.last_sequence == 1,
        }
        payload = envelope("runs.cancel", "OK", result)
        return payload, lambda: f"Run ID: {record.run_id}\nStatus: {record.status.value}", 0

    _run(ctx, "runs.cancel", action)


@evidence_app.command("verify")
def evidence_verify(
    ctx: typer.Context, run_id: str | None = typer.Option(None, "--run-id")
) -> None:
    """Verify immutable ledger hashes, sequencing, transitions, and index agreement."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = verify_evidence_report(_config(ctx), run_id)
        results = list(report.verifications)
        snapshot = _run_result(report.snapshot_evidence)
        result = {
            "verifications": [_run_result(item) for item in results],
            "snapshot_evidence": snapshot,
        }
        valid = all(item.status == "VALID" for item in results)
        payload = envelope("evidence.verify", "OK" if valid else "INCOMPLETE", result)
        return (
            payload,
            lambda: _evidence_verify_human(results, snapshot),
            0 if valid else 4,
        )

    _run(ctx, "evidence.verify", action)


@snapshots_app.command("create")
def snapshots_create(
    ctx: typer.Context,
    scope: list[str] = typer.Option([], "--scope"),
    max_entries: int | None = typer.Option(None, "--max-entries"),
    max_total_size: int | None = typer.Option(None, "--max-total-size"),
    max_duration_seconds: float | None = typer.Option(None, "--max-duration-seconds"),
    max_depth: int | None = typer.Option(None, "--max-depth"),
    payload_hash: bool = typer.Option(False, "--payload-hash"),
    reuse_verified_payloads: bool = typer.Option(False, "--reuse-verified-payloads"),
    max_hash_file_bytes: int | None = typer.Option(None, "--max-hash-file-bytes"),
    max_total_hash_bytes: int | None = typer.Option(None, "--max-total-hash-bytes"),
    max_hash_duration_seconds: float | None = typer.Option(None, "--max-hash-duration-seconds"),
    hash_chunk_size: int | None = typer.Option(None, "--hash-chunk-size"),
) -> None:
    """Perform a bounded read-only filesystem observation."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        hash_options = (
            max_hash_file_bytes,
            max_total_hash_bytes,
            max_hash_duration_seconds,
            hash_chunk_size,
        )
        if not payload_hash and (
            reuse_verified_payloads or any(option is not None for option in hash_options)
        ):
            raise SnapshotBudgetError(
                "PAYLOAD_HASH_POLICY_INVALID: payload hash options require --payload-hash"
            )
        policy = (
            default_payload_hash_policy(
                max_hash_file_bytes=(
                    max_hash_file_bytes if max_hash_file_bytes is not None else 1_073_741_824
                ),
                max_total_hash_bytes=(
                    max_total_hash_bytes if max_total_hash_bytes is not None else 8_589_934_592
                ),
                max_hash_duration_seconds=(
                    max_hash_duration_seconds if max_hash_duration_seconds is not None else 300.0
                ),
                hash_chunk_size=hash_chunk_size if hash_chunk_size is not None else 1_048_576,
                allow_verified_reuse=reuse_verified_payloads,
            )
            if payload_hash
            else None
        )
        snapshot = create_snapshot(
            _config(ctx),
            tuple(scope),
            make_budget(max_entries, max_total_size, max_duration_seconds, max_depth),
            policy,
        )
        result = {"snapshot": _run_result(snapshot)}
        payload = envelope("snapshots.create", "OK", result)
        return (
            payload,
            lambda: (
                f"Snapshot ID: {snapshot.snapshot_id}\nRun ID: {snapshot.run_id}\nStatus: {snapshot.status.value}\nEntries: {snapshot.entry_count}\nSnapshot Digest: {snapshot.snapshot_digest}"
            ),
            4 if snapshot.status.value == "partial" else 0,
        )

    _run(ctx, "snapshots.create", action)


def _acquisition_human(result: dict[str, Any]) -> str:
    verification = result.get("verification")
    verification_status = (
        verification.get("status", "UNAVAILABLE")
        if isinstance(verification, dict)
        else "UNAVAILABLE"
    )
    return (
        f"Acquisition Disposition: {result['disposition']}\n"
        f"Run ID: {result['run_id']}\n"
        f"Run Status: {result['run_status']}\n"
        f"Snapshot ID: {result['snapshot_id']}\n"
        f"Snapshot Status: {result['snapshot_status']}\n"
        f"Entries: {result['entry_count']}\n"
        f"Verification Status: {verification_status}"
    )


@snapshots_app.command("acquire")
def snapshots_acquire(
    ctx: typer.Context,
    scope: str = typer.Option(..., "--scope"),
    yes: bool = typer.Option(False, "--yes"),
    max_entries: int | None = typer.Option(None, "--max-entries"),
    max_total_stat_bytes: int | None = typer.Option(None, "--max-total-stat-bytes"),
    max_duration_seconds: float | None = typer.Option(None, "--max-duration-seconds"),
    max_depth: int | None = typer.Option(None, "--max-depth"),
) -> None:
    """Create one lifecycle-closed metadata-only Snapshot for an explicit scope."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = acquire_snapshot(
            _config(ctx),
            SnapshotAcquisitionRequest(
                scope,
                make_budget(
                    max_entries,
                    max_total_stat_bytes,
                    max_duration_seconds,
                    max_depth,
                ),
                yes,
            ),
        )
        result = _run_result(report)
        incomplete = report.disposition == "PARTIAL"
        payload = envelope("snapshots.acquire", "INCOMPLETE" if incomplete else "OK", result)
        return payload, lambda: _acquisition_human(result), 4 if incomplete else 0

    _run(ctx, "snapshots.acquire", action)


@snapshots_app.command("acquisition-status")
def snapshots_acquisition_status(ctx: typer.Context, run_id: str) -> None:
    """Classify one acquisition ledger without repair or current-scope access."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = snapshot_acquisition_status(_config(ctx), run_id)
        result = _run_result(report)
        payload = envelope("snapshots.acquisition-status", "OK", result)
        return payload, lambda: _acquisition_human(result), 0

    _run(ctx, "snapshots.acquisition-status", action)


@snapshots_app.command("recover-acquisition")
def snapshots_recover_acquisition(
    ctx: typer.Context,
    run_id: str,
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Explicitly close one governed acquisition prefix without rescanning its scope."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = recover_snapshot_acquisition(_config(ctx), run_id, confirmed=yes)
        result = _run_result(report)
        incomplete = report.disposition == "PARTIAL"
        payload = envelope(
            "snapshots.recover-acquisition",
            "INCOMPLETE" if incomplete else "OK",
            result,
        )
        return payload, lambda: _acquisition_human(result), 4 if incomplete else 0

    _run(ctx, "snapshots.recover-acquisition", action)


def _change_review_human(review: dict[str, Any]) -> str:
    summary = review["diff_summary"]
    events = review["event_summary"]
    lines = [
        f"Base Snapshot ID: {review['base_snapshot_id']}",
        f"Target Snapshot ID: {review['target_snapshot_id']}",
        f"Scope ID: {safe_text(review['scope_id'])}",
        f"Created: {events['created_count']}",
        f"Deleted Locations: {events['deleted_count']}",
        f"Modified: {events['modified_count']}",
        f"Unchanged: {summary['unchanged_count']}",
        f"Returned Changes: {review['returned_count']} / {review['full_event_count']}",
        f"Has More: {str(review['has_more']).lower()}",
        f"Review Digest: {review['review_digest']}",
    ]
    for item in review["items"]:
        lines.append(
            " ".join(
                (
                    item["event_type"],
                    f"scope={safe_text(item['scope_id'])}",
                    f"path={safe_text(item['relative_path'])}",
                    f"fields={safe_text(','.join(item['changed_fields']))}",
                )
            )
        )
    return "\n".join(lines)


@snapshots_app.command("refresh")
def snapshots_refresh(
    ctx: typer.Context,
    scope: str = typer.Option(..., "--scope"),
    against: str = typer.Option(..., "--against"),
    yes: bool = typer.Option(False, "--yes"),
    max_entries: int | None = typer.Option(None, "--max-entries"),
    max_total_stat_bytes: int | None = typer.Option(None, "--max-total-stat-bytes"),
    max_duration_seconds: float | None = typer.Option(None, "--max-duration-seconds"),
    max_depth: int | None = typer.Option(None, "--max-depth"),
    change_limit: int = typer.Option(100, "--change-limit"),
    change_offset: int = typer.Option(0, "--change-offset"),
) -> None:
    """Acquire one same-Scope Snapshot and publish a bounded verified change review."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        report = refresh_snapshot(
            _config(ctx),
            SnapshotRefreshRequest(
                scope,
                against,
                make_budget(
                    max_entries if max_entries is not None else MAX_REFRESH_ENTRIES,
                    max_total_stat_bytes,
                    max_duration_seconds,
                    max_depth,
                ),
                yes,
                change_limit,
                change_offset,
            ),
        )
        result = _run_result(report)
        complete = report.disposition == "COMPLETE"
        payload = envelope(
            "snapshots.refresh",
            "OK" if complete else "INCOMPLETE",
            result,
            errors=list(report.review_errors),
        )

        def human() -> str:
            lines = [
                f"Refresh Disposition: {report.disposition}",
                f"Base Snapshot ID: {report.base_snapshot_id}",
                f"Target Snapshot ID: {report.acquisition.snapshot_id}",
                f"Run ID: {report.acquisition.run_id}",
                f"Run Status: {report.acquisition.run_status}",
            ]
            if report.review is not None:
                lines.extend(("", _change_review_human(_run_result(report.review))))
            elif report.review_errors:
                lines.append(f"Review Error: {safe_text(report.review_errors[0]['code'])}")
            return "\n".join(lines)

        return payload, human, 0 if complete else 4

    _run(ctx, "snapshots.refresh", action)


@snapshots_app.command("change-review")
def snapshots_change_review(
    ctx: typer.Context,
    base_snapshot_id: str,
    target_snapshot_id: str,
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Review one bounded page of deterministic same-Scope Snapshot changes."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        review = review_snapshot_changes(
            _config(ctx),
            SnapshotChangeReviewRequest(
                base_snapshot_id,
                target_snapshot_id,
                limit,
                offset,
            ),
        )
        result = _run_result(review)
        payload = envelope("snapshots.change-review", "OK", result)
        return payload, lambda: _change_review_human(result), 0

    _run(ctx, "snapshots.change-review", action)


@snapshots_app.command("list")
def snapshots_list(ctx: typer.Context, limit: int = typer.Option(50, "--limit")) -> None:
    """List persisted snapshot summaries without filesystem access."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        summaries = list_snapshots(_config(ctx), limit)
        result = {"snapshots": [_run_result(item) for item in summaries]}
        payload = envelope("snapshots.list", "OK", result)
        return (
            payload,
            lambda: (
                "\n".join(
                    f"{item.snapshot_id} {item.status.value} {item.entry_count}"
                    for item in summaries
                )
                or "No snapshots."
            ),
            0,
        )

    _run(ctx, "snapshots.list", action)


@snapshots_app.command("show")
def snapshots_show(ctx: typer.Context, snapshot_id: str) -> None:
    """Show metadata only after authoritative Snapshot verification succeeds."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        config = _config(ctx)
        verification, snapshot = _verified_snapshot_detail(config, snapshot_id)
        if verification.status != "VALID":
            return _verification_failure("snapshots.show", verification)
        result = {
            "verification": _run_result(verification),
            "snapshot": _run_result(snapshot),
        }
        payload = envelope("snapshots.show", "OK", result)
        return (
            payload,
            lambda: (
                f"Verification Status: {verification.status}\nSnapshot ID: {snapshot.snapshot_id}\nPersistent Run ID: {snapshot.run_id}\nStatus: {snapshot.status.value}\nConsistency: {snapshot.consistency.value}\nCreated At: {snapshot.created_at}\nStarted At: {snapshot.started_at}\nCompleted At: {snapshot.completed_at}\nScope IDs: {', '.join(snapshot.scope_ids)}\nEntry Count: {snapshot.entry_count}\nObserved Count: {snapshot.observed_count}\nError Count: {snapshot.error_count}\nExcluded Count: {snapshot.excluded_count}\nRegular File Bytes: {snapshot.total_regular_file_bytes}\nMaximum Depth: {snapshot.max_depth_observed}\nEntries Digest: {snapshot.entries_digest}\nSnapshot Digest: {snapshot.snapshot_digest}\nEvidence ID: {snapshot.evidence_id}\nEvidence Relative Path: {snapshot.evidence_relative_path}"
            ),
            0,
        )

    _run(ctx, "snapshots.show", action)


@snapshots_app.command("entries")
def snapshots_entries(
    ctx: typer.Context,
    snapshot_id: str,
    scope: str | None = typer.Option(None, "--scope"),
    object_type: str | None = typer.Option(None, "--type"),
    observation_status: str | None = typer.Option(None, "--status"),
    path_prefix: str | None = typer.Option(None, "--path-prefix"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Query verified historical Entry rows with stable filters and pagination."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        from .models import FilesystemObjectType, FilesystemObservationStatus

        config = _config(ctx)
        verification, snapshot, page = _verified_snapshot_entries(
            config,
            snapshot_id,
            scope,
            FilesystemObjectType(object_type) if object_type else None,
            FilesystemObservationStatus(observation_status) if observation_status else None,
            path_prefix,
            limit,
            offset,
        )
        if verification.status != "VALID":
            return _verification_failure("snapshots.entries", verification)
        result = {
            "verification": _run_result(verification),
            "page": _run_result(page),
        }
        payload = envelope("snapshots.entries", "OK", result)
        return (
            payload,
            lambda: (
                f"Verification Status: {verification.status}\n"
                + (
                    "\n".join(
                        f"{safe_text(entry.scope_id)} {safe_text(entry.relative_path)} {entry.object_type.value}"
                        for entry in page.entries
                    )
                    or "No entries."
                )
            ),
            0,
        )

    _run(ctx, "snapshots.entries", action)


@snapshots_app.command("verify")
def snapshots_verify(ctx: typer.Context, snapshot_id: str) -> None:
    """Verify one persisted Snapshot across Evidence, index, and Run lifecycle."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        verification = verify_snapshot(_config(ctx), snapshot_id)
        result = {"verification": _run_result(verification)}
        payload = envelope(
            "snapshots.verify",
            verification.status,
            result,
            errors=list(verification.errors),
            warnings=list(verification.warnings),
        )
        code = _verification_exit_code(verification.status)
        return (
            payload,
            lambda: _snapshot_verification_human(verification),
            code,
        )

    _run(ctx, "snapshots.verify", action)


@snapshots_app.command("diff")
def snapshots_diff(ctx: typer.Context, left_snapshot_id: str, right_snapshot_id: str) -> None:
    """Review deterministic changes between two verified persisted Snapshots."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        left_id = _snapshot_id(left_snapshot_id)
        right_id = _snapshot_id(right_snapshot_id)
        snapshot_diff = compute_verified_snapshot_diff(_config(ctx), left_id, right_id)
        events = change_events_from_snapshot_diff(snapshot_diff)
        event_summary = summarize_change_events(events)
        result = {
            "left_snapshot_id": left_id,
            "right_snapshot_id": right_id,
            "snapshot_diff": _run_result(snapshot_diff),
            "change_events": [_run_result(event) for event in events],
            "change_event_summary": _run_result(event_summary),
        }
        payload = envelope("snapshots.diff", "OK", result)

        def value(item: object) -> str:
            if item is None:
                return "null"
            if isinstance(item, bool):
                return str(item).lower()
            return safe_text(item)

        def human() -> str:
            summary = snapshot_diff.summary
            lines = [
                f"Left Snapshot ID: {left_id}",
                f"Right Snapshot ID: {right_id}",
                "Diff Summary:",
                f"Added: {summary.added_count}",
                f"Removed: {summary.removed_count}",
                f"Modified: {summary.modified_count}",
                f"Unchanged: {summary.unchanged_count}",
                f"Total Left: {summary.removed_count + summary.modified_count + summary.unchanged_count}",
                f"Total Right: {summary.added_count + summary.modified_count + summary.unchanged_count}",
            ]
            if not events:
                lines.extend(("", "No filesystem changes."))
                return "\n".join(lines)
            lines.append("Change Events:")
            lines.extend(
                " ".join(
                    (
                        event.event_type.value,
                        f"scope={safe_text(event.scope_id)}",
                        f"path={safe_text(event.relative_path)}",
                        f"size_delta={value(event.size_delta)}",
                        f"hash_changed={value(event.hash_changed)}",
                        f"metadata_changed={value(event.metadata_changed)}",
                    )
                )
                for event in events
            )
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "snapshots.diff", action)


@snapshots_app.command("relate")
def snapshots_relate(
    ctx: typer.Context,
    base_snapshot_id: str,
    target_snapshot_id: str,
    kind: str | None = typer.Option(None, "--kind"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Query verified cross-Snapshot relations without persisting a result."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        base_id = _relation_snapshot_id(base_snapshot_id)
        target_id = _relation_snapshot_id(target_snapshot_id)
        result = query_verified_snapshot_relations(
            _config(ctx),
            base_id,
            target_id,
            kind=_relation_kind(kind),
            limit=limit,
            offset=offset,
        )
        output = {"relation_query": _run_result(result)}
        payload = envelope("snapshots.relate", "OK", output)

        def references(label: str, values: tuple[Any, ...]) -> list[str]:
            lines = [f"  {label}:"]
            lines.extend(
                f"    - scope={safe_text(item.scope_id)} path={safe_text(item.relative_path)}"
                for item in values
            )
            return lines

        def human() -> str:
            start = result.offset + 1 if result.returned_relation_item_count else 0
            end = result.offset + result.returned_relation_item_count
            lines = [
                f"Base Snapshot ID: {result.base_snapshot_id}",
                f"Target Snapshot ID: {result.target_snapshot_id}",
                f"Relation Schema Version: {result.relation_schema_version}",
                f"Algorithm: {result.algorithm} v{result.algorithm_version}",
                f"Relation Set Digest: {result.relation_set_digest}",
                f"Complete Relation Items: {result.relation_item_count}",
                f"Filtered Relation Items: {result.filtered_relation_item_count}",
                f"Page: {start}-{end} (limit {result.limit}, offset {result.offset})",
                f"Next Offset: {result.next_offset if result.next_offset is not None else 'none'}",
                f"Kind Filter: {result.kind_filter.value if result.kind_filter else 'none'}",
            ]
            if not result.relation_items:
                lines.extend(("", "No relations in this page."))
                return "\n".join(lines)
            lines.append("Relations:")
            for item in result.relation_items:
                detail = item.certainty.value
                if item.certainty.value == "CANDIDATE":
                    detail += " (not a confirmed move or rename)"
                elif item.certainty.value == "AMBIGUOUS":
                    detail += " (no one-to-one assignment)"
                elif item.certainty.value == "UNKNOWN":
                    detail += " (evidence insufficient)"
                lines.extend(
                    (
                        f"- {item.kind.value} [{detail}]",
                        f"  Relation ID: {item.relation_id}",
                        f"  Reason Codes: {', '.join(item.reason_codes)}",
                        f"  Ambiguity Group: {item.ambiguity_group_id or 'none'}",
                    )
                )
                lines.extend(references("Source", item.source_entries))
                lines.extend(references("Target", item.target_entries))
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "snapshots.relate", action)


@snapshots_app.command("duplicates")
def snapshots_duplicates(
    ctx: typer.Context,
    snapshot_id: str,
    only_exact: bool = typer.Option(False, "--only-exact"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Query verified exact-payload groups without persisting a result."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        selected_snapshot_id = _duplicate_snapshot_id(snapshot_id)
        result = query_verified_snapshot_duplicates(
            _config(ctx),
            selected_snapshot_id,
            only_exact=only_exact,
            limit=limit,
            offset=offset,
        )
        payload = envelope("snapshots.duplicates", "OK", {"duplicate_query": _run_result(result)})

        def references(values: tuple[Any, ...]) -> list[str]:
            return [
                f"    - snapshot={safe_text(item.snapshot_id)} scope={safe_text(item.scope_id)} path={safe_text(item.relative_path)}"
                for item in values
            ]

        def human() -> str:
            coverage = result.coverage
            physical = result.physical_storage
            start = result.offset + 1 if result.returned_payload_equality_group_count else 0
            end = result.offset + result.returned_payload_equality_group_count
            lines = [
                f"Snapshot ID: {result.snapshot_id}",
                f"Analysis Schema Version: {result.analysis_schema_version}",
                f"Algorithm: {result.algorithm} v{result.algorithm_version}",
                f"Complete Analysis Digest: {result.analysis_digest}",
                f"Complete Payload Equality Groups: {result.payload_equality_group_count}",
                f"Filtered Payload Equality Groups: {result.filtered_payload_equality_group_count}",
                f"Page: {start}-{end} (limit {result.limit}, offset {result.offset})",
                f"Next Offset: {result.next_offset if result.next_offset is not None else 'none'}",
                f"Only Exact Filter: {str(result.only_exact).lower()}",
                "",
                "Coverage",
                f"Total Entries: {coverage.total_entry_count}",
                f"Total Regular Entries: {coverage.total_regular_entry_count}",
                f"Payload-Analyzable Regular Entries: {coverage.payload_analyzable_regular_entry_count}",
                f"Payload-Unknown Regular Entries: {coverage.payload_unknown_regular_entry_count}",
                f"Analyzable Logical Bytes: {coverage.analyzable_logical_bytes}",
                f"Unknown Logical Bytes: {coverage.unknown_logical_bytes}",
                f"Alias Paths: {coverage.alias_path_count}",
                f"Known Storage Units: {coverage.known_storage_unit_count}",
                f"Unknown Storage-Unit Memberships: {coverage.unknown_storage_unit_membership_count}",
                "Payload Unknown Reasons: "
                + (
                    ", ".join(
                        f"{item.code}={item.count}"
                        for item in coverage.payload_unknown_reason_counts
                    )
                    or "none"
                ),
                "",
                "Physical Storage Boundary",
                f"Observed Allocation Status: {physical.allocation_status.value}",
                f"Physical Block Sharing: {physical.physical_block_sharing_status.value}",
                f"Reclaimable Space: {physical.reclaimable_status.value}",
                "Logical redundant bytes are not reclaimable disk space.",
            ]
            if not result.payload_equality_groups:
                lines.extend(("", "No payload equality groups in this page."))
            else:
                lines.extend(("", "Payload Equality Groups"))
                for group in result.payload_equality_groups:
                    if group.is_exact_duplicate:
                        classification = (
                            "exact payload duplicate across known distinct storage units"
                        )
                    elif (
                        group.known_storage_unit_count == 1
                        and group.unknown_storage_unit_count == 0
                    ):
                        classification = "payload equality with hard-link aliases; one storage unit, not multiple storage copies"
                    else:
                        classification = "payload equality with storage-unit membership unknown"
                    lines.extend(
                        (
                            f"- {classification}",
                            f"  Payload Group ID: {group.payload_group_id}",
                            f"  SHA-256: {group.digest}",
                            f"  Logical Size Bytes: {group.logical_size_bytes}",
                            f"  Members: {len(group.member_entries)}",
                            f"  Known Storage Units: {group.known_storage_unit_count}",
                            f"  Unknown Storage Units: {group.unknown_storage_unit_count}",
                            f"  Logical Redundant Bytes: {group.logical_redundant_bytes if group.logical_redundant_bytes is not None else 'UNKNOWN'}",
                        )
                    )
                    lines.extend(references(group.member_entries))
            lines.extend(("", "Hard-Link Alias Sets"))
            if not result.hard_link_alias_sets:
                lines.append("none")
            for alias in result.hard_link_alias_sets:
                lines.append(
                    f"- Alias Set {alias.alias_set_id}: device={alias.device_id} inode={alias.inode} paths={len(alias.member_entries)} (one observed storage unit, not multiple storage copies)"
                )
                lines.extend(references(alias.member_entries))
            lines.extend(("", "Integrity Conflicts"))
            if not result.integrity_conflicts:
                lines.append("none")
            for conflict in result.integrity_conflicts:
                lines.append(f"- {conflict.code}")
                lines.extend(references(conflict.entries))
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "snapshots.duplicates", action)


@snapshots_app.command("structure")
def snapshots_structure(
    ctx: typer.Context,
    snapshot_id: str,
    scope: str | None = typer.Option(None, "--scope"),
    path_prefix: str | None = typer.Option(None, "--path-prefix"),
    depth: int | None = typer.Option(None, "--depth"),
    rank: str | None = typer.Option(None, "--rank"),
    min_bytes: int | None = typer.Option(None, "--min-bytes"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Query one verified Snapshot Path View without persisting a result."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        result = query_verified_snapshot_structure(
            _config(ctx),
            _structure_snapshot_id(snapshot_id),
            scope=scope,
            path_prefix=path_prefix,
            depth=depth,
            rank=_structure_rank(rank),
            min_bytes=min_bytes,
            limit=limit,
            offset=offset,
        )
        payload = envelope("snapshots.structure", "OK", {"structure_query": _run_result(result)})

        def human() -> str:
            coverage = result.coverage
            physical = result.physical_boundary
            start = result.offset + 1 if result.returned_path_node_count else 0
            end = result.offset + result.returned_path_node_count
            lines = [
                "Storage Structure",
                f"Snapshot ID: {result.snapshot_id}",
                f"Structure Schema Version: {result.structure_schema_version}",
                f"Algorithm: {result.algorithm} v{result.algorithm_version}",
                f"Complete Structure Digest: {result.structure_digest}",
                f"Complete Path Nodes: {result.full_path_node_count}",
                f"Selected Path Nodes: {result.selected_path_node_count}",
                f"Page: {start}-{end} (limit {result.limit}, offset {result.offset})",
                f"Next Offset: {result.next_offset if result.next_offset is not None else 'none'}",
                f"Scope: {result.scope_filter or 'all'}",
                f"Path Prefix: {result.path_prefix_filter or '.'}",
                f"Depth: {result.depth if result.depth is not None else 'all'}",
                f"Rank: {result.rank.value if result.rank else 'canonical'}",
                f"Minimum Bytes: {result.min_bytes if result.min_bytes is not None else 'none'}",
                "",
                "Coverage",
                f"Known Path Logical Bytes: {coverage.known_logical_bytes}",
                f"Known-Size Regular Files: {coverage.known_size_regular_file_count}",
                f"Unknown-Size Regular Files: {coverage.unknown_size_regular_file_count}",
                f"Excluded Entries: {coverage.excluded_entry_count}",
                f"Metadata-Failed Entries: {coverage.metadata_failed_entry_count}",
                f"Coverage Complete: {str(coverage.complete).lower()}",
                "",
                "Physical Storage Boundary",
                "Object-Aware Capacity: UNKNOWN (deferred)",
                f"Allocation: {physical.allocation_status.value}",
                f"Physical Block Sharing: {physical.physical_block_sharing_status.value}",
                f"Reclaimable Space: {physical.reclaimable_status.value}",
                "",
                "Path View Nodes",
            ]
            if not result.path_nodes:
                lines.append("No path nodes in this page.")
            for node in result.path_nodes:
                representation = (
                    "observed directory Entry"
                    if node.observed_directory_entry
                    else "derived path prefix"
                )
                lines.extend(
                    (
                        f"- scope={safe_text(node.scope_id)} path={safe_text(node.relative_directory_path)} ({representation})",
                        f"  Path Node ID: {node.path_node_id}",
                        f"  Direct Logical Bytes: {node.direct_known_logical_bytes}",
                        f"  Recursive Logical Bytes: {node.recursive_known_logical_bytes}",
                        f"  Direct Regular Files: {node.direct_regular_file_count}",
                        f"  Recursive Regular Files: {node.recursive_regular_file_count}",
                        f"  Recursive Unknown-Size Regular Files: {node.recursive_unknown_size_regular_count}",
                    )
                )
            lines.extend(("", "Limitations"))
            if not result.limitations:
                lines.append("none")
            else:
                lines.extend(f"- {item.code}" for item in result.limitations)
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "snapshots.structure", action)


@snapshots_app.command("growth")
def snapshots_growth(
    ctx: typer.Context,
    base_snapshot_id: str,
    target_snapshot_id: str,
    scope: str | None = typer.Option(None, "--scope"),
    path_prefix: str | None = typer.Option(None, "--path-prefix"),
    depth: int | None = typer.Option(None, "--depth"),
    rank: str | None = typer.Option(None, "--rank"),
    min_bytes: int | None = typer.Option(None, "--min-bytes"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """Query directional verified Snapshot growth without persisting a result."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        result = query_verified_snapshot_growth(
            _config(ctx),
            _growth_snapshot_id(base_snapshot_id),
            _growth_snapshot_id(target_snapshot_id),
            scope=scope,
            path_prefix=path_prefix,
            depth=depth,
            rank=_growth_rank(rank),
            min_bytes=min_bytes,
            limit=limit,
            offset=offset,
        )
        payload = envelope("snapshots.growth", "OK", {"growth_query": _run_result(result)})

        def human() -> str:
            coverage = result.coverage
            physical = result.physical_boundary
            start = result.offset + 1 if result.returned_path_node_count else 0
            end = result.offset + result.returned_path_node_count
            lines = [
                "Storage Growth",
                f"Base Snapshot ID: {result.base_snapshot_id}",
                f"Target Snapshot ID: {result.target_snapshot_id}",
                f"Growth Schema Version: {result.growth_schema_version}",
                f"Algorithm: {result.algorithm} v{result.algorithm_version}",
                f"Complete Growth Digest: {result.growth_digest}",
                f"Complete Path Nodes: {result.full_path_node_count}",
                f"Selected Path Nodes: {result.selected_path_node_count}",
                f"Page: {start}-{end} (limit {result.limit}, offset {result.offset})",
                f"Next Offset: {result.next_offset if result.next_offset is not None else 'none'}",
                f"Scope: {result.scope_filter or 'all'}",
                f"Path Prefix: {result.path_prefix_filter or '.'}",
                f"Depth: {result.depth if result.depth is not None else 'all'}",
                f"Rank: {result.rank.value if result.rank else 'canonical'}",
                f"Minimum Bytes: {result.min_bytes if result.min_bytes is not None else 'none'}",
                "",
                "Growth Coverage",
                f"Known Logical Delta: {coverage.known_net_logical_delta}",
                f"Added Bytes: {sum(item.added_logical_bytes for item in result.scope_summaries)}",
                f"Removed Bytes: {sum(item.removed_logical_bytes for item in result.scope_summaries)}",
                f"Unknown-Size Contributions: {coverage.unknown_size_contribution_count}",
                f"Decomposition Complete: {str(coverage.decomposition_complete).lower()}",
                "Content Attribution: not part of storage growth v0.1",
                "Physical Disk Growth: UNKNOWN (logical Path View only)",
                "",
                "Physical Storage Boundary",
                "Object-Aware Capacity: UNKNOWN (deferred)",
                f"Allocation: {physical.allocation_status.value}",
                f"Physical Block Sharing: {physical.physical_block_sharing_status.value}",
                f"Reclaimable Space: {physical.reclaimable_status.value}",
                "",
                "Path Growth Nodes",
            ]
            if not result.path_nodes:
                lines.append("No path nodes in this page.")
            for node in result.path_nodes:
                lines.extend(
                    (
                        f"- scope={safe_text(node.scope_id)} path={safe_text(node.relative_directory_path)}",
                        f"  Growth Node ID: {node.growth_node_id}",
                        f"  Base Path Logical Bytes: {node.recursive_base_known_logical_bytes}",
                        f"  Target Path Logical Bytes: {node.recursive_target_known_logical_bytes}",
                        f"  Known Logical Delta: {node.recursive_known_net_logical_delta}",
                        f"  Added Bytes: {node.recursive_added_logical_bytes}",
                        f"  Removed Bytes: {node.recursive_removed_logical_bytes}",
                        f"  Increase Bytes: {node.recursive_same_location_increase_bytes}",
                        f"  Decrease Bytes: {node.recursive_same_location_decrease_bytes}",
                        f"  Unknown-Size Contributions: {node.recursive_unknown_size_contribution_count}",
                        f"  Decomposition Complete: {str(node.decomposition_complete).lower()}",
                    )
                )
            return "\n".join(lines)

        return payload, human, 0

    _run(ctx, "snapshots.growth", action)


def _document_inspection_human(page: Any) -> str:
    lines = [
        f"Inspection Status: {safe_text(page.status)}",
        f"Source Format: {safe_text(page.source_format)}",
        f"Backend: {safe_text(page.backend_name)} {safe_text(page.backend_version)}",
        f"Source Kind: {safe_text(page.source_kind)}",
        f"Scope ID: {safe_text(page.scope_id)}",
        f"Relative Path: {safe_text(page.relative_path)}",
        f"Source SHA-256: {safe_text(page.source_sha256)}",
        f"Full Item Count: {page.full_item_count}",
        f"Returned Count: {page.returned_count}",
        f"Limit: {page.limit}",
        f"Offset: {page.offset}",
        f"Has More: {str(page.has_more).lower()}",
        f"Next Offset: {safe_text(page.next_offset)}",
        f"Document Observation Digest: {safe_text(page.document_observation_digest)}",
    ]
    if page.identification_reason is not None:
        lines.append(f"Identification Reason: {safe_text(page.identification_reason)}")
    if page.warnings:
        lines.extend(("Warnings:", *(safe_text(item) for item in page.warnings)))
    if page.items:
        lines.append("Items:")
        for index, item in enumerate(page.items, start=page.offset):
            location = json.dumps(item.location, ensure_ascii=False, sort_keys=True)
            lines.append(f"{index}: {safe_text(item.kind)} location={safe_text(location)}")
            if item.parent is not None:
                lines.append(f"  Parent: {safe_text(item.parent)}")
            if item.text_or_value is not None:
                lines.append(f"  Value: {safe_text(item.text_or_value)}")
            if item.extension is not None:
                extension = json.dumps(item.extension, ensure_ascii=False, sort_keys=True)
                lines.append(f"  Extension: {safe_text(extension)}")
    content_search = page.content_search
    if content_search is not None:
        lines.extend(
            (
                "Content Search:",
                f"  Query: {safe_text(content_search.query)}",
                f"  Match Mode: {safe_text(content_search.match_mode)}",
                f"  Status: {safe_text(content_search.status)}",
                f"  Matched Items: {content_search.matched_item_count}",
                f"  Matched Occurrences: {content_search.matched_occurrence_count}",
                f"  Returned Matches: {content_search.returned_count}",
                f"  Match Limit: {content_search.limit}",
                f"  Match Offset: {content_search.offset}",
                f"  Has More: {str(content_search.has_more).lower()}",
                f"  Next Match Offset: {safe_text(content_search.next_offset)}",
            )
        )
        for match in content_search.matches:
            location = json.dumps(match.location, ensure_ascii=False, sort_keys=True)
            lines.extend(
                (
                    f"- Match item={match.item_index} kind={safe_text(match.kind)} "
                    f"location={safe_text(location)} occurrences={match.match_count}",
                    f"  Excerpt: {safe_text(match.excerpt)}",
                )
            )
    evidence_selection = page.evidence_selection
    if evidence_selection is not None:
        lines.extend(
            (
                "Evidence Selection:",
                f"  Status: {safe_text(evidence_selection.status)}",
                f"  Requested Mode: {safe_text(evidence_selection.requested_mode)}",
                f"  Matched Items: {evidence_selection.matched_item_count}",
                f"  Returned Slices: {evidence_selection.returned_slice_count}",
                f"  Selected Items: {evidence_selection.selected_item_count}",
                f"  Selected Characters: {evidence_selection.selected_character_count}",
                f"  Selection Digest: {safe_text(evidence_selection.selection_digest)}",
                f"  Has More: {str(evidence_selection.has_more).lower()}",
                f"  Next Match Offset: {safe_text(evidence_selection.next_offset)}",
            )
        )
        for evidence_slice in evidence_selection.slices:
            lines.append(
                f"- Slice {safe_text(evidence_slice.slice_id)} "
                f"mode={safe_text(evidence_slice.selection_mode)} "
                f"anchor={evidence_slice.anchor_item_index}"
            )
            for item in evidence_slice.items:
                location = json.dumps(item.location, ensure_ascii=False, sort_keys=True)
                lines.append(
                    f"  {safe_text(item.relation)} item={item.item_index} "
                    f"role={safe_text(item.role)} location={safe_text(location)}"
                )
                if item.text is not None:
                    lines.append(f"    Value: {safe_text(item.text)}")
    return "\n".join(lines)


@documents_app.command("inspect")
def documents_inspect(
    ctx: typer.Context,
    scope: str = typer.Option(..., "--scope"),
    relative_path: str = typer.Option(..., "--path"),
    yes: bool = typer.Option(False, "--yes"),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
    expected_source_sha256: str | None = typer.Option(None, "--expected-source-sha256"),
    content_query: str | None = typer.Option(None, "--content-query"),
    content_limit: int = typer.Option(20, "--content-limit"),
    content_offset: int = typer.Option(0, "--content-offset"),
    evidence: bool = typer.Option(False, "--evidence"),
    evidence_mode: str = typer.Option("AUTO", "--evidence-mode"),
    evidence_context_items: int = typer.Option(2, "--evidence-context-items"),
    evidence_max_characters: int = typer.Option(12_000, "--evidence-max-characters"),
    evidence_page: int | None = typer.Option(None, "--evidence-page"),
) -> None:
    """Inspect one confirmed scoped document without providers or persistence."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        page = inspect_document(
            _config(ctx),
            DocumentInspectionRequest(
                scope_id=scope,
                relative_path=relative_path,
                confirmed=yes,
                limit=limit,
                offset=offset,
                expected_source_sha256=expected_source_sha256,
                content_query=content_query,
                content_limit=content_limit,
                content_offset=content_offset,
                parser_profile="AUTO" if evidence else "FAST",
                view="READ",
                intent="EVIDENCE" if evidence else "READ",
                evidence_mode=evidence_mode,
                evidence_context_items=evidence_context_items,
                evidence_max_characters=evidence_max_characters,
                evidence_page=evidence_page,
            ),
        )
        result = {"inspection": _run_result(page)}
        payload = envelope(
            "documents.inspect",
            page.status,
            result,
            warnings=list(page.warnings),
        )
        return (
            payload,
            lambda: _document_inspection_human(page),
            0 if page.status == "COMPLETE" else 4,
        )

    _run(ctx, "documents.inspect", action)


def _resource_observation_human(observation: Any, *, unknown_text: str = "null") -> str:
    """Render stable resource facts without assigning health or risk states."""

    def value(item: object) -> str:
        return unknown_text if item is None else safe_text(item)

    cpu = observation.cpu
    memory = observation.memory
    disk = observation.disk
    network = observation.network
    summary = observation.process_summary
    lines = [
        "System CPU",
        f"Sample Seconds: {observation.sample_seconds}",
        f"Logical CPU Count: {cpu.logical_cpu_count}",
        f"Physical CPU Count: {value(cpu.physical_cpu_count)}",
        f"Total Percent: {cpu.total_percent}",
        f"User Percent: {cpu.user_percent}",
        f"System Percent: {cpu.system_percent}",
        f"Idle Percent: {cpu.idle_percent}",
        "Per CPU Percent: " + ", ".join(str(item) for item in cpu.per_cpu_percent),
        "",
        "Memory",
        f"Total Bytes: {memory.total_bytes}",
        f"Available Bytes: {memory.available_bytes}",
        f"Used Bytes: {memory.used_bytes}",
        f"Percent: {memory.percent}",
        f"Active Bytes: {value(memory.active_bytes)}",
        f"Inactive Bytes: {value(memory.inactive_bytes)}",
        f"Wired Bytes: {value(memory.wired_bytes)}",
        f"Swap Total Bytes: {value(memory.swap_total_bytes)}",
        f"Swap Used Bytes: {value(memory.swap_used_bytes)}",
        f"Swap Free Bytes: {value(memory.swap_free_bytes)}",
        f"Swap Percent: {value(memory.swap_percent)}",
        f"Swap In Delta: {value(memory.swap_in_delta)}",
        f"Swap Out Delta: {value(memory.swap_out_delta)}",
        "",
        "Disk and Network",
        f"Mount Path: {safe_text(disk.mount_path)}",
        f"Disk Total Bytes: {disk.total_bytes}",
        f"Disk Used Bytes: {disk.used_bytes}",
        f"Disk Free Bytes: {disk.free_bytes}",
        f"Disk Percent: {disk.percent}",
        f"Disk Read Bytes Delta: {value(disk.read_bytes_delta)}",
        f"Disk Write Bytes Delta: {value(disk.write_bytes_delta)}",
        f"Network Bytes Sent Delta: {value(network.bytes_sent_delta)}",
        f"Network Bytes Received Delta: {value(network.bytes_received_delta)}",
        "",
        "Top Processes",
        f"Sort: {summary.sort.value}",
        f"Examined: {summary.examined_count}",
        f"Returned: {summary.returned_count}",
        f"Unavailable: {summary.unavailable_count}",
    ]
    lines.extend(
        " ".join(
            (
                str(process.pid),
                safe_text(process.name),
                f"cpu_percent={process.cpu_percent}",
                f"rss_bytes={process.rss_bytes}",
                f"memory_percent={process.memory_percent}",
                f"thread_count={process.thread_count}",
                f"status={safe_text(process.status)}",
            )
        )
        for process in observation.processes
    )
    if observation.warnings:
        lines.extend(("", "Warnings:", *(safe_text(item) for item in observation.warnings)))
    return "\n".join(lines)


@resources_app.command("observe")
def resources_observe(
    ctx: typer.Context,
    sample_seconds: float = typer.Option(1.0, "--sample-seconds"),
    top: int = typer.Option(20, "--top"),
    sort: str = typer.Option("cpu", "--sort"),
) -> None:
    """Collect one read-only CPU, memory, disk, network, and process sample."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        observation = observe_resources(sample_seconds, top, sort)
        payload = envelope(
            "resources.observe",
            "OK",
            {"observation": _run_result(observation)},
            warnings=list(observation.warnings),
        )
        return payload, lambda: _resource_observation_human(observation), 0

    _run(ctx, "resources.observe", action)


def _system_status_human(review: Any) -> str:
    """Render the aggregate as facts and limitations, never recommendations."""
    evidence = review.evidence_health
    snapshot_evidence = _run_result(evidence.snapshot_evidence)
    storage = _run_result(review.storage_health)
    recent = review.recent_changes
    lines = [
        "System Resources",
        _resource_observation_human(review.resources, unknown_text="unknown"),
    ]
    lines.extend(("", "Evidence Health", f"Status: {evidence.status}"))
    lines.extend(
        _evidence_verify_human(list(evidence.verifications), snapshot_evidence).splitlines()
    )
    lines.extend(("", "Storage Health", _storage_status_human(storage)))
    lines.extend(("", "Recent Filesystem Changes", f"Status: {recent.status}"))
    if recent.status == "UNAVAILABLE":
        lines.append(f"Limitation: {recent.limitation}")
    else:
        summary = recent.snapshot_diff_summary
        events = recent.change_event_summary
        assert summary is not None and events is not None
        lines.extend(
            (
                f"Left Snapshot ID: {recent.left_snapshot_id}",
                f"Right Snapshot ID: {recent.right_snapshot_id}",
                f"Added: {summary.added_count}",
                f"Removed: {summary.removed_count}",
                f"Modified: {summary.modified_count}",
                f"Unchanged: {summary.unchanged_count}",
                f"Event Count: {events.event_count}",
            )
        )
        if recent.change_events:
            lines.append("Change Events:")
            lines.extend(
                f"{event.event_type.value} scope={safe_text(event.scope_id)} "
                f"path={safe_text(event.relative_path)} "
                f"size_delta={'unknown' if event.size_delta is None else event.size_delta} "
                f"hash_changed={'unknown' if event.hash_changed is None else str(event.hash_changed).lower()} "
                f"metadata_changed={str(event.metadata_changed).lower()}"
                for event in recent.change_events
            )
        else:
            lines.append("No filesystem changes.")
    lines.extend(("", "Known Limitations"))
    lines.extend(f"- {safe_text(item)}" for item in review.limitations)
    if not review.limitations:
        lines.append("- none")
    return "\n".join(lines)


@app.command("status")
def system_status(
    ctx: typer.Context,
    sample_seconds: float = typer.Option(1.0, "--sample-seconds"),
    top: int = typer.Option(20, "--top"),
    sort: str = typer.Option("cpu", "--sort"),
) -> None:
    """Review existing system facts without creating persistent state."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        review = build_system_status_review(_config(ctx), sample_seconds, top, sort)
        payload = envelope(
            "status",
            "OK",
            {"review": _run_result(review)},
            warnings=list(review.limitations),
        )
        return payload, lambda: _system_status_human(review), 0

    _run(ctx, "status", action)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check required and optional local capabilities without scanning scopes."""

    def action() -> tuple[CommandEnvelope, Callable[[], str], int]:
        try:
            config = _config(ctx)
        except ConfigurationError as error:
            result = {
                "status": CapabilityStatus.MISCONFIGURED.value,
                "checks": [],
                "warnings": [],
                "errors": [str(error)],
            }
            payload = envelope(
                "doctor",
                CapabilityStatus.MISCONFIGURED.value,
                result,
                errors=[{"code": error.code, "message": str(error)}],
            )
            message = safe_text(error)
            return (
                payload,
                lambda: f"Overall Status: MISCONFIGURED\nErrors: {message}",
                error.exit_code,
            )
        summary = run_doctor(config)
        result = to_jsonable(summary)
        payload = envelope(
            "doctor",
            summary.status.value,
            result,
            errors=[
                {"code": "DOCTOR_REQUIRED_CAPABILITY_MISSING", "message": message}
                for message in summary.errors
            ],
            warnings=list(summary.warnings),
        )
        code = EXIT_CAPABILITY if summary.status == CapabilityStatus.UNAVAILABLE else 0

        def human() -> str:
            required = [item for item in summary.checks if item.required]
            optional = [item for item in summary.checks if not item.required]
            lines = [f"Overall Status: {summary.status.value}", "Required Checks:"]
            lines.extend(
                f"  {item.status.value} {safe_text(item.check_id)} — {safe_text(item.message)}"
                for item in required
            )
            lines.append("Optional Checks:")
            lines.extend(
                f"  {item.status.value} {safe_text(item.check_id)} — {safe_text(item.message)}"
                for item in optional
            )
            lines.append(
                f"Managed Root Summary: {sum(scope.role == ScopeRole.MANAGED_ROOT and scope.enabled for scope in config.scopes)} enabled"
            )
            lines.append(
                "Warnings: " + ("; ".join(safe_text(item) for item in summary.warnings) or "none")
            )
            lines.append(
                "Errors: " + ("; ".join(safe_text(item) for item in summary.errors) or "none")
            )
            return "\n".join(lines)

        return payload, human, code

    _run(ctx, "doctor", action)


def main() -> None:
    """Module entry point."""
    app()


if __name__ == "__main__":
    main()
