#!/usr/bin/env python3
"""Store prompts and locally queue the prior completed Codex turn.

Runs on UserPromptSubmit, but performs local file I/O only. Network delivery is
owned by a detached drain worker so prompt admission cannot wait on Cognee.

Configuration:
    Resolves session state from local plugin config and the local session map.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _proc import background_process_kwargs, hide_console_window, pid_alive

if __name__ == "__main__":
    hide_console_window()

from _plugin_common import (
    bump_save_counter,
    get_session_key,
    hook_log,
    notify,
    quiet_hook_output,
    read_stdin_utf8,
    remember_pending_prompt,
    resolve_session_key_from_payload,
    set_session_key,
    touch_activity,
)
from config import get_dataset, get_session_id, load_config

MAX_TEXT = 4000
_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")
_STORE_SPEC = importlib.util.spec_from_file_location(
    "cognee_store_to_session",
    Path(__file__).with_name("store-to-session.py"),
)
_STORE_MODULE = importlib.util.module_from_spec(_STORE_SPEC)
_STORE_SPEC.loader.exec_module(_STORE_MODULE)


def _load_session() -> tuple[str, str, str]:
    config = load_config()
    return get_session_id(config), get_dataset(config), ""


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
    except Exception as exc:
        hook_log("prompt_watcher_alive_check_failed", {"error": str(exc)[:200]})
        return False
    return pid_alive(pid)


def _ensure_idle_watcher(session_id: str, dataset: str, user_id: str, config: dict) -> None:
    """Start the idle watcher on a new prompt if the prior one exited after bridging."""
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    if not session_id or _watcher_alive():
        return

    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("prompt_watcher_stop_unlink_failed", {"error": str(exc)[:200]})

    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "session_key": os.environ.get("COGNEE_SESSION_KEY", ""),
        "config": {
            "base_url": config.get("base_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = _STATE_DIR / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("prompt_watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        env = os.environ.copy()
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            close_fds=True,
            **background_process_kwargs(),
        )
        hook_log("idle_watcher_restarted", {"session": session_id, "dataset": dataset})
    except Exception as exc:
        hook_log("idle_watcher_restart_failed", {"error": str(exc)[:200]})


def _prompt_context(payload: dict) -> str:
    context = {
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "turn_id": payload.get("turn_id"),
        "transcript_path": payload.get("transcript_path"),
    }
    return json.dumps({k: v for k, v in context.items() if v}, default=str)


def _store(prompt: str, payload: dict):
    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "prompt"})
        return

    try:
        _STORE_MODULE._queue_latest_completed_turn(payload.get("transcript_path"))
    except Exception as exc:
        hook_log("prior_answer_flush_failed", {"error": str(exc)[:200]})

    config = load_config()
    touch_activity()
    _ensure_idle_watcher(session_id, dataset, user_id, config)

    remember_pending_prompt(
        session_id,
        prompt[:MAX_TEXT],
        turn_id=str(payload.get("turn_id") or ""),
        context=_prompt_context(payload),
    )
    hook_log("prompt_pending", {"chars": len(prompt), "turn_id": payload.get("turn_id")})
    notify(f"user prompt pending ({len(prompt)} chars)")
    bump_save_counter(session_id, "prompt")


def main():
    payload_raw = read_stdin_utf8()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json", {"event": "prompt"})
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log("prompt_session_key", {"source": session_key_source, "value": session_key_candidate})
    if not get_session_key():
        hook_log("prompt_missing_session_key")
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    try:
        with quiet_hook_output("store-user-prompt"):
            _store(prompt, payload)
    except Exception as exc:
        hook_log("prompt_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "",
                }
            }
        )
    )
