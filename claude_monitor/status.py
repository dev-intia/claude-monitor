"""Pure decisions: session state and advice. No I/O, so every case is testable."""

from __future__ import annotations

from .registry import PERMISSION_WAITS

# State identifiers; the page translates them to Spanish.
PERMISSION = "espera_permiso"
YOUR_TURN = "te_toca"
AGENTS = "esperando_agentes"
WORKING = "trabajando"
STOPPED = "parada"

# Advice levels.
WAIT = "espera"
KEEP_GOING = "seguir"
WATCH = "vigilar"
CLEAN = "limpiar"
EXPIRED = "caducada"
UNKNOWN = "sin_datos"


def openness(session: dict, entry: dict | None, registry_verified: bool, hook: dict, now: float, cfg) -> str:
    """open | closed | recent | unknown. "open" is only said when verified."""
    activity = session.get("last_activity")
    if entry and entry.get("liveness") == "open":
        return "open"
    recent_cut = cfg.recent_activity_minutes * 60
    grace = cfg.unregistered_grace_minutes * 60
    age = None if activity is None else now - activity
    ended = hook.get("ended_ts")
    if ended is not None and (activity is None or ended >= activity):
        return "closed"
    if entry and entry.get("liveness") == "closed":
        return "closed"
    if registry_verified:
        # Registry works here and does not list it: closed, or replaced by /clear.
        return "recent" if age is not None and age < grace else "closed"
    return "recent" if age is not None and age < recent_cut else "unknown"


def derive_state(session: dict, hook: dict, entry: dict | None, subagents: int, open_state: str, now: float, cfg):
    """Return (state, wait_since, token, source).

    `token` identifies one concrete wait as told by a hook; None when it comes from
    history or the registry. Two different tokens mean two different waits.
    """
    last_activity = session.get("last_activity") or 0
    turn = session.get("turn_state")
    last_turn = session.get("last_turn")
    if open_state == "closed":
        return STOPPED, None, None, None

    live = entry if entry and entry.get("liveness") == "open" else None
    status = live.get("status") if live else None
    status_ts = (live.get("statusUpdatedAt") or 0) / 1000 if live else 0

    # A hook wait stands until something newer happens in the session.
    wait, wait_ts = hook.get("wait"), hook.get("wait_ts")
    if wait and last_activity <= wait_ts and status_ts <= wait_ts:
        if wait == "permission":
            return PERMISSION, wait_ts, wait_ts, "hook"
        if not subagents:
            return YOUR_TURN, last_turn or wait_ts, wait_ts, "hook"

    if status == "waiting" and live.get("waitingFor") in PERMISSION_WAITS:
        return PERMISSION, status_ts or now, None, "registro"

    # Checked before "busy": the registry stays busy while background agents run,
    # even though the turn ended. New activity after the Stop voids this.
    pending = hook.get("pending") or 0
    pending_ts = hook.get("pending_ts")
    pending_valid = (
        pending > 0
        and pending_ts is not None
        and last_activity <= pending_ts
        and now - pending_ts < cfg.subagent_stale_minutes * 60
    )
    if pending_valid:
        return AGENTS, None, None, "hook"
    if status == "busy":
        return WORKING, None, None, "registro"
    if subagents:
        return AGENTS, None, None, "historial"

    if status is None and turn in ("open", "tool"):
        if now - last_activity < cfg.working_stale_minutes * 60:
            return WORKING, None, None, "historial"

    if turn == "ended" and last_turn is not None and open_state in ("open", "recent"):
        if now - last_turn >= cfg.idle_seconds:
            return YOUR_TURN, last_turn, None, "historial"
    return STOPPED, None, None, None


def turn_age_seconds(session: dict, now: float) -> float | None:
    last = session.get("last_turn")
    return None if last is None else max(0.0, now - last)


def is_stopped(state: str) -> bool:
    return state in (STOPPED, YOUR_TURN)


def _fmt_k(tokens: int) -> str:
    return f"{round(tokens / 1000)}K"


def _fmt_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h".replace(".", ",")


def advise(context, state: str, subagents: int, age_seconds, cfg) -> tuple[str, str]:
    """Return (level, one-sentence reason in Spanish). First match wins."""
    if subagents or state == AGENTS:
        return WAIT, "Tiene subagentes activos: espera a que acaben antes de tocar nada."
    if context is None:
        return UNKNOWN, "Aún no se conoce el contexto; se sabrá en su próximo turno."
    ttl = cfg.cache_ttl_minutes * 60
    stopped = is_stopped(state)
    if stopped and age_seconds is not None and age_seconds > ttl and context > cfg.expired_context_over:
        return EXPIRED, (
            f"Lleva {_fmt_age(age_seconds)} sin turno: la caché ya no vale y rehacer {_fmt_k(context)} "
            f"sale peor que abrir una nueva (unos {_fmt_k(cfg.new_session_context)})."
        )
    if context >= cfg.clean_from:
        if stopped:
            return CLEAN, f"Está parada con {_fmt_k(context)}: cada turno nuevo arrastra todo ese contexto."
        return WATCH, f"Supera {_fmt_k(cfg.clean_from)}, pero sigue en marcha: se valorará cuando pare."
    if context >= cfg.watch_from:
        return WATCH, f"Contexto entre {_fmt_k(cfg.watch_from)} y {_fmt_k(cfg.clean_from)}: se acerca al punto de limpiar."
    return KEEP_GOING, f"Contexto por debajo de {_fmt_k(cfg.watch_from)}."
