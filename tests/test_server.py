import http.client
import json
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from claude_monitor import status as st
from claude_monitor.registry import Registry
from claude_monitor.server import PAGE, Monitor, create_server, host_is_local
from claude_monitor.snapshot import build_view

from .helpers import SID, SID2, WITNESS, TempDirTest, assistant, jsonl, tool_result, usage, user

ROOT = Path(__file__).resolve().parent.parent


class HostHeader(unittest.TestCase):
    def test_local_names_any_port(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "localhost:9000", "[::1]:8765", "LOCALHOST", "127.0.0.1"):
            self.assertTrue(host_is_local(host), host)

    def test_external_names(self):
        for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example", "localhost.evil.example",
                     "192.168.1.5:8765", "", None, "user@evil.example"):
            self.assertFalse(host_is_local(host), host)


class Page(unittest.TestCase):
    def test_no_external_url(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"https?:|//[a-z0-9.-]+\.[a-z]{2,}|@import", html, re.I))
        self.assertNotIn("url(", html)  # CSS url(); createObjectURL( is a different thing.
        self.assertIsNone(re.search(r"<(link|img|iframe|script\s+src)\b", html, re.I))

    def test_single_file_in_spanish(self):
        html = PAGE.read_text(encoding="utf-8")
        for text in ('lang="es"', "<style>", "<script>", "Conviene limpiar", "actividad reciente", "estimación"):
            self.assertIn(text, html)


class Api(TempDirTest):
    def setUp(self):
        super().setUp()
        now = time.time()
        self.write_main([
            user(now - 400, f"mi secreto {WITNESS}"),
            assistant("m1", now - 390, usage(5, 100, 310000, 50), stop="tool_use", text=WITNESS),
            tool_result(now - 380),
            assistant("m2", now - 370, usage(5, 100, 310000, 80), text=WITNESS),
        ], ts=now)
        self.write_agent([user(now - 200, WITNESS), assistant("a1", now - 199, usage(1, 1, 400000, 1), model="claude-haiku-5-5", text=WITNESS, agent="a")],
                         finished=True, ts=now)
        self.write_main([assistant("x1", now - 100, usage(1, 0, 10, 1), model="claude-nuevo-9", sid=SID2, text=WITNESS)], SID2, ts=now)
        # The hook file and registry also carry the witness where it must be dropped.
        (self.claude / "sessions" / "123.json").write_text(json.dumps({
            "pid": 123, "sessionId": SID, "cwd": f"C:\\x\\demo", "procStart": "1", "pidDomain": "otro:host",
            "status": "idle", "name": WITNESS, "peerToken": WITNESS, "messagingSocketPath": WITNESS,
        }), encoding="utf-8")
        (self.claude / "sessions" / "123.abc.key").write_text(WITNESS, encoding="utf-8")
        self.monitor = Monitor(self.cfg)
        self.monitor.tick()
        self.server = create_server(0, self.monitor)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/api/state", host=None, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = dict(headers or {})
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        for key, value in all_headers.items():
            connection.putheader(key, value)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body, response

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_external_host_rejected(self):
        for path in ("/", "/api/state"):
            status, body, _ = self.request(path, host="evil.example")
            self.assertEqual(status, 403)
            self.assertNotIn(b"project", body)

    def test_cross_origin_rejected(self):
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)

    def test_read_only(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.request(method=method)[0], 405)

    def test_page_served_with_strict_csp(self):
        status, body, response = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"claude-monitor", body)
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))

    def test_witness_not_in_api_or_cache(self):
        status, body, _ = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn(WITNESS.encode(), body)
        self.monitor.save_cache()
        self.monitor.engine.save()
        for path in self.state.iterdir():
            self.assertNotIn(WITNESS, path.read_text(encoding="utf-8"), path.name)

    def test_api_exposes_no_paths_or_tokens(self):
        data = json.loads(self.request()[1])
        text = json.dumps(data)
        for forbidden in ("peerToken", "messagingSocketPath", "cwd", "C:\\\\x"):
            self.assertNotIn(forbidden, text)

    def test_rows_and_totals(self):
        data = json.loads(self.request()[1])
        row = next(r for r in data["rows"] if r["sid"] == SID)
        self.assertEqual(row["project"], "demo")
        self.assertEqual(row["context"], 310105)
        self.assertEqual(row["state"], st.STOPPED if row["closed"] else st.YOUR_TURN)
        self.assertGreater(row["today_sub"], 0)
        self.assertGreater(row["today_main"], 0)
        self.assertEqual(data["summary"]["unknown_models"], ["claude-nuevo-9"])
        unknown = next(r for r in data["rows"] if r["sid"] == SID2)
        self.assertFalse(unknown["model_known"])
        self.assertEqual(unknown["today_main"], 0)
        self.assertGreater(unknown["today_unweighted_tokens"], 0)

    def test_signals_without_hooks(self):
        self.assertFalse(json.loads(self.request()[1])["signals"]["hooks_installed"])

    def test_stopped_loaded_sessions_are_not_hidden(self):
        sids = {r["sid"] for r in json.loads(self.request()[1])["rows"]}
        self.assertEqual(sids, {SID, SID2})

    def test_custom_title_wins_and_never_reaches_notices(self):
        with open(self.main_file(SID), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "TITULO-AI"}) + "\n")
            fh.write(json.dumps({"type": "custom-title", "customTitle": "TITULO-PROPIO"}) + "\n")
        self.monitor.tick()
        data = json.loads(self.request()[1])
        row = next(r for r in data["rows"] if r["sid"] == SID)
        self.assertEqual(row["title"], "TITULO-PROPIO")
        self.assertNotIn("TITULO", json.dumps(data["notices"]))

    def test_rows_sort_by_latest_activity_first(self):
        rows = json.loads(self.request()[1])["rows"]
        ages = [r["last_activity_seconds"] for r in rows]
        self.assertEqual(len(ages), 2)
        self.assertLess(ages[0], ages[1], "fixture sessions must differ for this to prove the order")


class RegistryGone(TempDirTest):
    def test_session_seen_open_then_missing_is_closed(self):
        path = self.claude / "sessions" / "7.json"
        path.write_text(json.dumps({"pid": 7, "sessionId": SID}), encoding="utf-8")
        registry = Registry(self.claude / "sessions", checker=lambda entry: "open")
        registry.refresh()
        self.assertEqual(registry.entries[SID]["liveness"], "open")
        path.unlink()
        registry.refresh()
        self.assertEqual(registry.entries[SID]["liveness"], "closed")
        self.assertNotIn(SID, registry.open_sids())


class KeyFiles(TempDirTest):
    def test_key_files_are_never_opened_and_token_not_copied(self):
        (self.claude / "sessions" / "7.key").write_text("{no es json", encoding="utf-8")
        (self.claude / "sessions" / "7.json").write_text(
            json.dumps({"pid": 7, "sessionId": SID, "peerToken": WITNESS}), encoding="utf-8")
        registry = Registry(self.claude / "sessions", checker=lambda entry: "unknown")
        registry.refresh()
        self.assertNotIn("peerToken", registry.entries[SID])
        self.assertNotIn(WITNESS, json.dumps(registry.entries))


class PortBusy(unittest.TestCase):
    def test_busy_port_exits_with_clear_error_and_does_not_move(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        try:
            result = subprocess.run([sys.executable, str(ROOT / "monitor.py"), "--port", str(port)],
                                    capture_output=True, text=True, timeout=30, cwd=ROOT)
        finally:
            blocker.close()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"puerto {port}", result.stderr)
        self.assertNotIn("http://", result.stdout)


class Views(unittest.TestCase):
    def test_context_never_comes_from_subagent_in_view(self):
        from claude_monitor.config import load_config
        from claude_monitor.hookstore import empty_signal
        cfg = load_config()
        now = time.time()
        session = {"sid": "s", "project": "p", "turn_state": "ended", "last_turn": now - 300,
                   "last_activity": now - 300, "context": 1000, "context_source": "turn", "days": {}}
        view = build_view(session, 3, empty_signal(), {"liveness": "open", "status": "idle", "statusUpdatedAt": 0}, True, now, cfg)
        self.assertEqual(view["context"], 1000)
        self.assertEqual(view["advice"], st.WAIT)
        self.assertEqual(view["subagents"], 3)


if __name__ == "__main__":
    unittest.main()
