"""Opt-in prompt previews: read the user prompt behind a turn id from the local source log.

Only reading and sanitizing live here; storage and integration are elsewhere.
Nothing in this module raises for bad input: unreadable or unknown -> None.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .why import (
    _codex_event_turn_id, _codex_user_event_identity, _explicit_turn_id,
    _first_text, _is_genuine_user_row, _mapping, _meta_text,
)

REDACTED = "[redacted]"
_SECRET_PATTERNS = [re.compile(p, flags) for p, flags in (
    (r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S),
    (r"\bsk-[A-Za-z0-9_-]{8,}", 0),
    (r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})", 0),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", 0),
    (r"\bxox[abprs]-[A-Za-z0-9-]{8,}", 0),
    (r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", 0),
)]
_KEY_VALUE = re.compile(
    r"""(?i)(\w*(?:password|passwd|secret|token|api[_-]?key)\w*["']?\s*[=:]\s*)("[^"]*"|'[^']*'|\S+)""")
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
# base64-ish run: >=40 chars, must mix letters and digits so long plain words/paths are spared
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/_=-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{40,}={0,2}")
_MARKER_PARTIAL = re.compile(r"\[(?:r(?:e(?:d(?:a(?:c(?:t(?:e(?:d)?)?)?)?)?)?)?)?$")
_WRAPPERS = re.compile(
    r"<(system-reminder|command-message|command-args|local-command-stdout|local-command-stderr)\b[^>]*>.*?</\1>",
    re.S)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)


def sanitize(text: object, limit: int = 200) -> str | None:
    """Collapse whitespace, mask secrets, then truncate at a word boundary with an ellipsis."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HEX_RUN.sub(REDACTED, text)
    text = _B64_RUN.sub(REDACTED, text)
    if len(text) <= limit:
        return text
    cut = text[:max(0, limit)]
    if text[len(cut)] != " ":
        space = cut.rfind(" ")
        if space > len(cut) // 2:
            cut = cut[:space]
    cut = _MARKER_PARTIAL.sub("", cut).rstrip()
    return cut + "…"


def _json_rows(path: Path):
    try:
        with path.open(errors="replace") as handle:
            for raw in handle:
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] if isinstance(b, dict) and b.get("type") in {"text", "input_text"}
            and isinstance(b.get("text"), str) else b if isinstance(b, str) else ""
            for b in content)
    return ""


def _strip_claude_wrappers(text: str) -> str:
    text = _WRAPPERS.sub(" ", text)
    return _COMMAND_NAME.sub(lambda m: " " + m.group(1) + " ", text)


def _claude(path: Path, turn_id: str, limit: int) -> str | None:
    fallback = None
    for row in _json_rows(path):
        if not _is_genuine_user_row(row):
            continue
        message = _mapping(row.get("message"))
        if _first_text(row, "uuid", "id") == turn_id:
            found = row
            break
        if fallback is None and _explicit_turn_id(row, message) == turn_id:
            fallback = row
    else:
        found = fallback
    if found is None:
        return None
    message = _mapping(found.get("message"))
    return sanitize(_strip_claude_wrappers(_blocks_text(message.get("content", found.get("content")))), limit)


def _codex_text(row: dict, payload: dict) -> str:
    for mapping in (payload, row):
        for key in ("message", "text", "content"):
            found = _blocks_text(mapping.get(key))
            if found.strip():
                return found
    return ""


def _codex(path: Path, turn_id: str, limit: int) -> str | None:
    """Replay collect_codex's turn assignment, tracking the user text behind each id."""
    latest: str | None = None   # latest user_message text seen so far
    awaiting = False            # task_started carried turn_id; text comes from the next user_message
    for row in _json_rows(path):
        payload = _mapping(row.get("payload"))
        row_type, event_type = row.get("type"), payload.get("type")
        if row_type == "turn_context":
            if _meta_text(_codex_event_turn_id(row, payload)) == turn_id and latest:
                return sanitize(latest, limit)
            continue
        if row_type == "session_meta" or event_type == "token_count":
            continue
        if event_type == "task_started":
            if _meta_text(_codex_event_turn_id(row, payload)) == turn_id:
                if latest:
                    return sanitize(latest, limit)
                awaiting = True
        elif event_type in {"user_message", "user_input", "message"}:
            latest = _codex_text(row, payload)
            assigned = _meta_text(_codex_event_turn_id(row, payload), _codex_user_event_identity(row, payload))
            if awaiting or assigned == turn_id:
                return sanitize(latest, limit)
    return None


def _opencode(path: Path, session: str, turn_id: str, limit: int) -> str | None:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, OSError):
        return None
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT p.data FROM part AS p JOIN message AS m ON m.id = p.message_id "
            "WHERE m.id = ? AND m.session_id = ? ORDER BY p.time_created, p.id",
            (turn_id, session)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    texts = []
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("type") == "text" and isinstance(data.get("text"), str) \
                and not data.get("synthetic"):
            texts.append(data["text"])
    return sanitize("\n".join(texts), limit)


def _pi(path: Path, turn_id: str, limit: int) -> str | None:
    for row in _json_rows(path):
        message = _mapping(row.get("message"))
        if row.get("id") == turn_id and message.get("role") == "user":
            return sanitize(_blocks_text(message.get("content")), limit)
    return None


def extract_prompt(harness: str, source: object, session: object, turn_id: object, limit: int = 200) -> str | None:
    """Sanitized preview of the user prompt that started `turn_id`, or None. Never raises."""
    try:
        if not isinstance(turn_id, str) or not turn_id or not isinstance(source, (str, Path)):
            return None
        path = Path(source)
        if not path.is_file():
            return None
        if harness == "claude":
            return _claude(path, turn_id, limit)
        if harness == "codex":
            return _codex(path, turn_id, limit)
        if harness == "opencode":
            return _opencode(path, session, turn_id, limit) if isinstance(session, str) else None
        if harness == "pi":
            return _pi(path, turn_id, limit)
    except (OSError, ValueError, sqlite3.Error):
        return None
    return None
