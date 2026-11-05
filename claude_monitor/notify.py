"""Decides when to notify. Runs on the server, so it needs no browser to test.

Rules: one notice per new wait; one context notice per session and level, on
entering it; a level may fire again after the session drops below it. Context
notices wait until the session is stopped without subagents and never precede
"te toca": if both are due they merge into one. The first run is silent. Memory
survives reloads and restarts because it lives in a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .status import CLEAN, EXPIRED, PERMISSION, STOPPED, YOUR_TURN

WAIT_LABELS = {PERMISSION: "Espera permiso", YOUR_TURN: "Te toca"}
LEVEL_LABELS = {CLEAN: "conviene limpiar", EXPIRED: "caché caducada, empieza de nuevo"}
FORGET_AFTER_SECONDS = 30 * 86400


def format_tokens(tokens) -> str:
    return f"{round((tokens or 0) / 1000)}K"


def notice_text(project: str, wait: str | None, level: str | None, tokens) -> str:
    """Only project, level and tokens: never anything from the conversation."""
    parts = []
    if wait:
        parts.append(WAIT_LABELS[wait])
    parts.append(project)
    if level:
        parts.append(format_tokens(tokens))
        parts.append(LEVEL_LABELS[level])
    return " · ".join(parts)


def context_flags(context, age_seconds, cfg) -> dict[str, bool]:
    """Whether each context level holds now, regardless of what the session is doing."""
    ctx = context or 0
    return {
        CLEAN: ctx >= cfg.clean_from,
        EXPIRED: age_seconds is not None
        and age_seconds > cfg.cache_ttl_minutes * 60
        and ctx > cfg.expired_context_over,
    }


class NotificationEngine:
    def __init__(self, path: Path | None, keep: int = 100):
        self.path = Path(path) if path else None
        self.keep = keep
        self.first_run = True
        self.seq = 0
        self.sessions: dict[str, dict] = {}
        self.notices: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.first_run = False
        self.seq = data.get("seq", 0)
        self.sessions = data.get("sessions", {})
        self.notices = data.get("notices", [])

    def save(self) -> None:
        if not self.path:
            return
        data = {"seq": self.seq, "sessions": self.sessions, "notices": self.notices}
        temporary = str(self.path) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(temporary, self.path)

    def evaluate(self, views: list[dict], now: float, cfg) -> list[dict]:
        """Update memory from the current views and return the new notices.

        Each view needs: sid, project, state, token, context, age_seconds,
        subagents, closed, te_toca_due.
        """
        silent = self.first_run
        self.first_run = False
        emitted = []
        for view in views:
            notice = self._evaluate_one(view, now, cfg, silent)
            if notice:
                emitted.append(notice)
        if emitted:
            self.notices = (self.notices + emitted)[-self.keep :]
        for sid in [s for s, r in self.sessions.items() if now - r.get("seen", now) > FORGET_AFTER_SECONDS]:
            del self.sessions[sid]
        return emitted

    def _evaluate_one(self, view: dict, now: float, cfg, silent: bool) -> dict | None:
        record = self.sessions.setdefault(view["sid"], {"wait": None, "token": None, "levels": []})
        record["seen"] = now
        state = view["state"]

        # Waits: new when the kind changes, or when a hook reports a different one.
        wait_part = None
        if state in WAIT_LABELS:
            token = view.get("token")
            is_new = record["wait"] != state or (
                record["token"] is not None and token is not None and record["token"] != token
            )
            if is_new:
                wait_part = state
            record["wait"] = state
            record["token"] = token if token is not None else record["token"]
        else:
            record["wait"] = None
            record["token"] = None

        # Levels the session left may fire again.
        flags = context_flags(view.get("context"), view.get("age_seconds"), cfg)
        record["levels"] = [level for level in record["levels"] if flags.get(level)]

        eligible = (
            state in (STOPPED, YOUR_TURN)
            and not view.get("subagents")
            and not view.get("closed")
            and not view.get("te_toca_due")
        )
        level_part = None
        if eligible:
            due = [level for level in (EXPIRED, CLEAN) if flags[level] and level not in record["levels"]]
            if due:
                level_part = due[0]
                # A later, lesser level of the same stop is not news.
                record["levels"] = [level for level in (EXPIRED, CLEAN) if flags[level]]

        if silent:
            record["levels"] = [level for level in (EXPIRED, CLEAN) if flags[level]]
            return None
        if not wait_part and not level_part:
            return None
        self.seq += 1
        return {
            "seq": self.seq,
            "ts": now,
            "sid": view["sid"],
            "project": view["project"],
            "wait": wait_part,
            "level": level_part,
            "tokens": view.get("context") or 0,
            "text": notice_text(view["project"], wait_part, level_part, view.get("context")),
        }
