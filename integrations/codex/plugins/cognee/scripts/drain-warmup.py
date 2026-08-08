#!/usr/bin/env python3
"""Bounded detached worker for replaying warmup-buffered Cognee entries."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _plugin_common import drain_warmup_entries, hook_log, set_session_key  # noqa: E402

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
_BREAKER_DIR = _PLUGIN_DIR / "drain-breakers"
_DEFAULT_DEADLINE_SECONDS = 30.0
_PER_ENTRY_TIMEOUT_SECONDS = 5.0
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 5.0)
_BREAKER_FAILURE_THRESHOLD = 3
_BREAKER_OPEN_SECONDS = 60.0
_WORKER_LOCK_STALE_SECONDS = 45.0


def _state_key(dataset: str, session_id: str) -> str:
    return hashlib.sha256(f"{dataset}\0{session_id}".encode("utf-8")).hexdigest()


def _breaker_path(dataset: str, session_id: str) -> Path:
    return _BREAKER_DIR / f"{_state_key(dataset, session_id)}.json"


def _worker_lock_path(dataset: str, session_id: str) -> Path:
    return _BREAKER_DIR.parent / "drain-locks" / f"{_state_key(dataset, session_id)}.lock"


def _read_breaker(dataset: str, session_id: str) -> dict:
    path = _breaker_path(dataset, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_breaker(dataset: str, session_id: str, state: dict) -> None:
    path = _breaker_path(dataset, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _clear_breaker(dataset: str, session_id: str) -> None:
    try:
        _breaker_path(dataset, session_id).unlink()
    except FileNotFoundError:
        pass


def _try_acquire_worker_lock(dataset: str, session_id: str) -> bool:
    path = _worker_lock_path(dataset, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and time.time() - path.stat().st_mtime > _WORKER_LOCK_STALE_SECONDS:
            path.unlink()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False
    except FileNotFoundError:
        return _try_acquire_worker_lock(dataset, session_id)
    except OSError as exc:
        hook_log("drain_failed", {"reason": "worker_lock", "error": str(exc)[:200]})
        return False


def _release_worker_lock(dataset: str, session_id: str) -> None:
    try:
        _worker_lock_path(dataset, session_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        hook_log("drain_failed", {"reason": "worker_unlock", "error": str(exc)[:200]})


def run_worker(payload: dict) -> int:
    """Replay one session buffer within a bounded retry window."""
    dataset = str(payload.get("dataset") or "").strip() if isinstance(payload, dict) else ""
    session_id = str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""
    session_key = str(payload.get("session_key") or "").strip() if isinstance(payload, dict) else ""
    if not dataset or not session_id:
        hook_log("drain_failed", {"reason": "invalid_payload"})
        return 2
    if session_key:
        set_session_key(session_key)

    now = time.time()
    breaker = _read_breaker(dataset, session_id)
    open_until = float(breaker.get("open_until") or 0.0)
    if open_until > now:
        hook_log(
            "drain_breaker_open",
            {"dataset": dataset, "session": session_id, "open_until": open_until},
        )
        return 0

    if not _try_acquire_worker_lock(dataset, session_id):
        hook_log(
            "drain_deferred",
            {"dataset": dataset, "session": session_id, "reason": "worker_locked"},
        )
        return 0

    started = time.monotonic()
    deadline = started + _DEFAULT_DEADLINE_SECONDS
    total_drained = 0
    remaining = 0
    attempts = 0
    hook_log("drain_started", {"dataset": dataset, "session": session_id})
    try:
        for attempt, delay in enumerate((*_BACKOFF_SECONDS, 0.0), start=1):
            if time.monotonic() >= deadline:
                break
            attempts = attempt
            try:
                drained, remaining = drain_warmup_entries(
                    dataset,
                    session_id,
                    deadline=deadline,
                    per_entry_timeout=_PER_ENTRY_TIMEOUT_SECONDS,
                )
                total_drained += drained
            except Exception as exc:
                remaining = max(1, remaining)
                hook_log(
                    "drain_failed",
                    {
                        "dataset": dataset,
                        "session": session_id,
                        "attempt": attempt,
                        "error": str(exc)[:200],
                    },
                )

            if remaining == 0:
                _clear_breaker(dataset, session_id)
                hook_log(
                    "drain_completed",
                    {
                        "dataset": dataset,
                        "session": session_id,
                        "drained": total_drained,
                        "attempts": attempts,
                    },
                )
                return 0

            hook_log(
                "drain_failed",
                {
                    "dataset": dataset,
                    "session": session_id,
                    "attempt": attempt,
                    "drained": total_drained,
                    "remaining": remaining,
                },
            )
            if delay <= 0:
                break
            seconds_left = deadline - time.monotonic()
            if seconds_left <= 0:
                break
            sleep_for = min(delay, seconds_left)
            hook_log(
                "drain_deferred",
                {
                    "dataset": dataset,
                    "session": session_id,
                    "delay": sleep_for,
                    "remaining": remaining,
                },
            )
            time.sleep(sleep_for)

        consecutive_failures = int(breaker.get("consecutive_failures") or 0) + 1
        open_until = 0.0
        if consecutive_failures >= _BREAKER_FAILURE_THRESHOLD:
            open_until = time.time() + _BREAKER_OPEN_SECONDS
        state = {
            "consecutive_failures": consecutive_failures,
            "open_until": open_until,
            "updated_at": time.time(),
        }
        _write_breaker(dataset, session_id, state)
        if open_until:
            hook_log(
                "drain_breaker_open",
                {
                    "dataset": dataset,
                    "session": session_id,
                    "open_until": open_until,
                    "remaining": remaining,
                },
            )
        return 1
    finally:
        _release_worker_lock(dataset, session_id)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        hook_log("drain_failed", {"reason": "missing_payload"})
        return 2
    try:
        payload = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        hook_log("drain_failed", {"reason": "invalid_json", "error": str(exc)[:200]})
        return 2
    return run_worker(payload)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
