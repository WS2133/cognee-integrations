"""Regression checks for immediate and fallback assistant capture."""

import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile
from unittest import mock

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _PLUGIN_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
_SPEC = importlib.util.spec_from_file_location(
    "cognee_store_to_session",
    _SCRIPTS_DIR / "store-to-session.py",
)
_STORE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_STORE)


def test_capture_fallback_when_stop_hook_is_missed():
    rows = [
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "old",
                "last_agent_message": "old answer",
            },
        },
        "not json",
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "new",
                "last_agent_message": "new answer",
            },
        },
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        transcript = pathlib.Path(temp_dir) / "rollout.jsonl"
        transcript.write_text(
            "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows),
            encoding="utf-8",
        )
        completed = {"turn_id": "new", "last_assistant_message": "new answer"}
        assert _STORE._latest_completed_turn(transcript) == completed

        save = mock.AsyncMock()
        with mock.patch.object(_STORE, "_store_assistant_stop", save):
            asyncio.run(_STORE._store_latest_completed_turn(transcript))
        save.assert_awaited_once_with(completed)

    with (
        mock.patch.object(_STORE, "_load_session", return_value=("session", "dataset", "user")),
        mock.patch.object(_STORE, "pop_pending_prompt", return_value={}),
        mock.patch.object(_STORE, "load_config") as load_config,
    ):
        asyncio.run(
            _STORE._store_assistant_stop(
                {"turn_id": "already-stored", "last_assistant_message": "answer"}
            )
        )
    load_config.assert_not_called()

    hooks = json.loads((_PLUGIN_DIR / "hooks.json").read_text(encoding="utf-8"))
    stop_hooks = hooks["hooks"]["Stop"][0]["hooks"]
    assert stop_hooks == [
        {
            "type": "command",
            "command": 'python3 -X utf8 "${PLUGIN_ROOT}/scripts/store-to-session.py" --stop',
            "timeout": 120,
            "statusMessage": "Saving assistant response...",
        }
    ]


if __name__ == "__main__":
    test_capture_fallback_when_stop_hook_is_missed()
    print("PASS test_capture_fallback_when_stop_hook_is_missed")
