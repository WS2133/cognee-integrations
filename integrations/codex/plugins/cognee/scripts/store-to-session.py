#!/usr/bin/env python3
"""Store tool calls and assistant responses into the Cognee session cache.

Routes tool calls to the structured ``TraceEntry`` path (new trace-step
shape with origin_function / method_params / method_return_value /
status). Routes a completed assistant message to a ``QAEntry``.

Runs from the PostToolUse path and transcript capture callers.

Configuration:
    Networked callers resolve session state through Cognee; latency-sensitive
    transcript callers use the local config and replay buffer.
"""

import asyncio
import json
import os
import sys
import urllib.error
from pathlib import Path

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    append_http_bridge_entry,
    append_warmup_entry,
    bump_save_counter,
    bump_turn_counter,
    get_session_key,
    hook_log,
    http_api_ready,
    load_resolved,
    notify,
    pop_pending_prompt,
    quiet_hook_output,
    read_stdin_utf8,
    remember_entry_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    run_session_improve,
    schedule_warmup_drain,
    server_ready_hint,
    set_session_key,
    touch_activity,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    improve_session_local,
    load_config,
)

# Hard cap per field to avoid ballooning the cache with massive tool outputs.
_MAX_PARAMS_BYTES = 4000
_MAX_RETURN_BYTES = 8000
_MAX_ASSISTANT_BYTES = 8000


def _latest_completed_turn(transcript_path) -> dict:
    """Return the latest completed Codex turn from its JSONL transcript."""
    if not transcript_path:
        return {}

    latest = {}
    try:
        # ponytail: linear scan is ample for local transcripts; index offsets if they grow large.
        with Path(transcript_path).open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, dict) or row.get("type") != "event_msg":
                    continue
                if payload.get("type") != "task_complete":
                    continue
                turn_id = str(payload.get("turn_id") or "")
                message = str(payload.get("last_agent_message") or "")
                if message and message != "null":
                    latest = {
                        "turn_id": turn_id,
                        "last_assistant_message": message,
                    }
    except OSError as exc:
        hook_log("transcript_read_failed", {"error": str(exc)[:200]})
    return latest


async def _fire_improve_background(dataset: str, session_id: str, user, reason: str) -> None:
    """Fire-and-forget session improve; failures are logged but never raised.

    The server selectively distills the session cache instead of re-posting the
    raw transcript — see run_session_improve.
    """
    try:
        if http_api_ready():
            wrote = run_session_improve(dataset, session_id)
            hook_log(
                "auto_improve_fired",
                {"reason": reason, "session": session_id, "via": "http_improve", "wrote": wrote},
            )
            if wrote:
                notify(f"session improve submitted ({reason})")
            return

        await ensure_dataset_ready(dataset, user)
        result = await improve_session_local(dataset, session_id, user)
        hook_log(
            "auto_improve_fired",
            {
                "reason": reason,
                "session": session_id,
                "via": "local_improve",
                "ok": bool(result.get("ok")),
            },
        )
        notify(f"session improve completed ({reason})")
    except Exception as exc:
        hook_log("auto_improve_error", {"reason": reason, "error": str(exc)[:200]})


def _truncate_str(value, cap: int) -> str:
    """Coerce to string and cap at ``cap`` bytes (utf-8), appending ``...`` if truncated."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    text = encoded.decode("utf-8")
    if len(encoded) <= cap:
        return text
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 409, 425, 429) or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _infer_status(payload: dict) -> tuple[str, str]:
    """Return (status, error_message) from a PostToolUse payload."""
    # Codex and Claude-style payloads may set tool_response.is_error=True on failures; also
    # check for an explicit 'error' key at the top level.
    response = payload.get("tool_response") or payload.get("tool_output") or ""
    if isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            err = response.get("error") or response.get("message") or "Tool reported an error."
            return "error", _truncate_str(err, 500)
    if isinstance(payload.get("error"), str) and payload["error"]:
        return "error", _truncate_str(payload["error"], 500)
    return "success", ""


def _load_session() -> tuple[str, str, str]:
    """Load session_id, dataset, user_id from resolved cache with fallbacks."""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _load_session_local() -> tuple[str, str, str]:
    """Resolve routing from local config/session maps without network I/O."""
    config = load_config()
    return get_session_id(config), get_dataset(config), ""


def _prepare_paired_qa(payload: dict, session_loader) -> tuple | None:
    """Pair one completed answer with its locally pending prompt."""
    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message") or "")
    if not msg or msg == "null":
        return None

    msg = _truncate_str(msg, _MAX_ASSISTANT_BYTES)
    session_id, dataset, user_id = session_loader()
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return None

    pending = pop_pending_prompt(session_id, turn_id=str(payload.get("turn_id") or ""))
    if not pending.get("prompt"):
        hook_log("stop_store_skipped_no_pending", {"turn_id": payload.get("turn_id")})
        return None

    entry = {
        "type": "qa",
        "question": pending.get("prompt", ""),
        "answer": msg,
        "context": pending.get("context", ""),
    }
    return session_id, dataset, user_id, pending, msg, entry


async def _store_tool_call(payload: dict) -> None:
    """Write a PostToolUse event as a TraceEntry."""
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input") or {}
    tool_output = payload.get("tool_output") or payload.get("tool_response") or ""

    # Suppress self-reference: any Bash call that mentions 'cognee' is
    # likely the plugin/CLI talking to itself and would recurse.
    if tool_name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command", ""))
        if "cognee" in cmd:
            hook_log("skip_self_cognee_bash", {"cmd_prefix": cmd[:80]})
            return

    status, error_message = _infer_status(payload)

    # Normalize method_params: small structured dict is ideal; fall back
    # to a truncated-string dict if we got something non-JSON-safe.
    if isinstance(tool_input, dict):
        params = {}
        for k, v in tool_input.items():
            params[k] = _truncate_str(v, _MAX_PARAMS_BYTES)
    else:
        params = {"value": _truncate_str(tool_input, _MAX_PARAMS_BYTES)}

    return_value = _truncate_str(tool_output, _MAX_RETURN_BYTES)

    session_id, dataset, user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"tool": tool_name})
        return

    config = load_config()
    runtime = resolve_runtime_mode()
    use_http = runtime["mode"] == "http"
    entry = {
        "type": "trace",
        "origin_function": tool_name,
        "status": status,
        "method_params": params,
        "method_return_value": return_value,
        "error_message": error_message,
        # LLM-backed feedback per step is expensive on a busy session —
        # fall back to the deterministic one-liner. Users who want the
        # LLM summary can flip this in a future config.
        "generate_feedback_with_llm": False,
    }

    if not use_http and not server_ready_hint(runtime.get("base_url", "")):
        # Server still warming: don't block the tool call and don't lose the
        # trace. Buffer the structured entry for a later /remember/entry replay
        # (improve bridges only what the server session cache holds), and keep
        # the legacy text mirror for the document-bridge fallback path.
        append_warmup_entry(dataset, session_id, entry)
        trace_text = (
            f"{tool_name} [{status}]\n"
            f"Params: {json.dumps(params, ensure_ascii=False)}\n"
            f"Return: {return_value}"
        )
        append_http_bridge_entry(dataset, session_id, trace=trace_text)
        bump_save_counter(session_id, "trace")
        hook_log("store_buffered_warming", {"hook": "tool", "tool": tool_name})
        return
    if not use_http:
        await ensure_cognee_ready(config)

    try:
        if use_http:
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
        else:
            import cognee
            from cognee.memory import TraceEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                TraceEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("trace_store_error", {"tool": tool_name, "error": str(exc)[:200]})
        notify(f"trace store failed ({exc})")
        return

    if result:
        trace_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log(
            "trace_stored",
            {
                "tool": tool_name,
                "status": status,
                "trace_id": trace_id,
            },
        )
        notify(f"trace stored ({tool_name}, {status})")
        if use_http:
            trace_text = (
                f"{tool_name} [{status}]\n"
                f"Params: {json.dumps(params, ensure_ascii=False)}\n"
                f"Return: {return_value}"
            )
            append_http_bridge_entry(
                dataset,
                session_id,
                trace=trace_text,
            )
        bump_save_counter(session_id, "trace")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")
    else:
        hook_log("trace_store_noresult", {"tool": tool_name})


async def _store_assistant_stop(payload: dict) -> None:
    """Write a final assistant message as a QAEntry."""
    prepared = _prepare_paired_qa(payload, _load_session)
    if prepared is None:
        return
    session_id, dataset, user_id, pending, msg, entry = prepared
    config = load_config()
    runtime = resolve_runtime_mode()
    use_http = runtime["mode"] == "http"

    if not use_http and not server_ready_hint(runtime.get("base_url", "")):
        # Server still warming: buffer the structured entry for a later
        # /remember/entry replay (improve bridges only what the server session
        # cache holds), and keep the legacy text mirror for the document-bridge
        # fallback path.
        append_warmup_entry(dataset, session_id, entry)
        append_http_bridge_entry(
            dataset,
            session_id,
            question=pending.get("prompt", ""),
            answer=msg,
        )
        bump_save_counter(session_id, "answer")
        hook_log("store_buffered_warming", {"hook": "stop"})
        return
    if not use_http:
        await ensure_cognee_ready(config)

    try:
        if use_http:
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
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
        if use_http and _retryable_http_error(exc):
            append_warmup_entry(dataset, session_id, entry)
            append_http_bridge_entry(
                dataset,
                session_id,
                question=pending.get("prompt", ""),
                answer=msg,
            )
            bump_save_counter(session_id, "answer")
            hook_log(
                "store_buffered_retryable_error",
                {"hook": "stop", "status": getattr(exc, "code", 0)},
            )
            return
        hook_log("stop_store_error", {"error": str(exc)[:200]})
        notify(f"stop store failed ({exc})")
        return

    if result:
        if use_http:
            append_http_bridge_entry(
                dataset,
                session_id,
                question=pending.get("prompt", ""),
                answer=msg,
            )
        qa_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log("stop_stored", {"chars": len(msg), "qa_id": qa_id})
        notify(f"assistant message stored ({len(msg)} chars)")
        bump_save_counter(session_id, "answer")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")


def _queue_assistant_stop(payload: dict) -> bool:
    """Durably queue a completed turn using local I/O only.

    UserPromptSubmit and SessionEnd are latency-sensitive host boundaries. They
    write the paired QA entry to the existing replay buffer, then let the
    no-window drain worker perform all network I/O after the hook returns.
    """
    prepared = _prepare_paired_qa(payload, _load_session_local)
    if prepared is None:
        return False
    session_id, dataset, _user_id, pending, msg, entry = prepared
    append_warmup_entry(dataset, session_id, entry)
    append_http_bridge_entry(
        dataset,
        session_id,
        question=pending.get("prompt", ""),
        answer=msg,
    )
    bump_save_counter(session_id, "answer")
    touch_activity()
    bump_turn_counter(session_id)
    scheduled = schedule_warmup_drain(dataset, session_id)
    hook_log(
        "stop_queued",
        {
            "chars": len(msg),
            "turn_id": payload.get("turn_id"),
            "drain_scheduled": scheduled,
        },
    )
    return True


async def _store_latest_completed_turn(transcript_path) -> None:
    completed = _latest_completed_turn(transcript_path)
    if completed:
        await _store_assistant_stop(completed)


def _queue_latest_completed_turn(transcript_path) -> bool:
    completed = _latest_completed_turn(transcript_path)
    return bool(completed and _queue_assistant_stop(completed))


def main():
    payload_raw = read_stdin_utf8()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log("store_session_key", {"source": session_key_source, "value": session_key_candidate})
    if not get_session_key():
        hook_log("store_missing_session_key")
        return

    is_stop = "--stop" in sys.argv
    try:
        with quiet_hook_output("store-to-session"):
            if is_stop:
                asyncio.run(_store_assistant_stop(payload))
            else:
                asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
