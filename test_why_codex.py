#!/usr/bin/env python3
"""Regression tests for Codex per-turn attribution."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import why


def _write_rollout(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _meta(session_id="session-1", source="cli", originator="codex-tui"):
    return {
        "timestamp": "2026-09-03T08:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": session_id, "model_provider": "openai", "source": source,
            "originator": originator, "cwd": "/work/initial",
        },
    }


def _context(timestamp, model, effort, cwd):
    return {
        "timestamp": timestamp, "type": "turn_context",
        "payload": {"model": model, "effort": effort, "cwd": cwd},
    }


def _tokens(timestamp, ordinal, last, cumulative=None):
    return {
        "timestamp": timestamp, "ordinal": ordinal, "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": cumulative or {
                    "input_tokens": 999999, "output_tokens": 999999,
                },
            },
        },
    }


class CodexAttributionTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def test_uses_per_turn_deltas_and_normalizes_token_semantics(self):
        rows = [
            _meta(),
            _context("2026-09-03T09:00:00Z", "gpt-test", "high", "/work/project"),
            _tokens("2026-09-03T09:01:00Z", 10, {
                "input_tokens": 1000, "cached_input_tokens": 600,
                "cache_write_input_tokens": 100, "output_tokens": 80,
                "reasoning_output_tokens": 50,
            }, {
                "input_tokens": 1000, "cached_input_tokens": 600,
                "cache_write_input_tokens": 100, "output_tokens": 80,
                "reasoning_output_tokens": 50,
            }),
            _tokens("2026-09-03T09:02:00Z", 11, {
                "input_tokens": 200, "cached_input_tokens": 50,
                "cache_write_input_tokens": 25, "output_tokens": 20,
                "reasoning_output_tokens": 10,
            }, {
                "input_tokens": 1200, "cached_input_tokens": 650,
                "cache_write_input_tokens": 125, "output_tokens": 100,
                "reasoning_output_tokens": 60,
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-one.jsonl", rows)
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual(len(records), 2)
        self.assertEqual(sum(row.fresh_input for row in records), 425)
        self.assertEqual(sum(row.cache_read for row in records), 650)
        self.assertEqual(sum(row.cache_write for row in records), 125)
        self.assertEqual(sum(row.output for row in records), 100)
        self.assertEqual(sum(row.reasoning for row in records), 60)
        self.assertEqual(sum(row.total_tokens for row in records), 1300)

    def test_per_turn_deltas_reconcile_with_final_cumulative_total(self):
        first = {
            "input_tokens": 100, "cached_input_tokens": 50,
            "cache_write_input_tokens": 10, "output_tokens": 20,
            "reasoning_output_tokens": 5,
        }
        second = {
            "input_tokens": 200, "cached_input_tokens": 75,
            "cache_write_input_tokens": 25, "output_tokens": 40,
            "reasoning_output_tokens": 15,
        }
        final = {field: first[field] + second[field] for field in first}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-reconcile.jsonl"
            _write_rollout(path, [
                _meta(),
                _tokens("2026-09-03T09:01:00Z", 1, first, first),
                # Codex may emit the same token snapshot again during a later
                # UI/status event. This is not another model call.
                _tokens("2026-09-03T09:01:01Z", 2, first, first),
                _tokens("2026-09-03T09:02:00Z", 3, second, final),
            ])
            result = why.reconcile_codex_rollout(path)

        self.assertTrue(result["matches"])
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["summed"], result["final"])

    def test_reconciliation_accounts_for_cumulative_counter_reset(self):
        first = {"input_tokens": 100, "output_tokens": 10}
        second = {"input_tokens": 50, "output_tokens": 5}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-reset.jsonl"
            _write_rollout(path, [
                _meta(),
                _tokens("2026-09-03T09:01:00Z", 1, first, first),
                # A resumed/restarted segment begins its cumulative counter at zero.
                _tokens("2026-09-03T10:01:00Z", 2, second, second),
            ])
            result = why.reconcile_codex_rollout(path)

        self.assertTrue(result["matches"])
        self.assertEqual(result["segments"], 2)
        self.assertEqual(result["final"]["input_tokens"], 150)

    def test_attributes_each_call_from_latest_context_and_subagent_metadata(self):
        source = {"subagent": {"thread_spawn": {
            "parent_thread_id": "parent", "depth": 1,
            "agent_nickname": "Sagan", "agent_role": "worker",
        }}}
        rows = [
            _meta(source=source, originator="Codex Desktop"),
            _context("2026-09-03T09:00:00Z", "gpt-a", "low", "/work/first"),
            _tokens("2026-09-03T09:01:00Z", 1, {
                "input_tokens": 10, "output_tokens": 2,
            }, {"input_tokens": 10, "output_tokens": 2}),
            _context("2026-09-03T10:00:00Z", "gpt-b", "xhigh", "/work/second"),
            _tokens("2026-09-03T10:01:00Z", 2, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_rollout(root / "rollout-context.jsonl", rows)
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual([row.model for row in records], ["gpt-a", "gpt-b"])
        self.assertEqual([row.effort for row in records], ["low", "xhigh"])
        self.assertEqual([row.project for row in records], ["first", "second"])
        self.assertTrue(all(row.thread_kind == "subagent" for row in records))
        self.assertTrue(all(row.agent == "Sagan" for row in records))
        self.assertTrue(all(row.entrypoint == "Codex Desktop" for row in records))

    def test_filters_on_event_time_in_resumed_old_rollout_and_ignores_noise(self):
        rows = [
            _meta(),
            {"timestamp": "2026-09-03T00:00:00Z", "type": "event_msg", "payload": "bad"},
            _context("2026-09-02T22:00:00Z", "gpt-test", "medium", "/work/project"),
            _tokens("2026-09-02T23:59:59Z", 1, {
                "input_tokens": 100, "output_tokens": 10,
            }, {"input_tokens": 100, "output_tokens": 10}),
            {"timestamp": "2026-09-03T00:00:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": None}},
            _tokens("2026-09-03T00:00:01Z", 2, {
                "input_tokens": 200, "output_tokens": 20,
            }, {"input_tokens": 300, "output_tokens": 30}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2025" / "01" / "01" / "rollout-old.jsonl"
            _write_rollout(path, rows)
            with path.open("a") as handle:
                handle.write("not json\n")
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].total_tokens, 220)
        self.assertIn(":2:", records[0].call_id)

    def test_result_order_is_deterministic_for_equal_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for session_id in ("z-session", "a-session"):
                _write_rollout(root / f"rollout-{session_id}.jsonl", [
                    _meta(session_id=session_id),
                    _context("2026-09-03T09:00:00Z", "gpt-test", "low", "/work/p"),
                    _tokens("2026-09-03T09:01:00Z", 1, {
                        "input_tokens": 10, "output_tokens": 1,
                    }),
                ])
            records = why.collect_codex(root, self.start, self.end)

        self.assertEqual([row.session_id for row in records], ["a-session", "z-session"])

    def test_call_ids_survive_windows_and_copied_rollouts(self):
        rows = [
            _meta(),
            _context("2026-09-02T23:00:00Z", "gpt-a", "high", "/work/project"),
            _tokens("2026-09-02T23:59:59Z", 41, {
                "input_tokens": 10, "output_tokens": 2,
            }, {"input_tokens": 10, "output_tokens": 2}),
            _tokens("2026-09-03T00:00:01Z", 42, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "rollout-original.jsonl"
            copied = root / "renamed-copy.jsonl"
            _write_rollout(first, rows)
            copied.write_text(first.read_text())
            narrow = why.collect_codex(root, self.start, self.end, paths=[first, copied])
            all_time = why.collect_codex(
                root,
                datetime(2026, 9, 2, tzinfo=timezone.utc),
                self.end,
                paths=[first, copied],
            )

        self.assertEqual(len(narrow), 1)
        self.assertEqual(narrow[0].call_id, "session-1:42:token_count")
        self.assertEqual(narrow[0].call_id, all_time[1].call_id)

    def test_missing_context_fields_reset_and_turn_metadata_is_derived(self):
        rows = [
            _meta(),
            {"timestamp": "2026-09-03T08:59:00Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "task-1"}},
            _context("2026-09-03T09:00:00Z", "gpt-a", "high", "/work/one"),
            _tokens("2026-09-03T09:01:00Z", 1, {
                "input_tokens": 10, "output_tokens": 2,
            }),
            {"timestamp": "2026-09-03T09:02:00Z", "type": "turn_context",
             "payload": {"cwd": "/work/two"}},
            _tokens("2026-09-03T09:03:00Z", 2, {
                "input_tokens": 20, "output_tokens": 4,
            }, {"input_tokens": 30, "output_tokens": 6}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rollout-context-reset.jsonl"
            _write_rollout(path, rows)
            records = why.collect_codex(root, self.start, self.end, paths=[path])

        self.assertEqual(records[0].turn_id, "task-1")
        self.assertEqual(records[0].turn_confidence, "derived")
        self.assertEqual(records[0].model, "gpt-a")
        self.assertEqual(records[1].model, "unknown")
        self.assertEqual(records[1].effort, "unknown")

    def test_idless_session_meta_uses_synthetic_identity_per_event(self):
        def idless_rollout(timestamp, output):
            return [
                {"timestamp": "2026-09-03T08:00:00Z", "type": "session_meta",
                 "payload": {"model_provider": "openai"}},
                _tokens(timestamp, None, {"input_tokens": 1, "output_tokens": output},
                        {"input_tokens": output, "output_tokens": output}),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "rollout-a.jsonl"
            second = root / "rollout-b.jsonl"
            _write_rollout(first, idless_rollout("2026-09-03T08:01:00Z", 2))
            _write_rollout(second, idless_rollout("2026-09-03T08:02:00Z", 3))
            records = why.collect_codex(root, self.start, self.end, paths=[first, second])

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.id_synthetic for record in records))
        self.assertNotEqual(records[0].call_id, records[1].call_id)


class CodexHostileMetadataTests(unittest.TestCase):
    def test_non_string_metadata_is_never_persisted(self):
        secret = {"prompt": "SECRET"}
        meta = _meta(session_id=secret, source={"subagent": {"thread_spawn": {
            "agent_nickname": secret, "parent_thread_id": secret}}}, originator=secret)
        meta["payload"].update(model_provider=secret, cwd=secret)
        counts = {"input_tokens": 10, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 2}
        rows = [meta, {"timestamp": "2026-09-03T10:00:00Z", "type": "turn_context",
                       "payload": {"model": secret, "effort": secret, "cwd": secret, "turn_id": secret}},
                _tokens("2026-09-03T10:00:01Z", 1, counts, counts)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-x.jsonl"
            _write_rollout(path, rows)
            records = why.collect_codex(Path(tmp), datetime(2026, 9, 3, tzinfo=timezone.utc),
                                        datetime(2026, 9, 4, tzinfo=timezone.utc))
        self.assertEqual(len(records), 1)
        self.assertNotIn("SECRET", repr(records[0]))
        record = records[0]
        self.assertEqual((record.provider, record.model, record.effort, record.entrypoint),
                         ("openai", "unknown", "unknown", "unknown"))

if __name__ == "__main__":
    unittest.main()
