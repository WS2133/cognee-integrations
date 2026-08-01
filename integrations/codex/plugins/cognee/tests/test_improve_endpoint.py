"""Regression tests for the official Cognee improve API contract."""

import pathlib
import sys
import urllib.error
from unittest import mock

_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import _plugin_common  # noqa: E402


def test_session_improve_uses_selective_endpoint_and_payload():
    with mock.patch.object(
        _plugin_common,
        "_json_http_request",
        return_value={"status": "completed"},
    ) as request:
        result = _plugin_common.improve_session_via_http("pc1_will_memory", "session-1")

    request.assert_called_once_with(
        "/api/v1/improve/distill",
        {
            "dataset_name": "pc1_will_memory",
            "session_id": "session-1",
        },
        timeout=mock.ANY,
    )
    assert result["ok"] is True


def test_session_improve_treats_conflict_as_retryable_busy():
    error = urllib.error.HTTPError("https://cognee.example", 409, "conflict", {}, None)
    with mock.patch.object(_plugin_common, "_json_http_request", side_effect=error):
        result = _plugin_common.improve_session_via_http("pc1_will_memory", "session-1")

    assert result == {"ok": False, "busy": True, "status": 409}


def test_session_improve_treats_in_progress_as_retryable_busy():
    with mock.patch.object(
        _plugin_common,
        "_json_http_request",
        return_value={"status": "in_progress", "accepted_count": 0, "rejected_count": 0},
    ):
        result = _plugin_common.improve_session_via_http("pc1_will_memory", "session-1")

    assert result == {"ok": False, "busy": True}
