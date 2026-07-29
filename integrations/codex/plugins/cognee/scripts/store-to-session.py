#!/usr/bin/env python3
"""Store a final assistant response as one paired Q&A session entry."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    _redact_secrets,
    append_http_bridge_entry,
    append_warmup_entry,
    bump_save_counter,
    get_session_key,
    hook_log,
    load_resolved,
    notify,
    pop_pending_prompt,
    quiet_hook_output,
    remember_entry_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    server_ready_hint,
    set_session_key,
    touch_activity,
)
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

_MAX_ASSISTANT_BYTES = 8000


def _truncate_str(value, cap: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return encoded.decode("utf-8")
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _load_session() -> tuple[str, str, str]:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


async def _store_assistant_stop(payload: dict) -> None:
    message = _redact_secrets(_truncate_str(
        payload.get("assistant_message") or payload.get("last_assistant_message") or "",
        _MAX_ASSISTANT_BYTES,
    ))
    if not message or message == "null":
        return

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return

    pending = pop_pending_prompt(session_id, turn_id=str(payload.get("turn_id") or ""))
    entry = {
        "type": "qa",
        "question": pending.get("prompt", ""),
        "answer": message,
        "context": pending.get("context", ""),
    }
    config = load_config()
    runtime = resolve_runtime_mode()
    use_http = runtime["mode"] == "http"

    if not use_http and not server_ready_hint(runtime.get("base_url", "")):
        append_warmup_entry(dataset, session_id, entry)
        append_http_bridge_entry(
            dataset,
            session_id,
            question=entry["question"],
            answer=message,
        )
        bump_save_counter(session_id, "answer")
        hook_log("store_buffered_warming", {"hook": "stop"})
        return
    if not use_http:
        await ensure_cognee_ready(config)

    try:
        if use_http:
            result = remember_entry_via_http(dataset, session_id, entry)
        else:
            import cognee
            from cognee.memory import QAEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                QAEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("stop_store_error", {"error": str(exc)[:200]})
        notify(f"stop store failed ({exc})")
        return

    if result:
        if use_http:
            append_http_bridge_entry(
                dataset,
                session_id,
                question=entry["question"],
                answer=message,
            )
        hook_log("stop_stored", {"chars": len(message)})
        notify(f"assistant message stored ({len(message)} chars)")
        bump_save_counter(session_id, "answer")
        touch_activity()


def main():
    if "--stop" not in sys.argv:
        hook_log("skip_non_stop_capture")
        return

    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return

    session_key, source = resolve_session_key_from_payload(payload)
    if session_key:
        set_session_key(session_key)
    hook_log("store_session_key", {"source": source, "value": session_key})
    if not get_session_key():
        hook_log("store_missing_session_key")
        return

    try:
        with quiet_hook_output("store-to-session"):
            asyncio.run(_store_assistant_stop(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": True, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
