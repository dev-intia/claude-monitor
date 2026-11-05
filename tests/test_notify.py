import unittest

from claude_monitor import status as st
from claude_monitor.config import load_config
from claude_monitor.notify import NotificationEngine, notice_text

from .helpers import NOW, TempDirTest

CFG = load_config()


def view(state=st.STOPPED, context=100_000, age=300, subagents=0, sid="s1", project="demo",
         token=None, closed=False, te_toca_due=False):
    return {"sid": sid, "project": project, "state": state, "token": token, "context": context,
            "age_seconds": age, "subagents": subagents, "closed": closed, "te_toca_due": te_toca_due}


def engine_after_first_run():
    engine = NotificationEngine(None)
    engine.evaluate([], NOW, CFG)
    return engine


class FirstRun(unittest.TestCase):
    def test_silent_for_what_was_already_there(self):
        engine = NotificationEngine(None)
        emitted = engine.evaluate([view(st.YOUR_TURN, 310_000), view(st.PERMISSION, sid="s2"),
                                   view(st.STOPPED, 310_000, age=7200, sid="s3")], NOW, CFG)
        self.assertEqual(emitted, [])
        self.assertEqual(engine.evaluate([view(st.YOUR_TURN, 310_000), view(st.PERMISSION, sid="s2"),
                                          view(st.STOPPED, 310_000, age=7200, sid="s3")], NOW + 3, CFG), [])

    def test_notices_after_a_later_change(self):
        engine = NotificationEngine(None)
        engine.evaluate([view(st.YOUR_TURN, 310_000)], NOW, CFG)
        engine.evaluate([view(st.WORKING, 310_000, age=1)], NOW + 3, CFG)
        emitted = engine.evaluate([view(st.YOUR_TURN, 310_000, age=70)], NOW + 6, CFG)
        self.assertEqual([n["wait"] for n in emitted], [st.YOUR_TURN])
        self.assertIsNone(emitted[0]["level"], "the level was already known at first run")

    def test_a_new_session_after_first_run_does_notify(self):
        engine = engine_after_first_run()
        self.assertEqual(len(engine.evaluate([view(st.PERMISSION, sid="nueva")], NOW, CFG)), 1)


class Waits(unittest.TestCase):
    def test_one_notice_per_wait_without_repeats(self):
        engine = engine_after_first_run()
        self.assertEqual(len(engine.evaluate([view(st.PERMISSION, token=10)], NOW, CFG)), 1)
        for i in range(1, 5):
            self.assertEqual(engine.evaluate([view(st.PERMISSION, token=10)], NOW + i, CFG), [])

    def test_a_new_wait_notifies_again(self):
        engine = engine_after_first_run()
        engine.evaluate([view(st.YOUR_TURN)], NOW, CFG)
        engine.evaluate([view(st.WORKING)], NOW + 1, CFG)
        self.assertEqual(len(engine.evaluate([view(st.YOUR_TURN)], NOW + 2, CFG)), 1)

    def test_two_prompts_told_apart_by_hook_time(self):
        engine = engine_after_first_run()
        engine.evaluate([view(st.PERMISSION, token=10)], NOW, CFG)
        self.assertEqual(len(engine.evaluate([view(st.PERMISSION, token=20)], NOW + 1, CFG)), 1)

    def test_history_then_hook_is_the_same_wait(self):
        engine = engine_after_first_run()
        engine.evaluate([view(st.YOUR_TURN, token=None)], NOW, CFG)
        self.assertEqual(engine.evaluate([view(st.YOUR_TURN, token=99)], NOW + 1, CFG), [])

    def test_waiting_for_agents_has_no_notice(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.AGENTS, subagents=1)], NOW, CFG), [])


class ContextLevels(unittest.TestCase):
    def test_one_notice_per_level(self):
        engine = engine_after_first_run()
        first = engine.evaluate([view(st.STOPPED, 250_000)], NOW, CFG)
        self.assertEqual([n["level"] for n in first], [st.CLEAN])
        self.assertEqual(engine.evaluate([view(st.STOPPED, 260_000)], NOW + 1, CFG), [])

    def test_watch_never_notifies(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.STOPPED, 170_000)], NOW, CFG), [])

    def test_held_while_subagents_are_active_then_fires_when_stopped(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.AGENTS, 250_000, subagents=2)], NOW, CFG), [])
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, subagents=1)], NOW + 1, CFG), [])
        self.assertEqual(len(engine.evaluate([view(st.STOPPED, 250_000)], NOW + 2, CFG)), 1)

    def test_held_while_working_then_fires_when_stopped(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.WORKING, 250_000, age=1)], NOW, CFG), [])
        self.assertEqual(len(engine.evaluate([view(st.STOPPED, 250_000, age=70)], NOW + 1, CFG)), 1)

    def test_fires_again_after_dropping_below(self):
        engine = engine_after_first_run()
        engine.evaluate([view(st.STOPPED, 250_000)], NOW, CFG)
        self.assertEqual(engine.evaluate([view(st.STOPPED, 15_000)], NOW + 1, CFG), [])
        self.assertEqual(len(engine.evaluate([view(st.STOPPED, 250_000)], NOW + 2, CFG)), 1)

    def test_drop_seen_while_working_also_rearms(self):
        engine = engine_after_first_run()
        engine.evaluate([view(st.STOPPED, 250_000)], NOW, CFG)
        engine.evaluate([view(st.WORKING, 15_000, age=1)], NOW + 1, CFG)
        engine.evaluate([view(st.WORKING, 220_000, age=1)], NOW + 2, CFG)
        self.assertEqual(len(engine.evaluate([view(st.STOPPED, 220_000)], NOW + 3, CFG)), 1)

    def test_clean_then_expired_are_two_levels(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, age=300)], NOW, CFG)[0]["level"], st.CLEAN)
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, age=3700)], NOW + 1, CFG)[0]["level"], st.EXPIRED)

    def test_expired_first_does_not_also_announce_clean(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, age=3700)], NOW, CFG)[0]["level"], st.EXPIRED)
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, age=3800)], NOW + 1, CFG), [])

    def test_closed_sessions_do_not_notify(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.STOPPED, 250_000, closed=True)], NOW, CFG), [])


class Merging(unittest.TestCase):
    def test_coinciding_notices_merge(self):
        engine = engine_after_first_run()
        notices = engine.evaluate([view(st.YOUR_TURN, 310_000, age=70)], NOW, CFG)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["text"], "Te toca · demo · 310K · conviene limpiar")

    def test_context_notice_does_not_arrive_before_te_toca(self):
        engine = engine_after_first_run()
        self.assertEqual(engine.evaluate([view(st.STOPPED, 310_000, age=5, te_toca_due=True)], NOW, CFG), [])
        notices = engine.evaluate([view(st.YOUR_TURN, 310_000, age=70)], NOW + 65, CFG)
        self.assertEqual([(n["wait"], n["level"]) for n in notices], [(st.YOUR_TURN, st.CLEAN)])

    def test_permission_does_not_merge_with_context(self):
        engine = engine_after_first_run()
        notices = engine.evaluate([view(st.PERMISSION, 310_000)], NOW, CFG)
        self.assertEqual(notices[0]["text"], "Espera permiso · demo")

    def test_text_carries_only_project_level_and_tokens(self):
        self.assertEqual(notice_text("demo", None, st.EXPIRED, 80_000),
                         "demo · 80K · caché caducada, empieza de nuevo")


class Persistence(TempDirTest):
    def test_memory_survives_restart(self):
        path = self.tmp / "n.json"
        engine = NotificationEngine(path)
        engine.evaluate([], NOW, CFG)
        self.assertEqual(len(engine.evaluate([view(st.PERMISSION, 310_000, token=1)], NOW, CFG)), 1)
        self.assertEqual(len(engine.evaluate([view(st.STOPPED, 310_000, sid="s2")], NOW, CFG)), 1)
        engine.save()

        again = NotificationEngine(path)
        self.assertFalse(again.first_run)
        self.assertEqual(again.evaluate([view(st.PERMISSION, 310_000, token=1),
                                         view(st.STOPPED, 310_000, sid="s2")], NOW + 10, CFG), [])
        self.assertEqual(again.seq, 2)

    def test_restart_still_detects_changes(self):
        path = self.tmp / "n.json"
        engine = NotificationEngine(path)
        engine.evaluate([view(st.WORKING)], NOW, CFG)
        engine.save()
        again = NotificationEngine(path)
        self.assertEqual(len(again.evaluate([view(st.YOUR_TURN)], NOW + 10, CFG)), 1)


if __name__ == "__main__":
    unittest.main()
