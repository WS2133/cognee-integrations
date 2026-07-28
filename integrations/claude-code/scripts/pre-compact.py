#!/usr/bin/env python3
"""Emit a bounded Q&A-only memory anchor before context compaction."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_key,
    hook_log,
    load_resolved,
    quiet_hook_output,
    recall_via_http,
    resolve_session_key_from_payload,
    resolve_user,
    set_session_key,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
)

_SESSION_TOP_K = 5
_ANCHOR_MAX_CHARS = 4000


def _load_resolved_fields() -> tuple[str, str, str]:
    if not get_session_key():
        hook_log("precompact_missing_session_key")
        return "", "", ""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _as_dict(entry):
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    if hasattr(entry, "dict"):
        return entry.dict()
    if hasattr(entry, "__dict__"):
        return dict(entry.__dict__)
    return entry


async def _recall(session_id: str, dataset: str, config: dict, user=None) -> list:
    try:
        if is_cloud_mode(config):
            results = recall_via_http(
                "",
                session_id=session_id,
                top_k=_SESSION_TOP_K,
                scope=["session"],
                only_context=True,
            )
        else:
            import cognee

            results = await cognee.recall(
                "",
                session_id=session_id,
                top_k=_SESSION_TOP_K,
                scope=["session"],
                user=user,
            )
        return list(results) if results else []
    except Exception as exc:
        hook_log("precompact_recall_error", {"error": str(exc)[:200]})
        return []


def _format_session_section(entries: list) -> str:
    lines = ["### Recent decisions and discussion"]
    for raw in entries[-_SESSION_TOP_K:]:
        entry = _as_dict(raw)
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if question:
            lines.append(f"- Q: {question[:500]}")
        if answer:
            lines.append(f"  A: {answer[:700]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_anchor(entries: list) -> str:
    section = _format_session_section(entries)
    if not section:
        return ""
    anchor = (
        "## Cognee Memory Anchor\n"
        "Preserved recent user prompts and final assistant responses only.\n\n"
        f"{section}"
    )
    return anchor[:_ANCHOR_MAX_CHARS]


async def _run() -> str:
    session_id, dataset, user_id = _load_resolved_fields()
    if not session_id:
        return ""

    config = load_config()
    await ensure_cognee_ready(config)
    user = None
    if not is_cloud_mode(config):
        user = await resolve_user(user_id)
        await ensure_dataset_ready(dataset, user)

    entries = await _recall(session_id, dataset, config, user)
    if not entries and not is_cloud_mode(config):
        try:
            from cognee.infrastructure.session.get_session_manager import get_session_manager

            manager = get_session_manager()
            if manager.is_available and user_id:
                entries = list(
                    await manager.get_session(
                        user_id=user_id,
                        session_id=session_id,
                        formatted=False,
                    )
                    or []
                )
        except Exception as exc:
            hook_log("precompact_direct_fetch_error", {"error": str(exc)[:200]})

    anchor = _build_anchor(entries)
    hook_log("precompact_anchor", {"session_entries": min(len(entries), _SESSION_TOP_K)})
    return anchor


def main():
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    session_key, _ = resolve_session_key_from_payload(payload)
    if session_key:
        set_session_key(session_key)

    anchor = ""
    try:
        with quiet_hook_output("pre-compact"):
            anchor = asyncio.run(_run())
    except Exception as exc:
        hook_log("precompact_run_exception", {"error": str(exc)[:200]})
    if anchor:
        print(anchor)


if __name__ == "__main__":
    main()
