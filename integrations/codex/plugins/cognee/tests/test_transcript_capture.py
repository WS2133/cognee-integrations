"""Regression check for capture without a Codex Stop hook."""

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


def test_capture_without_stop_hook():
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

        queue = mock.Mock(return_value=True)
        with mock.patch.object(_STORE, "_queue_assistant_stop", queue):
            assert _STORE._queue_latest_completed_turn(transcript)
        queue.assert_called_once_with(completed)

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
    assert "Stop" not in hooks["hooks"]
    prompt_hooks = hooks["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert [hook["timeout"] for hook in prompt_hooks] == [10, 3]
    assert all(hook["commandWindows"].startswith("py -3 ") for hook in prompt_hooks)


def test_completed_turn_queue_is_local_only():
    payload = {"turn_id": "turn-1", "last_assistant_message": "answer"}
    pending = {"prompt": "question", "context": "cwd"}
    with (
        mock.patch.object(_STORE, "_load_session_local", return_value=("session", "dataset", "")),
        mock.patch.object(_STORE, "pop_pending_prompt", return_value=pending),
        mock.patch.object(_STORE, "append_warmup_entry") as append,
        mock.patch.object(_STORE, "append_http_bridge_entry") as mirror,
        mock.patch.object(_STORE, "bump_save_counter"),
        mock.patch.object(_STORE, "bump_turn_counter"),
        mock.patch.object(_STORE, "touch_activity"),
        mock.patch.object(_STORE, "schedule_warmup_drain", return_value=True) as schedule,
        mock.patch.object(_STORE, "remember_entry_via_http") as network_write,
    ):
        assert _STORE._queue_assistant_stop(payload)

    entry = {"type": "qa", "question": "question", "answer": "answer", "context": "cwd"}
    append.assert_called_once_with("dataset", "session", entry)
    mirror.assert_called_once_with("dataset", "session", question="question", answer="answer")
    schedule.assert_called_once_with("dataset", "session")
    network_write.assert_not_called()


if __name__ == "__main__":
    test_capture_without_stop_hook()
    test_completed_turn_queue_is_local_only()
    print("PASS test_capture_without_stop_hook")
