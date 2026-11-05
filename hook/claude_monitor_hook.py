#!/usr/bin/env python3
"""Claude Code hook: records that an event happened, and nothing else.

Keeps only session_id, event, notification_type, pending task count and time.
Prompts, messages, titles and tool input are never written. Always exits 0 and
never prints to stdout. Works whether or not the panel is running.
"""

import json
import os
import re
import sys
import time

# Long-lived helpers a session is not waiting on: a dev server would otherwise
# keep it "esperando agentes" for ever.
IGNORED_TASK_TYPES = {"shell", "monitor"}
FINISHED = {"completed", "failed", "killed", "stopped", "cancelled", "canceled", "error", "done"}
_SAFE = re.compile(r"^[\w.:-]{1,100}$")


def count_pending(tasks) -> int | None:
    if not isinstance(tasks, list):
        return None
    pending = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("type") in IGNORED_TASK_TYPES:
            continue
        if str(task.get("status", "")).lower() in FINISHED:
            continue
        pending += 1
    return pending


def build_record(payload: dict, now: float) -> dict | None:
    """Whitelist the fields that may reach disk."""
    session = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not (isinstance(session, str) and _SAFE.match(session)):
        return None
    if not (isinstance(event, str) and _SAFE.match(event)):
        return None
    kind = payload.get("notification_type")
    return {
        "session_id": session,
        "event": event,
        "notification_type": kind if isinstance(kind, str) and _SAFE.match(kind) else None,
        "pending": count_pending(payload.get("background_tasks")),
        "ts": round(now, 3),
    }


def parse_args(argv) -> tuple[str, int]:
    state_dir = os.environ.get("CLAUDE_MONITOR_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude-monitor"
    )
    max_bytes = 1048576
    for flag, value in zip(argv, argv[1:]):
        if flag == "--state-dir":
            state_dir = value
        elif flag == "--max-bytes":
            max_bytes = int(value)
    return state_dir, max_bytes


def append(record: dict, state_dir: str, max_bytes: int) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "hook-events.jsonl")
    try:
        if os.path.getsize(path) >= max_bytes:
            os.replace(path, path + ".1")  # Bounded: at most two files of max_bytes.
    except OSError:
        pass
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def main() -> None:
    state_dir, max_bytes = parse_args(sys.argv[1:])
    payload = json.loads(sys.stdin.buffer.read())
    record = build_record(payload, time.time())
    if record:
        append(record, state_dir, max_bytes)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # Whatever happens, the session must not notice.
        pass
    os._exit(0)
