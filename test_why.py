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

    def test_enrichment_is_stable_across_windows_and_does_not_store_prompt_text(self):
        usage = {
            "input_tokens": 3,
            "cache_read_input_tokens": -1,
            "output_tokens": 7,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 11,
                "ephemeral_1h_input_tokens": 13,
                "secret": "drop",
            },
            "output_tokens_details": {"thinking_tokens": 2, "secret": "drop"},
            "iterations": [{
                "type": "message", "model": "claude-test",
                "input_tokens": 3, "prompt": "do not retain",
            }],
            "prompt": "do not retain",
            "invalid_tokens": -1,
        }
        rows = [
            {"type": "user", "uuid": "turn-1", "timestamp": "2026-09-02T23:59:00Z",
             "message": {"role": "user", "content": "private prompt"}},
            _claude_row("2026-09-02T23:59:59Z", None, usage, uuid="uuid-a",
                        message={"id": "message-1", "model": "claude-test", "usage": usage},
                        cwd="/work/project", version="v-test"),
            _claude_row("2026-09-03T00:00:01Z", None, {**usage, "output_tokens": 9}, uuid="uuid-b",
                        message={"id": "message-1", "model": "claude-test", "usage": {**usage, "output_tokens": 9}},
                        cwd="/work/project", version="v-test"),
            _claude_row("2026-09-03T00:00:02Z", "advisor-only", {
                "iterations": [{"type": "message", "model": "claude-test"}],
            }, cwd="/work/project", version="v-test"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "project" / "session.jsonl"
            _write_jsonl(path, rows)
            narrow = why.collect_claude(root, self.start, self.end, paths=[path])
            full = why.collect_claude(
                root,
                datetime(2026, 9, 2, tzinfo=timezone.utc),
                self.end,
                paths=[path],
            )

        self.assertEqual([record.call_id for record in narrow], ["message-1", "advisor-only"])
        self.assertEqual(narrow[0].output, 9)
        self.assertEqual(narrow[0].call_id, full[0].call_id)
        self.assertEqual(narrow[0].turn_id, "turn-1")
        self.assertEqual(narrow[0].turn_confidence, "derived")
        self.assertEqual(narrow[0].project_id, "/work/project")
        self.assertEqual(narrow[0].harness_version, "v-test")
        self.assertNotIn("prompt", narrow[0].raw_usage)
        self.assertNotIn("invalid_tokens", narrow[0].raw_usage)
        self.assertIsNone(narrow[0].raw_usage["cache_read_input_tokens"])
        self.assertEqual(narrow[0].raw_usage["cache_creation"], {
            "ephemeral_5m_input_tokens": 11,
            "ephemeral_1h_input_tokens": 13,
        })
        self.assertEqual(narrow[0].raw_usage["iterations"], [{
            "type": "message", "model": "claude-test", "input_tokens": 3,
        }])
        self.assertEqual(narrow[1].raw_usage["iterations"], [{
            "type": "message", "model": "claude-test",
        }])

    def test_nested_cache_write_counts_and_growing_iterations_keep_prior_maxima(self):
        raw={'input_tokens':1,'output_tokens':2,'cache_creation':{
            'ephemeral_5m_input_tokens':3,'ephemeral_1h_input_tokens':4}}
        rows=[_claude_row('2026-09-03T10:00:00Z','req',raw)]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'session.jsonl';_write_jsonl(path,rows)
            record=why.collect_claude(root,self.start,self.end)[0]
        self.assertEqual(record.cache_write,7)
        first={'iterations':[{'type':'message','output_tokens':20}]}
        second={'iterations':[{'type':'message','output_tokens':5},{'type':'advisor_message','output_tokens':10}]}
        merged=why._merge_sanitized_usage(first,second)
        self.assertEqual(merged['iterations'][0]['output_tokens'],20)
        self.assertEqual(len(merged['iterations']),2)

    def test_project_summary_uses_full_project_ids(self):
        records = [
            why.AttributionRecord(
                harness="claude", provider="anthropic", timestamp=self.start,
                session_id="s1", call_id="a", model="m", effort="high",
                project="repo", project_id="/one/repo", entrypoint="cli",
                thread_kind="main", agent="main", fresh_input=1, cache_read=0,
                cache_write=0, output=0, reasoning=0,
            ),
            why.AttributionRecord(
                harness="claude", provider="anthropic", timestamp=self.start,
                session_id="s2", call_id="b", model="m", effort="high",
                project="repo", project_id="/two/repo", entrypoint="cli",
                thread_kind="main", agent="main", fresh_input=1, cache_read=0,
                cache_write=0, output=0, reasoning=0,
            ),
        ]
        names = {row["name"] for row in why.summarize_records(records)["groups"]["project"]}
        self.assertEqual(names, {"/one/repo", "/two/repo"})

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


class ClaudeMetadataReviewTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.usage = {"input_tokens": 1, "output_tokens": 2}

    def collect(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, rows in files.items():
                _write_jsonl(root / name, rows)
            return why.collect_claude(root, self.start, self.end)

    def test_non_string_or_oversized_metadata_is_never_persisted(self):
        secret = {"prompt": "SECRET"}
        row = _claude_row("2026-09-03T12:00:00Z", "req", self.usage, sessionId=secret,
                          entrypoint=secret, effort=["SECRET"], cwd=secret, turnId=secret,
                          attributionAgent=secret, agentId=secret, parentSessionId=secret)
        row["message"]["model"] = secret
        for hostile in (row, dict(row, entrypoint="x" * 300, effort="a\nb")):
            records = self.collect({"project/hostile.jsonl": [hostile]})
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertNotIn("SECRET", repr(record))
            self.assertEqual((record.model, record.effort, record.entrypoint, record.agent), ("unknown",) * 3 + ("main",))
            self.assertEqual(record.session_id, "hostile")
            self.assertIsNone(record.cwd)
            self.assertIsNone(record.parent_session_id)

    def test_later_sparse_row_keeps_earlier_metadata(self):
        full = _claude_row("2026-09-03T12:00:00Z", "req", {"input_tokens": 5, "output_tokens": 1},
                           turnId="turn-1", version="1.2.3", attributionAgent="Explore")
        sparse = {"type": "assistant", "timestamp": "2026-09-03T12:00:05Z", "requestId": "req",
                  "message": {"usage": {"input_tokens": 5, "output_tokens": 9}}}
        for rows in ([full, sparse], [sparse, full]):
            record = self.collect({"project/session-main.jsonl": rows})[0]
            self.assertEqual((record.model, record.effort, record.entrypoint), ("claude-test", "high", "cli"))
            self.assertEqual((record.session_id, record.cwd, record.turn_id), ("session-main", "/work/project-one", "turn-1"))
            self.assertEqual((record.agent, record.harness_version), ("Explore", "1.2.3"))
            self.assertEqual(record.output, 9)
            self.assertEqual(record.timestamp, datetime(2026, 9, 3, 12, 0, 5, tzinfo=timezone.utc))

    def test_copied_request_session_owner_is_earliest_row_then_first_seen(self):
        def files(first_ts, second_ts, first="aaa", second="zzz"):
            return {"p/a.jsonl": [_claude_row(first_ts, "req", self.usage, sessionId=first)],
                    "p/b.jsonl": [_claude_row(second_ts, "req", self.usage, sessionId=second)]}
        early, late = "2026-09-03T12:00:00Z", "2026-09-03T12:00:09Z"
        self.assertEqual(self.collect(files(early, late))[0].session_id, "aaa")
        self.assertEqual(self.collect(files(late, early))[0].session_id, "zzz")
        self.assertEqual(self.collect(files(early, early))[0].session_id, "aaa")
        self.assertEqual(self.collect(files(early, early, "zzz", "aaa"))[0].session_id, "zzz")

    def test_subagent_paths_link_to_parent_session(self):
        row = _claude_row("2026-09-03T12:00:00Z", "req-1", self.usage, agentId="a1")
        row.pop("sessionId")
        journal = [{"type": "system", "timestamp": "2026-09-03T12:00:00Z", "message": {"content": "note"}}]
        files = {
            "proj/parent-uuid/subagents/agent-a1.jsonl": [row],
            "proj/parent-uuid/subagents/workflows/wf_9/agent-a2.jsonl":
                [dict(row, requestId="req-2", sessionId="parent-uuid")],
            "proj/parent-uuid/subagents/workflows/wf_9/journal.jsonl": journal,
            "proj/top-session.jsonl": [dict(row, requestId="req-3", agentId=None)],
        }
        records = {r.call_id: r for r in self.collect(files)}
        self.assertEqual(sorted(records), ["req-1", "req-2", "req-3"])
        for call_id in ("req-1", "req-2"):
            self.assertEqual((records[call_id].session_id, records[call_id].parent_session_id,
                              records[call_id].thread_kind), ("parent-uuid", "parent-uuid", "subagent"))
        self.assertEqual((records["req-3"].session_id, records["req-3"].parent_session_id), ("top-session", None))

    def test_workflow_path_supplies_session_and_parent_without_session_id(self):
        row = _claude_row("2026-09-03T12:00:00Z", "req", self.usage)
        row.pop("sessionId")
        record = self.collect({"proj/sess-x/subagents/workflows/wf_x/agent-a.jsonl": [row]})[0]
        self.assertEqual((record.session_id, record.parent_session_id, record.thread_kind),
                         ("sess-x", "sess-x", "subagent"))

    def test_subagents_named_root_or_ancestor_does_not_confuse_path_derivation(self):
        row = _claude_row("2026-09-03T12:00:00Z", "req", self.usage)
        row.pop("sessionId")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "subagents" / "root"
            _write_jsonl(root / "project" / "top.jsonl", [dict(row, requestId="r1")])
            _write_jsonl(root / "project" / "parent" / "subagents" / "agent.jsonl", [dict(row, requestId="r2")])
            records = {r.call_id: r for r in why.collect_claude(root, self.start, self.end)}
        self.assertEqual((records["r1"].session_id, records["r1"].parent_session_id, records["r1"].thread_kind),
                         ("top", None, "main"))
        self.assertEqual((records["r2"].session_id, records["r2"].parent_session_id), ("parent", "parent"))

    def test_output_final_requires_a_stop_reason_on_some_row(self):
        def row(ts, output, stop):
            r = _claude_row(ts, "req", {"input_tokens": 5, "output_tokens": output})
            r["message"]["stop_reason"] = stop
            return r
        start = row("2026-09-03T12:00:00Z", 3, None)
        done = row("2026-09-03T12:00:05Z", 90, "end_turn")
        cases = ([start], [start, dict(start, timestamp="2026-09-03T12:00:01Z")], [start, done], [done, start])
        result = [self.collect({"p/s.jsonl": rows})[0] for rows in cases]
        self.assertEqual([r.output_final for r in result], [False, False, True, True])
        self.assertEqual(result[2].output, 90)
        self.assertIs(self.collect({"p/s.jsonl": [row("2026-09-03T12:00:00Z", 3, "")]})[0].output_final, False)

    def test_missing_stop_reason_key_is_not_output_final(self):
        row = _claude_row("2026-09-03T12:00:00Z", "req", {"input_tokens": 5, "output_tokens": 3})
        row["message"].pop("stop_reason", None)
        self.assertIs(self.collect({"p/s.jsonl": [row]})[0].output_final, False)

    def test_explicit_parent_session_field_wins_over_path(self):
        row = _claude_row("2026-09-03T12:00:00Z", "req", self.usage, agentId="a1", parentSessionId="explicit")
        record = self.collect({"proj/dir-session/subagents/agent-a1.jsonl": [row]})[0]
        self.assertEqual(record.parent_session_id, "explicit")

if __name__ == "__main__":
    unittest.main()
