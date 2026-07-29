"""Contract tests for the selective session-distillation bridge."""

import json
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import _plugin_common as pc  # noqa: E402


class _Resp:
    status = 200

    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _with_seams(**overrides):
    saved = {key: getattr(pc, key) for key in overrides}
    for key, value in overrides.items():
        setattr(pc, key, value)
    return saved


def _restore(saved):
    for key, value in saved.items():
        setattr(pc, key, value)


def test_distill_posts_only_dataset_and_session():
    captured = {}
    original = urllib.request.urlopen

    def _fake(request, timeout=None, context=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Resp(b'{"status":"completed","documents":[]}')

    urllib.request.urlopen = _fake
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        result = pc.distill_session_via_http("project-context", "session-1")
    finally:
        _restore(saved)
        urllib.request.urlopen = original

    request = captured["request"]
    assert result["ok"] is True
    assert request.full_url == "http://x/api/v1/improve/distill"
    assert json.loads(request.data) == {
        "datasetName": "project-context",
        "sessionId": "session-1",
    }
    assert request.get_header("X-api-key") == "k"
    assert captured["timeout"] >= 600


def test_distill_network_error_is_graceful():
    original = urllib.request.urlopen

    def _raise(request, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    urllib.request.urlopen = _raise
    saved = _with_seams(_local_api_url=lambda: "http://x", _api_key=lambda: "k")
    try:
        result = pc.distill_session_via_http("ds", "sid")
    finally:
        _restore(saved)
        urllib.request.urlopen = original

    assert result["ok"] is False
    assert result["status"] == 0


def _run_distill(*, drain_results=None, response=None):
    calls = {"drain": 0, "distill": 0}

    def _drain(dataset, session):
        calls["drain"] += 1
        results = drain_results or [(0, 0)]
        return results[min(calls["drain"] - 1, len(results) - 1)]

    saved = _with_seams(
        _local_api_url=lambda: "http://x",
        _backend_reachable=lambda url: True,
        drain_warmup_entries=_drain,
        ensure_dataset_via_http=lambda dataset: None,
        distill_session_via_http=lambda dataset, session, **kwargs: (
            calls.__setitem__("distill", calls["distill"] + 1)
            or (response or {"ok": True})
        ),
        hook_log=lambda *args, **kwargs: None,
        _DRAIN_RETRY_PAUSE_SECONDS=0.0,
    )
    try:
        result = pc.run_session_distill("ds", "sid")
    finally:
        _restore(saved)
    return result, calls


def test_run_session_distill_drains_then_distills():
    result, calls = _run_distill()
    assert result is True
    assert calls == {"drain": 1, "distill": 1}


def test_incomplete_drain_defers_distillation_for_retry():
    result, calls = _run_distill(drain_results=[(0, 2), (0, 2)])
    assert result is False
    assert calls == {"drain": 2, "distill": 0}


def test_distill_failure_returns_false():
    result, calls = _run_distill(response={"ok": False, "status": 500})
    assert result is False
    assert calls["distill"] == 1


def test_run_session_distill_skips_when_same_session_is_in_flight():
    original_dir = pc._DISTILL_LOCK_DIR
    with tempfile.TemporaryDirectory(prefix="cognee-distill-lock-") as temp_dir:
        pc._DISTILL_LOCK_DIR = pathlib.Path(temp_dir)
        try:
            with pc.distill_session_lock("sid", "holder") as claimed:
                assert claimed is True
                result, calls = _run_distill()
            assert result is False
            assert calls == {"drain": 0, "distill": 0}
        finally:
            pc._DISTILL_LOCK_DIR = original_dir
