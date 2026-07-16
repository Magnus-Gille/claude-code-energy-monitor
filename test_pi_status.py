#!/usr/bin/env python3
"""Regression tests for Pi harness session accounting."""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pi_status
import pi_stepcount


def _write_session(path, session_id, events, started_at=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{
        "type": "session", "version": 3, "id": session_id,
        "timestamp": started_at or events[0][1], "cwd": "/tmp/project",
    }]
    for entry_id, timestamp, usage, model in events:
        rows.append({
            "type": "message", "id": entry_id, "parentId": None,
            "timestamp": timestamp,
            "message": {
                "role": "assistant", "provider": "test", "model": model,
                "responseId": f"response-{entry_id}",
                "usage": usage, "stopReason": "stop",
            },
        })
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _local_timestamp(day, hour=12):
    return datetime.combine(day, datetime.min.time()).astimezone().replace(
        hour=hour
    ).isoformat()


class PiStatusTests(unittest.TestCase):
    def test_aggregates_calls_by_call_date_and_deduplicates_cloned_entries(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        usage_today = {
            "input": 100, "output": 20, "cacheRead": 300,
            "cacheWrite": 40, "reasoning": 5, "totalTokens": 460,
        }
        usage_yesterday = {
            "input": 10, "output": 2, "cacheRead": 30,
            "cacheWrite": 4, "totalTokens": 46,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copied = ("copyme01", _local_timestamp(yesterday), usage_yesterday, "old-model")
            # The fork sorts first to verify source-session attribution is not
            # dependent on path order. Its copied response still belongs to the
            # earlier original session.
            _write_session(
                root / "a-project" / "fork.jsonl", "fork",
                [copied, ("newcall1", _local_timestamp(today), usage_today, "new-model")],
                started_at=_local_timestamp(today, 8),
            )
            _write_session(
                root / "z-project" / "original.jsonl", "original", [copied],
                started_at=_local_timestamp(yesterday, 8),
            )

            payload = pi_status.build_payload(root, None)

        self.assertEqual(payload["day"]["fresh_input"], 100)
        self.assertEqual(payload["day"]["cached_input"], 300)
        self.assertEqual(payload["day"]["cache_write"], 40)
        self.assertEqual(payload["day"]["output"], 20)
        self.assertEqual(payload["day"]["reasoning_output"], 5)
        self.assertEqual(payload["day"]["total_tokens"], 460)
        self.assertEqual(payload["day"]["sessions"], 1)
        self.assertEqual(payload["week"]["total_tokens"], 506)
        self.assertEqual(payload["week"]["sessions"], 2)
        self.assertEqual(payload["active"]["model"], "new-model")
        self.assertGreater(payload["day"]["energy_mwh"], 0)

    def test_explicit_file_selects_active_model(self):
        today = date.today()
        usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "older.jsonl"
            newer = root / "newer.jsonl"
            _write_session(older, "old", [("oldcall", _local_timestamp(today, 10), usage, "old-model")])
            _write_session(newer, "new", [("newcall", _local_timestamp(today, 12), usage, "new-model")])
            payload = pi_status.build_payload(root, older)

        self.assertEqual(payload["active"]["model"], "old-model")
        self.assertEqual(payload["day"]["total_tokens"], 4)

    def test_utc_timestamp_is_grouped_by_local_date(self):
        local_time = datetime.combine(date.today(), datetime.min.time()).astimezone().replace(
            hour=0, minute=30
        )
        as_utc = local_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        parsed = pi_status.parse_timestamp(as_utc)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.date(), date.today())

    def test_stepcount_renders_prebuilt_payload_without_rescanning(self):
        period = {"total_tokens": 1200, "sessions": 2, "energy_mwh": 500}
        payload = {"day": period, "week": period, "month": period}
        output = pi_stepcount.view_all(payload, energy=True)
        table = pi_stepcount.view_table(payload, energy=False)
        self.assertIn("⚡ Pi", output)
        self.assertIn("1k tokens", output)
        self.assertIn("sess", table)

    def test_ignores_errors_without_usage_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "broken.jsonl"
            path.write_text(
                '{"type":"session","id":"s","timestamp":"2026-01-01T00:00:00Z","cwd":"/tmp"}\n'
                'not json\n'
                '{"type":"message","id":"e","timestamp":"2026-01-01T00:00:01Z",'
                '"message":{"role":"assistant","model":"m","stopReason":"error",'
                '"usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0}}}\n'
            )
            payload = pi_status.build_payload(root, None)

        self.assertEqual(payload["month"]["total_tokens"], 0)
        self.assertEqual(payload["month"]["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
