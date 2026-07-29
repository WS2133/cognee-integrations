"""Contract tests for selective Codex session capture."""

import asyncio
import importlib.util
import json
import pathlib
import sys
import urllib.request
from unittest.mock import patch

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PLUGIN = _ROOT / "plugins" / "cognee"
_SCRIPTS = _PLUGIN / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), _SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commands(manifest, event):
    return [
        hook["command"]
        for group in manifest["hooks"].get(event, [])
        for hook in group.get("hooks", [])
    ]


def test_hook_manifest_captures_prompts_and_answers_without_tool_traces_or_recall():
    manifest = json.loads((_PLUGIN / "hooks.json").read_text(encoding="utf-8"))

    assert "PostToolUse" not in manifest["hooks"]
    assert _commands(manifest, "UserPromptSubmit") == [
        'python3 "${PLUGIN_ROOT}/scripts/store-user-prompt.py"'
    ]
    assert _commands(manifest, "Stop") == [
        'python3 "${PLUGIN_ROOT}/scripts/store-to-session.py" --stop'
    ]
    assert len(_commands(manifest, "PreCompact")) == 1
    assert len(_commands(manifest, "SessionEnd")) == 1


def test_precompact_anchor_is_session_only_and_bounded():
    module = _load_script("pre-compact.py")
    entries = [
        {
            "question": "Q" * 5000,
            "answer": "A" * 5000,
            "origin_function": "Bash",
            "method_return_value": "tool output",
        }
    ]

    anchor = module._build_anchor(entries)

    assert 0 < len(anchor) <= 4000
    assert "Bash" not in anchor
    assert "tool output" not in anchor


def test_prompt_context_keeps_only_project_slug():
    module = _load_script("store-user-prompt.py")

    context = json.loads(
        module._prompt_context(
            {
                "cwd": r"C:\git\contextctl",
                "model": "secret-model",
                "turn_id": "secret-turn",
                "transcript_path": r"C:\secret\transcript.jsonl",
            }
        )
    )

    assert context == {"project": "contextctl"}


def test_capture_redacts_common_secret_shapes():
    module = _load_script("store-user-prompt.py")
    text = "API_KEY=supersecret Bearer bearer-secret sk-1234567890abcdefghijkl"

    redacted = module._redact_secrets(text)

    assert "supersecret" not in redacted
    assert "bearer-secret" not in redacted
    assert "sk-1234567890abcdefghijkl" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_http_stop_ignores_local_readiness_hint():
    module = _load_script("store-to-session.py")
    calls = {"remember": 0, "buffer": 0}

    with (
        patch.object(module, "_load_session", return_value=("session", "dataset", "")),
        patch.object(module, "load_config", return_value={}),
        patch.object(
            module,
            "resolve_runtime_mode",
            return_value={"mode": "http", "base_url": "http://example.invalid"},
        ),
        patch.object(module, "server_ready_hint", return_value=False),
        patch.object(
            module,
            "pop_pending_prompt",
            return_value={"prompt": "What changed?", "context": ""},
        ),
        patch.object(
            module,
            "remember_entry_via_http",
            side_effect=lambda *_args: calls.__setitem__(
                "remember", calls["remember"] + 1
            )
            or {"entry_id": "qa"},
        ),
        patch.object(
            module,
            "append_warmup_entry",
            side_effect=lambda *_args: calls.__setitem__(
                "buffer", calls["buffer"] + 1
            ),
        ),
        patch.object(module, "append_http_bridge_entry"),
        patch.object(module, "bump_save_counter"),
        patch.object(module, "hook_log"),
        patch.object(module, "notify"),
        patch.object(module, "touch_activity"),
    ):
        asyncio.run(
            module._store_assistant_stop(
                {"assistant_message": "Use one selective write after the answer."}
            )
        )

    assert calls == {"remember": 1, "buffer": 0}


def test_http_payload_replaces_lone_surrogate():
    import _plugin_common

    captured = {}

    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, **_kwargs):
        captured["data"] = request.data
        return _Response()

    with (
        patch.object(urllib.request, "urlopen", side_effect=fake_urlopen),
        patch.object(_plugin_common, "_local_api_url", return_value="http://example.invalid"),
        patch.object(_plugin_common, "_api_key", return_value=""),
    ):
        _plugin_common._json_http_request("/test", {"value": "before\udc9dafter"})

    assert json.loads(captured["data"].decode("utf-8")) == {"value": "before?after"}
