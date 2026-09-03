#!/usr/bin/env python3
"""Explain which Claude Code and Codex calls consumed a time window.

This is deliberately stateless: it reads the harnesses' retained JSONL files,
normalises one record per model call, and groups the result by actionable
dimensions.  Reasoning tokens are a subset of output and are never added twice.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_STATE = Path.home() / ".claude"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


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
    except (TypeError, ValueError):
        return 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


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


def _read_json_lines(path: Path) -> Iterable[tuple[int, dict]]:
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


def collect_claude(root: Path, start: datetime, end: datetime) -> list[AttributionRecord]:
    """Collect Claude calls, deduplicating streamed rows by requestId.

    Claude can write several content-block rows for a request.  Early rows may
    contain placeholder output counts, so every token field uses its maximum.
    """
    calls: dict[str, dict] = {}
    for path in _recent_files(root, "*.jsonl", start):
        is_subagent_path = "subagents" in path.parts
        for line_number, row in _read_json_lines(path):
            message = _mapping(row.get("message"))
            usage = _mapping(message.get("usage"))
            timestamp = parse_iso_timestamp(row.get("timestamp"))
            if not usage or timestamp is None:
                continue
            if not start <= timestamp < end:
                continue
            request_id = row.get("requestId")
            session_id = str(row.get("sessionId") or "unknown")
            if not request_id:
                request_id = row.get("uuid") or f"{path}:{line_number}"
            key = str(request_id)
            values = {
                "fresh_input": _nonnegative_int(usage.get("input_tokens")),
                "cache_read": _nonnegative_int(usage.get("cache_read_input_tokens")),
                "cache_write": _nonnegative_int(usage.get("cache_creation_input_tokens")),
                "output": _nonnegative_int(usage.get("output_tokens")),
                "reasoning": _nonnegative_int(
                    _mapping(usage.get("output_tokens_details")).get("thinking_tokens")
                ),
            }
            existing = calls.get(key)
            if existing is None:
                existing = {**values, "timestamp": timestamp}
                calls[key] = existing
            else:
                for field, value in values.items():
                    existing[field] = max(existing[field], value)
                existing["timestamp"] = max(existing["timestamp"], timestamp)

            # Dimensions should be stable for a request; prefer the latest row
            # when a streamed record becomes more complete.
            if timestamp >= existing["timestamp"]:
                agent_id = row.get("agentId")
                existing.update({
                    "session_id": session_id,
                    "model": str(message.get("model") or "unknown"),
                    "effort": str(row.get("effort") or "unknown"),
                    "project": _project_name(
                        row.get("cwd"), _claude_project_fallback(path, root)
                    ),
                    "entrypoint": str(row.get("entrypoint") or "unknown"),
                    "thread_kind": "subagent" if agent_id or is_subagent_path else "main",
                    "agent": str(row.get("attributionAgent") or agent_id or "main"),
                })

    records = [
        AttributionRecord(
            harness="claude", provider="anthropic", call_id=call_id, **values
        )
        for call_id, values in calls.items()
        if sum(
            values[field]
            for field in ("fresh_input", "cache_read", "cache_write", "output")
        ) > 0
    ]
    return sorted(records, key=lambda item: (item.timestamp, item.session_id, item.call_id))


def _codex_thread(source: object, thread_source: object) -> tuple[str, str]:
    if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        subagent = source["subagent"]
        spawn = subagent.get("thread_spawn") or {}
        agent = spawn.get("agent_nickname") or spawn.get("agent_role")
        if not agent and subagent.get("other"):
            agent = subagent["other"]
        return "subagent", str(agent or "subagent")
    source_name = str(thread_source or source or "main")
    if source_name in {"subagent", "automation"}:
        return source_name, source_name
    return "main", "main"


def collect_codex(root: Path, start: datetime, end: datetime) -> list[AttributionRecord]:
    """Collect Codex per-turn deltas from rollout JSONL files."""
    records: list[AttributionRecord] = []
    for path in _recent_files(root, "rollout-*.jsonl", start):
        session_id = path.stem
        provider = "openai"
        originator = "unknown"
        source: object = "unknown"
        thread_source: object = None
        cwd: object = None
        model = "unknown"
        effort = "unknown"
        event_index = 0
        last_total_signature = None

        for line_number, row in _read_json_lines(path):
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
                continue
            if row_type == "turn_context":
                model = str(payload.get("model") or model)
                effort = str(payload.get("effort") or payload.get("reasoning_effort") or effort)
                cwd = payload.get("cwd") or cwd
                continue
            if row_type != "event_msg" or payload.get("type") != "token_count":
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
                last_total_signature = signature
            if not start <= timestamp < end:
                continue

            event_index += 1
            total_input = _nonnegative_int(usage.get("input_tokens"))
            cache_read = _nonnegative_int(usage.get("cached_input_tokens"))
            cache_write = _nonnegative_int(usage.get("cache_write_input_tokens"))
            output = _nonnegative_int(usage.get("output_tokens"))
            reasoning = _nonnegative_int(usage.get("reasoning_output_tokens"))
            if total_input + cache_write + output <= 0:
                continue
            thread_kind, agent = _codex_thread(source, thread_source)
            ordinal = row.get("ordinal")
            ordinal_or_line = ordinal if ordinal is not None else line_number
            call_id = f"{session_id}:{ordinal_or_line}:{event_index}"
            records.append(AttributionRecord(
                harness="codex",
                provider=provider,
                timestamp=timestamp,
                session_id=session_id,
                call_id=call_id,
                model=model,
                effort=effort,
                project=_project_name(cwd),
                entrypoint=originator,
                thread_kind=thread_kind,
                agent=agent,
                fresh_input=max(0, total_input - cache_read - cache_write),
                cache_read=cache_read,
                cache_write=cache_write,
                output=output,
                reasoning=reasoning,
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
        description="Explain Claude Code and Codex token use by agent, project, model and session."
    )
    parser.add_argument("--harness", choices=("both", "claude", "codex"), default="both")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--date", help="Local calendar date (YYYY-MM-DD)")
    window.add_argument("--hours", type=float, help="Trailing number of hours (default: 24)")
    parser.add_argument("--limit", type=int, default=5, help="Rows per grouping (default: 5)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--claude-root", type=Path, default=CLAUDE_PROJECTS)
    parser.add_argument("--claude-state-dir", type=Path, default=CLAUDE_STATE)
    parser.add_argument("--codex-root", type=Path, default=CODEX_SESSIONS)
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
        "notes": [
            "Reasoning is a subset of output and is not double-counted.",
            "Transcript and rollout formats are internal and may change.",
        ],
    }
    if args.harness in ("both", "claude"):
        records = collect_claude(args.claude_root.expanduser(), start, end)
        summary = summarize_records(records, args.limit)
        monitor = load_claude_monitor_day(args.claude_state_dir.expanduser(), selected_day) \
            if selected_day else None
        summary["monitor_coverage"] = _coverage(monitor, summary["totals"])
        report["harnesses"]["claude"] = summary
    if args.harness in ("both", "codex"):
        records = collect_codex(args.codex_root.expanduser(), start, end)
        report["harnesses"]["codex"] = summarize_records(records, args.limit)

    if args.json:
        print(json.dumps(report, default=_jsonable, indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
