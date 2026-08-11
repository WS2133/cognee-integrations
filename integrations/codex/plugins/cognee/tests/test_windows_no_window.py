"""Windows canaries for plugin-scoped console suppression."""

import ctypes
import json
import pathlib
import subprocess
import sys
import time

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _PLUGIN_DIR / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
from _proc import background_process_kwargs  # noqa: E402

_CONSOLE_WINDOW_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "ConsoleWindowClass",
}


def _visible_windows():
    user32 = ctypes.windll.user32
    rows = {}
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        class_name = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        user32.GetWindowTextW(hwnd, title, len(title))
        rows[int(hwnd)] = (process_id.value, class_name.value, title.value)
        return True

    user32.EnumWindows(collect, 0)
    return rows


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows behavior")
def test_background_children_use_create_no_window():
    flags = background_process_kwargs()["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & subprocess.DETACHED_PROCESS


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows behavior")
def test_hook_hides_inherited_console_from_gui_parent(tmp_path):
    pythonw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("pythonw.exe is unavailable")

    marker = tmp_path / "hidden.marker"
    probe = tmp_path / "hide_probe.py"
    marker_write = (
        f"pathlib.Path({str(marker)!r}).write_text("
        "json.dumps({'hidden':hidden,'title':title.value}), encoding='utf-8')\n"
    )
    probe_source = (
        "import ctypes,json,pathlib,sys,time\n"
        f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
        "from _proc import hide_console_window\n"
        "hidden=hide_console_window()\n"
        "title=ctypes.create_unicode_buffer(512)\n"
        "ctypes.windll.kernel32.GetConsoleTitleW(title, len(title))\n"
        + marker_write
        + "time.sleep(0.75)\n"
    )
    probe.write_text(probe_source, encoding="utf-8")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"& '{sys.executable}' '{probe}'",
    ]
    source = (
        "import json,subprocess;"
        f"subprocess.run(json.loads({json.dumps(json.dumps(command))}),"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )

    baseline = set(_visible_windows())
    launcher = subprocess.Popen([str(pythonw), "-c", source])
    deadline = time.monotonic() + 3
    visible_after_hide = {}
    while launcher.poll() is None and time.monotonic() < deadline:
        for hwnd, row in _visible_windows().items():
            if marker.exists() and hwnd not in baseline and row[1] in _CONSOLE_WINDOW_CLASSES:
                visible_after_hide[hwnd] = row
        time.sleep(0.01)
    launcher.wait(timeout=3)
    assert marker.exists(), "console-hiding probe did not start"
    result = json.loads(marker.read_text(encoding="utf-8"))
    assert result["hidden"] is True
    assert visible_after_hide == {}, result
