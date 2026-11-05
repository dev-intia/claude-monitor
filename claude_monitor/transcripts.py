"""Incremental reader of Claude Code transcripts.

Only numbers, model names, timestamps and the project folder name are kept.
Conversation text is parsed in passing to classify a line and then discarded.

Facts checked against real files (see README):
* One API message spans several lines sharing `message.id`. Recent versions repeat
  identical usage on each line; older ones grow `output_tokens` between lines
  (16, 16, 273). Keeping the per-field maximum is right for both, whereas "first
  line" undercounts output and summing double counts.
* Subagents live in <sessionId>/subagents/agent-*.jsonl and carry the parent's id.
* `/clear` may stay in the same file (older versions) or open a new one.
* Compaction writes a `compact_boundary` system line whose metadata has `postTokens`.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

FIELDS = (
    "input",
    "cache_write_5m",
    "cache_write_1h",
    "cache_write_unspecified",
    "cache_read",
    "output",
)
SYNTHETIC_MODEL = "<synthetic>"
UNKNOWN_MODEL = "desconocido"
KEEP_DAYS = 8
_UUID_JSONL = re.compile(r"^[0-9a-fA-F-]{36}\.jsonl$")
_RELEVANT = (b'"assistant"', b'"user"', b"compact_boundary", b'"ai-title"', b'"custom-title"')
MAX_TITLE_CHARS = 200


def usage_vector(usage: dict) -> list[int]:
    """Flatten `message.usage` to one integer per entry of FIELDS."""
    written = int(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation") or {}
    five = int(split.get("ephemeral_5m_input_tokens") or 0)
    hour = int(split.get("ephemeral_1h_input_tokens") or 0)
    return [
        int(usage.get("input_tokens") or 0),
        five,
        hour,
        max(0, written - five - hour),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    ]


def context_of(vector: list[int]) -> int:
    """input + cache_creation + cache_read: everything sent on that turn."""
    return sum(vector[:5])


def parse_ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def project_from_cwd(cwd: str) -> str:
    return cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def new_session(sid: str, project_dir: str) -> dict:
    return {
        "sid": sid,
        # Fallback only: the encoded folder name is ambiguous, cwd is preferred.
        "project": project_dir.rstrip("-").rsplit("-", 1)[-1] or project_dir,
        "project_from_cwd": False,
        "cwd": None,  # Folder it started in, where the extension looks for it. Never sent to the page.
        "entrypoint": None,
        "last_activity": None,
        "last_turn": None,
        "model": None,
        "context": None,
        "context_source": None,  # turn | compact | clear
        "turn_state": None,  # open | tool | ended | cleared
        "ai_title": None,
        "custom_title": None,
        "days": {},
    }


class TranscriptStore:
    def __init__(self, projects_dir: Path, first_scan_days: float = 7):
        self.projects_dir = Path(projects_dir)
        self.first_scan_days = first_scan_days
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.bad_lines = 0

    # ---------------------------------------------------------------- persistence
    def dump(self) -> dict:
        return {"files": self.files, "sessions": self.sessions, "bad_lines": self.bad_lines}

    def restore(self, data: dict) -> None:
        self.files = data.get("files", {})
        self.sessions = data.get("sessions", {})
        self.bad_lines = data.get("bad_lines", 0)

    # ---------------------------------------------------------------- scanning
    def discover(self):
        """Yield (key, path, sid, project_dir, is_subagent) for each transcript."""
        try:
            projects = list(os.scandir(self.projects_dir))
        except OSError:
            return
        for project in projects:
            if not project.is_dir():
                continue
            try:
                entries = list(os.scandir(project.path))
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and _UUID_JSONL.match(entry.name):
                    yield (f"{project.name}/{entry.name}", entry.path, entry.name[:-6], project.name, False)
                elif entry.is_dir():
                    try:
                        agents = list(os.scandir(os.path.join(entry.path, "subagents")))
                    except OSError:
                        continue
                    for agent in agents:
                        name = agent.name
                        if name.startswith("agent-") and name.endswith(".jsonl"):
                            key = f"{project.name}/{entry.name}/subagents/{name}"
                            yield (key, agent.path, entry.name, project.name, True)

    def scan(self, now: float, always_read: frozenset | set = frozenset()) -> bool:
        """Read whatever is new. `always_read` holds sessions known to be open."""
        changed = False
        cutoff = now - self.first_scan_days * 86400
        for key, path, sid, project_dir, is_sub in self.discover():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            record = self.files.get(key)
            if record is None:
                if stat.st_mtime < cutoff and sid not in always_read:
                    continue
                record = {
                    "offset": 0,
                    "mtime": 0,
                    "sid": sid,
                    "dir": project_dir,
                    "sub": is_sub,
                    "seen": {},
                    "finished": False,
                    "meta_mtime": 0,
                }
                self.files[key] = record
                changed = True
            if is_sub:
                changed |= self._refresh_meta(record, path)
            if stat.st_size < record["offset"]:
                self._forget_file(record)
                changed = True
            record["mtime"] = stat.st_mtime
            if stat.st_size > record["offset"]:
                changed |= self._read_new(record, path)
        return changed

    def _refresh_meta(self, record: dict, path: str) -> bool:
        """Only `finished` is taken from .meta.json; its description is ignored."""
        meta = path[: -len(".jsonl")] + ".meta.json"
        try:
            mtime = os.stat(meta).st_mtime
            if mtime == record["meta_mtime"]:
                return False
            with open(meta, encoding="utf-8") as fh:
                finished = json.load(fh).get("finished") is True
        except (OSError, ValueError, AttributeError):
            return False
        record["meta_mtime"] = mtime
        if finished == record["finished"]:
            return False
        record["finished"] = finished
        return True

    def _forget_file(self, record: dict) -> None:
        """The file shrank (rewritten): take out what it contributed, re-read it."""
        session = self.sessions.get(record["sid"])
        if session:
            kind = "sub" if record["sub"] else "main"
            for vector, day, model in record["seen"].values():
                bucket = session["days"].get(day, {}).get(kind, {}).get(model)
                if bucket:
                    for i, value in enumerate(vector):
                        bucket[i] = max(0, bucket[i] - value)
        record["seen"] = {}
        record["offset"] = 0

    def _read_new(self, record: dict, path: str) -> bool:
        session = self.sessions.get(record["sid"])
        if session is None:
            session = new_session(record["sid"], record["dir"])
            self.sessions[record["sid"]] = session
        try:
            with open(path, "rb") as fh:
                fh.seek(record["offset"])
                # A line without a final newline is still being written: it stays
                # unread, and the offset does not pass it until it is complete.
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    record["offset"] += len(raw)
                    if any(marker in raw for marker in _RELEVANT):
                        self._ingest(raw, record, session)
        except OSError:
            pass
        return True

    # ---------------------------------------------------------------- line handling
    def _ingest(self, raw: bytes, record: dict, session: dict) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "assistant":
            self._assistant(event, record, session)
        elif record["sub"]:
            return
        elif kind == "user":
            self._user(event, session)
        elif kind == "system" and event.get("subtype") == "compact_boundary":
            self._compact(event, session)
        elif kind == "ai-title":
            self._title(event.get("aiTitle"), "ai_title", session)
        elif kind == "custom-title":
            self._title(event.get("customTitle"), "custom_title", session)

    def _assistant(self, event: dict, record: dict, session: dict) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return
        model = message.get("model")
        if model == SYNTHETIC_MODEL or event.get("isApiErrorMessage"):
            return  # Local placeholder: no tokens, not a real turn.
        ts = parse_ts(event.get("timestamp"))
        if ts is None:
            return
        vector = usage_vector(message.get("usage") or {})
        model = model if isinstance(model, str) and model else UNKNOWN_MODEL
        self._add_usage(record, session, message.get("id") or event.get("uuid"), vector, ts, model)
        if record["sub"]:
            return
        self._touch(session, event, ts)
        session["last_turn"] = ts
        session["model"] = model
        session["context"] = context_of(vector)
        session["context_source"] = "turn"
        stop = message.get("stop_reason")
        session["turn_state"] = {"tool_use": "tool", None: "open"}.get(stop, "ended")

    def _add_usage(self, record, session, message_id, vector, ts, model) -> None:
        kind = "sub" if record["sub"] else "main"
        seen = record["seen"].get(message_id)
        if seen is None:
            day = local_day(ts)
            delta = vector
            record["seen"][message_id] = [vector, day, model]
        else:
            previous, day, model = seen
            merged = [max(a, b) for a, b in zip(previous, vector)]
            delta = [m - p for m, p in zip(merged, previous)]
            seen[0] = merged
        bucket = (
            session["days"].setdefault(day, {}).setdefault(kind, {}).setdefault(model, [0] * len(FIELDS))
        )
        for i, value in enumerate(delta):
            bucket[i] += value

    def _user(self, event: dict, session: dict) -> None:
        if event.get("isMeta") or event.get("isCompactSummary"):
            return
        ts = parse_ts(event.get("timestamp"))
        if ts is None:
            return
        content = (event.get("message") or {}).get("content")
        text = content if isinstance(content, str) else ""
        is_tool_result = False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        is_tool_result = True
                    elif block.get("type") == "text" and not text:
                        text = block.get("text") or ""
        head = text[:200].lstrip()  # Inspected for markers only, never stored.
        self._touch(session, event, ts)
        if head.startswith("<command-name>/clear"):
            session["context"] = 0
            session["context_source"] = "clear"
            session["turn_state"] = "cleared"
            session["last_turn"] = None
        elif head.startswith(("<command-name>", "<command-message>", "<local-command")):
            pass  # A local slash command starts no model turn.
        elif not is_tool_result and head.startswith("[Request interrupted"):
            session["turn_state"] = "ended"
        else:
            session["turn_state"] = "open"

    def _compact(self, event: dict, session: dict) -> None:
        ts = parse_ts(event.get("timestamp"))
        if ts is None:
            return
        self._touch(session, event, ts)
        metadata = event.get("compactMetadata")
        post = metadata.get("postTokens") if isinstance(metadata, dict) else None
        # Without postTokens the size is unknown until the next turn: None, not stale.
        session["context"] = post if isinstance(post, int) else None
        session["context_source"] = "compact"

    @staticmethod
    def _title(value, field: str, session: dict) -> None:
        # Titles carry no timestamp and are not activity: the last one in the file wins.
        if isinstance(value, str) and value.strip():
            session[field] = value.strip()[:MAX_TITLE_CHARS]

    @staticmethod
    def _touch(session: dict, event: dict, ts: float) -> None:
        if session["last_activity"] is None or ts > session["last_activity"]:
            session["last_activity"] = ts
        cwd = event.get("cwd")
        if not session["project_from_cwd"] and isinstance(cwd, str) and cwd:
            session["project"] = project_from_cwd(cwd)
            session["project_from_cwd"] = True
            session["cwd"] = cwd
        entrypoint = event.get("entrypoint")
        if not session["entrypoint"] and isinstance(entrypoint, str):
            session["entrypoint"] = entrypoint

    # ---------------------------------------------------------------- queries
    def active_subagents_by_session(self, now: float, stale_seconds: float) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.files.values():
            if record["sub"] and not record["finished"] and now - record["mtime"] < stale_seconds:
                counts[record["sid"]] = counts.get(record["sid"], 0) + 1
        return counts

    def prune(self, now: float) -> None:
        """Drop per-day data and message ids too old to matter for any total."""
        oldest = local_day(now - KEEP_DAYS * 86400)
        for session in self.sessions.values():
            for day in [d for d in session["days"] if d < oldest]:
                del session["days"][day]
        for record in self.files.values():
            for key in [k for k, v in record["seen"].items() if v[1] < oldest]:
                del record["seen"][key]
