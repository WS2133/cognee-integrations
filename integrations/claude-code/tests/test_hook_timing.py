"""Contract tests for selective Claude Code session capture."""

import importlib.util
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
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
    manifest = json.loads((_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert "PostToolUse" not in manifest["hooks"]
    assert _commands(manifest, "UserPromptSubmit") == [
        "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/store-user-prompt.py"
    ]
    assert _commands(manifest, "Stop")[0] == (
        "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/store-to-session.py --stop"
    )
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
