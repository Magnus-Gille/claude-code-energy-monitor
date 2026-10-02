#!/usr/bin/env python3
"""Regression tests for Pi and OpenCode attribution adapters."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why


START = datetime(2026, 9, 3, tzinfo=timezone.utc)
END = datetime(2026, 9, 4, tzinfo=timezone.utc)


def write_pi_session(path, session_id, started_at, cwd, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{
        "type": "session", "version": 3, "id": session_id,
        "timestamp": started_at, "cwd": cwd,
    }]
    rows.extend(messages)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def pi_message(entry_id, response_id, timestamp, usage, provider="test", model="model"):
    return {
        "type": "message", "id": entry_id, "timestamp": timestamp,
        "message": {
            "role": "assistant", "provider": provider, "model": model,
            "responseId": response_id, "usage": usage,
            "content": "PRIVATE ASSISTANT TEXT",
        },
    }


def create_opencode_db(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, version TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
    """)
    return connection


class PiAttributionTests(unittest.TestCase):
    def test_maps_usage_and_keeps_original_session_when_a_fork_sorts_first(self):
        usage = {
            "input": 10, "cacheRead": 20, "cacheWrite": 3,
            "output": 8, "reasoning": 2, "totalTokens": 41,
            "secret": "DROP",
        }
        copied = pi_message("entry", "response", "2026-09-03T10:00:00Z", usage)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pi_session(root / "a-fork.jsonl", "fork", "2026-09-03T09:00:00Z", "/work/fork", [copied])
            write_pi_session(root / "z-original.jsonl", "original", "2026-09-02T09:00:00Z", "/work/original", [copied])
            records = why.collect_pi(root, START, END)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.session_id, "original")
        self.assertEqual(record.project_id, "/work/original")
        self.assertEqual((record.fresh_input, record.cache_read, record.cache_write), (10, 20, 3))
        self.assertEqual((record.output, record.reasoning, record.total_tokens), (8, 2, 41))
        self.assertEqual(record.raw_usage, {
            "input": 10, "cacheRead": 20, "cacheWrite": 3,
            "output": 8, "reasoning": 2, "totalTokens": 41,
        })
        self.assertNotIn("PRIVATE", json.dumps(record.raw_usage))

    def test_response_identity_is_provider_scoped_and_fallback_is_synthetic(self):
        usage = {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages = [
                pi_message("one", "same", "2026-09-03T10:00:00Z", usage, provider="a"),
                pi_message("two", "same", "2026-09-03T10:01:00Z", usage, provider="b"),
                pi_message("fallback", "", "2026-09-03T10:02:00Z", usage, provider="a"),
            ]
            write_pi_session(root / "session.jsonl", "session", "2026-09-03T09:00:00Z", "/work/app", messages)
            records = why.collect_pi(root, START, END)

        self.assertEqual(len(records), 3)
        self.assertEqual({r.provider for r in records if r.call_id == "same"}, {"a", "b"})
        self.assertEqual(sum(r.id_synthetic for r in records), 1)


class OpenCodeAttributionTests(unittest.TestCase):
    def test_reads_completed_assistant_calls_without_retaining_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            connection = create_opencode_db(db)
            connection.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                ("child", "project", "parent", "/work/app", "1.18.32", 1, 2),
            )
            data = {
                "role": "assistant", "providerID": "openai", "modelID": "gpt-test",
                "agent": "explore", "variant": "high",
                "time": {"created": 1788429600000, "completed": 1788429601000},
                "path": {"cwd": "/work/app", "root": "/work"},
                "tokens": {
                    "input": 10, "output": 5, "reasoning": 3,
                    "cache": {"read": 20, "write": 2}, "total": 37,
                },
                "content": "PRIVATE ASSISTANT TEXT",
            }
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?,?)",
                ("message-1", "child", 1788429600000, 1788429601000, json.dumps(data)),
            )
            connection.commit()
            connection.close()

            records = why.collect_opencode(db, START, END)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.provider, "openai")
        self.assertEqual(record.model, "gpt-test")
        self.assertEqual(record.effort, "high")
        self.assertEqual(record.agent, "explore")
        self.assertEqual(record.thread_kind, "subagent")
        self.assertEqual(record.parent_session_id, "parent")
        self.assertEqual(record.harness_version, "1.18.32")
        self.assertEqual(record.project_id, "/work/app")
        self.assertEqual((record.fresh_input, record.cache_read, record.cache_write), (10, 20, 2))
        self.assertEqual(record.output, 8)
        self.assertEqual(record.reasoning, 3)
        self.assertEqual(record.total_tokens, 40)
        self.assertNotIn("PRIVATE", json.dumps(record.raw_usage))


if __name__ == "__main__":
    unittest.main()
