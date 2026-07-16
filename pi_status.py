#!/usr/bin/env python3
"""Pi companion status line based on Pi harness session JSONL files.

Pi stores normalized per-response usage under ~/.pi/agent/sessions/. This
script aggregates those records into daily, weekly, and monthly token and
order-of-magnitude energy summaries. It is suitable for a shell prompt, tmux
status, or a sidecar terminal; Pi's own footer already shows live-session use.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from codex_status import fmt_nrg, fmt_tok
from energy_constants import E_CACHE, E_CW, E_IN, E_OUT

PI_DIR = Path.home() / ".pi" / "agent"
SESSIONS_DIR = PI_DIR / "sessions"


@dataclass(frozen=True)
class UsageEvent:
    entry_id: str
    response_id: str
    session_id: str
    session_started_at: datetime
    timestamp: datetime
    provider: str
    model: str
    fresh_input: int
    cached_input: int
    cache_write: int
    output: int
    reasoning_output: int

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cached_input + self.cache_write + self.output

    @property
    def dedup_key(self) -> tuple:
        # responseId is provider-issued and stronger than Pi's 8-hex entry ID.
        # Older/provider-specific records may omit it, so retain a conservative
        # full-response fallback that makes accidental deduplication unlikely.
        if self.response_id:
            return ("response", self.provider, self.response_id)
        return (
            "entry",
            self.entry_id,
            self.timestamp,
            self.provider,
            self.model,
            self.fresh_input,
            self.cached_input,
            self.cache_write,
            self.output,
            self.reasoning_output,
        )


@dataclass
class AggregateTotals:
    fresh_input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning_output: int = 0
    sessions: int = 0

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cached_input + self.cache_write + self.output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a Pi harness token/energy status line from session JSONL files."
    )
    parser.add_argument(
        "--root", type=Path, default=SESSIONS_DIR,
        help="Pi sessions root (default: ~/.pi/agent/sessions)",
    )
    parser.add_argument(
        "--file", type=Path,
        help="Use one specific Pi session JSONL file as the active session.",
    )
    parser.add_argument("--watch", action="store_true", help="Continuously refresh the status line.")
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Refresh interval for --watch (seconds). Default: 2.0",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    return parser.parse_args()


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone()
    except ValueError:
        return None


def parse_session(path: Path) -> list[UsageEvent]:
    session_id = path.stem
    session_started_at: datetime | None = None
    events: list[UsageEvent] = []
    try:
        with path.open() as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                if entry.get("type") == "session":
                    session_id = str(entry.get("id") or session_id)
                    session_started_at = parse_timestamp(entry.get("timestamp")) or session_started_at
                    continue
                if entry.get("type") != "message":
                    continue

                message = entry.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                values = {
                    "fresh_input": int(usage.get("input", 0) or 0),
                    "cached_input": int(usage.get("cacheRead", 0) or 0),
                    "cache_write": int(usage.get("cacheWrite", 0) or 0),
                    "output": int(usage.get("output", 0) or 0),
                    "reasoning_output": int(usage.get("reasoning", 0) or 0),
                }
                if sum(values[key] for key in ("fresh_input", "cached_input", "cache_write", "output")) <= 0:
                    continue
                timestamp = parse_timestamp(entry.get("timestamp") or message.get("timestamp"))
                if timestamp is None:
                    continue
                events.append(UsageEvent(
                    entry_id=str(entry.get("id") or ""),
                    response_id=str(message.get("responseId") or ""),
                    session_id=session_id,
                    session_started_at=session_started_at or timestamp,
                    timestamp=timestamp,
                    provider=str(message.get("provider") or ""),
                    model=str(message.get("model") or "Pi"),
                    **values,
                ))
    except OSError:
        return []
    return events


_FILE_CACHE: dict[Path, tuple[int, int, list[UsageEvent]]] = {}


def _events_for_file(path: Path) -> list[UsageEvent]:
    try:
        before = path.stat()
    except OSError:
        return []
    cached = _FILE_CACHE.get(path)
    if cached and cached[0] == before.st_mtime_ns and cached[1] == before.st_size:
        return cached[2]

    events = parse_session(path)
    try:
        after = path.stat()
    except OSError:
        return events
    if before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size:
        _FILE_CACHE[path] = (after.st_mtime_ns, after.st_size, events)
    return events


def load_events(root: Path, explicit_file: Path | None = None) -> list[UsageEvent]:
    # A file's mtime advances whenever a continued session gets a response.
    # Ignore files untouched beyond the 30-day display horizon so prompt-time
    # integrations do not repeatedly parse an entire lifetime of sessions.
    cutoff = time.time() - 31 * 24 * 60 * 60
    files = []
    if root.exists():
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                continue
    files.sort()
    if explicit_file and explicit_file not in files:
        files.append(explicit_file)

    # Group copies first, then attribute each response to the earliest source
    # session. This keeps session counts stable even when a fork's project path
    # sorts before the original file.
    unique: dict[tuple, UsageEvent] = {}
    for path in files:
        for event in _events_for_file(path):
            existing = unique.get(event.dedup_key)
            if existing is None or event.session_started_at < existing.session_started_at:
                unique[event.dedup_key] = event
    return list(unique.values())


def energy_mid(totals: AggregateTotals) -> float:
    # Provider-agnostic proxy using the repository's documented token constants.
    return (
        totals.fresh_input / 1000 * E_IN
        + totals.cached_input / 1000 * E_CACHE
        + totals.cache_write / 1000 * E_CW
        + totals.output / 1000 * E_OUT
    )


def aggregate_range(events: list[UsageEvent], days: int) -> AggregateTotals:
    today = date.today()
    first_day = today - timedelta(days=days - 1)
    totals = AggregateTotals()
    session_ids: set[str] = set()
    for event in events:
        event_day = event.timestamp.date()
        if event_day < first_day or event_day > today:
            continue
        totals.fresh_input += event.fresh_input
        totals.cached_input += event.cached_input
        totals.cache_write += event.cache_write
        totals.output += event.output
        totals.reasoning_output += event.reasoning_output
        session_ids.add(event.session_id)
    totals.sessions = len(session_ids)
    return totals


def summarize_totals(totals: AggregateTotals) -> dict[str, object]:
    return {
        "fresh_input": totals.fresh_input,
        "cached_input": totals.cached_input,
        "cache_write": totals.cache_write,
        "output": totals.output,
        "reasoning_output": totals.reasoning_output,
        "total_tokens": totals.total_tokens,
        "sessions": totals.sessions,
        "energy_mwh": energy_mid(totals),
    }


def build_payload(root: Path, explicit_file: Path | None) -> dict[str, object]:
    events = load_events(root, explicit_file)
    active_events = parse_session(explicit_file) if explicit_file else events
    active = max(active_events, key=lambda event: event.timestamp, default=None)
    return {
        "active": {
            "file": str(explicit_file) if explicit_file else None,
            "provider": active.provider if active else None,
            "model": active.model if active else "Pi",
            "latest_call_tokens": active.total_tokens if active else 0,
            "has_usage": active is not None,
        },
        "day": summarize_totals(aggregate_range(events, 1)),
        "week": summarize_totals(aggregate_range(events, 7)),
        "month": summarize_totals(aggregate_range(events, 30)),
    }


def render_status(payload: dict[str, object]) -> str:
    active = payload["active"]
    parts = [str(active["model"])]
    for label, key in (("D", "day"), ("W", "week"), ("M", "month")):
        period = payload[key]
        parts.append(
            f"{label}:{fmt_tok(int(period['total_tokens']))} "
            f"{fmt_nrg(float(period['energy_mwh']))}"
        )
    return " | ".join(parts)


def print_watch(line: str) -> None:
    if sys.stdout.isatty():
        print(f"\r{line:<120}", end="", flush=True)
    else:
        print(line, flush=True)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser()
    explicit_file = args.file.expanduser() if args.file else None
    if args.watch:
        try:
            while True:
                payload = build_payload(root, explicit_file)
                print_watch(json.dumps(payload) if args.json else render_status(payload))
                time.sleep(max(args.interval, 0.2))
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            return 0

    payload = build_payload(root, explicit_file)
    print(json.dumps(payload) if args.json else render_status(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
