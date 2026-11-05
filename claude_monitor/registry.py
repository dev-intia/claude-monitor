"""Which sessions are really open, from ~/.claude/sessions/<pid>.json.

Checked on real files (Windows): `pid` + `procStart` match the process creation
time exactly (FILETIME), which also protects against reused pids. `status` is
busy|idle|waiting and `waitingFor` says why. The format is undocumented, so any
doubt yields "unknown" and the panel falls back to labelling recent activity.

Only the whitelisted fields are copied. The *.key files next to them are never
opened: `<pid>.json` is the only name pattern read.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_NAME = re.compile(r"^\d+\.json$")
FIELDS = ("pid", "sessionId", "cwd", "entrypoint", "procStart", "pidDomain", "status", "waitingFor", "statusUpdatedAt")
STILL_ACTIVE = 259
PERMISSION_WAITS = {"permission prompt", "sandbox request", "worker request", "goal proposal", "input needed"}


def _windows_start(pid: int) -> int | None:
    """Creation time of a running process, else None. A dead one can still be opened
    and report its times while any program holds a handle to it: its exit code tells."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def check_process(entry: dict) -> str:
    """Return open | closed | unknown for one registry entry."""
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return "unknown"
    domain = str(entry.get("pidDomain") or "")
    if domain and not domain.startswith(sys.platform):
        return "unknown"  # Written from another OS context (WSL, container...).
    if sys.platform == "win32":
        real = _windows_start(pid)
        if real is None:
            return "closed"
        try:
            return "open" if int(entry.get("procStart")) == real else "closed"
        except (TypeError, ValueError):
            return "unknown"
    # Outside Windows only "dead" can be proven: procStart's format is unverified there.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "closed"
    except OSError:
        return "unknown"
    return "unknown"


class Registry:
    def __init__(self, sessions_dir: Path, checker=check_process):
        self.sessions_dir = Path(sessions_dir)
        self.checker = checker
        self.entries: dict[str, dict] = {}
        self.verified = False  # True when "absent from the registry" means closed.
        self.ever_open: set[str] = set()  # Seen open in this run.

    def refresh(self) -> None:
        entries: dict[str, dict] = {}
        verified = False
        try:
            names = os.listdir(self.sessions_dir)
        except OSError:
            names = []
        for name in names:
            if not _NAME.match(name):
                continue
            try:
                with open(self.sessions_dir / name, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("sessionId"), str):
                continue
            entry = {k: raw.get(k) for k in FIELDS}
            entry["liveness"] = self.checker(entry)
            if entry["liveness"] == "open":
                verified = True
            sid = entry["sessionId"]
            if sid not in entries or entry["liveness"] == "open":
                entries[sid] = entry
        # Seen open, now gone (killed, exited, replaced by /clear): closed right away.
        self.ever_open |= {sid for sid, e in entries.items() if e["liveness"] == "open"}
        for sid in self.ever_open:
            if sid not in entries:
                entries[sid] = {**dict.fromkeys(FIELDS), "liveness": "closed"}
        self.entries = entries
        self.verified = verified

    def open_sids(self) -> set[str]:
        return {s for s, e in self.entries.items() if e["liveness"] == "open"}
