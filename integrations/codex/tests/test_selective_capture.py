"""Regression tests for selective remote session capture."""

import asyncio
import importlib.util
import json
import pathlib
import sys

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
    monkeypatch.setattr(module, "pop_pending_prompt", lambda *_args, **_kwargs: {})
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


def test_tool_traces_are_not_registered():
    hooks = json.loads((_PLUGIN / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    assert "PostToolUse" not in hooks
