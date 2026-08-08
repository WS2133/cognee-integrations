"""Contract tests for Codex UserPromptSubmit hook output."""

import asyncio
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "plugins" / "cognee" / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), _SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_context_output_uses_codex_schema(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")
    recalled_scopes = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": ""})
    monkeypatch.setattr(module, "server_ready_hint", lambda _url: True)
    monkeypatch.setattr(module, "get_session_key", lambda: "session")
    monkeypatch.setattr(
        module,
        "read_and_reset_save_counter",
        lambda _session: {"prompt": 0, "trace": 0, "answer": 0},
    )
    monkeypatch.setattr(
        module,
        "recall_via_http",
        lambda *args, **kwargs: recalled_scopes.append(kwargs["scope"]) or [],
    )
    monkeypatch.setattr(module, "render_status_for_host", lambda _session: "Cognee")

    output = asyncio.run(module._run("remember this"))

    assert output["systemMessage"].startswith("Cognee")
    assert "systemMessage" not in output["hookSpecificOutput"]
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert recalled_scopes == [["session"], ["session_context"], ["graph"]]


def test_first_ready_prompt_schedules_drain_without_waiting(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")
    scheduled = []
    synchronous_calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(module, "load_config", lambda: {"dataset": "pc1_will_memory"})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "http://localhost:8011"},
    )
    monkeypatch.setattr(module, "server_ready_hint", lambda _url: False)
    monkeypatch.setattr(module, "authed_liveness", lambda _url, timeout: "ready")
    monkeypatch.setattr(module, "mark_server_ready", lambda _url: None)
    monkeypatch.setattr(module, "_load_session_id", lambda: "session")
    monkeypatch.setattr(module, "get_dataset", lambda _config: "pc1_will_memory")
    monkeypatch.setattr(
        module,
        "schedule_warmup_drain",
        lambda dataset, session_id: scheduled.append((dataset, session_id)) or True,
        raising=False,
    )

    def _synchronous_drain(*args, **kwargs):
        synchronous_calls.append((args, kwargs))
        raise AssertionError("the prompt hook must not drain synchronously")

    monkeypatch.setattr(module, "drain_warmup_entries", _synchronous_drain, raising=False)
    monkeypatch.setattr(module, "get_session_key", lambda: "session")
    monkeypatch.setattr(
        module,
        "read_and_reset_save_counter",
        lambda _session: {"prompt": 0, "trace": 0, "answer": 0},
    )
    monkeypatch.setattr(module, "recall_via_http", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "render_status_for_host", lambda _session: "Cognee")

    started = time.perf_counter()
    asyncio.run(module._run("remember this"))
    elapsed = time.perf_counter() - started

    assert scheduled == [("pc1_will_memory", "session")]
    assert synchronous_calls == []
    assert elapsed < 0.25


def test_noop_hooks_emit_valid_user_prompt_submit_json(tmp_path):
    payload = json.dumps({"session_id": "test", "prompt": "no"})
    for script in ("session-context-lookup.py", "store-user-prompt.py"):
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS / script)],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": str(pathlib.Path(sys.executable).parent),
            },
        )
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"] == {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "",
        }
