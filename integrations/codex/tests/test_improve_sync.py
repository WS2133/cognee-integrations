"""Unit tests for the Codex session improvement bridge.

The sync posts only the dataset and session ID to Cognee's official improve
endpoint. Failures remain retryable and never fall back to persisting the raw
session document.

Run: python integrations/codex/tests/test_improve_sync.py (or via pytest).
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import _plugin_common as pc  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _with_seams(**overrides):
    saved = {k: getattr(pc, k) for k in overrides}
    for k, v in overrides.items():
        setattr(pc, k, v)
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(pc, k, v)


def test_improve_posts_expected_json_payload():
    captured = {}
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, **kwargs):
        captured["req"] = req
        captured["timeout"] = timeout
        return _Resp(b'{"status":"completed","accepted_count":1,"rejected_count":0}')

    urllib.request.urlopen = _fake
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is True
    assert captured["req"].full_url.endswith("/api/v1/improve/distill")
    body = json.loads(captured["req"].data.decode("utf-8"))
    assert body == {
        "dataset_name": "ds",
        "session_id": "sid",
    }
    assert captured["timeout"] >= 60


def test_improve_404_is_retryable_without_a_persistent_marker():
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, **kwargs):
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)

    urllib.request.urlopen = _raise
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["status"] == 404
    assert "error" in res


def test_improve_network_error_is_graceful():
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, **kwargs):
        raise urllib.error.URLError("connection refused")

    urllib.request.urlopen = _raise
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["status"] == 0
    assert "error" in res


def _run_session_improve(improve_result, *, drain_results=None):
    """Drive run_session_improve with all seams mocked; return (result, calls).

    ``drain_results`` is an optional list of (drained, remaining) tuples returned
    per drain call, defaulting to a clean (0, 0).
    """
    calls = {"drain": 0, "improve": 0}

    def _drain(d, s):
        calls["drain"] += 1
        if drain_results:
            return drain_results[min(calls["drain"] - 1, len(drain_results) - 1)]
        return (0, 0)

    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=_drain,
        ensure_dataset_via_http=lambda d: None,
        improve_session_via_http=lambda d, s, **k: (
            calls.__setitem__("improve", calls["improve"] + 1) or improve_result
        ),
        hook_log=lambda *a, **k: None,
        _DRAIN_RETRY_PAUSE_SECONDS=0.0,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
    return wrote, calls


def test_run_session_improve_happy_path_drains_then_improves():
    wrote, calls = _run_session_improve({"ok": True})
    assert wrote is True
    assert calls == {"drain": 1, "improve": 1}


def test_run_session_improve_does_not_fallback_when_endpoint_fails():
    wrote, calls = _run_session_improve({"ok": False, "status": 404, "error": "missing"})
    assert wrote is False
    assert calls["improve"] == 1


def test_run_session_improve_error_returns_false_without_legacy():
    wrote, calls = _run_session_improve({"ok": False, "status": 500, "error": "boom"})
    assert wrote is False


def test_improve_lock_skip_reports_busy():
    # improve() returns {} when the per-session lock skips the run — the helper
    # must surface that as busy, never as success.
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, **kwargs):
        return _Resp(b"{}")

    urllib.request.urlopen = _fake
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        res = pc.improve_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = orig

    assert res["ok"] is False
    assert res["busy"] is True


def test_run_session_improve_retries_busy_until_lock_frees():
    # A lock-skipped improve may have snapshotted the cache before the latest
    # turns — run_session_improve must re-submit until a run actually lands.
    import os

    results = [{"ok": False, "busy": True}, {"ok": False, "busy": True}, {"ok": True}]
    calls = {"improve": 0}

    def _improve(d, s, **k):
        calls["improve"] += 1
        return results[min(calls["improve"] - 1, len(results) - 1)]

    os.environ["COGNEE_IMPROVE_BUSY_RETRY_INTERVAL"] = "0.1"
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=lambda d, s: (0, 0),
        improve_session_via_http=_improve,
        hook_log=lambda *a, **k: None,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
        os.environ.pop("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", None)

    assert wrote is True
    assert calls["improve"] == 3


def test_incomplete_drain_returns_false_but_improve_still_runs():
    # Undelivered warmup entries mean the improve persisted an incomplete
    # session: the improve must still run (partial persist beats none), but the
    # sync must report failure so the caller's retry loop re-drives it.
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(0, 3), (0, 3)])
    assert wrote is False
    assert calls["improve"] == 1  # improve ran despite the incomplete drain
    assert calls["drain"] == 2  # one in-place retry happened


def test_drain_retry_recovers_and_sync_succeeds():
    # First drain fails on a blip, the in-place retry delivers the tail →
    # the sync is complete and reports success.
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(0, 3), (3, 0)])
    assert wrote is True
    assert calls["drain"] == 2
    assert calls["improve"] == 1


def test_clean_drain_skips_retry():
    wrote, calls = _run_session_improve({"ok": True}, drain_results=[(2, 0)])
    assert wrote is True
    assert calls["drain"] == 1  # nothing remaining → no retry


def test_run_session_improve_busy_deadline_gives_up():
    import os

    calls = {"improve": 0}

    def _always_busy(d, s, **k):
        calls["improve"] += 1
        return {"ok": False, "busy": True}

    os.environ["COGNEE_IMPROVE_BUSY_RETRY_INTERVAL"] = "0.1"
    os.environ["COGNEE_IMPROVE_BUSY_DEADLINE"] = "0.25"
    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=lambda d, s: (0, 0),
        improve_session_via_http=_always_busy,
        hook_log=lambda *a, **k: None,
    )
    try:
        wrote = pc.run_session_improve("ds", "sid")
    finally:
        _restore(saved)
        os.environ.pop("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", None)
        os.environ.pop("COGNEE_IMPROVE_BUSY_DEADLINE", None)

    assert wrote is False  # still busy at deadline → reported as not-synced
    assert calls["improve"] >= 2  # at least one retry happened


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
