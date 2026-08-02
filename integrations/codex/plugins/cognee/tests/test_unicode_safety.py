"""Regression tests for malformed Unicode in captured Codex context."""

import importlib.util
import json
import pathlib
import sys
import urllib.request
from unittest import mock

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _PLUGIN_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _plugin_common  # noqa: E402

_STORE_SPEC = importlib.util.spec_from_file_location(
    "cognee_store_to_session",
    _SCRIPTS_DIR / "store-to-session.py",
)
_STORE_MODULE = importlib.util.module_from_spec(_STORE_SPEC)
_STORE_SPEC.loader.exec_module(_STORE_MODULE)


def test_captured_text_replaces_lone_surrogate():
    result = _STORE_MODULE._truncate_str("before\udc9dafter", 100)

    assert result == "before?after"
    json.dumps({"value": result}, ensure_ascii=False).encode("utf-8")


def test_http_payload_replaces_lone_surrogate():
    captured = {}

    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, **_kwargs):
        captured["data"] = request.data
        return _Response()

    with (
        mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen),
        mock.patch.object(_plugin_common, "_local_api_url", return_value="http://example.invalid"),
        mock.patch.object(_plugin_common, "_api_key", return_value=""),
    ):
        _plugin_common._json_http_request("/test", {"value": "before\udc9dafter"})

    assert json.loads(captured["data"].decode("utf-8")) == {"value": "before?after"}
