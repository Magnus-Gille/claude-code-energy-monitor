#!/usr/bin/env python3
"""Regression tests for the stateless cross-harness attribution command."""

import json
import os
import tempfile
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import why


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _claude_row(timestamp, request_id, usage, **overrides):
    row = {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "sessionId": "session-main",
        "entrypoint": "cli",
        "effort": "high",
        "cwd": "/work/project-one",
        "message": {"model": "claude-test", "usage": usage},
    }
    row.update(overrides)
    return row


class ClaudeAttributionTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def test_deduplicates_request_by_maximum_of_each_usage_field(self):
        early = {
            "input_tokens": 2,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 30,
            "output_tokens": 4,
        }
        final = {
            "input_tokens": 2,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 30,
            "output_tokens": 254,
            "output_tokens_details": {"thinking_tokens": 40},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_jsonl(root / "project" / "session.jsonl", [
                _claude_row("2026-09-03T10:00:00Z", "req-1", early),
                _claude_row("2026-09-03T10:00:01Z", "req-1", final),
            ])
            records = why.collect_claude(root, self.start, self.end)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.fresh_input, 2)
        self.assertEqual(record.cache_read, 100)
        self.assertEqual(record.cache_write, 30)
        self.assertEqual(record.output, 254)
        self.assertEqual(record.reasoning, 40)
        self.assertEqual(record.total_tokens, 386)

    def test_attributes_subagent_and_uses_response_timestamp(self):
        usage = {"input_tokens": 1, "output_tokens": 2}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "project" / "session" / "subagents" / "agent-a1.jsonl"
            _write_jsonl(path, [
                _claude_row(
                    "2026-09-03T12:00:00Z", "req-sub", usage,
                    agentId="a1", attributionAgent="Explore",
                    sessionId="parent-session", entrypoint="sdk-cli",
                    cwd="/work/specific-project",
                )
            ])
            records = why.collect_claude(root, self.start, self.end)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.thread_kind, "subagent")
        self.assertEqual(record.agent, "Explore")
        self.assertEqual(record.entrypoint, "sdk-cli")
        self.assertEqual(record.project, "specific-project")
        self.assertEqual(record.session_id, "parent-session")

    def test_subagent_without_cwd_uses_project_directory_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "project-from-path" / "session" / "subagents" / "agent-a1.jsonl"
            _write_jsonl(path, [
                _claude_row(
                    "2026-09-03T12:00:00Z", "req-no-cwd",
                    {"input_tokens": 1, "output_tokens": 2},
                    agentId="a1", cwd=None,
                )
            ])
            records = why.collect_claude(root, self.start, self.end)

        self.assertEqual(records[0].project, "project-from-path")

    def test_filters_by_event_time_and_ignores_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "project" / "session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                "not json\n"
                + json.dumps({"timestamp": "2026-09-03T00:00:00Z", "message": "bad"}) + "\n"
                + json.dumps(_claude_row(
                    "2026-09-02T23:59:59Z", "too-early",
                    {"input_tokens": 10, "output_tokens": 1},
                )) + "\n"
                + json.dumps(_claude_row(
                    "2026-09-03T00:00:00Z", "included",
                    {"input_tokens": 20, "output_tokens": 2},
                )) + "\n"
            )
            records = why.collect_claude(root, self.start, self.end)

        self.assertEqual([record.call_id for record in records], ["included"])

    def test_summary_groups_are_ranked_and_shares_use_all_token_types(self):
        records = [
            why.AttributionRecord(
                harness="claude", provider="anthropic", timestamp=self.start,
                session_id="s1", call_id="a", model="m1", effort="high",
                project="p1", entrypoint="cli", thread_kind="main", agent="main",
                fresh_input=10, cache_read=20, cache_write=30, output=40,
                reasoning=5,
            ),
            why.AttributionRecord(
                harness="claude", provider="anthropic", timestamp=self.start,
                session_id="s2", call_id="b", model="m2", effort="low",
                project="p2", entrypoint="cli", thread_kind="subagent", agent="Explore",
                fresh_input=1, cache_read=2, cache_write=3, output=4,
                reasoning=1,
            ),
        ]
        summary = why.summarize_records(records, limit=5)

        self.assertEqual(summary["totals"]["tokens"], 110)
        self.assertEqual(summary["totals"]["reasoning"], 6)
        self.assertEqual(summary["groups"]["project"][0]["name"], "p1")
        self.assertAlmostEqual(summary["groups"]["project"][0]["share_pct"], 90.91)

    def test_reads_statusline_day_for_coverage_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp)
            _write_jsonl(claude_dir / "statusline_history.jsonl", [{
                "date": "2026-09-03", "input": 10, "output": 20,
                "cache_read": 30, "cache_write": 40,
            }])
            totals = why.load_claude_monitor_day(claude_dir, "2026-09-03")

        self.assertEqual(totals, {
            "fresh_input": 10, "cache_read": 30,
            "cache_write": 40, "output": 20, "tokens": 100,
        })

    @unittest.skipUnless(hasattr(time_module, "tzset"), "requires POSIX timezone support")
    def test_local_dates_use_the_offset_for_the_selected_date(self):
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Stockholm"
            time_module.tzset()
            winter, _, _ = why._window(SimpleNamespace(date="2026-01-15", hours=None))
            summer, _, _ = why._window(SimpleNamespace(date="2026-07-15", hours=None))
            parsed_winter = why.parse_iso_timestamp("2026-01-15T12:00:00")
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time_module.tzset()

        self.assertEqual(winter.utcoffset(), timedelta(hours=1))
        self.assertEqual(summer.utcoffset(), timedelta(hours=2))
        self.assertEqual(parsed_winter.utcoffset(), timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
