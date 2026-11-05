"""Load config.toml. The file is the single source of thresholds and weights."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.toml"


@dataclass(frozen=True)
class Config:
    port: int
    refresh_seconds: float
    claude_dir: Path
    state_dir: Path
    first_scan_days: float
    show_days: float
    weights: dict
    model_weights: dict
    watch_from: int
    clean_from: int
    expired_context_over: int
    new_session_context: int
    cache_ttl_minutes: float
    idle_seconds: float
    working_stale_minutes: float
    subagent_stale_minutes: float
    recent_activity_minutes: float
    unregistered_grace_minutes: float
    notice_max_age_minutes: float
    keep_notices: int
    max_event_file_bytes: int

    @property
    def projects_dir(self) -> Path:
        return self.claude_dir / "projects"

    @property
    def sessions_dir(self) -> Path:
        return self.claude_dir / "sessions"

    @property
    def settings_path(self) -> Path:
        return self.claude_dir / "settings.json"

    @property
    def ide_dir(self) -> Path:
        return self.claude_dir / "ide"

    @property
    def windows_dir(self) -> Path:
        return self.state_dir / "windows"

    @property
    def hook_events_path(self) -> Path:
        return self.state_dir / "hook-events.jsonl"


def load_config(path: Path | str | None = None, **overrides) -> Config:
    """Read the TOML file. A missing key is an error: there are no hidden defaults."""
    with open(path or DEFAULT_PATH, "rb") as fh:
        raw = tomllib.load(fh)
    values = dict(
        port=raw["server"]["port"],
        refresh_seconds=raw["server"]["refresh_seconds"],
        claude_dir=Path(raw["paths"]["claude_dir"]).expanduser(),
        state_dir=Path(raw["paths"]["state_dir"]).expanduser(),
        first_scan_days=raw["scan"]["first_scan_days"],
        show_days=raw["scan"]["show_days"],
        weights=raw["weights"],
        model_weights=raw["model_weights"],
        watch_from=raw["advice"]["watch_from"],
        clean_from=raw["advice"]["clean_from"],
        expired_context_over=raw["advice"]["expired_context_over"],
        new_session_context=raw["advice"]["new_session_context"],
        cache_ttl_minutes=raw["cache"]["ttl_minutes"],
        idle_seconds=raw["states"]["idle_seconds"],
        working_stale_minutes=raw["states"]["working_stale_minutes"],
        subagent_stale_minutes=raw["states"]["subagent_stale_minutes"],
        recent_activity_minutes=raw["states"]["recent_activity_minutes"],
        unregistered_grace_minutes=raw["states"]["unregistered_grace_minutes"],
        notice_max_age_minutes=raw["notifications"]["max_age_minutes"],
        keep_notices=raw["notifications"]["keep_notices"],
        max_event_file_bytes=raw["hooks"]["max_event_file_bytes"],
    )
    values.update(overrides)
    return Config(**values)
