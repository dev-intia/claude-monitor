"""Build the API response. Every field is listed by hand: nothing else can leak."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import status as st
from .jump import supported, vscode_state
from .transcripts import FIELDS


def units_of(vector: list[int], model: str, cfg) -> float | None:
    """Weighted units for one model; None when the model has no configured weight."""
    weight = cfg.model_weights.get(model)
    if weight is None:
        return None
    return weight * sum(count * cfg.weights[field] for field, count in zip(FIELDS, vector))


class _Tally:
    """Units per name, plus raw tokens of models without a weight."""

    def __init__(self):
        self.units = 0.0
        self.unweighted_tokens = 0

    def add(self, vector: list[int], model: str, cfg) -> None:
        value = units_of(vector, model, cfg)
        if value is None:
            self.unweighted_tokens += sum(vector)
        else:
            self.units += value


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _session_spend(session: dict, day: str, cfg) -> dict:
    spend = {"main": _Tally(), "sub": _Tally()}
    for kind, models in session["days"].get(day, {}).items():
        for model, vector in models.items():
            spend[kind].add(vector, model, cfg)
    return spend


def build_view(session: dict, store_sub: int, hook: dict, entry, verified: bool, now: float, cfg) -> dict:
    """Decisions for one session (also fed to the notification engine)."""
    open_state = st.openness(session, entry, verified, hook, now, cfg)
    closed = open_state == "closed"
    subagents = 0 if closed else store_sub
    state, since, token, source = st.derive_state(session, hook, entry, subagents, open_state, now, cfg)
    age = st.turn_age_seconds(session, now)
    context = session.get("context")
    level, reason = st.advise(context, state, subagents, age, cfg)
    # The turn just ended and "te toca" is on its way: context notices wait for it.
    te_toca_due = (
        state == st.STOPPED
        and session.get("turn_state") == "ended"
        and open_state in ("open", "recent")
        and age is not None
        and age < cfg.idle_seconds
    )
    return {
        "sid": session["sid"],
        "project": session["project"],
        "state": state,
        "state_source": source,
        "wait_since": since,
        "token": token,
        "open_state": open_state,
        "closed": closed,
        "subagents": subagents,
        "context": context,
        "context_source": session.get("context_source"),
        "age_seconds": age,
        "advice": level,
        "reason": reason,
        "te_toca_due": te_toca_due,
    }


def needs_attention(row: dict) -> bool:
    if row["state"] in (st.PERMISSION, st.YOUR_TURN):
        return True
    return not row["closed"] and row["advice"] in (st.CLEAN, st.EXPIRED)


def build_snapshot(store, registry, hooks, engine, cfg, now: float, *, hooks_installed: bool,
                   scan_info: dict | None = None, views: list[dict] | None = None) -> dict:
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    week_days = {(datetime.fromtimestamp(now) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
    if views is None:
        views = compute_views(store, registry, hooks, cfg, now)
    by_sid = {view["sid"]: view for view in views}

    rows = []
    for sid, view in by_sid.items():
        session = store.sessions.get(sid)
        spend = _session_spend(session, today, cfg)
        today_main, today_sub = spend["main"].units, spend["sub"].units
        age = view["age_seconds"]
        ttl = cfg.cache_ttl_minutes * 60
        rows.append({
            "sid": sid,
            "short_id": sid[:8],
            "project": view["project"],
            "state": view["state"],
            "state_source": view["state_source"],
            "open_state": view["open_state"],
            "closed": view["closed"],
            "vscode": vscode_state(registry.entries.get(sid), session, view["closed"]),
            "model": session.get("model"),
            "title": session.get("custom_title") or session.get("ai_title"),
            "model_known": session.get("model") in cfg.model_weights,
            "subagents": view["subagents"],
            "waiting_seconds": (now - view["wait_since"]) if view["wait_since"] else None,
            "last_activity_seconds": (now - session["last_activity"]) if session.get("last_activity") else None,
            "context": view["context"],
            "context_source": view["context_source"],
            "cache_seconds_left": None if age is None else ttl - age,
            "today_main": today_main,
            "today_sub": today_sub,
            "today_unweighted_tokens": spend["main"].unweighted_tokens + spend["sub"].unweighted_tokens,
            "advice": view["advice"],
            "reason": view["reason"],
        })
    rows.sort(key=lambda row: float("inf") if row["last_activity_seconds"] is None else row["last_activity_seconds"])
    for row in rows:
        row["attention"] = needs_attention(row)

    # Totals include every stored session, also those no longer in the table.
    projects: dict[str, dict] = {}
    models: dict[str, dict] = {}
    total_week = 0.0
    total_all_today = 0.0
    for session in store.sessions.values():
        for day, kinds in session["days"].items():
            if day not in week_days:
                continue
            is_today = day == today
            for models_in_kind in kinds.values():
                for model, vector in models_in_kind.items():
                    value = units_of(vector, model, cfg)
                    project = projects.setdefault(session["project"], {"today": 0.0, "week": 0.0})
                    entry = models.setdefault(model, {"today": 0.0, "week": 0.0, "tokens_today": 0, "tokens_week": 0})
                    tokens = sum(vector)
                    entry["tokens_week"] += tokens
                    if is_today:
                        entry["tokens_today"] += tokens
                    if value is None:
                        continue
                    total_week += value
                    project["week"] += value
                    entry["week"] += value
                    if is_today:
                        total_all_today += value
                        project["today"] += value
                        entry["today"] += value

    for row in rows:
        row["today_pct"] = _pct(row["today_main"] + row["today_sub"], total_all_today)

    project_rows = [
        {"name": name, "today": v["today"], "week": v["week"],
         "today_pct": _pct(v["today"], total_all_today), "week_pct": _pct(v["week"], total_week)}
        for name, v in projects.items()
    ]
    project_rows.sort(key=lambda r: (-r["today"], -r["week"]))
    model_rows = [
        {"name": name, "known": name in cfg.model_weights, "today": v["today"], "week": v["week"],
         "today_pct": _pct(v["today"], total_all_today), "week_pct": _pct(v["week"], total_week),
         "tokens_today": v["tokens_today"], "tokens_week": v["tokens_week"]}
        for name, v in models.items()
    ]
    model_rows.sort(key=lambda r: (-r["today"], -r["week"], -r["tokens_week"]))

    open_count = sum(1 for r in rows if r["open_state"] == "open")
    recent_count = sum(1 for r in rows if r["open_state"] == "recent")
    cutoff = now - cfg.notice_max_age_minutes * 60
    return {
        "now": now,
        "scan": scan_info or {},
        "rows": rows,
        "summary": {
            "active": open_count + recent_count,
            "open": open_count,
            "recent": recent_count,
            "waiting": sum(1 for r in rows if r["state"] in (st.PERMISSION, st.YOUR_TURN)),
            "attention": sum(1 for r in rows if r["attention"]),
            "today_units": total_all_today,
            "week_units": total_week,
            "projects": project_rows,
            "models": model_rows,
            "unknown_models": sorted(m["name"] for m in model_rows if not m["known"]),
        },
        "signals": {
            "liveness_verified": registry.verified,
            "jump_supported": supported(),
            "hooks_installed": hooks_installed,
            "last_hook_event_seconds": (now - hooks.last_event_ts) if hooks.last_event_ts else None,
        },
        "limits": {
            "watch_from": cfg.watch_from,
            "clean_from": cfg.clean_from,
            "cache_ttl_minutes": cfg.cache_ttl_minutes,
        },
        "notices": [
            {k: n[k] for k in ("seq", "sid", "project", "wait", "level", "tokens", "text")}
            for n in engine.notices if n["ts"] >= cutoff
        ],
        "notice_seq": engine.seq,
    }


def compute_views(store, registry, hooks, cfg, now: float) -> list[dict]:
    """Views for the sessions on show: recently active, or still open."""
    stale = cfg.subagent_stale_minutes * 60
    subagents = store.active_subagents_by_session(now, stale)
    show_cut = now - cfg.show_days * 86400
    open_sids = registry.open_sids()
    views = []
    for sid, session in store.sessions.items():
        activity = session.get("last_activity")
        if sid not in open_sids and sid not in subagents and (activity is None or activity < show_cut):
            continue
        views.append(build_view(session, subagents.get(sid, 0), hooks.get(sid), registry.entries.get(sid),
                                registry.verified, now, cfg))
    return views
