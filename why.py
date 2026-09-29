#!/usr/bin/env python3
"""Explain which local coding-harness calls consumed a time window.

This is deliberately stateless: it reads the harnesses' retained JSONL files,
normalises one record per model call, and groups the result by actionable
dimensions.  Reasoning tokens are a subset of output and are never added twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_STATE = Path.home() / ".claude"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


@dataclass(frozen=True)
class AttributionRecord:
    harness: str
    provider: str
    timestamp: datetime
    session_id: str
    call_id: str
    model: str
    effort: str
    project: str
    entrypoint: str
    thread_kind: str
    agent: str
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    reasoning: int
    project_id: str = ""
    cwd: str | None = None
    turn_id: str | None = None
    turn_confidence: str = "absent"
    parent_session_id: str | None = None
    harness_version: str | None = None
    session_started_at: datetime | None = None
    raw_usage: dict = field(default_factory=dict)
    id_synthetic: bool = False

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cache_read + self.cache_write + self.output


def parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (OverflowError, TypeError, ValueError):
        return 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# These are the usage fields observed in supported harness stores. Raw
# usage is deliberately narrower than the source row: prompt text, tool
# arguments, and arbitrary nested metadata must never enter the durable record.
_USAGE_NUMERIC_FIELDS = frozenset({
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "total_input_tokens",
    "total_output_tokens",
    "total_tokens",
    "input",
    "output",
    "reasoning",
    "cacheRead",
    "cacheWrite",
    "totalTokens",
})
_CACHE_CREATION_FIELDS = frozenset({
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
})


def _numeric_token(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return value if math.isfinite(value) and value >= 0 else None


def _sanitize_usage(value: object) -> dict | None:
    """Keep token counters and the small amount of usage metadata we expose."""
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for key, item in value.items():
        if key in _USAGE_NUMERIC_FIELDS:
            numeric = _numeric_token(item)
            result[key] = numeric
        elif key == "cache_creation" and isinstance(item, dict):
            nested = {
                nested_key: numeric
                for nested_key, nested_value in item.items()
                if nested_key in _CACHE_CREATION_FIELDS
                and (numeric := _numeric_token(nested_value)) is not None
            }
            if nested or item:
                result[key] = {
                    nested_key: (
                        _numeric_token(nested_value)
                        if nested_key in _CACHE_CREATION_FIELDS
                        else None
                    )
                    for nested_key, nested_value in item.items()
                    if nested_key in _CACHE_CREATION_FIELDS
                }
        elif key == "output_tokens_details" and isinstance(item, dict):
            thinking = _numeric_token(item.get("thinking_tokens"))
            if "thinking_tokens" in item:
                result[key] = {"thinking_tokens": thinking}
        elif key == "cache" and isinstance(item, dict):
            result[key] = {
                nested_key: _numeric_token(nested_value)
                for nested_key, nested_value in item.items()
                if nested_key in {"read", "write"}
            }
        elif key == "iterations":
            if item is None:
                result[key] = None
            elif isinstance(item, list):
                result[key] = [_sanitize_iteration(entry) or {} for entry in item]
            else:
                # Preserve invalid structure as an invalid, content-free entry.
                # Dropping it would incorrectly make iteration coverage complete.
                result[key] = [{}]
    return result


def _sanitize_iteration(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = _sanitize_usage(value) or {}
    # These identify the billing/model variant without retaining arbitrary
    # iteration text.  All other strings are intentionally discarded.
    for key in ("type", "model"):
        if isinstance(value.get(key), str) and value[key]:
            result[key] = value[key]
    return result


def _iteration_layout_compatible(a: list, b: list) -> bool:
    return all(
        all(left.get(key) == right.get(key) for key in ("type", "model"))
        for left, right in zip(a, b)
    )


def _merge_sanitized_usage(existing: dict, incoming: dict) -> dict:
    """Merge counter maxima without inventing iteration roles or models.

    Retain original sanitized iteration snapshots: positions are not identities
    when a provider inserts or reorders internal calls.
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if key in ("iterations", "iteration_snapshots"):
            continue
        prior = merged.get(key)
        if isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = _merge_sanitized_usage(prior, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = max(prior, value) if isinstance(prior, (int, float)) else value
        elif prior is None:
            merged[key] = value
        elif isinstance(prior, str) and isinstance(value, str):
            merged[key] = max(prior, value)
    snapshots = []
    for raw in (existing, incoming):
        snapshots.extend(raw.get("iteration_snapshots", []))
        if "iteration_snapshots" not in raw and isinstance(raw.get("iterations"), list):
            snapshots.append(raw["iterations"])
    if snapshots:
        unique = {json.dumps(x, sort_keys=True): x for x in snapshots}
        merged["iteration_snapshots"] = [unique[k] for k in sorted(unique)]
        # Derive a deterministic representative from original snapshots only.
        # Its maxima must never re-enter the raw evidence on a later refresh.
        template = max(snapshots, key=lambda x: (len(x), json.dumps(x, sort_keys=True)))
        iterations = [{} for _ in template]
        for snapshot in merged["iteration_snapshots"]:
            if _iteration_layout_compatible(template, snapshot):
                for i, entry in enumerate(snapshot):
                    iterations[i] = _merge_sanitized_usage(iterations[i], entry)
        merged["iterations"] = iterations
    elif "iterations" in incoming and "iterations" not in merged:
        merged["iterations"] = incoming["iterations"]
    return merged


def _raw_usage_requires_record(raw_usage: dict, required: tuple = ()) -> bool:
    """Keep observations whose normalized legacy totals cannot represent them."""
    if any(key not in raw_usage for key in required):
        return True
    for key, value in raw_usage.items():
        if value is None:
            return True
        if key == "iterations" and isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and _raw_usage_requires_record(value):
            return True
    return False


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _project_name(cwd: object, fallback: str = "unknown") -> str:
    if not isinstance(cwd, str) or not cwd:
        return fallback
    path = Path(cwd)
    return path.name or str(path)


def _claude_project_fallback(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "unknown"
    return relative.parts[0] if len(relative.parts) > 1 else "unknown"


def _claude_project_id(path: Path, root: Path) -> str:
    fallback = _claude_project_fallback(path, root)
    return f"claude:{fallback}" if fallback != "unknown" else "claude:unknown"


def _paths_for(root: Path, pattern: str, start: datetime, paths: list[Path] | None) -> list[Path]:
    if paths is not None:
        return sorted(Path(path) for path in paths)
    return _recent_files(root, pattern, start)


def _read_json_lines(path: Path, *, strict: bool = False) -> Iterable[tuple[int, dict]]:
    try:
        with path.open(errors="replace") as handle:
            for line_number, raw in enumerate(handle, 1):
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    yield line_number, value
    except OSError:
        if strict:
            raise
        return


def _recent_files(root: Path, pattern: str, start: datetime) -> list[Path]:
    """Return files that could contain an event at or after start.

    A JSONL append updates mtime, so an older resumed session remains eligible.
    The event timestamp remains authoritative once the file is opened.
    """
    if not root.exists():
        return []
    threshold = start.timestamp()
    files = []
    for path in root.rglob(pattern):
        try:
            if path.stat().st_mtime >= threshold:
                files.append(path)
        except OSError:
            continue
    return sorted(files)


def _first_text(mapping: dict, *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _explicit_turn_id(row: dict, message: dict) -> str | None:
    return _first_text(
        row,
        "turn_id", "turnId",
    ) or _first_text(message, "turn_id", "turnId")


def _is_genuine_user_row(row: dict) -> bool:
    if row.get("type") not in {"user", "user_message"} and _mapping(row.get("message")).get("role") != "user":
        return False
    message = _mapping(row.get("message"))
    content = message.get("content", row.get("content"))
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return True
    return any(
        not isinstance(item, dict) or item.get("type") not in {"tool_result", "tool_use_result"}
        for item in content
    )


def _claude_cache_write(usage: dict) -> int:
    if counter := usage.get("cache_creation_input_tokens"):
        return _nonnegative_int(counter)
    if usage.get("cache_creation_input_tokens") == 0:
        return 0
    split = _mapping(usage.get("cache_creation"))
    return sum(_nonnegative_int(split.get(key)) for key in _CACHE_CREATION_FIELDS)


def collect_claude(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Claude calls, deduplicating streamed rows by requestId.

    Claude can write several content-block rows for a request.  Early rows may
    contain placeholder output counts, so every token field uses its maximum.
    """
    calls: dict[str, dict] = {}
    for path in _paths_for(root, "*.jsonl", start, paths):
        is_subagent_path = "subagents" in path.parts
        last_user_turn: str | None = None
        for line_number, row in _read_json_lines(path, strict=strict):
            message = _mapping(row.get("message"))
            usage = message.get("usage")
            timestamp = parse_iso_timestamp(row.get("timestamp"))
            row_uuid = _first_text(row, "uuid", "id")
            if _is_genuine_user_row(row):
                last_user_turn = row_uuid or _stable_hash({
                    "session": row.get("sessionId"),
                    "timestamp": row.get("timestamp"),
                    "line": line_number,
                })
            if not isinstance(usage, dict) or timestamp is None:
                continue
            request_id = _first_text(row, "requestId")
            session_id = str(row.get("sessionId") or "unknown")
            id_synthetic = False
            if not request_id:
                request_id = _first_text(message, "id")
            if not request_id:
                request_id = row_uuid
                id_synthetic = True
            if not request_id:
                request_id = _stable_hash({
                    "session_id": session_id,
                    "timestamp": row.get("timestamp"),
                    "message": {
                        key: message.get(key)
                        for key in ("id", "model", "role")
                        if message.get(key) is not None
                    },
                    "usage": _sanitize_usage(usage),
                })
                id_synthetic = True
            key = str(request_id)
            values = {
                "fresh_input": _nonnegative_int(usage.get("input_tokens")),
                "cache_read": _nonnegative_int(usage.get("cache_read_input_tokens")),
                "cache_write": _claude_cache_write(usage),
                "output": _nonnegative_int(usage.get("output_tokens")),
                "reasoning": _nonnegative_int(
                    _mapping(usage.get("output_tokens_details")).get("thinking_tokens")
                ),
                "raw_usage": _sanitize_usage(usage) or {},
            }
            existing = calls.get(key)
            if existing is None:
                existing = {**values, "timestamp": timestamp}
                calls[key] = existing
            else:
                for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                    existing[token_field] = max(existing[token_field], values[token_field])
                existing["raw_usage"] = _merge_sanitized_usage(
                    existing.get("raw_usage", {}), values["raw_usage"]
                )
                existing["timestamp"] = max(existing["timestamp"], timestamp)

            # Dimensions should be stable for a request; prefer the latest row
            # when a streamed record becomes more complete.
            if timestamp >= existing["timestamp"]:
                agent_id = row.get("agentId")
                explicit_turn = _explicit_turn_id(row, message)
                parent_session_id = _first_text(
                    row, "parentSessionId", "parent_session_id"
                )
                cwd_value = row.get("cwd") if isinstance(row.get("cwd"), str) else None
                fallback_project = _claude_project_fallback(path, root)
                derived_turn = None if is_subagent_path else last_user_turn
                existing.update({
                    "session_id": session_id,
                    "model": str(message.get("model") or "unknown"),
                    "effort": str(row.get("effort") or "unknown"),
                    "project": _project_name(row.get("cwd"), fallback_project),
                    "project_id": cwd_value or _claude_project_id(path, root),
                    "cwd": cwd_value,
                    "turn_id": explicit_turn or derived_turn,
                    "turn_confidence": "observed" if explicit_turn else (
                        "derived" if derived_turn else "absent"
                    ),
                    "parent_session_id": parent_session_id,
                    "harness_version": _first_text(row, "version")
                    or _first_text(message, "version"),
                    "entrypoint": str(row.get("entrypoint") or "unknown"),
                    "thread_kind": "subagent" if agent_id or is_subagent_path else "main",
                    "agent": str(row.get("attributionAgent") or agent_id or "main"),
                    "id_synthetic": id_synthetic,
                })

    records = []
    for call_id, values in calls.items():
        if not start <= values["timestamp"] < end:
            continue
        if sum(
            values[field]
            for field in ("fresh_input", "cache_read", "cache_write", "output")
        ) <= 0 and not _raw_usage_requires_record(values.get("raw_usage", {}),
            ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')):
            continue
        records.append(
            AttributionRecord(
                harness="claude", provider="anthropic", call_id=call_id, **values
            )
        )
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def _codex_thread(source: object, thread_source: object) -> tuple[str, str, str | None]:
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        subagent = source["subagent"]
        spawn = subagent.get("thread_spawn") or {}
        agent = spawn.get("agent_nickname") or spawn.get("agent_role")
        if not agent and subagent.get("other"):
            agent = subagent["other"]
        parent = spawn.get("parent_thread_id")
        return "subagent", str(agent or "subagent"), str(parent) if parent else None
    source_name = str(thread_source or source or "main")
    if source_name in {"subagent", "automation"}:
        return source_name, source_name, None
    return "main", "main", None


def _codex_event_turn_id(row: dict, payload: dict) -> str | None:
    for mapping in (payload, row):
        found = _first_text(mapping, "turn_id", "turnId")
        if found:
            return found
    return None


def _codex_user_event_identity(row: dict, payload: dict) -> str | None:
    for mapping in (payload, row):
        found = _first_text(mapping, "event_id", "eventId", "id", "uuid")
        if found:
            return found
    return None


def collect_codex(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Codex per-turn deltas from rollout JSONL files.

    A rollout without a usable session ID gets a deterministic synthetic ID
    from its timestamp, source ordinal, and sanitized counters.  Such an
    identity is stable for copied files but remains inherently ambiguous when
    independent sessions emit identical events.
    """
    calls: dict[tuple[str, str], dict] = {}
    for path in _paths_for(root, "rollout-*.jsonl", start, paths):
        rows = list(_read_json_lines(path, strict=strict))
        meta_payload = next(
            (_mapping(row.get("payload")) for _, row in rows if row.get("type") == "session_meta"),
            {},
        )
        session_id_value = _first_text(meta_payload, "id")
        session_id = session_id_value or "unknown"
        has_session_meta = session_id_value is not None
        provider = str(meta_payload.get("model_provider") or "openai")
        originator = "unknown"
        source: object = meta_payload.get("source") or "unknown"
        thread_source: object = meta_payload.get("thread_source")
        cwd: object = meta_payload.get("cwd")
        model = "unknown"
        effort = "unknown"
        current_turn_id: str | None = None
        turn_confidence = "absent"
        pending_turn_id: str | None = None
        last_total_signature = None
        counter_segment = 0
        harness_version = _first_text(meta_payload, "cli_version", "version")
        originator = str(
            meta_payload.get("originator")
            or (source if isinstance(source, str) else None)
            or thread_source
            or originator
        )

        for line_number, row in rows:
            payload = _mapping(row.get("payload"))
            row_type = row.get("type")
            if row_type == "session_meta":
                session_id = str(payload.get("id") or session_id)
                provider = str(payload.get("model_provider") or provider)
                source = payload.get("source") or source
                thread_source = payload.get("thread_source") or thread_source
                originator = str(
                    payload.get("originator")
                    or (source if isinstance(source, str) else None)
                    or thread_source
                    or originator
                )
                cwd = payload.get("cwd") or cwd
                harness_version = _first_text(payload, "cli_version", "version") or harness_version
                continue
            if row_type == "turn_context":
                model = str(payload.get("model") or "unknown")
                effort = str(payload.get("effort") or payload.get("reasoning_effort") or "unknown")
                cwd = payload.get("cwd") or cwd
                explicit_turn = _codex_event_turn_id(row, payload)
                current_turn_id = explicit_turn or pending_turn_id
                turn_confidence = "observed" if explicit_turn else (
                    "derived" if pending_turn_id else "absent"
                )
                pending_turn_id = None
                continue
            if row_type != "event_msg" or payload.get("type") != "token_count":
                event_type = payload.get("type")
                if event_type == "task_started":
                    current_turn_id = _codex_event_turn_id(row, payload)
                    pending_turn_id = current_turn_id
                    turn_confidence = "derived" if current_turn_id else "absent"
                elif event_type in {"user_message", "user_input", "message"}:
                    current_turn_id = _codex_event_turn_id(row, payload) or _codex_user_event_identity(row, payload)
                    pending_turn_id = current_turn_id
                    turn_confidence = "derived" if current_turn_id else "absent"
                continue
            info = _mapping(payload.get("info"))
            usage = info.get("last_token_usage")
            timestamp = parse_iso_timestamp(row.get("timestamp"))
            if not isinstance(usage, dict) or timestamp is None:
                continue
            cumulative = _mapping(info.get("total_token_usage"))
            if cumulative:
                signature = tuple(
                    _nonnegative_int(cumulative.get(field)) for field in (
                        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "output_tokens", "reasoning_output_tokens", "total_tokens",
                    )
                )
                if signature == last_total_signature:
                    continue
                if last_total_signature is not None and any(
                    new < old for new, old in zip(signature, last_total_signature)
                ):
                    counter_segment += 1
                last_total_signature = signature
            total_input = _nonnegative_int(usage.get("input_tokens"))
            cache_read = _nonnegative_int(usage.get("cached_input_tokens"))
            cache_write = _nonnegative_int(usage.get("cache_write_input_tokens"))
            output = _nonnegative_int(usage.get("output_tokens"))
            reasoning = _nonnegative_int(usage.get("reasoning_output_tokens"))
            raw_usage = _sanitize_usage(usage) or {}
            if (
                total_input + cache_read + cache_write + output + reasoning <= 0
                and not _raw_usage_requires_record(raw_usage,
                    ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens'))
            ):
                continue
            thread_kind, agent, parent_session_id = _codex_thread(source, thread_source)
            ordinal = row.get("ordinal")
            if has_session_meta:
                event_identity = ordinal if ordinal is not None else _stable_hash({
                    "timestamp": row.get("timestamp"),
                    "total": _sanitize_usage(cumulative) if cumulative else raw_usage,
                    "segment": counter_segment,
                })
                call_id = f"{session_id}:{event_identity}:token_count"
                id_synthetic = ordinal is None and not cumulative
            else:
                call_id = "synthetic:" + _stable_hash({
                    "timestamp": row.get("timestamp"),
                    "ordinal": ordinal,
                    "segment": counter_segment,
                    "usage": _sanitize_usage(usage),
                    "total": _sanitize_usage(info.get("total_token_usage")),
                })
                id_synthetic = True
            key = (provider, call_id)
            candidate = {
                "timestamp": timestamp,
                "session_id": session_id,
                "call_id": call_id,
                "model": model,
                "effort": effort,
                "project": _project_name(cwd),
                "project_id": str(cwd) if isinstance(cwd, str) and cwd else "unknown",
                "cwd": cwd if isinstance(cwd, str) else None,
                "turn_id": current_turn_id,
                "turn_confidence": turn_confidence,
                "parent_session_id": parent_session_id,
                "harness_version": harness_version,
                "entrypoint": originator,
                "thread_kind": thread_kind,
                "agent": agent,
                "fresh_input": max(0, total_input - cache_read - cache_write),
                "cache_read": cache_read,
                "cache_write": cache_write,
                "output": output,
                "reasoning": reasoning,
                "raw_usage": raw_usage,
                "id_synthetic": id_synthetic,
            }
            existing = calls.get(key)
            if existing is None:
                calls[key] = candidate
            else:
                for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                    existing[token_field] = max(existing[token_field], candidate[token_field])
                existing["raw_usage"] = _merge_sanitized_usage(
                    existing.get("raw_usage", {}), raw_usage
                )
                if timestamp >= existing["timestamp"]:
                    existing.update({
                        field: candidate[field]
                        for field in (
                            "timestamp", "model", "effort", "project", "project_id", "cwd",
                            "turn_id", "turn_confidence", "parent_session_id", "harness_version",
                            "entrypoint", "thread_kind", "agent",
                        )
                    })
    records = [
        AttributionRecord(harness="codex", provider=provider, **values)
        for (provider, _), values in calls.items()
        if start <= values["timestamp"] < end
    ]
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def collect_pi(
    root: Path,
    start: datetime,
    end: datetime,
    *,
    paths: list[Path] | None = None,
    strict: bool = False,
) -> list[AttributionRecord]:
    """Collect Pi response usage and deduplicate copied fork history."""
    calls: dict[tuple[str, str], dict] = {}
    for path in _paths_for(root, "*.jsonl", start, paths):
        rows = list(_read_json_lines(path, strict=strict))
        session = next((row for _, row in rows if row.get("type") == "session"), {})
        session_id = str(session.get("id") or path.stem)
        session_started = parse_iso_timestamp(session.get("timestamp"))
        cwd = session.get("cwd") if isinstance(session.get("cwd"), str) else None
        harness_version = str(session.get("version")) if session.get("version") is not None else None
        for _, row in rows:
            message = _mapping(row.get("message"))
            usage = message.get("usage")
            if row.get("type") != "message" or message.get("role") != "assistant" or not isinstance(usage, dict):
                continue
            timestamp = parse_iso_timestamp(row.get("timestamp") or message.get("timestamp"))
            if timestamp is None:
                continue
            raw_usage = _sanitize_usage(usage) or {}
            provider = str(message.get("provider") or "unknown")
            response_id = _first_text(message, "responseId", "response_id")
            id_synthetic = response_id is None
            call_id = response_id or "synthetic:" + _stable_hash({
                "entry_id": row.get("id"), "timestamp": timestamp.isoformat(),
                "provider": provider, "model": message.get("model"), "usage": raw_usage,
            })
            values = {
                "fresh_input": _nonnegative_int(usage.get("input")),
                "cache_read": _nonnegative_int(usage.get("cacheRead")),
                "cache_write": _nonnegative_int(usage.get("cacheWrite")),
                "output": _nonnegative_int(usage.get("output")),
                "reasoning": _nonnegative_int(usage.get("reasoning")),
            }
            if sum(values[field] for field in ("fresh_input", "cache_read", "cache_write", "output")) <= 0 \
                    and not _raw_usage_requires_record(raw_usage, ("input", "cacheRead", "cacheWrite", "output")):
                continue
            candidate = {
                "timestamp": timestamp, "session_id": session_id, "call_id": call_id,
                "model": str(message.get("model") or "unknown"), "effort": "unknown",
                "project": _project_name(cwd), "project_id": cwd or "unknown", "cwd": cwd,
                "turn_id": None, "turn_confidence": "absent", "parent_session_id": None,
                "harness_version": harness_version, "entrypoint": "unknown",
                "thread_kind": "main", "agent": "main", "raw_usage": raw_usage,
                "id_synthetic": id_synthetic,
                "session_started": session_started or timestamp,
                **values,
            }
            key = (provider, call_id)
            existing = calls.get(key)
            if existing is None:
                calls[key] = candidate
                continue
            for token_field in ("fresh_input", "cache_read", "cache_write", "output", "reasoning"):
                existing[token_field] = max(existing[token_field], candidate[token_field])
            existing["raw_usage"] = _merge_sanitized_usage(existing["raw_usage"], raw_usage)
            existing["timestamp"] = max(existing["timestamp"], timestamp)
            if candidate["session_started"] < existing["session_started"]:
                for field in ("session_id", "project", "project_id", "cwd", "harness_version", "session_started"):
                    existing[field] = candidate[field]
    records = []
    for (provider, _), values in calls.items():
        values["session_started_at"] = values.pop("session_started")
        if start <= values["timestamp"] < end:
            records.append(AttributionRecord(harness="pi", provider=provider, **values))
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def _millisecond_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def collect_opencode(
    db_path: Path,
    start: datetime,
    end: datetime,
    diagnostics: dict[str, int] | None = None,
) -> list[AttributionRecord]:
    """Collect per-assistant-call usage from OpenCode's local SQLite store."""
    path = Path(db_path).expanduser().resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("""
            SELECT m.id, m.session_id, m.time_created, m.time_updated, m.data,
                   s.parent_id, s.directory, s.version
            FROM message AS m JOIN session AS s ON s.id = m.session_id
        """).fetchall()
    finally:
        connection.close()
    records = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            if diagnostics is not None:
                diagnostics["malformed_lines"] += 1
            continue
        if not isinstance(data, dict):
            if diagnostics is not None:
                diagnostics["malformed_lines"] += 1
            continue
        if data.get("role") != "assistant":
            continue
        usage = data.get("tokens")
        if not isinstance(usage, dict):
            if diagnostics is not None:
                diagnostics["unparsed_usage_lines"] += 1
            continue
        time_data = _mapping(data.get("time"))
        timestamp = _millisecond_timestamp(
            time_data.get("completed") or time_data.get("created") or row["time_updated"] or row["time_created"]
        )
        if timestamp is None:
            if diagnostics is not None:
                diagnostics["unparsed_usage_lines"] += 1
            continue
        if not start <= timestamp < end:
            continue
        raw_usage = _sanitize_usage(usage) or {}
        cache = _mapping(usage.get("cache"))
        values = {
            "fresh_input": _nonnegative_int(usage.get("input")),
            "cache_read": _nonnegative_int(cache.get("read")),
            "cache_write": _nonnegative_int(cache.get("write")),
            # OpenCode's own stats add reasoning to output; normalize to the
            # repository convention where reasoning is a subset of output.
            "output": _nonnegative_int(usage.get("output")) + _nonnegative_int(usage.get("reasoning")),
            "reasoning": _nonnegative_int(usage.get("reasoning")),
        }
        if sum(values[field] for field in ("fresh_input", "cache_read", "cache_write", "output")) <= 0 \
                and not _raw_usage_requires_record(raw_usage, ("input", "output", "reasoning", "cache")):
            continue
        call_id = str(row["id"] or "synthetic:" + _stable_hash({
            "session": row["session_id"], "timestamp": timestamp.isoformat(), "usage": raw_usage,
        }))
        cwd = _mapping(data.get("path")).get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = row["directory"] if isinstance(row["directory"], str) else None
        parent = str(row["parent_id"]) if row["parent_id"] else None
        turn_id = _first_text(data, "parentID", "parentId")
        records.append(AttributionRecord(
            harness="opencode", provider=str(data.get("providerID") or "unknown"),
            timestamp=timestamp, session_id=str(row["session_id"]), call_id=call_id,
            model=str(data.get("modelID") or "unknown"), effort=str(data.get("variant") or "unknown"),
            project=_project_name(cwd), project_id=cwd or "unknown", cwd=cwd,
            entrypoint="unknown", thread_kind="subagent" if parent else "main",
            agent=str(data.get("agent") or "unknown"), turn_id=turn_id,
            turn_confidence="observed" if turn_id else "absent", parent_session_id=parent,
            harness_version=str(row["version"]) if row["version"] is not None else None,
            raw_usage=raw_usage, id_synthetic=not bool(row["id"]), **values,
        ))
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def reconcile_codex_rollout(path: Path) -> dict:
    """Compare deltas with cumulative totals, accounting for counter resets."""
    fields = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens",
    )
    summed = {field: 0 for field in fields}
    final_snapshot = None
    completed_segments = {field: 0 for field in fields}
    segments = 0
    calls = 0
    last_total_signature = None
    for _, row in _read_json_lines(path):
        payload = _mapping(row.get("payload"))
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = _mapping(payload.get("info"))
        last = info.get("last_token_usage")
        total = _mapping(info.get("total_token_usage"))
        if total:
            normalized_total = {
                field: _nonnegative_int(total.get(field)) for field in fields
            }
            signature = tuple(
                _nonnegative_int(total.get(field))
                for field in (*fields, "total_tokens")
            )
            if signature == last_total_signature:
                continue
            if final_snapshot is not None and any(
                normalized_total[field] < final_snapshot[field] for field in fields
            ):
                for field in fields:
                    completed_segments[field] += final_snapshot[field]
                segments += 1
            last_total_signature = signature
        if isinstance(last, dict):
            calls += 1
            for field in fields:
                summed[field] += _nonnegative_int(last.get(field))
        if total:
            final_snapshot = normalized_total
    cumulative = None
    if final_snapshot is not None:
        cumulative = {
            field: completed_segments[field] + final_snapshot[field] for field in fields
        }
        segments += 1
    differences = {
        field: summed[field] - cumulative[field] for field in fields
    } if cumulative is not None else None
    return {
        "calls": calls,
        "summed": summed,
        "final": cumulative,
        "final_snapshot": final_snapshot,
        "segments": segments,
        "differences": differences,
        "matches": differences is not None and all(value == 0 for value in differences.values()),
    }


TOKEN_FIELDS = ("fresh_input", "cache_read", "cache_write", "output", "reasoning")
GROUP_FIELDS = (
    "thread_kind", "entrypoint", "agent", "model", "effort", "project", "session_id"
)


def summarize_records(records: list[AttributionRecord], limit: int = 5) -> dict:
    totals = {field: sum(getattr(record, field) for record in records) for field in TOKEN_FIELDS}
    totals["tokens"] = sum(record.total_tokens for record in records)
    totals["calls"] = len(records)
    totals["sessions"] = len({record.session_id for record in records})

    groups: dict[str, list[dict]] = {}
    for field in GROUP_FIELDS:
        buckets: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, **{token: 0 for token in TOKEN_FIELDS}, "tokens": 0}
        )
        for record in records:
            if field == "project":
                name = str(record.project_id or record.project or "unknown")
            else:
                name = str(getattr(record, field) or "unknown")
            bucket = buckets[name]
            bucket["calls"] += 1
            for token in TOKEN_FIELDS:
                bucket[token] += getattr(record, token)
            bucket["tokens"] += record.total_tokens
        rows = []
        for name, bucket in buckets.items():
            denominator = totals["tokens"]
            rows.append({
                "name": name,
                **bucket,
                "share_pct": round(bucket["tokens"] / denominator * 100, 2)
                if denominator else 0.0,
            })
        rows.sort(key=lambda row: (-row["tokens"], row["name"]))
        groups[field.removesuffix("_id")] = rows[:limit]
    return {"totals": totals, "groups": groups}


def load_claude_monitor_day(state_dir: Path, day: str) -> dict | None:
    selected = None
    history = state_dir / "statusline_history.jsonl"
    for _, row in _read_json_lines(history):
        if row.get("date") == day:
            selected = row
    daily = state_dir / "statusline_daily.json"
    try:
        live = json.loads(daily.read_text())
        if isinstance(live, dict) and live.get("date") == day:
            selected = live
    except (OSError, json.JSONDecodeError):
        pass
    if not isinstance(selected, dict):
        return None
    totals = {
        "fresh_input": _nonnegative_int(selected.get("input")),
        "cache_read": _nonnegative_int(selected.get("cache_read", selected.get("cached"))),
        "cache_write": _nonnegative_int(selected.get("cache_write")),
        "output": _nonnegative_int(selected.get("output")),
    }
    totals["tokens"] = sum(totals.values())
    return totals


def _coverage(monitor: dict | None, actual: dict) -> dict | None:
    if monitor is None:
        return None
    result = {"monitor": monitor}
    for field in ("tokens", "cache_read", "output"):
        denominator = actual.get(field, 0)
        result[f"{field}_pct"] = round(monitor.get(field, 0) / denominator * 100, 1) \
            if denominator else None
    return result


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, AttributionRecord):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    return value


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def render_report(report: dict) -> str:
    lines = [
        "Usage attribution",
        f"Window: {report['window']['start']} to {report['window']['end']}",
    ]
    for harness, summary in report["harnesses"].items():
        totals = summary["totals"]
        lines.extend([
            "",
            f"{harness.upper()}: {fmt_tokens(totals['tokens'])} tokens · "
            f"{totals['calls']} calls · {totals['sessions']} sessions",
            "  mix: "
            f"fresh {fmt_tokens(totals['fresh_input'])} · "
            f"cache-read {fmt_tokens(totals['cache_read'])} · "
            f"cache-write {fmt_tokens(totals['cache_write'])} · "
            f"output {fmt_tokens(totals['output'])}",
        ])
        coverage = summary.get("monitor_coverage")
        if coverage:
            output_pct = coverage.get("output_pct")
            cache_pct = coverage.get("cache_read_pct")
            lines.append(
                "  Claude statusline coverage: "
                f"output {output_pct if output_pct is not None else 'n/a'}% · "
                f"cache-read {cache_pct if cache_pct is not None else 'n/a'}%"
            )
        elif harness == "claude":
            lines.append("  Claude statusline coverage: unavailable for a partial-day window")

        for dimension in (
            "project", "thread_kind", "entrypoint", "agent", "model", "effort", "session"
        ):
            rows = summary["groups"].get(dimension, [])
            if not rows:
                continue
            formatted = ", ".join(
                f"{row['name']} {fmt_tokens(row['tokens'])} ({row['share_pct']:.1f}%)"
                for row in rows
            )
            lines.append(f"  by {dimension.replace('_', ' ')}: {formatted}")
    for harness, error in report.get("errors", {}).items():
        lines.extend(["", f"{harness.upper()}: unavailable ({error})"])
    return "\n".join(lines)


def _window(args: argparse.Namespace) -> tuple[datetime, datetime, str | None]:
    if args.date:
        selected = date.fromisoformat(args.date)
        start = datetime.combine(selected, time.min).astimezone()
        end = datetime.combine(selected + timedelta(days=1), time.min).astimezone()
        return start, end, args.date
    end = datetime.now().astimezone()
    return end - timedelta(hours=args.hours or 24.0), end, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explain local coding-harness token use by agent, project, model and session."
    )
    parser.add_argument(
        "--harness", choices=("all", "both", "claude", "codex", "pi", "opencode"),
        default="all", help="Harness to inspect; 'both' retains the legacy Claude+Codex pair.",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--date", help="Local calendar date (YYYY-MM-DD)")
    window.add_argument("--hours", type=float, help="Trailing number of hours (default: 24)")
    parser.add_argument("--limit", type=int, default=5, help="Rows per grouping (default: 5)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--claude-root", type=Path, default=CLAUDE_PROJECTS)
    parser.add_argument("--claude-state-dir", type=Path, default=CLAUDE_STATE)
    parser.add_argument("--codex-root", type=Path, default=CODEX_SESSIONS)
    parser.add_argument("--pi-root", type=Path, default=PI_SESSIONS)
    parser.add_argument("--opencode-db", type=Path, default=OPENCODE_DB)
    args = parser.parse_args(argv)
    if args.hours is not None and args.hours <= 0:
        parser.error("--hours must be greater than zero")
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")

    try:
        start, end, selected_day = _window(args)
    except ValueError as exc:
        parser.error(str(exc))

    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "harnesses": {},
        "errors": {},
        "notes": [
            "Reasoning is reported separately and normalized as a subset of output.",
            "Transcript, session, rollout, and database formats are internal and may change.",
        ],
    }
    if args.harness in ("all", "both", "claude"):
        records = collect_claude(args.claude_root.expanduser(), start, end)
        summary = summarize_records(records, args.limit)
        monitor = load_claude_monitor_day(args.claude_state_dir.expanduser(), selected_day) \
            if selected_day else None
        summary["monitor_coverage"] = _coverage(monitor, summary["totals"])
        report["harnesses"]["claude"] = summary
    if args.harness in ("all", "both", "codex"):
        records = collect_codex(args.codex_root.expanduser(), start, end)
        report["harnesses"]["codex"] = summarize_records(records, args.limit)
    if args.harness in ("all", "pi"):
        records = collect_pi(args.pi_root.expanduser(), start, end)
        report["harnesses"]["pi"] = summarize_records(records, args.limit)
    if args.harness in ("all", "opencode"):
        try:
            records = collect_opencode(args.opencode_db.expanduser(), start, end) \
                if args.opencode_db.expanduser().is_file() else []
            report["harnesses"]["opencode"] = summarize_records(records, args.limit)
        except (OSError, sqlite3.Error) as exc:
            message = f"{type(exc).__name__}: {exc}"
            if args.harness == "opencode":
                parser.exit(2, f"why: OpenCode unavailable: {message}\n")
            report["errors"]["opencode"] = message

    if args.json:
        print(json.dumps(report, default=_jsonable, indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
