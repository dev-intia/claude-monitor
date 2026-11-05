"""Add or remove the monitor's hooks in a Claude Code settings file.

Other hooks and settings are preserved. Our entries are recognised by the hook
script's name. Nothing is written without the caller's confirmation.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .hookstore import OUR_SCRIPT

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hook" / OUR_SCRIPT
NOTIFICATION_MATCHER = "permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
EVENTS = {
    "Notification": NOTIFICATION_MATCHER,
    "Stop": None,
    "SubagentStop": None,
    "UserPromptSubmit": None,
    "SessionEnd": None,
}


class SettingsError(Exception):
    pass


def read_settings(path: Path) -> tuple[dict, str]:
    """Return (settings, original text). A missing file is an empty settings object."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SettingsError(f"{path} no es JSON válido: {error}") from error
    if not isinstance(data, dict):
        raise SettingsError(f"{path} no contiene un objeto JSON")
    return data, text


def render(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _is_ours(hook) -> bool:
    if not isinstance(hook, dict):
        return False
    args = hook.get("args") if isinstance(hook.get("args"), list) else []
    return OUR_SCRIPT in str(hook.get("command", "")) or any(OUR_SCRIPT in str(a) for a in args)


def without_ours(settings: dict) -> dict:
    """Remove only our hooks, and any container that holds nothing else as a result."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list) or not any(_is_ours(h) for h in inner):
                kept.append(group)
                continue
            remaining = [h for h in inner if not _is_ours(h)]
            if remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del result["hooks"]
    return result


def with_ours(settings: dict, python: str, script: str, state_dir: str, max_bytes: int) -> dict:
    result = without_ours(settings)
    hooks = result.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        group: dict = {}
        if matcher:
            group["matcher"] = matcher
        group["hooks"] = [{
            "type": "command",
            "command": python,
            "args": [script, "--state-dir", state_dir, "--max-bytes", str(max_bytes)],
            "async": True,
            "timeout": 10,
        }]
        hooks.setdefault(event, []).append(group)
    return result


def diff_text(before: str, after: str, path: Path) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{path} (actual)", tofile=f"{path} (propuesto)",
    ))


def backup(path: Path) -> Path | None:
    if not Path(path).exists():
        return None
    target = Path(f"{path}.bak-claude-monitor-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, target)
    return target


def write_atomic(path: Path, text: str) -> None:
    temporary = f"{path}.tmp-claude-monitor"
    with open(temporary, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(temporary, path)


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")


def _apply(path: Path, before_text: str, after: dict, yes: bool, dry_run: bool, action: str) -> int:
    after_text = render(after)
    if after_text == before_text or (not before_text and not after):
        print("No hay nada que cambiar.")
        return 0
    print(diff_text(before_text, after_text, path))
    if dry_run:
        print("Simulación: no se ha escrito nada.")
        return 0
    if not yes and not _confirm(f"¿{action} en {path}?"):
        print("Cancelado: no se ha escrito nada.")
        return 1
    saved = backup(path)
    if saved:
        print(f"Copia de seguridad: {saved}")
    write_atomic(path, after_text)
    print("Hecho.")
    return 0


def install(path: Path, state_dir: str, max_bytes: int, *, python: str | None = None,
            yes: bool = False, dry_run: bool = False) -> int:
    settings, text = read_settings(path)
    after = with_ours(settings, python or sys.executable, str(HOOK_SCRIPT), str(state_dir), max_bytes)
    return _apply(Path(path), text, after, yes, dry_run, "Instalar los hooks")


def uninstall(path: Path, *, yes: bool = False, dry_run: bool = False) -> int:
    settings, text = read_settings(path)
    return _apply(Path(path), text, without_ours(settings), yes, dry_run, "Desinstalar los hooks")
