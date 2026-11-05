import json
import os
import unittest

from claude_monitor.snapshot import units_of
from claude_monitor.transcripts import TranscriptStore

from .helpers import NOW, SID, TempDirTest, assistant, compact, jsonl, tool_result, usage, user


class Base(TempDirTest):
    def store(self):
        return TranscriptStore(self.claude / "projects", 7)

    def day_vector(self, session, kind="main", model="claude-opus-5-5"):
        total = [0] * 6
        for day in session["days"].values():
            for i, v in enumerate(day.get(kind, {}).get(model, [0] * 6)):
                total[i] += v
        return total


class MessageDedupe(Base):
    def test_identical_repeats_count_once(self):
        u = usage(10, 100, 1000, 50)
        self.write_main([assistant("m1", NOW, u), assistant("m1", NOW, u), assistant("m1", NOW, u)])
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(self.day_vector(store.sessions[SID]), [10, 0, 0, 100, 1000, 50])

    def test_growing_output_keeps_the_final_value(self):
        # First-line counting would give 16; adding them up, 305.
        lines = [assistant("m1", NOW, usage(2, 0, 0, o)) for o in (16, 16, 273)]
        self.write_main(lines)
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(self.day_vector(store.sessions[SID])[5], 273)

    def test_repeat_arriving_in_a_later_read(self):
        path = self.write_main([assistant("m1", NOW, usage(2, 0, 0, 16))])
        store = self.store()
        store.scan(NOW + 1)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(jsonl([assistant("m1", NOW, usage(2, 0, 0, 273))]))
        store.scan(NOW + 2)
        vector = self.day_vector(store.sessions[SID])
        self.assertEqual(vector[5], 273)
        self.assertEqual(vector[0], 2)

    def test_dedupe_survives_a_restart(self):
        path = self.write_main([assistant("m1", NOW, usage(2, 0, 0, 16))])
        store = self.store()
        store.scan(NOW + 1)
        saved = json.loads(json.dumps(store.dump()))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(jsonl([assistant("m1", NOW, usage(2, 0, 0, 20))]))
        again = self.store()
        again.restore(saved)
        again.scan(NOW + 2)
        self.assertEqual(self.day_vector(again.sessions[SID])[5], 20)
        self.assertEqual(self.day_vector(again.sessions[SID])[0], 2)


class IncompleteLines(Base):
    def test_partial_line_is_kept_for_the_next_read(self):
        complete = jsonl([assistant("m1", NOW, usage(1, 0, 0, 1))])
        second = jsonl([assistant("m2", NOW + 1, usage(1, 0, 0, 7))])
        path = self.main_file()
        path.write_bytes((complete + second[:40]).encode())
        os.utime(path, (NOW, NOW))
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(self.day_vector(store.sessions[SID])[5], 1)
        self.assertEqual(store.bad_lines, 0)
        with open(path, "ab") as fh:
            fh.write(second[40:].encode())
        store.scan(NOW + 2)
        self.assertEqual(self.day_vector(store.sessions[SID])[5], 8)
        self.assertEqual(store.bad_lines, 0)

    def test_shrunk_file_is_reread_without_double_counting(self):
        path = self.write_main([assistant("m1", NOW, usage(1, 0, 0, 5)), assistant("m2", NOW, usage(1, 0, 0, 5))])
        store = self.store()
        store.scan(NOW + 1)
        path.write_text(jsonl([assistant("m1", NOW, usage(1, 0, 0, 5))]), encoding="utf-8")
        store.scan(NOW + 2)
        self.assertEqual(self.day_vector(store.sessions[SID])[5], 5)


class FirstScanWindow(Base):
    def test_old_files_are_skipped_until_they_change(self):
        old = NOW - 30 * 86400
        path = self.write_main([assistant("m1", old, usage(1, 0, 0, 5))], ts=old)
        store = self.store()
        store.scan(NOW)
        self.assertNotIn(SID, store.sessions)
        os.utime(path, (NOW, NOW))
        store.scan(NOW)
        self.assertIn(SID, store.sessions)

    def test_open_session_is_read_even_if_old(self):
        old = NOW - 30 * 86400
        self.write_main([assistant("m1", old, usage(1, 0, 0, 5))], ts=old)
        store = self.store()
        store.scan(NOW, {SID})
        self.assertIn(SID, store.sessions)


class Separation(Base):
    def test_subagent_tokens_are_separate_and_never_set_context(self):
        self.write_main([assistant("m1", NOW, usage(10, 0, 90000, 5))])
        self.write_agent([assistant("a1", NOW + 5, usage(1, 0, 400000, 9), model="claude-haiku-5-5", agent="a")])
        store = self.store()
        store.scan(NOW + 10)
        session = store.sessions[SID]
        self.assertEqual(session["context"], 90010)
        self.assertEqual(session["model"], "claude-opus-5-5")
        self.assertEqual(self.day_vector(session, "main")[4], 90000)
        self.assertEqual(self.day_vector(session, "sub", "claude-haiku-5-5")[4], 400000)
        self.assertEqual(self.day_vector(session, "main", "claude-haiku-5-5")[4], 0)

    def test_each_message_weighted_with_its_own_model(self):
        self.write_main([
            assistant("m1", NOW, usage(100, 0, 0, 0), model="claude-opus-5-5"),
            assistant("m2", NOW + 1, usage(100, 0, 0, 0), model="claude-haiku-5-5"),
        ])
        store = self.store()
        store.scan(NOW + 2)
        cfg = self.cfg
        weights = dict(cfg.model_weights, **{"claude-opus-5-5": 5.0, "claude-haiku-5-5": 1.0})
        class C:
            model_weights = weights
            weights_ = cfg.weights
        C.weights = cfg.weights
        session = store.sessions[SID]
        total = 0
        for models in (d["main"] for d in session["days"].values()):
            for model, vector in models.items():
                total += units_of(vector, model, C)
        self.assertEqual(total, 600)

    def test_5m_and_1h_writes_are_split(self):
        self.write_main([assistant("m1", NOW, usage(0, 30, 0, 0, five=10, hour=20))])
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(self.day_vector(store.sessions[SID])[:4], [0, 10, 20, 0])
        self.assertEqual(store.sessions[SID]["context"], 30)

    def test_unknown_model_gets_no_units(self):
        self.assertIsNone(units_of([100] * 6, "claude-nuevo-9", self.cfg))


class ContextChanges(Base):
    def test_context_above_200k_is_not_capped(self):
        self.write_main([assistant("m1", NOW, usage(1, 0, 440000, 5))])
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(store.sessions[SID]["context"], 440001)

    def test_compaction_replaces_stale_context(self):
        self.write_main([assistant("m1", NOW, usage(1, 0, 300000, 5)), compact(NOW + 1, post=15000)])
        store = self.store()
        store.scan(NOW + 2)
        self.assertEqual(store.sessions[SID]["context"], 15000)
        self.assertEqual(store.sessions[SID]["context_source"], "compact")

    def test_clear_in_the_same_file_resets_context(self):
        self.write_main([assistant("m1", NOW, usage(1, 0, 300000, 5)),
                         user(NOW + 1, "<command-name>/clear</command-name>")])
        store = self.store()
        store.scan(NOW + 2)
        session = store.sessions[SID]
        self.assertEqual(session["context"], 0)
        self.assertEqual(session["turn_state"], "cleared")

    def test_turn_states(self):
        for events, expected in (
            ([assistant("m", NOW, usage(), stop="tool_use")], "tool"),
            ([assistant("m", NOW, usage(), stop="tool_use"), tool_result(NOW + 1)], "open"),
            ([assistant("m", NOW, usage())], "ended"),
            ([assistant("m", NOW, usage()), user(NOW + 1, "hola")], "open"),
            ([assistant("m", NOW, usage()), user(NOW + 1, "[Request interrupted by user]")], "ended"),
        ):
            self.write_main(events)
            store = self.store()
            store.scan(NOW + 2)
            self.assertEqual(store.sessions[SID]["turn_state"], expected)

    def test_synthetic_message_is_not_a_turn(self):
        self.write_main([assistant("m1", NOW, usage(1, 0, 100000, 5)),
                         assistant("s", NOW + 1, usage(0), model="<synthetic>")])
        store = self.store()
        store.scan(NOW + 2)
        self.assertEqual(store.sessions[SID]["context"], 100001)


class SubagentActivity(Base):
    def test_finished_flag_and_staleness(self):
        self.write_main([assistant("m1", NOW, usage())])
        self.write_agent([assistant("a1", NOW, usage(), agent="a")], "agent-running", ts=NOW)
        self.write_agent([assistant("a2", NOW, usage(), agent="b")], "agent-done", finished=True, ts=NOW)
        self.write_agent([assistant("a3", NOW, usage(), agent="c")], "agent-dead", ts=NOW - 7200)
        store = self.store()
        store.scan(NOW)
        self.assertEqual(store.active_subagents_by_session(NOW, 1800), {SID: 1})


class Titles(Base):
    def scan(self, events):
        self.write_main(events)
        store = self.store()
        store.scan(NOW + 1)
        return store.sessions[SID]

    def test_last_ai_title_wins(self):
        session = self.scan([assistant("m1", NOW, usage(1)), {"type": "ai-title", "aiTitle": "Uno"}, {"type": "ai-title", "aiTitle": "Dos"}])
        self.assertEqual(session["ai_title"], "Dos")

    def test_title_is_not_activity(self):
        session = self.scan([assistant("m1", NOW - 50, usage(1)), {"type": "ai-title", "aiTitle": "Uno"}])
        self.assertEqual(session["last_activity"], NOW - 50)

    def test_long_title_is_cut_and_bad_title_ignored(self):
        session = self.scan([{"type": "ai-title", "aiTitle": "x" * 500}, {"type": "ai-title", "aiTitle": 7}, {"type": "custom-title", "customTitle": "  "}])
        self.assertEqual(len(session["ai_title"]), 200)
        self.assertIsNone(session["custom_title"])

    def test_subagent_title_does_not_rename_the_session(self):
        self.write_main([assistant("m1", NOW, usage(1)), {"type": "ai-title", "aiTitle": "Principal"}])
        self.write_agent([assistant("a1", NOW, usage(1), agent="a"), {"type": "ai-title", "aiTitle": "Agente"}])
        store = self.store()
        store.scan(NOW + 1)
        self.assertEqual(store.sessions[SID]["ai_title"], "Principal")


if __name__ == "__main__":
    unittest.main()


class Origin(TempDirTest):
    def test_keeps_the_first_folder_and_entrypoint(self):
        first = assistant("m1", NOW, usage(1, 0, 0, 1))
        first["entrypoint"] = "claude-vscode"
        later = assistant("m2", NOW + 1, usage(1, 0, 0, 1))
        later["cwd"] = "C:\\code\\demo\\src"
        later["entrypoint"] = "cli"
        self.write_main([first, later])
        store = TranscriptStore(self.cfg.projects_dir, 7)
        store.scan(NOW + 2, set())
        session = store.sessions[SID]
        self.assertEqual((session["cwd"], session["entrypoint"]), ("C:\\code\\demo", "claude-vscode"))
