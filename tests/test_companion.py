"""Runs the real extension.js in Node against a stand-in `vscode` module, and the real Python client."""

import http.client
import json
import os
import shutil
import subprocess
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from claude_monitor import jump
from install_extension import build_vsix

from .helpers import SID, TempDirTest

ROOT = Path(__file__).resolve().parent.parent
FOLDER = r"c:\code\demo"
HARNESS = """
const Module = require("module");
const commands = process.env.HAS_COMMAND === "1" ? ["claude-vscode.primaryEditor.open"] : [];
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request !== "vscode") return load.call(this, request, ...rest);
  return {
    workspace: { workspaceFolders: [{ uri: { fsPath: process.env.WINDOW_FOLDER } }], onDidChangeWorkspaceFolders: () => ({ dispose() {} }) },
    commands: {
      getCommands: async () => commands,
      executeCommand: async (name, sid) => console.log(`RAN ${name} ${sid}`),
    },
  };
};
const extension = require(process.argv[2]);
extension.activate({ subscriptions: [] });
process.stdin.on("data", () => { extension.deactivate(); console.log("BYE"); process.exit(0); });
"""


@unittest.skipUnless(shutil.which("node"), "Node no disponible")
class Companion(TempDirTest):
    def start(self, has_command=True):
        script = self.tmp / "harness.js"
        script.write_text(HARNESS, encoding="utf-8")
        env = {**os.environ, "USERPROFILE": str(self.tmp), "HOME": str(self.tmp),
               "WINDOW_FOLDER": FOLDER, "HAS_COMMAND": "1" if has_command else "0"}
        self.node = subprocess.Popen(["node", str(script), str(ROOT / "vscode-extension" / "extension.js")], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.node.kill)
        self.addCleanup(self.node.stdout.close)
        self.addCleanup(self.node.stdin.close)
        self.windows = self.tmp / ".claude-monitor" / "windows"
        self.file = self.windows / f"{self.node.pid}.json"
        deadline = time.monotonic() + 10
        while not self.file.exists():
            self.assertLess(time.monotonic(), deadline, "la extensión no publicó su ventana")
            time.sleep(0.01)
        return json.loads(self.file.read_text(encoding="utf-8"))

    def send(self, info, path="/open", token=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
        all_headers = {"Authorization": f"Bearer {token or info['token']}", **(headers or {})}
        connection.request("POST", path, json.dumps({"sid": SID} if body is None else body), all_headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_client_opens_the_session_in_that_window(self):
        self.start()
        self.assertTrue(jump.open_in_window(self.windows, FOLDER.upper(), SID))
        self.assertEqual(self.node.stdout.readline().strip(), f"RAN claude-vscode.primaryEditor.open {SID}")

    def test_client_ignores_other_windows_and_dead_ports(self):
        self.start()
        self.assertFalse(jump.open_in_window(self.windows, r"c:\code\other", SID))
        (self.windows / "1.json").write_text(json.dumps({"pid": 1, "folders": [r"c:\code\dead"], "port": 1, "token": "x"}), encoding="utf-8")
        self.assertFalse(jump.open_in_window(self.windows, r"c:\code\dead", SID))

    def test_fallback_when_the_claude_command_is_missing(self):
        info = self.start(has_command=False)
        self.assertEqual(self.send(info), 404)
        self.assertFalse(jump.open_in_window(self.windows, FOLDER, SID))

    def test_rejections(self):
        info = self.start()
        self.assertEqual(self.send(info, token="mal"), 401)
        self.assertEqual(self.send(info, headers={"Origin": "http://evil.example"}), 403)
        self.assertEqual(self.send(info, path="/otra"), 404)
        for body in ({"sid": "../x"}, {"sid": 5}, {"sid": SID + "&prompt=x"}, {"x": "y" * 5000}):
            self.assertEqual(self.send(info, body=body), 400, body)

    def test_file_removed_on_exit(self):
        self.start()
        self.node.stdin.write("fin\n")
        self.node.stdin.flush()
        self.assertEqual(self.node.stdout.readline().strip(), "BYE")
        self.assertFalse(self.file.exists())

    def test_dead_window_files_are_cleaned_up(self):
        self.windows = self.tmp / ".claude-monitor" / "windows"
        self.windows.mkdir(parents=True)
        dead = subprocess.run(["node", "-e", "console.log(process.pid)"], capture_output=True, text=True).stdout.strip()
        stale = self.windows / f"{dead}.json"
        stale.write_text(json.dumps({"pid": int(dead), "folders": [FOLDER], "port": 1, "token": "x"}), encoding="utf-8")
        self.start()
        self.assertFalse(stale.exists())


class GoWithCompanion(TempDirTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(jump.sys, "platform", "win32")
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.claude / "ide").mkdir()
        (self.claude / "ide" / "1.lock").write_text(json.dumps({"workspaceFolders": [FOLDER]}), encoding="utf-8")
        self.entry = {"cwd": FOLDER, "entrypoint": "claude-vscode", "liveness": "open"}
        self.desktop = mock.Mock()
        self.desktop.foreground_title.return_value = "x - otra - Google Chrome"

    def go(self, opened):
        with mock.patch.object(jump, "open_in_window", return_value=opened):
            return jump.go(SID, self.entry, self.claude / "ide", self.desktop, self.tmp, timeout=0.01, switch_timeout=0.01, poll=0.001, settle=0)

    def test_no_link_needed_even_if_the_focus_is_not_confirmed(self):
        result = self.go(True)
        self.assertTrue(result["ok"])
        self.assertIn("Windows", result["message"])
        self.desktop.open_uri.assert_not_called()
        self.assertEqual(result["trace"]["tab"], "window")

    def test_short_wait_when_the_tab_is_already_open(self):
        with mock.patch.object(jump, "open_in_window", return_value=True), mock.patch.object(jump, "OPENED_FOCUS_TIMEOUT_SECONDS", 0.01):
            started = time.monotonic()
            jump.go(SID, self.entry, self.claude / "ide", self.desktop, self.tmp, switch_timeout=0.01, poll=0.001, settle=0)
        self.assertLess(time.monotonic() - started, 1)

    def test_tab_is_ready_before_the_window_moves(self):
        calls = []
        self.desktop.switch_to.side_effect = lambda folder: calls.append("switch") or True
        self.desktop.foreground_title.return_value = "x - demo - Visual Studio Code"
        with mock.patch.object(jump, "open_in_window", side_effect=lambda *a: calls.append("open") or True):
            result = jump.go(SID, self.entry, self.claude / "ide", self.desktop, self.tmp, settle=0)
        self.assertEqual(calls, ["open", "switch"])
        self.assertEqual((result["ok"], result["message"], result["trace"]["focus"]), (True, "", "switch"))
        self.desktop.open_uri.assert_not_called()

    def test_link_when_the_extension_is_not_loaded(self):
        self.desktop.foreground_title.return_value = "x - demo - Visual Studio Code"
        self.assertEqual(self.go(False)["trace"]["tab"], "link")
        self.desktop.open_uri.assert_called_once_with(jump.SESSION_URI + SID)


class Package(TempDirTest):
    def test_vsix_contents(self):
        target = self.tmp / "x.vsix"
        build_vsix(target)
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            manifest = archive.read("extension.vsixmanifest").decode("utf-8")
        self.assertEqual(names, {"[Content_Types].xml", "extension.vsixmanifest", "extension/package.json", "extension/extension.js"})
        self.assertIn('Id="claude-monitor-companion"', manifest)
        self.assertIn('Publisher="local"', manifest)
