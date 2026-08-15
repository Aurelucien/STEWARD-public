"""One-shot, read-only operating-system resource observation."""

from dataclasses import dataclass
from typing import Protocol

import psutil

from .errors import ResourceCollectionError, ResourceObservationError
from .models import (
    CpuObservation,
    DiskObservation,
    MemoryObservation,
    NetworkObservation,
    ProcessObservation,
    ProcessObservationSummary,
    ResourceObservation,
    ResourceProcessSort,
)

MAX_SAMPLE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class RawCpuTimes:
    user_percent: float
    system_percent: float
    idle_percent: float


@dataclass(frozen=True, slots=True)
class RawMemory:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent: float
    active_bytes: int | None
    inactive_bytes: int | None
    wired_bytes: int | None


@dataclass(frozen=True, slots=True)
class RawSwap:
    total_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    percent: float | None
    in_count: int | None
    out_count: int | None


@dataclass(frozen=True, slots=True)
class RawDisk:
    mount_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    read_count: int | None
    write_count: int | None


@dataclass(frozen=True, slots=True)
class RawNetwork:
    sent_count: int | None
    received_count: int | None


@dataclass(frozen=True, slots=True)
class RawProcess:
    pid: int
    name: str
    cpu_percent: float
    rss_bytes: int
    memory_percent: float
    thread_count: int
    status: str


@dataclass(frozen=True, slots=True)
class RawResourceSample:
    logical_cpu_count: int
    physical_cpu_count: int | None
    per_cpu_times: tuple[RawCpuTimes, ...]
    memory: RawMemory
    swap_before: RawSwap
    swap_after: RawSwap
    disk_before: RawDisk
    disk_after: RawDisk
    network_before: RawNetwork
    network_after: RawNetwork
    processes: tuple[RawProcess, ...]
    examined_count: int
    unavailable_count: int
    process_enumeration_unavailable: bool = False
    swap_unavailable: bool = False
    disk_counter_unavailable: bool = False
    network_counter_unavailable: bool = False


class ResourceProvider(Protocol):
    """Small injectable boundary for one common sampling window."""

    def collect(self, sample_seconds: float) -> RawResourceSample: ...


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _counter_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None or after < before:
        return None
    return after - before


def _cpu_time(value: object) -> RawCpuTimes:
    return RawCpuTimes(
        float(getattr(value, "user", 0.0)),
        float(getattr(value, "system", 0.0)),
        float(getattr(value, "idle", 0.0)),
    )


def _swap(value: object) -> RawSwap:
    return RawSwap(
        int(getattr(value, "total")),
        int(getattr(value, "used")),
        int(getattr(value, "free")),
        float(getattr(value, "percent")),
        _optional_int(getattr(value, "sin", None)),
        _optional_int(getattr(value, "sout", None)),
    )


def _unknown_swap() -> RawSwap:
    return RawSwap(None, None, None, None, None, None)


def _disk_counter(value: object | None, usage: object) -> RawDisk:
    return RawDisk(
        "/",
        int(getattr(usage, "total")),
        int(getattr(usage, "used")),
        int(getattr(usage, "free")),
        float(getattr(usage, "percent")),
        _optional_int(getattr(value, "read_bytes", None)) if value is not None else None,
        _optional_int(getattr(value, "write_bytes", None)) if value is not None else None,
    )


def _network_counter(value: object | None) -> RawNetwork:
    return RawNetwork(
        _optional_int(getattr(value, "bytes_sent", None)) if value is not None else None,
        _optional_int(getattr(value, "bytes_recv", None)) if value is not None else None,
    )


class PsutilResourceProvider:
    """psutil-backed collector; CPU sampling supplies the single wait window."""

    _unavailable = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)

    def collect(self, sample_seconds: float) -> RawResourceSample:
        try:
            process_enumeration_unavailable = False
            try:
                candidates = list(psutil.process_iter())
            except (PermissionError, OSError):
                candidates = []
                process_enumeration_unavailable = True
            ready: list[psutil.Process] = []
            unavailable = 1 if process_enumeration_unavailable else 0
            for process in candidates:
                try:
                    process.cpu_percent(None)
                    ready.append(process)
                except self._unavailable:
                    unavailable += 1

            swap_unavailable = False
            disk_counter_unavailable = False
            network_counter_unavailable = False
            try:
                swap_before = _swap(psutil.swap_memory())
            except OSError:
                swap_before = _unknown_swap()
                swap_unavailable = True
            usage_before = psutil.disk_usage("/")
            try:
                disk_counter_before = psutil.disk_io_counters()
            except OSError:
                disk_counter_before = None
                disk_counter_unavailable = True
            disk_before = _disk_counter(disk_counter_before, usage_before)
            try:
                network_counter_before = psutil.net_io_counters()
            except OSError:
                network_counter_before = None
                network_counter_unavailable = True
            network_before = _network_counter(network_counter_before)
            per_cpu_times = tuple(_cpu_time(value) for value in psutil.cpu_times_percent(
                interval=sample_seconds, percpu=True
            ))
            if not per_cpu_times:
                raise ResourceCollectionError("CPU sampling returned no logical CPUs")
            memory_value = psutil.virtual_memory()
            memory = RawMemory(
                int(memory_value.total),
                int(memory_value.available),
                int(memory_value.used),
                float(memory_value.percent),
                _optional_int(getattr(memory_value, "active", None)),
                _optional_int(getattr(memory_value, "inactive", None)),
                _optional_int(getattr(memory_value, "wired", None)),
            )
            try:
                swap_after = _swap(psutil.swap_memory())
            except OSError:
                swap_after = _unknown_swap()
                swap_unavailable = True
            if swap_unavailable:
                swap_before = swap_after = _unknown_swap()
            usage_after = psutil.disk_usage("/")
            try:
                disk_counter_after = psutil.disk_io_counters()
            except OSError:
                disk_counter_after = None
                disk_counter_unavailable = True
            disk_after = _disk_counter(disk_counter_after, usage_after)
            if disk_counter_unavailable:
                disk_before = _disk_counter(None, usage_before)
                disk_after = _disk_counter(None, usage_after)
            try:
                network_counter_after = psutil.net_io_counters()
            except OSError:
                network_counter_after = None
                network_counter_unavailable = True
            network_after = _network_counter(network_counter_after)
            if network_counter_unavailable:
                network_before = network_after = _network_counter(None)
            processes: list[RawProcess] = []
            for process in ready:
                try:
                    info = process.memory_info()
                    processes.append(
                        RawProcess(
                            process.pid,
                            process.name(),
                            float(process.cpu_percent(None)),
                            int(info.rss),
                            float(process.memory_percent()),
                            int(process.num_threads()),
                            str(process.status()),
                        )
                    )
                except self._unavailable:
                    unavailable += 1
            return RawResourceSample(
                int(psutil.cpu_count(logical=True) or len(per_cpu_times)),
                psutil.cpu_count(logical=False),
                per_cpu_times,
                memory,
                swap_before,
                swap_after,
                disk_before,
                disk_after,
                network_before,
                network_after,
                tuple(processes),
                len(candidates),
                unavailable,
                process_enumeration_unavailable,
                swap_unavailable,
                disk_counter_unavailable,
                network_counter_unavailable,
            )
        except ResourceCollectionError:
            raise
        except Exception as error:
            raise ResourceCollectionError(
                f"unable to collect operating-system resource facts: {type(error).__name__}"
            ) from error


def _validate(sample_seconds: float, top: int, sort: str) -> ResourceProcessSort:
    if sample_seconds <= 0 or sample_seconds > MAX_SAMPLE_SECONDS:
        raise ResourceObservationError(
            f"sample_seconds must be greater than 0 and at most {MAX_SAMPLE_SECONDS:g}"
        )
    if top <= 0:
        raise ResourceObservationError("top must be a positive integer")
    try:
        return ResourceProcessSort(sort)
    except ValueError as error:
        raise ResourceObservationError("sort must be cpu or memory") from error


def _ordered_processes(
    processes: tuple[RawProcess, ...], sort: ResourceProcessSort, top: int
) -> tuple[ProcessObservation, ...]:
    values = tuple(
        ProcessObservation(
            process.pid,
            process.name,
            process.cpu_percent,
            process.rss_bytes,
            process.memory_percent,
            process.thread_count,
            process.status,
        )
        for process in processes
    )
    if sort == ResourceProcessSort.CPU:
        ordered = sorted(values, key=lambda item: (-item.cpu_percent, -item.rss_bytes, item.pid))
    else:
        ordered = sorted(values, key=lambda item: (-item.rss_bytes, -item.cpu_percent, item.pid))
    return tuple(ordered[:top])


def observe_resources(
    sample_seconds: float = 1.0,
    top: int = 20,
    sort: str = "cpu",
    provider: ResourceProvider | None = None,
) -> ResourceObservation:
    """Collect one non-persistent operating-system sample through one provider call."""
    process_sort = _validate(sample_seconds, top, sort)
    try:
        raw = (provider or PsutilResourceProvider()).collect(sample_seconds)
    except ResourceCollectionError:
        raise
    except Exception as error:
        raise ResourceCollectionError(
            f"unable to collect operating-system resource facts: {type(error).__name__}"
        ) from error
    if not raw.per_cpu_times:
        raise ResourceCollectionError("CPU sampling returned no logical CPUs")
    count = len(raw.per_cpu_times)
    user = sum(item.user_percent for item in raw.per_cpu_times) / count
    system = sum(item.system_percent for item in raw.per_cpu_times) / count
    idle = sum(item.idle_percent for item in raw.per_cpu_times) / count
    processes = _ordered_processes(raw.processes, process_sort, top)
    warnings: list[str] = []
    swap_in = _counter_delta(raw.swap_before.in_count, raw.swap_after.in_count)
    swap_out = _counter_delta(raw.swap_before.out_count, raw.swap_after.out_count)
    disk_read = _counter_delta(raw.disk_before.read_count, raw.disk_after.read_count)
    disk_write = _counter_delta(raw.disk_before.write_count, raw.disk_after.write_count)
    network_sent = _counter_delta(raw.network_before.sent_count, raw.network_after.sent_count)
    network_received = _counter_delta(raw.network_before.received_count, raw.network_after.received_count)
    for label, value in (
        ("SWAP_IN", swap_in),
        ("SWAP_OUT", swap_out),
        ("DISK_READ", disk_read),
        ("DISK_WRITE", disk_write),
        ("NETWORK_SENT", network_sent),
        ("NETWORK_RECEIVED", network_received),
    ):
        if value is None:
            warnings.append(f"{label}_COUNTER_UNAVAILABLE_OR_RESET")
    if raw.process_enumeration_unavailable:
        warnings.append("PROCESS_ENUMERATION_UNAVAILABLE")
    if raw.swap_unavailable:
        warnings.append("SWAP_UNAVAILABLE")
    if raw.disk_counter_unavailable:
        warnings.append("DISK_IO_COUNTER_UNAVAILABLE")
    if raw.network_counter_unavailable:
        warnings.append("NETWORK_COUNTER_UNAVAILABLE")
    return ResourceObservation(
        sample_seconds,
        CpuObservation(
            raw.logical_cpu_count,
            raw.physical_cpu_count,
            100.0 - idle,
            user,
            system,
            idle,
            tuple(100.0 - item.idle_percent for item in raw.per_cpu_times),
        ),
        MemoryObservation(
            raw.memory.total_bytes,
            raw.memory.available_bytes,
            raw.memory.used_bytes,
            raw.memory.percent,
            raw.memory.active_bytes,
            raw.memory.inactive_bytes,
            raw.memory.wired_bytes,
            raw.swap_after.total_bytes,
            raw.swap_after.used_bytes,
            raw.swap_after.free_bytes,
            raw.swap_after.percent,
            swap_in,
            swap_out,
        ),
        DiskObservation(
            raw.disk_after.mount_path,
            raw.disk_after.total_bytes,
            raw.disk_after.used_bytes,
            raw.disk_after.free_bytes,
            raw.disk_after.percent,
            disk_read,
            disk_write,
        ),
        NetworkObservation(network_sent, network_received),
        processes,
        ProcessObservationSummary(
            raw.examined_count,
            len(processes),
            raw.unavailable_count,
            process_sort,
            top,
        ),
        tuple(warnings),
    )
