"""Windows integration tests for the popup-safe Codex hook shell."""

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_SOURCE = _PLUGIN_DIR / "windows" / "WindowlessPowerShell.cs"
_INSTALLER = _PLUGIN_DIR / "scripts" / "install-windowless-powershell.ps1"
_PRIVATE_INSTALLER = _PLUGIN_DIR.parents[1] / "scripts" / "install-private-plugin.ps1"
_CONSOLE_WINDOW_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "ConsoleWindowClass",
    "PseudoConsoleWindow",
}


def _csc_path():
    windows = pathlib.Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def _compile_proxy(tmp_path):
    assert _SOURCE.is_file(), f"missing windowless shell source: {_SOURCE}"
    compiler = _csc_path()
    if compiler is None:
        pytest.skip("Windows .NET Framework C# compiler is unavailable")
    output = tmp_path / "pwsh.exe"
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/optimize+",
            f"/out:{output}",
            str(_SOURCE),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def _pe_subsystem(path):
    image = path.read_bytes()
    pe_offset = int.from_bytes(image[0x3C:0x40], "little")
    optional_header = pe_offset + 24
    magic = int.from_bytes(image[optional_header : optional_header + 2], "little")
    subsystem_offset = optional_header + (88 if magic == 0x20B else 68)
    return int.from_bytes(image[subsystem_offset : subsystem_offset + 2], "little")


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
def test_proxy_is_gui_and_preserves_powershell_stdio_and_exit_code(tmp_path):
    proxy = _compile_proxy(tmp_path)

    assert _pe_subsystem(proxy) == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
    env = {**os.environ, "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}
    command = (
        "$text = [Console]::In.ReadToEnd(); "
        "[Console]::Out.Write($text.Trim()); "
        '[Console]::Error.Write("stderr-ok"); exit 7'
    )
    result = subprocess.run(
        [str(proxy), "-NoProfile", "-Command", command],
        input="stdin-ok\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.stdout == "stdin-ok"
    assert result.stderr == "stderr-ok"
    assert result.returncode == 7


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows behavior")
def test_proxy_creates_no_visible_console_from_gui_parent(tmp_path):
    proxy = _compile_proxy(tmp_path)
    pythonw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("pythonw.exe is unavailable")

    argv = [
        str(proxy),
        "-NoProfile",
        "-Command",
        "Start-Sleep -Milliseconds 750",
    ]
    source = (
        "import json,os,subprocess;"
        "env=os.environ.copy();"
        "env['CODEX_INTERNAL_ORIGINATOR_OVERRIDE']='Codex Desktop';"
        f"subprocess.run(json.loads({json.dumps(json.dumps(argv))}),"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,env=env)"
    )
    baseline = set(_visible_windows())
    launcher = subprocess.Popen([str(pythonw), "-c", source])
    observed = {}
    deadline = time.monotonic() + 3
    while launcher.poll() is None and time.monotonic() < deadline:
        for hwnd, row in _visible_windows().items():
            if hwnd not in baseline and row[1] in _CONSOLE_WINDOW_CLASSES:
                observed[hwnd] = row
        time.sleep(0.01)
    launcher.wait(timeout=3)

    assert observed == {}


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows behavior")
def test_installer_owns_updates_and_reversible_removal(tmp_path):
    destination = tmp_path / "codex-bin" / "pwsh.exe"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_INSTALLER),
        "-Destination",
        str(destination),
    ]

    first = subprocess.run(command, text=True, capture_output=True, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    installed = json.loads(first.stdout)
    marker = pathlib.Path(installed["marker"])
    assert pathlib.Path(installed["path"]) == destination
    assert destination.is_file()
    assert marker.is_file()
    assert installed["sha256"] == _sha256(destination)

    second = subprocess.run(command, text=True, capture_output=True, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["sha256"] == installed["sha256"]

    removed = subprocess.run(
        command + ["-Remove"], text=True, capture_output=True, check=False
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not destination.exists()
    assert not marker.exists()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"unrelated pwsh")
    refused = subprocess.run(command, text=True, capture_output=True, check=False)
    assert refused.returncode != 0
    assert destination.read_bytes() == b"unrelated pwsh"


def _sha256(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_private_plugin_installer_deploys_owned_windowless_shell():
    installer = _PRIVATE_INSTALLER.read_text(encoding="utf-8")
    assert "install-windowless-powershell.ps1" in installer
    assert "windowless_shell" in installer
