"""Regression test for disabling the unreliable Codex Desktop exit fallback."""

import importlib.util
import pathlib

_SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
)


def _load_session_start():
    spec = importlib.util.spec_from_file_location(
        "cognee_session_start", _SCRIPTS / "session-start.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exit_watcher_disabled_skips_process_launch(monkeypatch, tmp_path):
    module = _load_session_start()
    monkeypatch.setenv("COGNEE_EXIT_WATCHER_DISABLED", "1")
    monkeypatch.setattr(module, "_EXIT_WATCHERS_DIR", tmp_path / "exit-watchers")
    monkeypatch.setattr(module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(module, "_find_codex_parent_pid", lambda: 12345)
    monkeypatch.setattr(module, "_pid_alive", lambda _pid: False)

    launches = []

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append((_args, _kwargs)),
    )

    module._spawn_exit_watcher("session", "agent_sessions")
    assert launches == []
