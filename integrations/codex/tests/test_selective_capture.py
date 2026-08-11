"""Regression tests for selective remote session capture."""

import asyncio
import importlib.util
import json
import pathlib
import sys
import urllib.error

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PLUGIN = _ROOT / "plugins" / "cognee"
_SCRIPTS = _PLUGIN / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _load_store():
    spec = importlib.util.spec_from_file_location(
        "cognee_store_to_session", _SCRIPTS / "store-to-session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_session_start():
    spec = importlib.util.spec_from_file_location(
        "cognee_session_start", _SCRIPTS / "session-start.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_idle_watcher():
    spec = importlib.util.spec_from_file_location(
        "cognee_idle_watcher", _SCRIPTS / "idle-watcher.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_stop_does_not_buffer_when_readiness_marker_is_stale(monkeypatch):
    module = _load_store()
    calls = {"remember": 0, "buffer": 0}

    monkeypatch.setattr(module, "_load_session", lambda: ("session", "memory", ""))
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "http://example.invalid"},
    )
    monkeypatch.setattr(module, "server_ready_hint", lambda _url: False)
    monkeypatch.setattr(
        module,
        "pop_pending_prompt",
        lambda *_args, **_kwargs: {"prompt": "question", "context": "context"},
    )
    monkeypatch.setattr(
        module,
        "remember_entry_via_http",
        lambda *_args: calls.__setitem__("remember", calls["remember"] + 1)
        or {"entry_id": "qa"},
    )
    monkeypatch.setattr(
        module,
        "append_warmup_entry",
        lambda *_args: calls.__setitem__("buffer", calls["buffer"] + 1),
    )
    monkeypatch.setattr(module, "append_http_bridge_entry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "bump_save_counter", lambda *_args: None)
    monkeypatch.setattr(module, "bump_turn_counter", lambda *_args: (1, False))
    monkeypatch.setattr(module, "hook_log", lambda *_args: None)
    monkeypatch.setattr(module, "notify", lambda *_args: None)
    monkeypatch.setattr(module, "touch_activity", lambda: None)

    asyncio.run(module._store_assistant_stop({"assistant_message": "Keep the clean design."}))

    assert calls == {"remember": 1, "buffer": 0}


def test_remote_stop_buffers_retryable_write_failure(monkeypatch):
    module = _load_store()
    buffered = []
    mirrored = []
    counters = []

    monkeypatch.setattr(module, "_load_session", lambda: ("session", "memory", ""))
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "http://example.invalid"},
    )
    monkeypatch.setattr(
        module,
        "pop_pending_prompt",
        lambda *_args, **_kwargs: {"prompt": "question", "context": "context"},
    )

    def fail_store(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://example.invalid", 409, "Conflict", {}, None)

    monkeypatch.setattr(module, "remember_entry_via_http", fail_store)
    monkeypatch.setattr(
        module,
        "append_warmup_entry",
        lambda dataset, session, entry: buffered.append((dataset, session, entry)),
    )
    monkeypatch.setattr(
        module,
        "append_http_bridge_entry",
        lambda dataset, session, **entry: mirrored.append((dataset, session, entry)),
    )
    monkeypatch.setattr(
        module,
        "bump_save_counter",
        lambda session, kind: counters.append((session, kind)),
    )
    monkeypatch.setattr(module, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "notify", lambda *_args, **_kwargs: None)

    asyncio.run(module._store_assistant_stop({"assistant_message": "answer"}))

    assert buffered == [
        (
            "memory",
            "session",
            {"type": "qa", "question": "question", "answer": "answer", "context": "context"},
        )
    ]
    assert mirrored == [("memory", "session", {"question": "question", "answer": "answer"})]
    assert counters == [("session", "answer")]


def test_remote_stop_does_not_buffer_nonretryable_write_failure(monkeypatch):
    module = _load_store()
    buffered = []

    monkeypatch.setattr(module, "_load_session", lambda: ("session", "memory", ""))
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "http://example.invalid"},
    )
    monkeypatch.setattr(
        module,
        "pop_pending_prompt",
        lambda *_args, **_kwargs: {"prompt": "question", "context": "context"},
    )

    def fail_store(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://example.invalid", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(module, "remember_entry_via_http", fail_store)
    monkeypatch.setattr(module, "append_warmup_entry", lambda *_args: buffered.append(True))
    monkeypatch.setattr(module, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "notify", lambda *_args, **_kwargs: None)

    asyncio.run(module._store_assistant_stop({"assistant_message": "answer"}))

    assert buffered == []


def test_tool_traces_are_not_registered():
    hooks = json.loads((_PLUGIN / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    assert "PostToolUse" not in hooks
    assert "PreCompact" not in hooks


def test_idle_watcher_does_not_sync_an_unfinished_turn(monkeypatch):
    module = _load_idle_watcher()
    improve_calls = []
    owns_pidfile = iter((True, False))

    monkeypatch.setattr(module, "_should_stop", False)
    monkeypatch.setattr(module, "_owns_pidfile", lambda: next(owns_pidfile))
    monkeypatch.setattr(module, "_read_activity_ts", lambda: 0.0)
    monkeypatch.setattr(module, "_pending_turn_exists", lambda *_args: True)
    monkeypatch.setattr(module, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_run_update_check", lambda: None)
    monkeypatch.setattr(module, "_check_llm_key", lambda _config: None)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(module, "IDLE_SECONDS", 0)

    async def improve(*_args):
        improve_calls.append(True)
        return True

    monkeypatch.setattr(module, "_improve_once", improve)
    asyncio.run(module._main_loop("session", "memory", {"session_key": "host"}))

    assert improve_calls == []


def test_windows_session_start_uses_official_session_end(monkeypatch):
    module = _load_session_start()
    exit_watchers = []

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "load_config", lambda: {"base_url": "http://example.invalid"})
    monkeypatch.setattr(module, "resolve_session_key_from_payload", lambda _payload: ("host", "test"))
    monkeypatch.setattr(module, "set_session_key", lambda key: key)
    monkeypatch.setattr(module, "ensure_launch_record", lambda *_args: ("session", "connection"))
    monkeypatch.setattr(module, "get_dataset", lambda _config: "memory")
    monkeypatch.setattr(module, "_health_ok", lambda _url: True)

    async def ready(*_args, **_kwargs):
        return "user", "", True

    monkeypatch.setattr(module, "_run_heavy", ready)
    monkeypatch.setattr(module, "_purge_legacy_resolved_files", lambda: None)
    monkeypatch.setattr(module, "save_config", lambda _config: None)
    monkeypatch.setattr(module, "touch_activity", lambda: None)
    monkeypatch.setattr(module, "_spawn_idle_watcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_spawn_exit_watcher",
        lambda *_args, **_kwargs: exit_watchers.append(True),
    )
    monkeypatch.setattr(module, "render_status_for_host", lambda _key: "ready")
    monkeypatch.setattr(module, "_update_nudge_suffix", lambda: "")
    monkeypatch.setattr(module, "hook_log", lambda *_args, **_kwargs: None)

    asyncio.run(module._start({"session_id": "host"}))

    assert exit_watchers == []
