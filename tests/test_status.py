import unittest

from claude_monitor import status as st
from claude_monitor.config import load_config
from claude_monitor.hookstore import apply_event, empty_signal

from .helpers import NOW

CFG = load_config()
MIN = 60


def session(turn="ended", last_turn=None, activity=None, context=100000):
    last_turn = NOW - 300 if last_turn is None else last_turn
    return {"sid": "s", "project": "p", "turn_state": turn, "last_turn": last_turn,
            "last_activity": activity if activity is not None else last_turn, "context": context}


def open_entry(status="idle", waiting=None, updated=None):
    return {"liveness": "open", "status": status, "waitingFor": waiting,
            "statusUpdatedAt": (updated if updated is not None else NOW - 400) * 1000}


def signal(*events):
    result = empty_signal()
    for name, ts, extra in events:
        apply_event(result, {"event": name, "ts": ts, **extra})
    return result


def derive(sess=None, hook=None, entry=None, subagents=0, open_state="open"):
    return st.derive_state(sess or session(), hook or empty_signal(), entry, subagents, open_state, NOW, CFG)


class Advice(unittest.TestCase):
    def adv(self, context, state=st.STOPPED, subagents=0, age=300):
        return st.advise(context, state, subagents, age, CFG)[0]

    def test_keep_going_below_150k(self):
        self.assertEqual(self.adv(149_999), st.KEEP_GOING)

    def test_watch_between_thresholds(self):
        self.assertEqual(self.adv(150_000), st.WATCH)
        self.assertEqual(self.adv(199_999), st.WATCH)

    def test_clean_from_200k_when_stopped(self):
        self.assertEqual(self.adv(200_000), st.CLEAN)
        self.assertEqual(self.adv(310_000, st.YOUR_TURN), st.CLEAN)

    def test_never_clean_while_working(self):
        self.assertEqual(self.adv(310_000, st.WORKING), st.WATCH)
        self.assertEqual(self.adv(310_000, st.PERMISSION), st.WATCH)

    def test_wait_has_priority_over_everything(self):
        self.assertEqual(self.adv(310_000, st.STOPPED, subagents=2), st.WAIT)
        self.assertEqual(self.adv(310_000, st.AGENTS), st.WAIT)
        self.assertEqual(self.adv(310_000, st.STOPPED, subagents=1, age=4 * 3600), st.WAIT)

    def test_expired_cache(self):
        self.assertEqual(self.adv(60_001, age=61 * MIN), st.EXPIRED)
        self.assertEqual(self.adv(310_000, age=61 * MIN), st.EXPIRED)

    def test_expired_needs_both_time_and_size(self):
        self.assertEqual(self.adv(60_000, age=61 * MIN), st.KEEP_GOING)
        self.assertEqual(self.adv(100_000, age=59 * MIN), st.KEEP_GOING)

    def test_expired_not_while_working(self):
        self.assertEqual(self.adv(100_000, st.WORKING, age=61 * MIN), st.KEEP_GOING)

    def test_unknown_context_is_not_presented_as_fine(self):
        self.assertEqual(self.adv(None), st.UNKNOWN)

    def test_reason_is_one_sentence_for_every_level(self):
        for args in ((310_000, st.STOPPED, 2, 300), (100_000, st.STOPPED, 0, 300), (160_000, st.STOPPED, 0, 300),
                     (310_000, st.STOPPED, 0, 300), (310_000, st.STOPPED, 0, 7200), (None, st.STOPPED, 0, 300)):
            reason = st.advise(*args, CFG)[1]
            self.assertTrue(reason.endswith("."), reason)
            self.assertEqual(reason.count(". "), 0, reason)


class Openness(unittest.TestCase):
    def test_verified_open(self):
        self.assertEqual(st.openness(session(), {"liveness": "open"}, True, empty_signal(), NOW, CFG), "open")

    def test_absent_from_working_registry_is_closed(self):
        old = session(last_turn=NOW - 3600)
        self.assertEqual(st.openness(old, None, True, empty_signal(), NOW, CFG), "closed")

    def test_process_gone_is_closed_at_once_despite_recent_activity(self):
        fresh = session(last_turn=NOW - 10)
        self.assertEqual(st.openness(fresh, {"liveness": "closed"}, True, empty_signal(), NOW, CFG), "closed")

    def test_recent_write_is_not_declared_closed(self):
        fresh = session(last_turn=NOW - 60)
        self.assertEqual(st.openness(fresh, None, True, empty_signal(), NOW, CFG), "recent")

    def test_without_registry_only_recent_activity_is_claimed(self):
        self.assertEqual(st.openness(session(last_turn=NOW - 60), None, False, empty_signal(), NOW, CFG), "recent")
        self.assertEqual(st.openness(session(last_turn=NOW - 7200), None, False, empty_signal(), NOW, CFG), "unknown")

    def test_session_end_hook_closes(self):
        hook = signal(("SessionEnd", NOW - 10, {}))
        self.assertEqual(st.openness(session(last_turn=NOW - 60), None, False, hook, NOW, CFG), "closed")


class States(unittest.TestCase):
    def test_permission_prompt_hook(self):
        hook = signal(("Notification", NOW - 10, {"notification_type": "permission_prompt"}))
        state, since, token, source = derive(session(turn="tool"), hook, open_entry("waiting"))
        self.assertEqual((state, since, source), (st.PERMISSION, NOW - 10, "hook"))
        self.assertEqual(token, NOW - 10)

    def test_idle_prompt_hook(self):
        hook = signal(("Notification", NOW - 10, {"notification_type": "idle_prompt"}))
        state, since, _, _ = derive(session(last_turn=NOW - 70), hook, open_entry())
        self.assertEqual((state, since), (st.YOUR_TURN, NOW - 70))

    def test_elicitation_counts_as_blocked(self):
        hook = signal(("Notification", NOW - 10, {"notification_type": "elicitation_dialog"}))
        self.assertEqual(derive(session(turn="tool"), hook, open_entry())[0], st.PERMISSION)

    def test_agent_completed_adds_nothing(self):
        hook = signal(("Notification", NOW - 10, {"notification_type": "agent_completed"}))
        self.assertIsNone(hook["wait"])

    def test_stop_with_pending_waits_for_agents_without_notice_state(self):
        hook = signal(("Stop", NOW - 10, {"pending": 2}))
        self.assertEqual(derive(session(last_turn=NOW - 11), hook, open_entry())[0], st.AGENTS)

    def test_stop_without_pending_is_done_and_turns_into_te_toca_later(self):
        hook = signal(("Stop", NOW - 10, {"pending": 0}))
        self.assertEqual(derive(session(last_turn=NOW - 11), hook, open_entry())[0], st.STOPPED)
        hook = signal(("Stop", NOW - 90, {"pending": 0}))
        self.assertEqual(derive(session(last_turn=NOW - 91), hook, open_entry())[0], st.YOUR_TURN)

    def test_stop_with_pending_beats_a_busy_registry(self):
        hook = signal(("Stop", NOW - 10, {"pending": 1}))
        entry = open_entry("busy", updated=NOW - 20)
        self.assertEqual(derive(session(last_turn=NOW - 11), hook, entry)[0], st.AGENTS)

    def test_new_turn_after_stop_with_pending_is_working(self):
        hook = signal(("Stop", NOW - 30, {"pending": 1}))
        fresh = session(turn="open", last_turn=NOW - 31, activity=NOW - 5)
        self.assertEqual(derive(fresh, hook, open_entry("busy", updated=NOW - 6))[0], st.WORKING)

    def test_subagent_stop_discounts_the_agent_that_is_ending(self):
        hook = signal(("Stop", NOW - 100, {"pending": 2}), ("SubagentStop", NOW - 50, {"pending": 2}))
        self.assertEqual(hook["pending"], 1)
        hook = signal(("Stop", NOW - 100, {"pending": 1}), ("SubagentStop", NOW - 10, {"pending": 1}))
        self.assertEqual(hook["pending"], 0)
        self.assertNotEqual(derive(session(last_turn=NOW - 101), hook, open_entry())[0], st.AGENTS)

    def test_new_prompt_clears_the_wait(self):
        hook = signal(("Notification", NOW - 30, {"notification_type": "idle_prompt"}),
                      ("UserPromptSubmit", NOW - 5, {}))
        self.assertIsNone(hook["wait"])

    def test_newer_transcript_activity_clears_the_wait(self):
        hook = signal(("Notification", NOW - 30, {"notification_type": "permission_prompt"}))
        fresh = session(turn="open", last_turn=NOW - 40, activity=NOW - 5)
        self.assertEqual(derive(fresh, hook)[0], st.WORKING)

    def test_registry_busy_after_the_prompt_clears_the_wait(self):
        hook = signal(("Notification", NOW - 30, {"notification_type": "permission_prompt"}))
        self.assertEqual(derive(session(turn="tool", last_turn=NOW - 40), hook, open_entry("busy", updated=NOW - 5))[0], st.WORKING)

    def test_without_hooks_registry_reports_permission(self):
        entry = open_entry("waiting", "permission prompt", NOW - 20)
        state, since, token, source = derive(session(turn="tool"), None, entry)
        self.assertEqual((state, since, token, source), (st.PERMISSION, NOW - 20, None, "registro"))

    def test_without_hooks_or_registry_history_gives_te_toca(self):
        state, _, _, source = derive(session(last_turn=NOW - 120), None, None, open_state="recent")
        self.assertEqual((state, source), (st.YOUR_TURN, "historial"))

    def test_closed_session_never_waits(self):
        hook = signal(("Notification", NOW - 30, {"notification_type": "idle_prompt"}))
        self.assertEqual(derive(session(), hook, None, open_state="closed")[0], st.STOPPED)

    def test_stale_open_turn_is_no_longer_working(self):
        self.assertEqual(derive(session(turn="tool", last_turn=NOW - 3600), None, None, open_state="recent")[0], st.STOPPED)

    def test_subagents_keep_session_waiting(self):
        self.assertEqual(derive(session(), None, open_entry(), subagents=1)[0], st.AGENTS)

    def test_cleared_session_does_not_say_te_toca(self):
        cleared = session(turn="cleared", last_turn=NOW - 300)
        cleared["last_turn"] = None
        self.assertEqual(derive(cleared, None, open_entry())[0], st.STOPPED)


if __name__ == "__main__":
    unittest.main()
