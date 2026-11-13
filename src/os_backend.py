"""Thin ctypes wrapper for the C file-system backend."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "c_backend" / "filebox_os.c"
EXECUTABLE = PROJECT_ROOT / "c_backend" / "filebox_os.exe"


def _ensure_executable() -> Path:
    if not EXECUTABLE.exists() or EXECUTABLE.stat().st_mtime < SOURCE.stat().st_mtime:
        with _build_semaphore:
            if not EXECUTABLE.exists() or EXECUTABLE.stat().st_mtime < SOURCE.stat().st_mtime:
                compiler = os.environ.get("CC", "gcc")
                subprocess.run(
                    [compiler, "-O2", "-o", str(EXECUTABLE), str(SOURCE)],
                    check=True,
                    cwd=PROJECT_ROOT,
                )
    return EXECUTABLE


_requests: queue.Queue = queue.Queue()
_build_semaphore = threading.Semaphore(1)
_worker_started = False
_worker_start_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=max(4, min(16, int(os.environ.get("FILEBOX_FS_WORKERS", "8")))),
                               thread_name_prefix="filebox-fs")

#utkarshs work fcfs
def _worker():
    """FCFS admission to a worker pool; independent requests execute concurrently."""
    while True:
        future, operation, args, _mutating = _requests.get()
        if future.set_running_or_notify_cancel():
            try:
                execution = _executor.submit(operation, *args)
                execution.add_done_callback(lambda completed, result=future: _copy_future(completed, result))
            except BaseException as exc:
                future.set_exception(exc)
        _requests.task_done()


def _copy_future(source: Future, target: Future):
    try:
        target.set_result(source.result())
    except BaseException as exc:
        target.set_exception(exc)

#UTKARSH 
def _dispatch(operation, *args, mutating=False):
    global _worker_started
    with _worker_start_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="filebox-fcfs", daemon=True).start()
            _worker_started = True
    future = Future()
    _requests.put((future, operation, args, mutating))
    return future.result()


def _run_c(operation: str, *args: str) -> str:
    encoded = [str(arg).encode("utf-8").hex() for arg in args]
    completed = subprocess.run(
        [str(_ensure_executable()), operation, *encoded],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        message = detail or f"C file-system operation '{operation}' failed (exit code {completed.returncode})."
        raise OSError(message)
    return completed.stdout.strip()


def _root(root: str | Path) -> str:
    return str(Path(root).resolve())


def make_directory(root, relative: str) -> None:
    _dispatch(_run_c, "mkdir", _root(root), relative, mutating=True)


def import_file(root, source: str, relative_destination: str) -> None:
    _dispatch(_run_c, "import", _root(root), str(Path(source).resolve()), relative_destination, mutating=True)


def export_file(root, relative_source: str, destination: str) -> None:
    target = Path(destination).resolve()
    _dispatch(_run_c, "export", _root(root), relative_source, str(target))


def move_file(root, old_relative: str, new_relative: str) -> None:
    _dispatch(_run_c, "move", _root(root), old_relative, new_relative, mutating=True)


def delete_file(root, relative: str) -> None:
    _dispatch(_run_c, "delete", _root(root), relative, mutating=True)


def delete_empty_directory(root, relative: str) -> None:
    _dispatch(_run_c, "rmdir", _root(root), relative, mutating=True)


def file_size(root, relative: str) -> int:
    return int(_dispatch(_run_c, "size", _root(root), relative))
