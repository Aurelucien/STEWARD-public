import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import local_steward.cli as cli_module
from local_steward.cli import app
from local_steward.database import database_path
from local_steward.errors import ResourceCollectionError, ResourceObservationError
from local_steward.resources import (
    RawCpuTimes,
    RawDisk,
    RawMemory,
    RawNetwork,
    RawProcess,
    RawResourceSample,
    RawSwap,
    observe_resources,
)

from .test_snapshot_queries import snapshot_fixture


class FakeProvider:
    def __init__(self, sample: RawResourceSample) -> None:
        self.sample = sample
        self.calls: list[float] = []

    def collect(self, sample_seconds: float) -> RawResourceSample:
        self.calls.append(sample_seconds)
        return self.sample


def _raw_sample(
    *,
    physical_cpu_count: int | None = None,
    processes: tuple[RawProcess, ...] | None = None,
    unavailable_count: int = 0,
    process_enumeration_unavailable: bool = False,
    swap_unavailable: bool = False,
    reset: bool = False,
) -> RawResourceSample:
    before = 100
    after = 50 if reset else 130
    return RawResourceSample(
        2,
        physical_cpu_count,
        (RawCpuTimes(10.0, 20.0, 70.0), RawCpuTimes(30.0, 10.0, 60.0)),
        RawMemory(1_000, 400, 600, 60.0, None, None, None),
        RawSwap(100, 20, 80, 20.0, before, before + 10)
        if not swap_unavailable
        else RawSwap(None, None, None, None, None, None),
        RawSwap(100, 30, 70, 30.0, after, after + 20)
        if not swap_unavailable
        else RawSwap(None, None, None, None, None, None),
        RawDisk("/", 10_000, 4_000, 6_000, 40.0, before, before + 20),
        RawDisk("/", 10_000, 4_500, 5_500, 45.0, after, after + 30),
        RawNetwork(before, before + 10),
        RawNetwork(after, after + 40),
        processes
        if processes is not None
        else (
            RawProcess(30, "cpu-tie-large", 50.0, 300, 3.0, 3, "running"),
            RawProcess(10, "cpu-tie-small", 50.0, 200, 2.0, 2, "sleeping"),
            RawProcess(20, "over-100", 150.0, 100, 1.0, 1, "running"),
            RawProcess(40, "memory-top", 10.0, 500, 5.0, 4, "sleeping"),
        ),
        6,
        unavailable_count,
        process_enumeration_unavailable,
        swap_unavailable,
    )


def _observation():
    return observe_resources(provider=FakeProvider(_raw_sample()))


def test_observation_defaults_map_cpu_memory_and_optional_facts() -> None:
    provider = FakeProvider(_raw_sample())

    observation = observe_resources(provider=provider)

    assert provider.calls == [1.0]
    assert observation.cpu.logical_cpu_count == 2
    assert observation.cpu.physical_cpu_count is None
    assert observation.cpu.total_percent == 35.0
    assert observation.cpu.user_percent == 20.0
    assert observation.cpu.system_percent == 15.0
    assert observation.cpu.idle_percent == 65.0
    assert observation.cpu.per_cpu_percent == (30.0, 40.0)
    assert observation.memory.active_bytes is observation.memory.inactive_bytes is None
    assert observation.memory.wired_bytes is None
    assert observation.memory.total_bytes == 1_000


def test_observation_uses_custom_sampling_time_and_counter_deltas() -> None:
    provider = FakeProvider(_raw_sample(physical_cpu_count=1))

    observation = observe_resources(2.5, 20, "cpu", provider)

    assert provider.calls == [2.5]
    assert observation.cpu.physical_cpu_count == 1
    assert observation.memory.swap_in_delta == 30
    assert observation.memory.swap_out_delta == 40
    assert observation.disk.read_bytes_delta == 30
    assert observation.disk.write_bytes_delta == 40
    assert observation.network.bytes_sent_delta == 30
    assert observation.network.bytes_received_delta == 60


def test_counter_resets_remain_unknown_with_warnings() -> None:
    observation = observe_resources(provider=FakeProvider(_raw_sample(reset=True)))

    assert observation.memory.swap_in_delta is None
    assert observation.memory.swap_out_delta is None
    assert observation.disk.read_bytes_delta is observation.disk.write_bytes_delta is None
    assert observation.network.bytes_sent_delta is observation.network.bytes_received_delta is None
    assert observation.warnings == (
        "SWAP_IN_COUNTER_UNAVAILABLE_OR_RESET",
        "SWAP_OUT_COUNTER_UNAVAILABLE_OR_RESET",
        "DISK_READ_COUNTER_UNAVAILABLE_OR_RESET",
        "DISK_WRITE_COUNTER_UNAVAILABLE_OR_RESET",
        "NETWORK_SENT_COUNTER_UNAVAILABLE_OR_RESET",
        "NETWORK_RECEIVED_COUNTER_UNAVAILABLE_OR_RESET",
    )


def test_process_cpu_sort_ties_top_and_unavailable_count() -> None:
    observation = observe_resources(1.0, 3, "cpu", FakeProvider(_raw_sample(unavailable_count=2)))

    assert [process.pid for process in observation.processes] == [20, 30, 10]
    assert observation.processes[0].cpu_percent == 150.0
    assert observation.process_summary.examined_count == 6
    assert observation.process_summary.returned_count == 3
    assert observation.process_summary.unavailable_count == 2


def test_process_enumeration_unavailable_remains_visible_without_failing_sample() -> None:
    observation = observe_resources(
        provider=FakeProvider(
            _raw_sample(processes=(), unavailable_count=1, process_enumeration_unavailable=True)
        )
    )

    assert observation.processes == ()
    assert observation.process_summary.unavailable_count == 1
    assert observation.warnings == ("PROCESS_ENUMERATION_UNAVAILABLE",)


def test_unavailable_swap_facts_remain_null_with_warning() -> None:
    observation = observe_resources(provider=FakeProvider(_raw_sample(swap_unavailable=True)))

    assert observation.memory.swap_total_bytes is None
    assert observation.memory.swap_used_bytes is None
    assert observation.memory.swap_free_bytes is None
    assert observation.memory.swap_percent is None
    assert observation.memory.swap_in_delta is observation.memory.swap_out_delta is None
    assert "SWAP_UNAVAILABLE" in observation.warnings


def test_process_memory_sort_tie_breaks_stably() -> None:
    processes = (
        RawProcess(3, "low", 5.0, 100, 1.0, 1, "sleeping"),
        RawProcess(2, "cpu", 20.0, 200, 2.0, 1, "running"),
        RawProcess(1, "pid", 20.0, 200, 2.0, 1, "running"),
    )

    observation = observe_resources(1.0, 20, "memory", FakeProvider(_raw_sample(processes=processes)))

    assert [process.pid for process in observation.processes] == [1, 2, 3]
    assert observation.process_summary.sort.value == "memory"


@pytest.mark.parametrize(
    ("sample_seconds", "top", "sort", "message"),
    (
        (0.0, 1, "cpu", "sample_seconds"),
        (61.0, 1, "cpu", "sample_seconds"),
        (1.0, 0, "cpu", "top"),
        (1.0, 1, "invalid", "sort"),
    ),
)
def test_invalid_observation_arguments_fail_before_provider(
    sample_seconds: float, top: int, sort: str, message: str
) -> None:
    provider = FakeProvider(_raw_sample())

    with pytest.raises(ResourceObservationError, match=message):
        observe_resources(sample_seconds, top, sort, provider)

    assert provider.calls == []


def test_system_collection_failure_is_domain_error() -> None:
    class FailingProvider:
        def collect(self, sample_seconds: float) -> RawResourceSample:
            raise OSError("injected")

    with pytest.raises(ResourceCollectionError, match="OSError"):
        observe_resources(provider=FailingProvider())


def test_resources_command_is_registered_and_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, int, str]] = []

    def observe(sample_seconds: float, top: int, sort: str):
        calls.append((sample_seconds, top, sort))
        return _observation()

    monkeypatch.setattr(cli_module, "observe_resources", observe)
    runner = CliRunner()
    help_result = runner.invoke(app, ["resources", "observe", "--help"])
    result = runner.invoke(app, ["resources", "observe"])

    assert help_result.exit_code == result.exit_code == 0
    assert calls == [(1.0, 20, "cpu")]
    assert "System CPU" in result.stdout and "Top Processes" in result.stdout
    assert "memory-top" not in result.stdout.split("Top Processes", 1)[0]


def test_resources_cli_passes_custom_sampling_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, int, str]] = []

    def observe(sample_seconds: float, top: int, sort: str):
        calls.append((sample_seconds, top, sort))
        return _observation()

    monkeypatch.setattr(cli_module, "observe_resources", observe)

    result = CliRunner().invoke(
        app,
        ["resources", "observe", "--sample-seconds", "2.5", "--top", "3", "--sort", "memory"],
    )

    assert result.exit_code == 0
    assert calls == [(2.5, 3, "memory")]


def test_resources_cli_human_and_json_are_stable_and_preserve_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation()
    monkeypatch.setattr(cli_module, "observe_resources", lambda *_args: observation)
    runner = CliRunner()

    first = runner.invoke(app, ["resources", "observe", "--top", "2", "--sort", "memory"])
    second = runner.invoke(app, ["resources", "observe", "--top", "2", "--sort", "memory"])
    encoded = runner.invoke(
        app, ["--format", "json", "resources", "observe", "--top", "2", "--sort", "memory"]
    )

    assert first.exit_code == second.exit_code == encoded.exit_code == 0
    assert first.stdout == second.stdout
    payload = json.loads(encoded.stdout)
    assert payload["command"] == "resources.observe" and payload["status"] == "OK"
    resource = payload["result"]["observation"]
    assert resource["memory"]["active_bytes"] is None
    assert resource["processes"][0]["pid"] == 20
    assert "run_id" in payload and encoded.stderr == ""


@pytest.mark.parametrize(
    ("option", "value", "code"),
    (("--sample-seconds", "0", "RESOURCE_OBSERVATION_INVALID"), ("--top", "0", "RESOURCE_OBSERVATION_INVALID"), ("--sort", "bad", "RESOURCE_OBSERVATION_INVALID")),
)
def test_resources_cli_invalid_arguments_use_error_envelope(option: str, value: str, code: str) -> None:
    result = CliRunner().invoke(
        app, ["--format", "json", "resources", "observe", option, value]
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["errors"][0]["code"] == code


def test_resources_cli_system_failure_uses_existing_error_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "observe_resources",
        lambda *_args: (_ for _ in ()).throw(ResourceCollectionError("unavailable")),
    )

    result = CliRunner().invoke(app, ["--format", "json", "resources", "observe"])

    assert result.exit_code == 3
    assert json.loads(result.stdout)["errors"][0]["code"] == "RESOURCE_OBSERVATION_FAILED"


def test_resources_cli_does_not_mutate_existing_persistent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _snapshot = snapshot_fixture(tmp_path)
    database_before = database_path(config).read_bytes()
    evidence_before = {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    }
    monkeypatch.setattr(cli_module, "observe_resources", lambda *_args: _observation())

    result = CliRunner().invoke(
        app, ["--config", str(config.source_path), "resources", "observe"]
    )

    assert result.exit_code == 0
    assert database_path(config).read_bytes() == database_before
    assert {
        path.relative_to(config.paths.evidence_dir): path.read_bytes()
        for path in config.paths.evidence_dir.rglob("*.json")
    } == evidence_before
