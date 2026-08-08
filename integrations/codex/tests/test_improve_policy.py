"""Contracts for count-based, idle, and shutdown Cognee promotion policy."""

import asyncio
import importlib.util
import json
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as plugin_common  # noqa: E402


def _load_idle_watcher():
    path = _SCRIPTS / "idle-watcher.py"
    spec = importlib.util.spec_from_file_location("idle_watcher_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zero_disables_count_based_improve(tmp_path, monkeypatch):
    counter = tmp_path / "counter.json"
    counter.write_text(json.dumps({"s1": 149}), encoding="utf-8")
    monkeypatch.setattr(plugin_common, "_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(plugin_common, "_COUNTER_FILE", counter)
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "0")

    assert plugin_common._auto_improve_threshold() == 0
    assert plugin_common.bump_turn_counter("s1") == (150, False)


def test_invalid_count_threshold_uses_documented_default(monkeypatch):
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "invalid")
    assert plugin_common._auto_improve_threshold() == plugin_common.AUTO_IMPROVE_EVERY_DEFAULT


def test_idle_false_keeps_signal_and_stop_shutdown_sync_enabled(monkeypatch):
    idle_watcher = _load_idle_watcher()
    monkeypatch.setenv("COGNEE_IDLE_IMPROVE", "false")

    assert idle_watcher.idle_improve_enabled() is False
    assert (
        asyncio.run(idle_watcher.should_run_shutdown_sync("signal", activity_is_new=True)) is True
    )
    assert (
        asyncio.run(idle_watcher.should_run_shutdown_sync("stop_sentinel", activity_is_new=True))
        is True
    )
    assert (
        asyncio.run(idle_watcher.should_run_shutdown_sync("pidfile_replaced", activity_is_new=True))
        is False
    )
