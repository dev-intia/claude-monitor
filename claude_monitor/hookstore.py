"""Reduce the hook's event file to the latest signal per session."""

from __future__ import annotations

import json
import os
from pathlib import Path

# Each of these blocks the session until the user answers. `agent_completed` and
# the other notification types add nothing beyond Stop and idle_prompt.
PERMISSION_TYPES = {
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
OUR_SCRIPT = "claude_monitor_hook.py"


def empty_signal() -> dict:
    return {
        "wait": None,  # permission | your_turn
        "wait_ts": None,
        "pending": 0,
        "pending_ts": None,
        "stop_ts": None,
        "prompt_ts": None,
        "ended_ts": None,
    }


def apply_event(signal: dict, event: dict) -> None:
    name = event.get("event")
    ts = event.get("ts")
    if not isinstance(ts, (int, float)):
        return
    if name == "Notification":
        kind = event.get("notification_type")
        if kind in PERMISSION_TYPES:
            signal["wait"], signal["wait_ts"] = "permission", ts
        elif kind == "idle_prompt":
            signal["wait"], signal["wait_ts"] = "your_turn", ts
    elif name == "UserPromptSubmit":
        signal["wait"], signal["wait_ts"] = None, None
        signal["prompt_ts"] = ts
        signal["ended_ts"] = None
    elif name == "Stop":
        signal["wait"], signal["wait_ts"] = None, None
        signal["stop_ts"] = ts
        signal["pending"] = event.get("pending") or 0
        signal["pending_ts"] = ts
    elif name == "SubagentStop":
        if isinstance(event.get("pending"), int):
            # Seen on real sessions: the list still includes the agent that is ending.
            signal["pending"] = max(0, event["pending"] - 1)
            signal["pending_ts"] = ts
    elif name == "SessionEnd":
        signal["wait"], signal["wait_ts"] = None, None
        signal["pending"] = 0
        signal["ended_ts"] = ts


class HookStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self.signals: dict[str, dict] = {}
        self.last_event_ts: float | None = None
        self._read_file(self._rotated, 0)

    @property
    def _rotated(self) -> Path:
        return Path(str(self.path) + ".1")

    def _read_file(self, path: Path, offset: int) -> int:
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    offset += len(raw)
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("session_id"), str):
                        continue
                    apply_event(self.signals.setdefault(event["session_id"], empty_signal()), event)
                    if isinstance(event.get("ts"), (int, float)):
                        self.last_event_ts = event["ts"]
        except OSError:
            pass
        return offset

    def refresh(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        if size < self.offset:  # The hook rotated the file: replaying is harmless.
            self.offset = 0
            self._read_file(self._rotated, 0)
        if size > self.offset:
            self.offset = self._read_file(self.path, self.offset)

    def get(self, sid: str) -> dict:
        return self.signals.get(sid) or empty_signal()


def hooks_installed(settings_path: Path) -> bool:
    """Look only at the `hooks` key of settings.json for our script."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            hooks = json.load(fh).get("hooks")
    except (OSError, ValueError, AttributeError):
        return False
    return OUR_SCRIPT in json.dumps(hooks) if hooks else False
