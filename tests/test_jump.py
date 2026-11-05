import http.client
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from claude_monitor import jump
from claude_monitor.registry import Registry, _windows_start, check_process
from claude_monitor.server import Monitor, create_server
from claude_monitor.snapshot import build_snapshot

from .helpers import SID, SID2, WITNESS, TempDirTest

WINDOW = r"c:\code\demo"
IN_FRONT = "Claude Code - demo - Visual Studio Code"


def entry(**changes):
    return {"cwd": WINDOW, "entrypoint": "claude-vscode", "liveness": "open", **changes}


class FakeDesktop:
    def __init__(self, titles=(IN_FRONT,), can_switch=False):
        self.titles = list(titles)
        self.can_switch = can_switch
        self.switched = []
        self.launched = []
        self.uris = []

    def switch_to(self, folder):
        self.switched.append(folder)
        return self.can_switch

    def launch(self, folder):
        self.launched.append(folder)

    def open_uri(self, uri):
        self.uris.append(uri)

    def foreground_title(self):
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]


class OnWindows(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(jump.sys, "platform", "win32")
        patcher.start()
        self.addCleanup(patcher.stop)


class Jumpable(OnWindows):
    def test_only_open_sessions_in_the_extension(self):
        self.assertTrue(jump.jumpable(entry()))
        self.assertFalse(jump.jumpable(entry(entrypoint="cli")))
        self.assertFalse(jump.jumpable(entry(liveness="closed")))
        self.assertFalse(jump.jumpable(entry(liveness="unknown")))
        self.assertFalse(jump.jumpable(None))

    def test_not_outside_windows(self):
        with mock.patch.object(jump.sys, "platform", "linux"):
            self.assertFalse(jump.jumpable(entry()))


class State(OnWindows):
    SESSION = {"cwd": WINDOW, "entrypoint": "claude-vscode"}

    def test_running_in_the_extension_is_open(self):
        self.assertEqual(jump.vscode_state(entry(), self.SESSION, False), "open")

    def test_closed_session_from_the_extension_can_reopen(self):
        self.assertEqual(jump.vscode_state(entry(liveness="closed"), self.SESSION, True), "closed")
        self.assertEqual(jump.vscode_state(None, self.SESSION, True), "closed")

    def test_no_logo_cases(self):
        self.assertIsNone(jump.vscode_state(None, {"cwd": WINDOW, "entrypoint": "cli"}, True), "terminal")
        self.assertIsNone(jump.vscode_state(None, {"cwd": None, "entrypoint": "claude-vscode"}, True), "sin carpeta")
        self.assertIsNone(jump.vscode_state(entry(entrypoint="cli"), self.SESSION, False), "viva en terminal")
        self.assertIsNone(jump.vscode_state(None, self.SESSION, False), "aún no se sabe si está cerrada")

    def test_not_outside_windows(self):
        with mock.patch.object(jump.sys, "platform", "linux"):
            self.assertIsNone(jump.vscode_state(None, self.SESSION, True))


class Folders(unittest.TestCase):
    def test_exact_folder_ignoring_case_and_slashes(self):
        self.assertEqual(jump.owning_folder("C:/Code/Demo", [WINDOW, r"c:\code\other"]), WINDOW)

    def test_closest_parent_wins(self):
        inner = WINDOW + r"\.worktrees\docs"
        self.assertEqual(jump.owning_folder(inner, [WINDOW, inner]), inner)
        self.assertEqual(jump.owning_folder(inner + r"\src", [WINDOW, inner]), inner)

    def test_similar_name_is_not_a_parent(self):
        self.assertIsNone(jump.owning_folder(WINDOW + "-dashboard", [WINDOW]))

    def test_no_window_for_the_folder(self):
        self.assertIsNone(jump.owning_folder(r"c:\code\other", [WINDOW]))


class Title(unittest.TestCase):
    def test_right_window(self):
        for title in (IN_FRONT, "demo - Visual Studio Code", "● a.py - demo - Perfil - Visual Studio Code [Administrator]"):
            self.assertTrue(jump.title_shows(title, WINDOW), title)

    def test_wrong_window(self):
        for title in ("Claude Code - demo-dashboard - Visual Studio Code", "demo - Google Chrome",
                      "demo - Visual Studio Code - Google Chrome", "Visual Studio Code", ""):
            self.assertFalse(jump.title_shows(title, WINDOW), title)


class Go(OnWindows, TempDirTest):
    def setUp(self):
        OnWindows.setUp(self)
        TempDirTest.setUp(self)
        self.ide = self.claude / "ide"
        self.ide.mkdir()
        self.write_lock("1.lock", [WINDOW])

    def write_lock(self, name, folders):
        (self.ide / name).write_text(json.dumps({"pid": 1, "workspaceFolders": folders, "authToken": WITNESS}), encoding="utf-8")

    def go(self, desktop, the_entry=None, sid=SID, timeout=0.05):
        return jump.go(sid, the_entry or entry(), self.ide, desktop, timeout=timeout, switch_timeout=0.01, poll=0.001, settle=0)

    def test_window_first_then_the_tab(self):
        desktop = FakeDesktop(["otra - Google Chrome", IN_FRONT])
        result = self.go(desktop)
        self.assertEqual((result["ok"], result["message"]), (True, ""))
        self.assertEqual(desktop.launched, [WINDOW])
        self.assertEqual(desktop.uris, [jump.SESSION_URI + SID])

    def test_direct_switch_needs_no_code_exe(self):
        desktop = FakeDesktop(can_switch=True)
        result = self.go(desktop)
        self.assertEqual((result["ok"], result["message"], result["trace"]["focus"]), (True, "", "switch"))
        self.assertEqual(desktop.launched, [])
        self.assertEqual(desktop.uris, [jump.SESSION_URI + SID])

    def test_code_exe_if_windows_blocks_the_switch(self):
        titles = ["otra - Google Chrome"] * 3000 + [IN_FRONT]
        desktop = FakeDesktop(titles, can_switch=True)
        desktop.launch = lambda folder: (desktop.launched.append(folder), desktop.titles.clear(), desktop.titles.append(IN_FRONT))
        self.assertTrue(self.go(desktop)["ok"])
        self.assertEqual(desktop.launched, [WINDOW])
        self.assertEqual(desktop.uris, [jump.SESSION_URI + SID])

    def test_no_link_if_the_window_never_comes_forward(self):
        desktop = FakeDesktop(["otra - Google Chrome"], can_switch=True)
        result = self.go(desktop)
        self.assertTrue(result["ok"])
        self.assertIn("a mano", result["message"])
        self.assertEqual(desktop.uris, [])

    def test_nothing_runs_for_a_terminal_session(self):
        desktop = FakeDesktop()
        self.assertFalse(self.go(desktop, entry(entrypoint="cli"))["ok"])
        self.assertEqual((desktop.switched, desktop.launched, desktop.uris), ([], [], []))

    def test_no_new_window_when_no_lock_has_the_folder(self):
        desktop = FakeDesktop()
        self.assertFalse(self.go(desktop, entry(cwd=r"c:\code\other"))["ok"])
        self.assertEqual(desktop.launched, [])

    def test_multi_folder_window_and_broken_lock_are_ignored(self):
        self.write_lock("1.lock", [WINDOW, r"c:\code\other"])
        (self.ide / "2.lock").write_text("{no es json", encoding="utf-8")
        desktop = FakeDesktop()
        self.assertFalse(self.go(desktop)["ok"])
        self.assertEqual(desktop.launched, [])

    def test_bad_session_id_never_reaches_the_link(self):
        desktop = FakeDesktop()
        self.assertFalse(self.go(desktop, sid=SID + "&prompt=x")["ok"])
        self.assertEqual((desktop.launched, desktop.uris), ([], []))

    def test_missing_code_exe(self):
        desktop = FakeDesktop()
        desktop.launch = mock.Mock(side_effect=FileNotFoundError())
        self.assertFalse(self.go(desktop)["ok"])
        self.assertEqual(desktop.uris, [])


class Endpoint(OnWindows, TempDirTest):
    def setUp(self):
        OnWindows.setUp(self)
        TempDirTest.setUp(self)
        (self.claude / "ide").mkdir()
        (self.claude / "ide" / "1.lock").write_text(json.dumps({"workspaceFolders": [WINDOW], "authToken": WITNESS}), encoding="utf-8")
        (self.claude / "sessions" / "123.json").write_text(json.dumps({
            "pid": 123, "sessionId": SID, "cwd": WINDOW + "\\" + WITNESS, "entrypoint": "claude-vscode", "procStart": "1", "pidDomain": "x",
        }), encoding="utf-8")
        self.monitor = Monitor(self.cfg)
        self.monitor.registry = Registry(self.cfg.sessions_dir, checker=lambda e: "open")
        self.desktop = FakeDesktop()
        self.monitor.desktop = self.desktop
        self.monitor.tick()
        self.server = create_server(0, self.monitor)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body=None, origin="same", content_type="application/json", path="/api/jump"):
        host = f"127.0.0.1:{self.port}"
        payload = json.dumps({"sid": SID} if body is None else body).encode("utf-8")
        headers = {"Host": host, "Content-Type": content_type}
        if origin:
            headers["Origin"] = f"http://{host}" if origin == "same" else origin
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", path, payload, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def assert_rejected(self, status, *args, **kwargs):
        self.assertEqual(self.post(*args, **kwargs)[0], status)
        self.assertEqual((self.desktop.launched, self.desktop.uris), ([], []))

    def test_jump(self):
        status, data = self.post()
        self.assertEqual((status, json.loads(data)["ok"]), (200, True))
        self.assertEqual(self.desktop.uris, [jump.SESSION_URI + SID])
        self.assertNotIn("trace", json.loads(data))
        self.assertEqual(self.monitor.jump_log[-1]["tab"], "link")

    def test_other_origins_rejected(self):
        for origin in (None, "null", "http://evil.example", f"http://localhost:{self.port + 1}", "http://127.0.0.1:1"):
            self.assert_rejected(403, origin=origin)

    def test_form_content_type_rejected(self):
        self.assert_rejected(415, content_type="text/plain")

    def test_bad_bodies_rejected(self):
        for body in ({"sid": "../../x"}, {"sid": 5}, {}, [SID], {"sid": "x" * 2000}):
            self.assert_rejected(400, body)

    def test_session_not_in_registry(self):
        status, data = self.post({"sid": SID2})
        self.assertEqual((status, json.loads(data)["ok"]), (200, False))
        self.assertEqual(self.desktop.launched, [])

    def test_a_closed_session_does_not_jump_even_if_the_list_is_old(self):
        self.monitor.registry.checker = lambda e: "closed"
        self.assertFalse(json.loads(self.post()[1])["ok"])
        self.assertEqual(self.desktop.launched, [])

    def test_every_rejection_arrives_with_its_response(self):
        # Without reading the body first, Windows reset some of these connections.
        for _ in range(100):
            self.assert_rejected(415, content_type="text/plain")
            self.assert_rejected(403, origin="http://evil.example")
            self.assert_rejected(405, path="/api/state")

    def test_other_posts_still_refused(self):
        self.assert_rejected(405, path="/api/state")

    def test_snapshot_offers_it_without_leaking_the_path(self):
        data = json.loads(self.monitor.snapshot_json)
        row = next(r for r in data["rows"] if r["sid"] == SID)
        self.assertEqual(row["vscode"], "open")
        self.assertNotIn(WITNESS.encode(), self.monitor.snapshot_json.replace(json.dumps(row["project"]).encode(), b""))
        self.assertTrue(data["signals"]["jump_supported"])
        self.assertNotIn("cwd", row)
        self.assertNotIn("entrypoint", row)


class Reopen(OnWindows, TempDirTest):
    def setUp(self):
        OnWindows.setUp(self)
        TempDirTest.setUp(self)
        self.ide = self.claude / "ide"
        self.ide.mkdir()
        self.folder = self.tmp / "demo"
        self.folder.mkdir()
        self.session = {"cwd": str(self.folder), "entrypoint": "claude-vscode"}
        self.front = f"x - demo - {jump.APP_NAME}"

    def reopen(self, desktop, opened=False, session=None, **kwargs):
        limits = dict(timeout=0.02, switch_timeout=0.01, poll=0.001, settle=0, new_window_timeout=0.05, companion_grace=0.01, new_window_poll=0.001)
        with mock.patch.object(jump, "open_in_window", return_value=opened) as asked:
            self.asked = asked
            return jump.reopen(SID, session or self.session, self.ide, desktop, self.tmp, **{**limits, **kwargs})

    def test_new_window_then_the_tab_through_the_extension(self):
        desktop = FakeDesktop([self.front])
        result = self.reopen(desktop, opened=True)
        self.assertEqual((result["ok"], result["trace"]["focus"], result["trace"]["tab"]), (True, "new_window", "window"))
        self.assertEqual(desktop.launched, [str(self.folder)])
        self.assertEqual(desktop.uris, [])

    def test_link_when_the_extension_never_answers(self):
        desktop = FakeDesktop([self.front])
        self.assertEqual(self.reopen(desktop)["trace"]["tab"], "link")
        self.assertEqual(desktop.uris, [jump.SESSION_URI + SID])

    def test_no_link_if_another_window_is_in_front(self):
        desktop = FakeDesktop([f"x - otra - {jump.APP_NAME}"])
        result = self.reopen(desktop)
        self.assertIn("a mano", result["message"])
        self.assertEqual(desktop.uris, [])

    def test_uses_the_open_window_of_that_exact_folder(self):
        (self.ide / "1.lock").write_text(json.dumps({"workspaceFolders": [str(self.folder)]}), encoding="utf-8")
        desktop = FakeDesktop([self.front], can_switch=True)
        self.assertNotEqual(self.reopen(desktop)["trace"]["focus"], "new_window")
        self.assertEqual(desktop.launched, [])

    def test_a_window_of_the_parent_folder_does_not_count(self):
        (self.ide / "1.lock").write_text(json.dumps({"workspaceFolders": [str(self.tmp)]}), encoding="utf-8")
        desktop = FakeDesktop([self.front])
        self.assertEqual(self.reopen(desktop)["trace"]["focus"], "new_window")

    def test_folder_gone(self):
        desktop = FakeDesktop()
        result = self.reopen(desktop, session={"cwd": str(self.tmp / "borrada"), "entrypoint": "claude-vscode"})
        self.assertIn("ya no existe", result["message"])
        self.assertEqual(desktop.launched, [])

    def test_terminal_session_refused(self):
        desktop = FakeDesktop()
        self.assertFalse(self.reopen(desktop, session={"cwd": str(self.folder), "entrypoint": "cli"})["ok"])
        self.assertEqual(desktop.launched, [])


class LivenessOnWindows(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Solo en Windows")
    def test_finished_process_is_closed_while_a_handle_keeps_it_known(self):
        process = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
        self.addCleanup(process.stdin.close)
        start = _windows_start(process.pid)
        the_entry = {"pid": process.pid, "procStart": str(start), "pidDomain": ""}
        self.assertEqual(check_process(the_entry), "open")
        process.stdin.write(b"\n")
        process.stdin.flush()
        process.wait(timeout=10)
        self.assertEqual(check_process(the_entry), "closed")  # Popen still holds its handle here.
