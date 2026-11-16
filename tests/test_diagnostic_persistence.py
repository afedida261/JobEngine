"""Diagnostic persistence failures must not become research failures."""

import ctypes
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from jobengine.terminal.artifacts import read_json, write_json


@contextmanager
def windows_reader(path):
    """A real reader that permits reads/writes but does not share file deletion."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    closed = threading.Event()

    def close():
        if not closed.is_set():
            kernel.CloseHandle(handle)
            closed.set()

    try:
        yield close
    finally:
        close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses Windows file-sharing semantics")
def test_transient_windows_reader_lock_allows_atomic_update(tmp_path):
    path = tmp_path / "job.json"
    write_json(path, {"version": "old"})
    with windows_reader(path) as close:
        release = threading.Timer(0.05, close)
        release.start()
        try:
            write_json(path, {"version": "new"})
        finally:
            release.join()
    assert read_json(path) == {"version": "new"}


def test_concurrent_writers_use_independent_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    write_json(path, {"version": "old"})
    ready = threading.Barrier(2)
    replace = Path.replace
    temporary_names = []
    local = threading.local()

    def simultaneous_replace(source, target):
        if not getattr(local, "arrived", False):
            local.arrived = True
            temporary_names.append(source.name)
            ready.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", simultaneous_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda version: write_json(path, {"version": version}), [1, 2]))
    assert len(set(temporary_names)) == 2
    assert read_json(path)["version"] in {1, 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_exhausted_retries_preserve_previous_json(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    write_json(path, {"version": "old"})
    attempts = []

    def denied(source, target):
        attempts.append(source)
        raise PermissionError("Synthetic reader lock")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(OSError) as failure:
        write_json(path, {"version": "new"})
    assert type(failure.value).__name__ == "PersistenceError"
    assert len(attempts) == 6
    assert read_json(path) == {"version": "old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_optional_diagnostic_failure_keeps_events_and_required_export(tmp_path, monkeypatch):
    from jobengine.infrastructure.storage import Store
    from jobengine.terminal.artifacts import Artifacts

    output = []
    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", Store(tmp_path / "data"), output.append
    )
    replace = Path.replace

    def denied_event(source, target):
        if Path(target).parent.name == "events":
            raise PermissionError("Synthetic persistent event-file lock")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", denied_event)
    trace("search", "completed", reason="Actual search succeeded")
    trace.save("search/run.json", {"actual_results": [1]}, required=True)
    trace.finish("completed")
    assert len(trace.events) == 1
    assert read_json(trace.path / "search/run.json") == {"actual_results": [1]}
    assert read_json(trace.path / "manifest.json")["status"] == "partial"
    assert read_json(trace.path / "failures.json")[0]["kind"] == "persistence"
    assert read_json(trace.path / "summary.json")["diagnostic_persistence_failures"] == 1
    assert any("persistence" in line for line in output)


def test_required_export_failure_is_explicit_and_preserves_old_export(tmp_path, monkeypatch):
    from jobengine.infrastructure.persistence import PersistenceError
    from jobengine.infrastructure.storage import Store

    store = Store(tmp_path)
    path = store.save_run("synthetic", {"version": "old"})

    def denied(source, target):
        raise PermissionError("Synthetic persistent export lock")

    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PersistenceError, match="Could not persist"):
        store.save_run("synthetic", {"version": "new"})
    assert read_json(path) == {"version": "old"}


def test_jsonl_failure_is_reported_without_losing_event(tmp_path, monkeypatch):
    from jobengine.infrastructure.storage import Store
    from jobengine.terminal.artifacts import Artifacts

    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", Store(tmp_path / "data"), lambda _: None
    )
    original = Path.open

    def denied_append(path, *args, **kwargs):
        if path.name == "events.jsonl":
            raise PermissionError("Synthetic JSONL lock")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_append)
    trace("search", "completed", reason="Actual search succeeded")
    trace.finish("completed")
    assert len(trace.events) == 1
    assert read_json(trace.path / "manifest.json")["status"] == "partial"
    assert trace.failures[0]["path"] == "events.jsonl"
    assert len(list((trace.path / "events").glob("*.json"))) == 1


def test_concurrent_observations_keep_unique_events(tmp_path):
    import json

    from jobengine.infrastructure.storage import Store
    from jobengine.terminal.artifacts import Artifacts

    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", Store(tmp_path / "data"), lambda _: None
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: trace("test", "observed", number=number), range(12)))
    trace.finish("completed")
    lines = (trace.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["sequence"] for line in lines] == list(range(1, 13))
    assert len(list((trace.path / "events").glob("*.json"))) == 12
    assert not trace.persistence_failures


def test_final_manifest_failure_does_not_claim_success(tmp_path, monkeypatch):
    from jobengine.infrastructure.storage import Store
    from jobengine.terminal.artifacts import Artifacts

    output = []
    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", Store(tmp_path / "data"), output.append
    )
    trace.save("search/run.json", {"actual_results": [1]}, required=True)
    replace = Path.replace

    def denied_manifest(source, target):
        if Path(target).name == "manifest.json":
            raise PermissionError("Synthetic permanent manifest lock")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", denied_manifest)
    assert trace.finish("completed") == "failed"
    assert read_json(trace.path / "search/run.json") == {"actual_results": [1]}
    assert "manifest.json" in trace.persistence_failures
    assert "Status: failed" in output[-1]


def test_unchanged_pool_update_does_not_rewrite_unrelated_job(tmp_path, case, monkeypatch):
    from jobengine.infrastructure.storage import Store
    from jobengine.terminal.artifacts import Artifacts

    trace = Artifacts(
        tmp_path / "debug", "synthetic", "search", Store(tmp_path / "data"), lambda _: None
    )
    item = case[1].model_dump(mode="json")
    trace("discover", "accepted", item=item)
    row = {"item": item, "page": case[2].model_dump(mode="json"), "rank": 1}
    trace("greenhouse", "pool", rows=[row])
    replace = Path.replace
    job_updates = []

    def record_replace(source, target):
        if Path(target).parent.name == "jobs":
            job_updates.append(target)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", record_replace)
    trace("greenhouse", "pool", rows=[row])
    assert not job_updates
    trace("greenhouse", "pool", rows=[row | {"rank": 2}])
    assert len(job_updates) == 1
