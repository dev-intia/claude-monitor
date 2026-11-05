"""Invented fixtures. Nothing here comes from a real transcript."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_monitor.config import load_config

WITNESS = "TESTIGO-SECRETO-7f3a"
SID = "11111111-1111-1111-1111-111111111111"
SID2 = "22222222-2222-2222-2222-222222222222"
PROJECT_DIR = "c--demo-proyecto"
NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def usage(i=1, cw=0, cr=0, out=0, five=None, hour=None):
    data = {"input_tokens": i, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": out}
    if five is not None or hour is not None:
        data["cache_creation"] = {"ephemeral_5m_input_tokens": five or 0, "ephemeral_1h_input_tokens": hour or 0}
    return data


def assistant(mid, ts, u, model="claude-opus-5-5", stop="end_turn", sid=SID, text=WITNESS, agent=None):
    event = {
        "type": "assistant",
        "uuid": mid + "-u",
        "timestamp": iso(ts),
        "sessionId": sid,
        "cwd": "C:\\code\\demo",
        "message": {"id": mid, "model": model, "usage": u, "stop_reason": stop,
                    "content": [{"type": "text", "text": text}]},
    }
    if agent:
        event["agentId"] = agent
        event["isSidechain"] = True
    return event


def user(ts, text=WITNESS, sid=SID, **extra):
    return {"type": "user", "timestamp": iso(ts), "sessionId": sid, "cwd": "C:\\code\\demo",
            "message": {"role": "user", "content": text}, **extra}


def tool_result(ts, sid=SID):
    return user(ts, [{"type": "tool_result", "tool_use_id": "x", "content": WITNESS}], sid)


def compact(ts, post=15000, sid=SID):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": iso(ts), "sessionId": sid,
            "cwd": "C:\\code\\demo", "compactMetadata": {"trigger": "auto", "preTokens": 300000, "postTokens": post}}


def jsonl(events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = self.tmp / "claude"
        self.projects = self.claude / "projects" / PROJECT_DIR
        self.projects.mkdir(parents=True)
        (self.claude / "sessions").mkdir()
        self.state = self.tmp / "state"
        self.cfg = load_config(claude_dir=self.claude, state_dir=self.state)

    def main_file(self, sid=SID) -> Path:
        return self.projects / f"{sid}.jsonl"

    def write_main(self, events, sid=SID, ts=NOW):
        path = self.main_file(sid)
        path.write_text(jsonl(events), encoding="utf-8")
        os.utime(path, (ts, ts))
        return path

    def write_agent(self, events, name="agent-a1", sid=SID, finished=None, ts=NOW):
        folder = self.projects / sid / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.jsonl"
        path.write_text(jsonl(events), encoding="utf-8")
        os.utime(path, (ts, ts))
        meta = {"agentType": "general-purpose", "description": WITNESS}
        if finished is not None:
            meta["finished"] = finished
        (folder / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return path
