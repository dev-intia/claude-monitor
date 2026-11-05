import json
import os
import subprocess
import sys
import unittest

from claude_monitor import status as st
from claude_monitor.hookstore import HookStore, hooks_installed
from claude_monitor.installer import (HOOK_SCRIPT, install, read_settings, render, uninstall, with_ours, without_ours)
from claude_monitor.notify import NotificationEngine
from claude_monitor.config import load_config

from .helpers import NOW, SID, WITNESS, TempDirTest

CFG = load_config()


def payload(event, **extra):
    return {"session_id": SID, "transcript_path": f"/x/{WITNESS}.jsonl", "cwd": f"/x/{WITNESS}",
            "hook_event_name": event, **extra}


class HookScript(TempDirTest):
    def run_hook(self, data, *args):
        text = data if isinstance(data, (str, bytes)) else json.dumps(data)
        if isinstance(text, str):
            text = text.encode()
        env = dict(os.environ, CLAUDE_MONITOR_STATE_DIR=str(self.state))
        return subprocess.run([sys.executable, str(HOOK_SCRIPT), *args], input=text, env=env,
                              capture_output=True, timeout=30)

    def records(self):
        path = self.state / "hook-events.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_keeps_only_the_whitelisted_fields(self):
        result = self.run_hook(payload(
            "Stop", last_assistant_message=WITNESS, message=WITNESS, title=WITNESS, tool_input={"command": WITNESS},
            prompt=WITNESS, background_tasks=[{"id": "1", "type": "subagent", "status": "running", "description": WITNESS}]))
        self.assertEqual((result.returncode, result.stdout), (0, b""))
        records = self.records()
        self.assertEqual(set(records[0]), {"session_id", "event", "notification_type", "pending", "ts"})
        self.assertEqual(records[0]["pending"], 1)
        self.assertNotIn(WITNESS, (self.state / "hook-events.jsonl").read_text(encoding="utf-8"))

    def test_notification_fields(self):
        self.run_hook(payload("Notification", message=WITNESS, title=WITNESS, notification_type="permission_prompt"))
        record = self.records()[0]
        self.assertEqual((record["event"], record["notification_type"]), ("Notification", "permission_prompt"))

    def test_garbage_input_exits_zero_silently(self):
        for data in ("no es json", "", "[]", '{"session_id": 3}', b"\xff\xfe"):
            result = self.run_hook(data)
            self.assertEqual((result.returncode, result.stdout), (0, b""))
        self.assertEqual(self.records(), [])

    def test_unwritable_state_exits_zero(self):
        blocker = self.tmp / "archivo"
        blocker.write_text("x")
        result = self.run_hook(payload("Stop"), "--state-dir", str(blocker / "sub"))
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_file_size_is_bounded(self):
        for _ in range(6):
            self.run_hook(payload("Stop"), "--max-bytes", "200")
        total = sum(p.stat().st_size for p in self.state.iterdir())
        self.assertLess(total, 2 * 200 + 400)
        self.assertTrue((self.state / "hook-events.jsonl.1").exists())

    def test_pending_ignores_finished_and_long_lived_helpers(self):
        tasks = [{"type": "subagent", "status": "running"}, {"type": "subagent", "status": "completed"},
                 {"type": "shell", "status": "running"}, {"type": "monitor", "status": "running"},
                 {"type": "workflow", "status": "pending"}]
        self.run_hook(payload("Stop", background_tasks=tasks))
        self.assertEqual(self.records()[0]["pending"], 2)

    def test_store_reads_incrementally_and_survives_rotation(self):
        store = HookStore(self.state / "hook-events.jsonl")
        self.run_hook(payload("Notification", notification_type="idle_prompt"))
        store.refresh()
        self.assertEqual(store.get(SID)["wait"], "your_turn")
        self.run_hook(payload("UserPromptSubmit", prompt=WITNESS))
        store.refresh()
        self.assertIsNone(store.get(SID)["wait"])
        for _ in range(3):
            self.run_hook(payload("Notification", notification_type="permission_prompt"), "--max-bytes", "120")
        store.refresh()
        self.assertEqual(store.get(SID)["wait"], "permission")


class WaitsEndToEnd(TempDirTest):
    """Sample hook events through to the notice decision."""

    def view(self, store, turn_age, context=310_000):
        sess = {"sid": SID, "project": "demo", "turn_state": "ended", "last_turn": NOW - turn_age,
                "last_activity": NOW - turn_age}
        entry = {"liveness": "open", "status": "idle", "statusUpdatedAt": (NOW - turn_age) * 1000}
        state, _, token, _ = st.derive_state(sess, store.get(SID), entry, 0, "open", NOW, CFG)
        return {"sid": SID, "project": "demo", "state": state, "token": token, "context": context,
                "age_seconds": turn_age, "subagents": 0, "closed": False,
                "te_toca_due": state == st.STOPPED and turn_age < CFG.idle_seconds}

    def write(self, *records):
        self.state.mkdir(exist_ok=True)
        with open(self.state / "hook-events.jsonl", "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps({"session_id": SID, **record}) + "\n")

    def setUp(self):
        super().setUp()
        self.engine = NotificationEngine(None)
        self.engine.evaluate([], NOW, CFG)
        self.store = HookStore(self.state / "hook-events.jsonl")

    def step(self, turn_age):
        self.store.refresh()
        return self.engine.evaluate([self.view(self.store, turn_age)], NOW, CFG)

    def test_stop_with_pending_is_silent_then_notifies_after_the_agent(self):
        self.write({"event": "Stop", "pending": 1, "ts": NOW - 5})
        self.assertEqual(self.step(6), [])
        self.assertEqual(self.engine.notices, [])
        # Agent ends and reports back: new prompt, final Stop, then the idle notice.
        self.write({"event": "SubagentStop", "pending": 1, "ts": NOW - 4},
                   {"event": "UserPromptSubmit", "ts": NOW - 3},
                   {"event": "Stop", "pending": 0, "ts": NOW - 2})
        self.assertEqual(self.step(3), [], "finished, but te toca still has to come")
        self.write({"event": "Notification", "notification_type": "idle_prompt", "ts": NOW - 1})
        notices = self.step(70)
        self.assertEqual(notices[0]["text"], "Te toca · demo · 310K · conviene limpiar")

    def test_permission_prompt_notifies_once(self):
        self.write({"event": "Notification", "notification_type": "permission_prompt", "ts": NOW - 1})
        self.assertEqual(self.step(8)[0]["text"], "Espera permiso · demo")
        self.assertEqual(self.step(9), [])


class Installer(TempDirTest):
    EXISTING = {
        "language": "spanish",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "mi-otro-hook"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}],
        },
        "permissions": {"allow": ["Read"]},
    }

    def settings(self, data=None) -> "os.PathLike":
        path = self.tmp / "settings.json"
        path.write_text(render(self.EXISTING if data is None else data), encoding="utf-8")
        return path

    def test_install_keeps_existing_settings_and_hooks(self):
        path = self.settings()
        self.assertEqual(install(path, str(self.state), 1000, python="py", yes=True), 0)
        data, _ = read_settings(path)
        self.assertEqual(data["language"], "spanish")
        self.assertEqual(data["permissions"], {"allow": ["Read"]})
        self.assertEqual(data["hooks"]["PreToolUse"], self.EXISTING["hooks"]["PreToolUse"])
        commands = [g["hooks"][0]["command"] for g in data["hooks"]["Stop"]]
        self.assertEqual(commands, ["mi-otro-hook", "py"])
        for event in ("Notification", "SubagentStop", "UserPromptSubmit", "SessionEnd"):
            self.assertTrue(data["hooks"][event][-1]["hooks"][0]["async"])
        self.assertTrue(hooks_installed(path))

    def test_uninstall_restores_the_exact_original(self):
        path = self.settings()
        original = path.read_text(encoding="utf-8")
        install(path, str(self.state), 1000, python="py", yes=True)
        self.assertNotEqual(path.read_text(encoding="utf-8"), original)
        uninstall(path, yes=True)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(hooks_installed(path))

    def test_uninstall_without_prior_hooks_key(self):
        path = self.settings({"language": "spanish"})
        original = path.read_text(encoding="utf-8")
        install(path, str(self.state), 1000, python="py", yes=True)
        uninstall(path, yes=True)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_install_twice_does_not_duplicate(self):
        path = self.settings()
        install(path, str(self.state), 1000, python="py", yes=True)
        first = path.read_text(encoding="utf-8")
        install(path, str(self.state), 1000, python="py", yes=True)
        self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_backup_is_made_and_holds_the_original(self):
        path = self.settings()
        original = path.read_text(encoding="utf-8")
        install(path, str(self.state), 1000, python="py", yes=True)
        backups = list(self.tmp.glob("settings.json.bak-claude-monitor-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_dry_run_writes_nothing(self):
        path = self.settings()
        original = path.read_text(encoding="utf-8")
        install(path, str(self.state), 1000, python="py", dry_run=True)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.tmp.glob("*.bak-*")), [])

    def test_invalid_settings_are_left_alone(self):
        from claude_monitor.installer import SettingsError
        path = self.tmp / "settings.json"
        path.write_text("{roto", encoding="utf-8")
        with self.assertRaises(SettingsError):
            install(path, str(self.state), 1000, python="py", yes=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "{roto")

    def test_group_shared_with_a_foreign_hook_keeps_that_hook(self):
        mine = with_ours({}, "py", "x/claude_monitor_hook.py", "s", 1)["hooks"]["Stop"][0]["hooks"][0]
        shared = {"hooks": {"Stop": [{"hooks": [mine, {"type": "command", "command": "ajeno"}]}]}}
        self.assertEqual(without_ours(shared)["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "ajeno"}])


if __name__ == "__main__":
    unittest.main()
