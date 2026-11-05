"""Bring one session's VS Code window and tab to the front. Windows only.

Checked on real files (extension 2.1.318, VS Code 1.144):
* `~/.claude/ide/*.lock` lists the exact folder of each open window.
* Switching to the window found by its title takes 0.02-0.11 s. `Code.exe <that
  folder>`, which also focuses it instead of opening another one, takes 0.5-0.9 s and
  is only the fallback. Windows refuses the switch to a background process, as this
  server is when you click in the browser, unless it has just received input: hence
  the empty mouse event, which moves nothing.
* `vscode://anthropic.claude-code/open?session=<id>` reveals the session's tab if
  it lives in the window that receives it. VS Code gives the link to the active
  window, and in any other window the extension would open a second copy of the
  running session. So the link is sent only once that window is confirmed in front.

* With the companion extension (vscode-extension/) the window itself opens the tab,
  in milliseconds and with no risk of hitting another window. The link is the fallback
  for windows that have not loaded it.

A closed session reopens in its own folder: in that window if it is open, in a new one
if not. A closed session has no running copy to duplicate, but its tab is still sent
only to a window confirmed to be the right one.

Lock files also hold an auth token: only `workspaceFolders` is read from them.
"""

from __future__ import annotations

import http.client
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SESSION_URI = "vscode://anthropic.claude-code/open?session="
APP_NAME = "Visual Studio Code"
FOCUS_TIMEOUT_SECONDS = 5.0
SWITCH_TIMEOUT_SECONDS = 0.4
OPENED_FOCUS_TIMEOUT_SECONDS = 1.5  # With the tab ready there is no duplicate to guard against.
NEW_WINDOW_TIMEOUT_SECONDS = 45.0
# Once a new window shows. The Claude extension took over 8 s to load there, and a link
# sent meanwhile was lost: the wait must be generous.
COMPANION_GRACE_SECONDS = 30.0
NEW_WINDOW_POLL_SECONDS = 0.2
COMPANION_TIMEOUT_SECONDS = 0.5
FOCUS_POLL_SECONDS = 0.01
SETTLE_SECONDS = 0.05  # VS Code learns which window is active just after Windows does.

_one_at_a_time = threading.Lock()  # Two jumps at once could send a link to the wrong window.


def supported() -> bool:
    return sys.platform == "win32"


def jumpable(entry: dict | None) -> bool:
    """Only sessions running now inside the extension: anything else has no tab to reveal."""
    return (
        supported()
        and bool(entry)
        and entry.get("liveness") == "open"
        and entry.get("entrypoint") == "claude-vscode"
    )


def reopenable(session: dict | None) -> bool:
    """Started in the extension, so the extension can find it again in that folder."""
    return supported() and bool(session) and session.get("entrypoint") == "claude-vscode" and bool(session.get("cwd"))


def vscode_state(entry: dict | None, session: dict | None, closed: bool) -> str | None:
    """open: running in VS Code. closed: can be reopened there. None: no button."""
    if jumpable(entry):
        return "open"
    return "closed" if closed and reopenable(session) else None


def _key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def window_folders(ide_dir: Path) -> list[str]:
    """Folders of open windows. Multi-folder windows are left out: `Code.exe` cannot find them."""
    folders = []
    try:
        names = os.listdir(ide_dir)
    except OSError:
        return folders
    for name in names:
        if not name.endswith(".lock"):
            continue
        try:
            with open(Path(ide_dir) / name, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        found = data.get("workspaceFolders") if isinstance(data, dict) else None
        if isinstance(found, list) and len(found) == 1 and isinstance(found[0], str):
            folders.append(found[0])
    return folders


def owning_folder(cwd: str, folders: list[str]) -> str | None:
    """The window folder holding cwd: itself, or the closest parent."""
    target = _key(cwd)
    matches = [f for f in folders if target == _key(f) or target.startswith(_key(f).rstrip("\\") + "\\")]
    return max(matches, key=lambda f: len(_key(f)), default=None)


def title_shows(title: str, folder: str) -> bool:
    """Default VS Code title: `tab - folder - Visual Studio Code`. Any other title counts as no."""
    parts = title.split(" - ")
    if len(parts) < 2 or APP_NAME not in parts[-1]:
        return False
    name = ntpath.basename(folder.rstrip("\\/")).casefold()
    return any(part.casefold() == name for part in parts[:-1])


class WindowsDesktop:
    @staticmethod
    def _user32():
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.SwitchToThisWindow.restype = None
        return user32

    @staticmethod
    def _empty_mouse_event(user32) -> None:
        import ctypes
        from ctypes import wintypes

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("mouse", MouseInput)]  # type 0 is INPUT_MOUSE.

        event = Input()
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))

    @staticmethod
    def _title(user32, window) -> str:
        import ctypes

        buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(window, buffer, len(buffer))
        return buffer.value

    def switch_to(self, folder: str) -> bool:
        """False unless exactly one window matches: two folders may share a name."""
        import ctypes
        from ctypes import wintypes

        user32 = self._user32()
        found = []

        def visit(window, _):
            if user32.IsWindowVisible(window) and title_shows(self._title(user32, window), folder):
                found.append(window)
            return True

        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
        user32.EnumWindows(callback, 0)
        if len(found) != 1:
            return False
        if user32.IsIconic(found[0]):
            user32.ShowWindow(found[0], 9)  # SW_RESTORE
        self._empty_mouse_event(user32)
        if not user32.SetForegroundWindow(found[0]):
            user32.SwitchToThisWindow(found[0], True)
        return True

    def launch(self, folder: str) -> None:
        """Runs Code.exe directly: going through code.cmd would let cmd.exe read the path."""
        command = shutil.which("code")
        candidates = [Path(command).parent.parent / "Code.exe"] if command else []
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe")
        exe = next((path for path in candidates if path.is_file()), None)
        if exe is None:
            raise FileNotFoundError("Code.exe")
        env = {key: value for key, value in os.environ.items() if key != "ELECTRON_RUN_AS_NODE"}
        subprocess.Popen([str(exe), folder], env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_uri(self, uri: str) -> None:
        os.startfile(uri)

    def foreground_title(self) -> str:
        user32 = self._user32()
        return self._title(user32, user32.GetForegroundWindow())


def open_in_window(windows_dir: Path | None, folder: str, sid: str) -> bool:
    """Ask the companion extension of that window. Any failure means: use the link."""
    try:
        names = os.listdir(windows_dir) if windows_dir else []
    except OSError:
        return False
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(Path(windows_dir) / name, encoding="utf-8") as fh:
                data = json.load(fh)
            if len(data["folders"]) != 1 or _key(data["folders"][0]) != _key(folder):
                continue
            connection = http.client.HTTPConnection("127.0.0.1", int(data["port"]), timeout=COMPANION_TIMEOUT_SECONDS)
            try:
                connection.request("POST", "/open", json.dumps({"sid": sid}),
                                   {"Authorization": f"Bearer {data['token']}", "Content-Type": "application/json"})
                if connection.getresponse().status == 202:
                    return True
            finally:
                connection.close()
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            continue
    return False


def _comes_forward(desktop, folder: str, timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while not title_shows(desktop.foreground_title(), folder):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def _bring_forward(desktop, folder: str, timeout: float, switch_timeout: float, poll: float) -> str:
    """Which route worked: switch, code_exe, unconfirmed (Code.exe ran, no proof) or no_code_exe."""
    if desktop.switch_to(folder) and _comes_forward(desktop, folder, switch_timeout, poll):
        return "switch"
    try:
        desktop.launch(folder)
    except OSError:
        return "no_code_exe"
    return "code_exe" if _comes_forward(desktop, folder, timeout, poll) else "unconfirmed"


def go(sid: str, entry: dict | None, ide_dir: Path, desktop=None, windows_dir: Path | None = None, *,
       timeout: float = FOCUS_TIMEOUT_SECONDS, switch_timeout: float = SWITCH_TIMEOUT_SECONDS,
       poll: float = FOCUS_POLL_SECONDS, settle: float = SETTLE_SECONDS) -> dict:
    """`ok` means VS Code was called, so the browser should not take the focus back."""
    if not jumpable(entry) or not SESSION_ID.fullmatch(sid):
        return {"ok": False, "message": "Esta sesión no está abierta en la extensión de VS Code."}
    cwd = entry.get("cwd")
    folder = owning_folder(cwd, window_folders(ide_dir)) if isinstance(cwd, str) and cwd else None
    if folder is None:
        return {"ok": False, "message": "No encuentro su ventana de VS Code. Las ventanas con varias carpetas no se pueden localizar."}
    return _show_in_open_window(sid, folder, desktop or WindowsDesktop(), windows_dir, timeout, switch_timeout, poll, settle)


def _finish(trace: dict, started: float, ok: bool, message: str = "") -> dict:
    trace["ms"] = round((time.perf_counter() - started) * 1000)
    return {"ok": ok, "message": message, "trace": trace}


def _show_in_open_window(sid, folder, desktop, windows_dir, timeout, switch_timeout, poll, settle) -> dict:
    name = ntpath.basename(folder.rstrip("\\/"))
    started = time.perf_counter()
    trace = {"window": name}
    with _one_at_a_time:
        # The tab first: the window then appears already showing it.
        opened = open_in_window(windows_dir, folder, sid)
        trace["tab"] = "window" if opened else "link"
        trace["tab_ms"] = round((time.perf_counter() - started) * 1000)
        focus_timeout = min(timeout, OPENED_FOCUS_TIMEOUT_SECONDS) if opened else timeout
        focus = _bring_forward(desktop, folder, focus_timeout, switch_timeout, poll)
        trace["focus"] = focus
        if opened:
            return _finish(trace, started, True, "" if focus in ("switch", "code_exe") else
                           f"La pestaña de {name} está lista, pero Windows no ha traído la ventana al frente.")
        if focus == "no_code_exe":
            trace["tab"] = "none"
            return _finish(trace, started, False, "No encuentro Code.exe en este equipo.")
        if focus == "unconfirmed":
            trace["tab"] = "none"
            return _finish(trace, started, True, f"No he podido confirmar la ventana de {name}: abre su pestaña a mano. "
                                                 "Así evito duplicar la sesión.")
        time.sleep(settle)
        try:
            desktop.open_uri(SESSION_URI + sid)
        except OSError:
            trace["tab"] = "none"
            return _finish(trace, started, True, f"He abierto la ventana de {name}, pero no su pestaña: ábrela a mano.")
    return _finish(trace, started, True)


def reopen(sid: str, session: dict | None, ide_dir: Path, desktop=None, windows_dir: Path | None = None, *,
           timeout: float = FOCUS_TIMEOUT_SECONDS, switch_timeout: float = SWITCH_TIMEOUT_SECONDS,
           poll: float = FOCUS_POLL_SECONDS, settle: float = SETTLE_SECONDS,
           new_window_timeout: float = NEW_WINDOW_TIMEOUT_SECONDS, companion_grace: float = COMPANION_GRACE_SECONDS,
           new_window_poll: float = NEW_WINDOW_POLL_SECONDS) -> dict:
    """Open a closed session again. A new window can take seconds, so the lock waits for the link only."""
    if not reopenable(session) or not SESSION_ID.fullmatch(sid):
        return {"ok": False, "message": "Esta sesión no se puede reabrir en VS Code."}
    cwd = session["cwd"]
    name = ntpath.basename(cwd.rstrip("\\/"))
    if not os.path.isdir(cwd):
        return {"ok": False, "message": f"La carpeta de {name} ya no existe."}
    desktop = desktop or WindowsDesktop()
    # Exact folder only: the extension finds a session by the folder it started in.
    window = next((folder for folder in window_folders(ide_dir) if _key(folder) == _key(cwd)), None)
    if window:
        return _show_in_open_window(sid, window, desktop, windows_dir, timeout, switch_timeout, poll, settle)

    started = time.perf_counter()
    trace = {"window": name, "focus": "new_window", "tab": "none"}
    try:
        desktop.launch(cwd)
    except OSError:
        return _finish(trace, started, False, "No encuentro Code.exe en este equipo.")
    deadline = time.monotonic() + new_window_timeout
    shown_at = None
    while time.monotonic() < deadline:
        if open_in_window(windows_dir, cwd, sid):
            trace["tab"] = "window"
            desktop.switch_to(cwd)
            return _finish(trace, started, True)
        if shown_at is None and title_shows(desktop.foreground_title(), cwd):
            shown_at = time.monotonic()
        if shown_at is not None and time.monotonic() - shown_at >= companion_grace:
            break
        time.sleep(new_window_poll)
    with _one_at_a_time:
        if not title_shows(desktop.foreground_title(), cwd):
            return _finish(trace, started, True, f"He abierto la carpeta de {name}, pero no su pestaña: ábrela a mano.")
        time.sleep(settle)
        try:
            desktop.open_uri(SESSION_URI + sid)
        except OSError:
            return _finish(trace, started, True, f"He abierto la ventana de {name}, pero no su pestaña: ábrela a mano.")
    trace["tab"] = "link"
    return _finish(trace, started, True)
