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


def _configure_context_hook(module, tmp_path, monkeypatch, recall):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(module, "load_config", lambda: {"dataset": "pc1_will_memory"})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "https://cognee.example"},
    )
    monkeypatch.setattr(module, "server_ready_hint", lambda _url: True)
    monkeypatch.setattr(module, "_load_session_id", lambda: "session")
    monkeypatch.setattr(module, "get_dataset", lambda _config: "pc1_will_memory")
    monkeypatch.setattr(module, "get_session_key", lambda: "session")
    monkeypatch.setattr(
        module,
        "read_and_reset_save_counter",
        lambda _session: {"prompt": 0, "trace": 0, "answer": 0},
    )
    monkeypatch.setattr(module, "recall_via_http", recall)
    monkeypatch.setattr(module, "render_status_for_host", lambda _session: "Cognee")
    monkeypatch.setattr(module, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "notify", lambda *_args, **_kwargs: None)

    client = importlib.import_module("_cognee_client")
    monkeypatch.setattr(client, "breaker_open", lambda: (False, 0))


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


def test_current_state_prompt_requires_live_verification_without_graph(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")
    recalled_scopes = []

    def recall(*_args, **kwargs):
        scope = kwargs["scope"]
        recalled_scopes.append(scope)
        if scope == ["session_context"]:
            return [
                {
                    "source": "session_context",
                    "content": (
                        "Verify volatile configuration against its authoritative live source."
                    ),
                }
            ]
        if scope == ["graph"]:
            return [{"source": "graph", "text": "The extraction model is Terra-medium."}]
        return []

    _configure_context_hook(module, tmp_path, monkeypatch, recall)

    output = asyncio.run(
        module._run(
            "What extraction model is currently in use for memory extraction? "
            "Treat this as volatile configuration and require verification from the live source."
        )
    )
    context = output["hookSpecificOutput"]["additionalContext"]

    assert recalled_scopes == [["session"], ["session_context"]]
    assert "live_verification_required" in context
    assert "Terra-medium" not in context
    assert "[agent-guidance]" in context


def test_live_state_classifier_covers_plural_and_predicate_forms():
    module = _load_script("session-context-lookup.py")

    prompts = [
        "What schedules are configured for Hermes?",
        "What prices were retrieved for the watch list?",
        "Is Cognee healthy?",
        "Which Cognee process is running?",
    ]

    assert all(module._requires_live_verification(prompt) for prompt in prompts)


def test_unrelated_graph_result_abstains(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")

    def recall(*_args, **kwargs):
        if kwargs["scope"] == ["graph"]:
            return [
                {
                    "source": "graph",
                    "text": "The last NVIDIA graphics driver reset interrupted a competitive game.",
                }
            ]
        return []

    _configure_context_hook(module, tmp_path, monkeypatch, recall)

    output = asyncio.run(
        module._run(
            "Which alpine mushroom species did Will photograph last weekend, "
            "and at what exact trail location?"
        )
    )
    context = output["hookSpecificOutput"]["additionalContext"]

    assert "0 graph" in output["systemMessage"]
    assert "NVIDIA" not in context
    assert "(no memory matches for this prompt)" in context


def test_control_prompt_does_not_inject_unrelated_graph(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")

    def recall(*_args, **kwargs):
        if kwargs["scope"] == ["graph"]:
            return [
                {
                    "source": "graph",
                    "text": "Browser session leases expire after the authorized login window.",
                }
            ]
        return []

    _configure_context_hook(module, tmp_path, monkeypatch, recall)

    output = asyncio.run(module._run("continue"))
    context = output["hookSpecificOutput"]["additionalContext"]

    assert "0 graph" in output["systemMessage"]
    assert "Browser session leases" not in context


def test_relevant_graph_paraphrase_survives_relevance_gate(tmp_path, monkeypatch):
    module = _load_script("session-context-lookup.py")

    def recall(*_args, **kwargs):
        if kwargs["scope"] == ["graph"]:
            return [
                {
                    "source": "graph",
                    "text": (
                        "Cognee keeps long-lived semantic decisions. ContextCtl reads precise "
                        "operational records from authoritative systems."
                    ),
                }
            ]
        return []

    _configure_context_hook(module, tmp_path, monkeypatch, recall)

    output = asyncio.run(
        module._run(
            "Which layer keeps long-lived semantic decisions, and which gateway reads "
            "precise operational facts?"
        )
    )
    context = output["hookSpecificOutput"]["additionalContext"]

    assert "1 graph" in output["systemMessage"]
    assert "[graph-snapshot]" in context
    assert "Cognee keeps long-lived semantic decisions" in context


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
