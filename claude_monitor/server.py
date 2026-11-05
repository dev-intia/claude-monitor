"""Local HTTP server: 127.0.0.1 and local Host header only. Reads, except one POST that moves windows."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import jump
from .hookstore import HookStore, hooks_installed
from .notify import NotificationEngine
from .registry import Registry
from .snapshot import build_snapshot, compute_views
from .transcripts import TranscriptStore, new_session

HOST = "127.0.0.1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PAGE = Path(__file__).with_name("index.html")
MAX_JUMP_BODY = 1024
CACHE_VERSION = 3  # 2: titles. 3: cwd, entrypoint. Offsets in older caches are past lines they never kept.
SAVE_EVERY_SECONDS = 30
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; worker-src blob:; base-uri 'none'; form-action 'none'"
)


def host_is_local(header: str | None) -> bool:
    """Accept any port (an SSH tunnel may remap it), but only local names."""
    if not header:
        return False
    try:
        return (urlsplit("//" + header.strip()).hostname or "").lower() in LOCAL_HOSTS
    except ValueError:
        return False


class Monitor:
    """Owns the data; the background thread refreshes it, handlers only read it."""

    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg.state_dir, exist_ok=True)
        self.cache_path = Path(cfg.state_dir) / "cache.json"
        self.store = TranscriptStore(cfg.projects_dir, cfg.first_scan_days)
        self.registry = Registry(cfg.sessions_dir)
        self.hooks = HookStore(cfg.hook_events_path)
        self.engine = NotificationEngine(Path(cfg.state_dir) / "notifications.json", cfg.keep_notices)
        self._load_cache()
        self.snapshot_json = json.dumps({"scan": {"ready": False}, "rows": []}).encode("utf-8")
        self.desktop = None  # Tests replace it; None means the real Windows desktop.
        self.jump_log = deque(maxlen=50)  # Memory only: which route each jump took, and how fast.
        self.first_scan_seconds = None
        self._last_save = 0.0
        self._dirty = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("version") == CACHE_VERSION:
            self.store.restore(data)

    def save_cache(self) -> None:
        temporary = str(self.cache_path) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump({"version": CACHE_VERSION, **self.store.dump()}, fh)
        os.replace(temporary, self.cache_path)
        self._dirty = False

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        started = time.time()
        self.registry.refresh()
        self.hooks.refresh()
        open_sids = self.registry.open_sids()
        self._dirty |= self.store.scan(now, open_sids)
        # An open session may have written nothing yet: it still belongs on screen.
        for sid in open_sids:
            if sid not in self.store.sessions:
                entry = self.registry.entries[sid]
                session = new_session(sid, "")
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    session["project"] = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                self.store.sessions[sid] = session
        views = compute_views(self.store, self.registry, self.hooks, self.cfg, now)
        emitted = self.engine.evaluate(views, now, self.cfg)
        if emitted or now - self._last_save >= SAVE_EVERY_SECONDS:
            self.engine.save()
        if self.first_scan_seconds is None:
            self.first_scan_seconds = time.time() - started
        if self._dirty and now - self._last_save >= SAVE_EVERY_SECONDS:
            self.store.prune(now)
            self.save_cache()
        if now - self._last_save >= SAVE_EVERY_SECONDS:
            self._last_save = now
        scan_info = {
            "ready": True,
            "files": len(self.store.files),
            "first_scan_seconds": round(self.first_scan_seconds, 1),
            "tick_ms": round((time.time() - started) * 1000),
            "bad_lines": self.store.bad_lines,
        }
        snapshot = build_snapshot(
            self.store, self.registry, self.hooks, self.engine, self.cfg, now,
            hooks_installed=hooks_installed(self.cfg.settings_path), scan_info=scan_info, views=views,
        )
        self.snapshot_json = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")

    def jump(self, sid: str) -> dict:
        """Takes the entry from the registry: the browser never supplies a path."""
        entry = self.registry.entries.get(sid)
        if entry is not None:
            entry = {**entry, "liveness": self.registry.checker(entry)}  # The list may be seconds old.
        if entry is not None and entry["liveness"] == "open":
            result = jump.go(sid, entry, self.cfg.ide_dir, self.desktop, self.cfg.windows_dir)
        else:
            result = jump.reopen(sid, self.store.sessions.get(sid), self.cfg.ide_dir, self.desktop, self.cfg.windows_dir)
        trace = result.pop("trace", None)
        if trace:
            self.jump_log.append(trace)
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # A bad tick must not kill the panel.
                print(f"Error al actualizar: {type(error).__name__}: {error}", flush=True)
            self._stop.wait(self.cfg.refresh_seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        self.engine.save()
        self.save_cache()


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-monitor"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10  # A client that promises a body and never sends it does not keep a thread.

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _guard(self) -> bool:
        if not host_is_local(self.headers.get("Host")):
            self._send(403, b"Host no permitido\n")
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null" and not host_is_local(urlsplit(origin).netloc):
            self._send(403, b"Origen no permitido\n")
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, self.server.monitor.snapshot_json, "application/json; charset=utf-8")
        elif path == "/api/jumps":
            self._send(200, json.dumps(list(self.server.monitor.jump_log), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"No encontrado\n")

    do_HEAD = do_GET

    def _read_only(self) -> None:
        self.close_connection = True
        self._send(405, b"El panel es de solo lectura\n")

    def _same_origin(self) -> bool:
        """Stricter than _guard: another local server on another port may not move windows."""
        origin = self.headers.get("Origin")
        try:
            netloc = urlsplit(origin).netloc.lower() if origin else ""
        except ValueError:
            return False
        return bool(netloc) and netloc == (self.headers.get("Host") or "").strip().lower()

    def do_POST(self) -> None:
        # Read before any answer: closing with unread data makes Windows reset the
        # connection, and the client may lose the response explaining the rejection.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        body = self.rfile.read(length) if 0 <= length <= MAX_JUMP_BODY else None
        if urlsplit(self.path).path != "/api/jump":
            self._read_only()
            return
        self.close_connection = True
        if not self._guard():
            return
        if not self._same_origin():
            self._send(403, b"Origen no permitido\n")
            return
        # A cross-site form cannot send this type without a preflight, which gets 405.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._send(415, b"Se espera JSON\n")
            return
        try:
            sid = json.loads(body).get("sid")
        except (TypeError, ValueError, AttributeError):
            sid = None  # No body, an oversized one, or not a JSON object.
        if not isinstance(sid, str) or not jump.SESSION_ID.fullmatch(sid):
            self._send(400, b"Peticion no valida\n")
            return
        result = self.server.monitor.jump(sid)
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _read_only


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let two servers share a port silently.
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port: int, monitor: Monitor | None = None) -> PanelServer:
    """Bind the socket; raises OSError when the port is busy."""
    server = PanelServer((HOST, port), Handler)
    server.monitor = monitor
    return server
