#!/usr/bin/env python3
"""Tests for opt-in prompt text extraction; synthetic fixtures only."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import why
from tokenatlas.prompt_text import _is_context_only, extract_prompt, sanitize

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 10, tzinfo=timezone.utc)
TS = "2026-09-03T09:00:00Z"


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


def claude_user(uuid, content, **extra):
    return {"type": "user", "uuid": uuid, "timestamp": TS, "message": {"role": "user", "content": content}, **extra}


def claude_asst(uuid, **extra):
    return {"type": "assistant", "uuid": uuid, "requestId": "r" + uuid, "timestamp": TS, "sessionId": "s1",
            "message": {"role": "assistant", "id": "m" + uuid, "model": "claude-x",
                        "usage": {"input_tokens": 5, "output_tokens": 7}}, **extra}


class ClaudeTests(TmpCase):
    def run_one(self, content, uuid="u1"):
        path = write_jsonl(self.tmp / "s.jsonl", [claude_user(uuid, content)])
        return extract_prompt("claude", str(path), "s1", uuid)

    def test_string_content(self):
        self.assertEqual(self.run_one("fix   the\nbug"), "fix the bug")

    def test_blocks_ignore_tool_result(self):
        self.assertEqual(self.run_one([
            {"type": "tool_result", "tool_use_id": "t", "content": "SECRET OUTPUT"},
            {"type": "text", "text": "hello"}, {"type": "image", "source": {}}, {"type": "text", "text": "world"},
        ]), "hello world")

    def test_wrappers_stripped(self):
        text = ("<system-reminder>\nnoise\n</system-reminder>do it<command-message>m</command-message>"
                "<command-args>a b</command-args><local-command-stdout>out</local-command-stdout>"
                "<local-command-stderr>err</local-command-stderr> now")
        self.assertEqual(self.run_one(text), "do it now")

    def test_only_wrappers_is_none(self):
        self.assertIsNone(self.run_one("<system-reminder>x</system-reminder>\n  "))

    def test_slash_command_keeps_name(self):
        self.assertEqual(self.run_one(
            "<command-message>review</command-message><command-name>/review</command-name><command-args>pr 5</command-args>"),
            "/review")

    def test_no_match_missing_and_directory(self):
        path = write_jsonl(self.tmp / "s.jsonl", [claude_user("u1", "hi")])
        self.assertIsNone(extract_prompt("claude", str(path), "s1", "other"))
        self.assertIsNone(extract_prompt("claude", str(self.tmp / "nope.jsonl"), "s1", "u1"))
        self.assertIsNone(extract_prompt("claude", str(self.tmp), "s1", "u1"))
        self.assertIsNone(extract_prompt("claude", None, "s1", "u1"))

    def test_tool_result_row_is_not_a_prompt(self):
        path = write_jsonl(self.tmp / "s.jsonl", [claude_user("u1", [{"type": "tool_result", "content": "x"}])])
        self.assertIsNone(extract_prompt("claude", str(path), "s1", "u1"))

    def test_explicit_turn_id_fallback(self):
        path = write_jsonl(self.tmp / "s.jsonl", [
            claude_user("u1", "first"), claude_user("u2", "second", turn_id="T9")])
        self.assertEqual(extract_prompt("claude", str(path), "s1", "T9"), "second")

    def test_matches_parser_turn_id(self):
        path = write_jsonl(self.tmp / "s.jsonl", [
            claude_user("u1", "first ask"), claude_asst("a1"),
            claude_user("u2", "second ask"), claude_asst("a2")])
        records = why.collect_claude(self.tmp, START, END, paths=[path])
        self.assertEqual({r.turn_id for r in records}, {"u1", "u2"})
        got = {r.turn_id: extract_prompt("claude", str(path), r.session_id, r.turn_id) for r in records}
        self.assertEqual(got, {"u1": "first ask", "u2": "second ask"})


def codex_meta():
    return {"timestamp": TS, "type": "session_meta", "payload": {"id": "cs1", "model_provider": "openai", "cwd": "/w"}}


def codex_user(text, **payload):
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": "user_message", "message": text, **payload}}


def codex_ctx(**extra):
    return {"timestamp": TS, "type": "turn_context", "payload": {"model": "gpt-x", "cwd": "/w", **extra}}


def codex_tokens(n):
    return {"timestamp": TS, "ordinal": n, "type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 10 * n, "output_tokens": n},
        "total_token_usage": {"input_tokens": 10 * n, "output_tokens": n, "total_tokens": 11 * n}}}}


class CodexTests(TmpCase):
    def check_against_parser(self, rows, expected):
        path = write_jsonl(self.tmp / "rollout-x.jsonl", rows)
        records = why.collect_codex(self.tmp, START, END, paths=[path])
        got = {r.turn_id: extract_prompt("codex", str(path), r.session_id, r.turn_id) for r in records}
        self.assertEqual(got, expected)

    def test_user_event_identity_shape(self):
        self.check_against_parser([
            codex_meta(), codex_user("first prompt", id="ev1"), codex_ctx(), codex_tokens(1),
            codex_user("second  prompt", id="ev2"), codex_ctx(), codex_tokens(2),
        ], {"ev1": "first prompt", "ev2": "second prompt"})

    def test_explicit_turn_id_on_user_event(self):
        self.check_against_parser([
            codex_meta(), codex_user("alpha", turn_id="T1", id="ignored"), codex_ctx(), codex_tokens(1),
            codex_user("beta", turn_id="T2"), codex_ctx(), codex_tokens(2),
        ], {"T1": "alpha", "T2": "beta"})

    def test_explicit_turn_id_on_later_turn_context(self):
        self.check_against_parser([
            codex_meta(), codex_user("gamma"), codex_ctx(turn_id="C1"), codex_tokens(1),
            codex_user("delta"), codex_ctx(turn_id="C2"), codex_tokens(2),
        ], {"C1": "gamma", "C2": "delta"})

    def test_task_started_then_user_message(self):
        rows = [codex_meta(), {"timestamp": TS, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "K1"}},
                codex_user("epsilon"), codex_ctx(), codex_tokens(1)]
        path = write_jsonl(self.tmp / "rollout-x.jsonl", rows)
        self.assertEqual(extract_prompt("codex", str(path), "cs1", "K1"), "epsilon")

    def test_current_format_skips_context_and_mid_turn_steering(self):
        def msg(role, text):
            return {"timestamp": TS, "type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}
        ctx = lambda t: codex_ctx(turn_id=t)
        done = lambda t: {"timestamp": TS, "type": "event_msg", "payload": {"type": "item_completed", "turn_id": t}}
        usage = {"timestamp": TS, "type": "token_usage_record", "payload": {"turn_id": "T1"}}
        self.check_against_parser([
            codex_meta(), msg("developer", "rules"), msg("user", "<environment_context><cwd>/w</cwd></environment_context>"),
            {"timestamp": TS, "type": "world_state", "payload": {}}, ctx("T1"), msg("user", "real prompt"), done("T1"),
            msg("assistant", "thinking"), usage, codex_tokens(1), msg("user", "steer now"), done("T1"),
            msg("assistant", "done"), codex_tokens(2),
            msg("user", "# AGENTS.md instructions for /w\nbody"), msg("user", "second prompt"), ctx("T2"), msg("assistant", "ok"),
            codex_tokens(3),
        ], {"T1": "real prompt", "T2": "second prompt"})

    def test_current_format_prompt_after_turn_start_or_absent(self):
        rows = [codex_meta(), codex_ctx(turn_id="T1"),
                {"timestamp": TS, "type": "response_item", "payload": {"type": "message", "role": "user", "content": "late prompt"}},
                codex_tokens(1)]
        self.check_against_parser(rows, {"T1": "late prompt"})
        path = write_jsonl(self.tmp / "rollout-y.jsonl", [codex_meta(), codex_ctx(turn_id="T9"), codex_tokens(1)])
        self.assertIsNone(extract_prompt("codex", str(path), "cs1", "T9"))

    def test_legacy_assistant_messages_do_not_start_turns(self):
        def msg(role, text):
            return {"timestamp": TS, "type": "response_item", "payload": {"type": "message", "role": role, "id": role + text, "content": text}}
        self.check_against_parser([
            codex_meta(), msg("user", "q1"), codex_ctx(), codex_tokens(1), msg("assistant", "a1"), codex_tokens(2),
        ], {"userq1": "q1"})

    def test_context_only(self):
        for text in ("<environment_context>x</environment_context>", "<user_instructions>y</user_instructions>\n<environment_context/>z</environment_context>",
                     "<permissions instructions>p</permissions instructions>", "# AGENTS.md instructions for /w"):
            self.assertTrue(_is_context_only(text), text)
        self.assertFalse(_is_context_only("fix <b>this</b> please"))
        # Codex goal mode wraps the typed goal in <objective>: that is the prompt, not injected context.
        self.assertFalse(_is_context_only("<objective>Ship the release</objective>"))

    def test_objective_wrapper_is_the_prompt(self):
        def msg(role, text):
            return {"timestamp": TS, "type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}
        self.check_against_parser([
            codex_meta(), {"timestamp": TS, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "T9"}},
            codex_ctx(turn_id="T9"), msg("user", "<environment_context><cwd>/w</cwd></environment_context>"),
            msg("user", "<codex_internal_context source=\"goal\">rules <objective>\n  Ship the release\n</objective> more rules</codex_internal_context>"),
            codex_tokens(1),
        ], {"T9": "Ship the release"})

    def test_missing_and_unknown(self):
        path = write_jsonl(self.tmp / "rollout-x.jsonl", [codex_meta(), codex_user("hi", id="e")])
        self.assertIsNone(extract_prompt("codex", str(path), "cs1", "nope"))
        self.assertIsNone(extract_prompt("codex", str(self.tmp / "gone.jsonl"), "cs1", "e"))
        self.assertIsNone(extract_prompt("codex", str(self.tmp), "cs1", "e"))


def make_opencode_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, version TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, data TEXT NOT NULL);
    """)
    ms = int(datetime(2026, 9, 3, 9, tzinfo=timezone.utc).timestamp() * 1000)
    c.execute("INSERT INTO session VALUES ('ses1','p',NULL,'/w','1',?,?)", (ms, ms))
    c.execute("INSERT INTO message VALUES ('msgU','ses1',?,?,?)", (ms, ms, json.dumps({"role": "user"})))
    c.execute("INSERT INTO message VALUES ('msgA','ses1',?,?,?)", (ms, ms, json.dumps({
        "role": "assistant", "parentID": "msgU", "tokens": {"input": 5, "output": 3, "cache": {"read": 0, "write": 0}},
        "time": {"created": ms, "completed": ms}})))
    for pid, t, mid, data in [
        ("p2", ms + 2, "msgU", {"type": "text", "text": "second half"}),
        ("p1", ms + 1, "msgU", {"type": "text", "text": "first   half"}),
        ("p3", ms + 3, "msgU", {"type": "file", "url": "x"}),
        ("p4", ms + 4, "msgA", {"type": "text", "text": "assistant reply"}),
    ]:
        c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (pid, mid, "ses1", t, json.dumps(data)))
    c.commit()
    c.close()
    return ms


class OpenCodeTests(TmpCase):
    def test_concatenates_text_parts_and_cross_checks_loader(self):
        db = self.tmp / "opencode.db"
        make_opencode_db(db)
        records = why.collect_opencode(db, START, END)
        self.assertEqual([r.turn_id for r in records], ["msgU"])
        r = records[0]
        self.assertEqual(extract_prompt("opencode", str(db), r.session_id, r.turn_id), "first half second half")

    def test_wrong_session_unknown_turn_bad_files(self):
        db = self.tmp / "opencode.db"
        make_opencode_db(db)
        self.assertIsNone(extract_prompt("opencode", str(db), "other", "msgU"))
        self.assertIsNone(extract_prompt("opencode", str(db), "ses1", "nope"))
        self.assertIsNone(extract_prompt("opencode", str(self.tmp / "gone.db"), "ses1", "msgU"))
        junk = self.tmp / "junk.db"
        junk.write_text("not sqlite")
        self.assertIsNone(extract_prompt("opencode", str(junk), "ses1", "msgU"))
        self.assertIsNone(extract_prompt("opencode", str(self.tmp), "ses1", "msgU"))

    def test_does_not_modify_db(self):
        db = self.tmp / "opencode.db"
        make_opencode_db(db)
        before = db.read_bytes()
        extract_prompt("opencode", str(db), "ses1", "msgU")
        self.assertEqual(db.read_bytes(), before)


class PiTests(TmpCase):
    def test_string_and_block_content(self):
        path = write_jsonl(self.tmp / "p.jsonl", [
            {"type": "session", "id": "ps1", "timestamp": TS, "cwd": "/w"},
            {"type": "message", "id": "e1", "timestamp": TS, "message": {"role": "user", "content": "plain  ask"}},
            {"type": "message", "id": "e2", "timestamp": TS, "message": {"role": "assistant", "content": "reply"}},
            {"type": "message", "id": "e3", "timestamp": TS, "message": {"role": "user", "content": [
                {"type": "text", "text": "block"}, {"type": "image", "data": "zz"}, {"type": "text", "text": "ask"}]}},
        ])
        self.assertEqual(extract_prompt("pi", str(path), "ps1", "e1"), "plain ask")
        self.assertEqual(extract_prompt("pi", str(path), "ps1", "e3"), "block ask")
        self.assertIsNone(extract_prompt("pi", str(path), "ps1", "e2"))
        self.assertIsNone(extract_prompt("pi", str(path), "ps1", "zz"))
        self.assertIsNone(extract_prompt("pi", str(self.tmp / "no.jsonl"), "ps1", "e1"))
        self.assertIsNone(extract_prompt("unknown", str(path), "ps1", "e1"))


class SanitizeTests(unittest.TestCase):
    def test_secret_patterns_masked(self):
        cases = [
            "sk-abcdefghijklmnop1234", "sk-ant-api03-AbCdEf_ghIJkl-mnop12", "ghp_" + "a" * 36, "gho_" + "b" * 36,
            "ghs_" + "c" * 36, "github_pat_" + "d" * 30, "AKIAABCDEFGHIJKLMNOP", "ASIAABCDEFGHIJKLMNOP",
            "xoxb-1234567890-abcdefghij", "xoxp-1234567890-abcdefghij", "xoxa-1234567890-abcdefghij",
            "xoxr-1234567890-abcdefghij", "xoxs-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36P",
            "0123456789abcdef0123456789ABCDEF", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A=",
        ]
        for secret in cases:
            with self.subTest(secret=secret):
                self.assertEqual(sanitize(f"use {secret} now"), "use [redacted] now")

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc/def\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(sanitize(f"key:\n{pem}\nthanks"), "key: [redacted] thanks")
        self.assertEqual(sanitize("x -----BEGIN PRIVATE KEY-----\nMIIE"), "x [redacted]")

    def test_key_value_masks_value_only(self):
        for text, want in [
            ("password=hunter2 ok", "password=[redacted] ok"),
            ("my_token: abc123 ok", "my_token: [redacted] ok"),
            ("API_KEY = foo ok", "API_KEY = [redacted] ok"),
            ("api-key:'a b c' ok", "api-key:[redacted] ok"),
            ('{"secret": "s3cr3t val"} ok', '{"secret": [redacted]} ok'),
            ("passwd:x", "passwd:[redacted]"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(sanitize(text), want)

    def test_plain_text_untouched(self):
        self.assertEqual(sanitize("tokens are nice; see /a/very/long/path/without/digits/or/anything/else/here"),
                         "tokens are nice; see /a/very/long/path/without/digits/or/anything/else/here")

    def test_whitespace_and_empty(self):
        self.assertEqual(sanitize("  a \t b\n\n c  "), "a b c")
        for empty in ("", "  \n\t", None):
            self.assertIsNone(sanitize(empty))

    def test_truncates_at_word_boundary(self):
        self.assertEqual(sanitize("alpha beta gamma delta", limit=13), "alpha beta…")
        self.assertEqual(sanitize("alpha beta gamma", limit=16), "alpha beta gamma")
        self.assertEqual(sanitize("alpha beta gamma", limit=10), "alpha beta…")
        self.assertEqual(sanitize("x" * 50, limit=10), "x" * 10 + "…")

    def test_secret_straddling_limit_never_partial(self):
        secret = "sk-" + "A1b2C3d4" * 6
        for limit in range(8, 40):
            out = sanitize(f"token {secret} tail words", limit=limit)
            self.assertNotIn("sk-", out)
            self.assertNotIn("A1b2", out)
        out = sanitize(f"aaaa {secret}", limit=10)
        self.assertEqual(out, "aaaa…")
        self.assertNotIn("[red", sanitize(f"aaaa{'b' * 3} {secret}", limit=10))


if __name__ == "__main__":
    unittest.main()
