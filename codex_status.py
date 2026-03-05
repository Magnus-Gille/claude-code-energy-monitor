#!/usr/bin/env python3
"""Codex companion status line based on rollout JSONL files.

Codex does not expose a Claude-style custom statusline hook. Instead, it
writes append-only rollout logs under ~/.codex/sessions/. This script reads
those logs, derives per-session token totals, and renders a one-line status
summary suitable for a shell prompt, tmux status, or a sidecar terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from energy_constants import E_CACHE, E_IN, E_OUT

CODEX_DIR = Path.home() / ".codex"
SESSIONS_DIR = CODEX_DIR / "sessions"
CACHE_FILE = CODEX_DIR / "statusline_rollout_cache.json"


@dataclass
class RolloutSummary:
    path: Path
    session_id: str
    model: str
    q5: float | None
    q7: float | None
    context_window: int | None
    total_input: int
    cached_input: int
    output: int
    reasoning_output: int
    latest_timestamp: str
    has_usage: bool

    @property
    def fresh_input(self) -> int:
        return max(0, self.total_input - self.cached_input)

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cached_input + self.output


@dataclass
class AggregateTotals:
    fresh_input: int = 0
    cached_input: int = 0
    output: int = 0
    reasoning_output: int = 0
    sessions: int = 0

    @property
    def total_tokens(self) -> int:
        return self.fresh_input + self.cached_input + self.output


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render a Codex token/energy status line from rollout JSONL files."
    )
    p.add_argument(
        "--root",
        type=Path,
        default=SESSIONS_DIR,
        help="Codex sessions root (default: ~/.codex/sessions)",
    )
    p.add_argument(
        "--file",
        type=Path,
        help="Use one specific rollout JSONL file as the active session source.",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Continuously refresh the status line.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval for --watch (seconds). Default: 2.0",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of a formatted status line.",
    )
    return p.parse_args()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def fmt_tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_nrg(mwh: float) -> str:
    if mwh < 1:
        return "~0"

    import math

    log = math.log10(mwh)
    decade = int(math.floor(log))
    frac = log - decade
    if frac < 0.15:
        val = 10 ** decade
    elif frac < 0.50:
        val = 2 * 10 ** decade
    elif frac < 0.85:
        val = 5 * 10 ** decade
    else:
        val = 10 ** (decade + 1)
    val = round(val)
    if val < 1000:
        return f"~{val}mWh"
    if val < 1_000_000:
        return f"~{val / 1000:g}Wh"
    return f"~{val / 1_000_000:g}kWh"


def energy_mid(fresh_input: int, cached_input: int, output: int) -> float:
    # Codex rollout logs currently expose cache reads but not cache writes.
    return (
        fresh_input / 1000 * E_IN
        + cached_input / 1000 * E_CACHE
        + output / 1000 * E_OUT
    )


def iter_rollout_files(root: Path, day: date) -> list[Path]:
    day_dir = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    if not day_dir.exists():
        return []
    return sorted(day_dir.glob("rollout-*.jsonl"))


def parse_rollout(path: Path) -> RolloutSummary | None:
    session_id = path.stem
    model = "Codex"
    q5 = None
    q7 = None
    context_window = None
    total_input = 0
    cached_input = 0
    output = 0
    reasoning_output = 0
    latest_timestamp = ""
    has_usage = False

    try:
        with path.open() as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                ts = entry.get("timestamp")
                if isinstance(ts, str):
                    latest_timestamp = ts

                entry_type = entry.get("type")
                payload = entry.get("payload") or {}

                if entry_type == "session_meta":
                    session_id = payload.get("id", session_id)
                    continue

                if entry_type == "turn_context":
                    model = payload.get("model") or model
                    continue

                if entry_type != "event_msg" or payload.get("type") != "token_count":
                    continue

                rate_limits = payload.get("rate_limits") or {}
                primary = rate_limits.get("primary") or {}
                secondary = rate_limits.get("secondary") or {}
                if primary.get("used_percent") is not None:
                    q5 = float(primary["used_percent"])
                if secondary.get("used_percent") is not None:
                    q7 = float(secondary["used_percent"])

                info = payload.get("info")
                if not info:
                    continue

                usage = info.get("total_token_usage") or {}
                total_input = int(usage.get("input_tokens", 0) or 0)
                cached_input = int(usage.get("cached_input_tokens", 0) or 0)
                output = int(usage.get("output_tokens", 0) or 0)
                reasoning_output = int(usage.get("reasoning_output_tokens", 0) or 0)
                context_window = info.get("model_context_window")
                has_usage = True
    except OSError:
        return None

    return RolloutSummary(
        path=path,
        session_id=session_id,
        model=model,
        q5=q5,
        q7=q7,
        context_window=context_window,
        total_input=total_input,
        cached_input=cached_input,
        output=output,
        reasoning_output=reasoning_output,
        latest_timestamp=latest_timestamp,
        has_usage=has_usage,
    )


def summary_to_dict(summary: RolloutSummary | None) -> dict | None:
    if summary is None:
        return None
    return {
        "path": str(summary.path),
        "session_id": summary.session_id,
        "model": summary.model,
        "q5": summary.q5,
        "q7": summary.q7,
        "context_window": summary.context_window,
        "total_input": summary.total_input,
        "cached_input": summary.cached_input,
        "output": summary.output,
        "reasoning_output": summary.reasoning_output,
        "latest_timestamp": summary.latest_timestamp,
        "has_usage": summary.has_usage,
    }


def summary_from_dict(data: dict | None) -> RolloutSummary | None:
    if not data:
        return None
    return RolloutSummary(
        path=Path(data["path"]),
        session_id=data["session_id"],
        model=data["model"],
        q5=data["q5"],
        q7=data["q7"],
        context_window=data["context_window"],
        total_input=data["total_input"],
        cached_input=data["cached_input"],
        output=data["output"],
        reasoning_output=data["reasoning_output"],
        latest_timestamp=data["latest_timestamp"],
        has_usage=data["has_usage"],
    )


def load_summaries(files: list[Path]) -> list[RolloutSummary]:
    cache = load_json(CACHE_FILE)
    cached_files = cache.get("files", {})
    next_cache: dict[str, dict] = {}
    summaries: list[RolloutSummary] = []

    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue

        key = str(path)
        cached = cached_files.get(key)
        summary: RolloutSummary | None
        if (
            cached
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("size") == stat.st_size
        ):
            summary = summary_from_dict(cached.get("summary"))
        else:
            summary = parse_rollout(path)

        next_cache[key] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "summary": summary_to_dict(summary),
        }
        if summary is not None:
            summaries.append(summary)

    if next_cache != cached_files:
        save_json(CACHE_FILE, {"files": next_cache})
    return summaries


def aggregate_day(summaries: list[RolloutSummary], day: date) -> AggregateTotals:
    totals = AggregateTotals()
    day_dir = f"/{day.year:04d}/{day.month:02d}/{day.day:02d}/"
    for summary in summaries:
        if day_dir not in str(summary.path) or not summary.has_usage:
            continue
        totals.fresh_input += summary.fresh_input
        totals.cached_input += summary.cached_input
        totals.output += summary.output
        totals.reasoning_output += summary.reasoning_output
        totals.sessions += 1
    return totals


def aggregate_range(summaries: list[RolloutSummary], days: int) -> AggregateTotals:
    today = date.today()
    totals = AggregateTotals()
    for offset in range(days):
        day_totals = aggregate_day(summaries, today - timedelta(days=offset))
        totals.fresh_input += day_totals.fresh_input
        totals.cached_input += day_totals.cached_input
        totals.output += day_totals.output
        totals.reasoning_output += day_totals.reasoning_output
        totals.sessions += day_totals.sessions
    return totals


def build_payload(root: Path, explicit_file: Path | None) -> dict[str, object]:
    today = date.today()
    files: list[Path] = []
    for offset in range(30):
        files.extend(iter_rollout_files(root, today - timedelta(days=offset)))
    if explicit_file and explicit_file not in files:
        files.append(explicit_file)

    summaries = load_summaries(files)
    active = None
    if explicit_file:
        active = next((s for s in summaries if s.path == explicit_file), None)
    elif summaries:
        active = max(summaries, key=lambda s: s.path.stat().st_mtime_ns)

    day_totals = aggregate_range(summaries, 1)
    week_totals = aggregate_range(summaries, 7)
    month_totals = aggregate_range(summaries, 30)

    return {
        "active": {
            "file": str(active.path) if active else None,
            "session_id": active.session_id if active else None,
            "model": active.model if active else "Codex",
            "quota_5h_pct": active.q5 if active else None,
            "quota_7d_pct": active.q7 if active else None,
            "context_window": active.context_window if active else None,
            "has_usage": active.has_usage if active else False,
        },
        "day": summarize_totals(day_totals),
        "week": summarize_totals(week_totals),
        "month": summarize_totals(month_totals),
    }


def summarize_totals(totals: AggregateTotals) -> dict[str, object]:
    return {
        "fresh_input": totals.fresh_input,
        "cached_input": totals.cached_input,
        "output": totals.output,
        "reasoning_output": totals.reasoning_output,
        "total_tokens": totals.total_tokens,
        "sessions": totals.sessions,
        "energy_mwh": energy_mid(
            totals.fresh_input, totals.cached_input, totals.output
        ),
    }


def render_status(payload: dict[str, object]) -> str:
    active = payload["active"]
    day = payload["day"]
    week = payload["week"]
    month = payload["month"]

    parts = [str(active["model"])]

    context_window = active["context_window"]
    if isinstance(context_window, int) and context_window > 0:
        parts.append(f"Win:{fmt_tok(context_window)}")

    q5 = active["quota_5h_pct"]
    q7 = active["quota_7d_pct"]
    if isinstance(q5, (int, float)):
        quota = f"5h:{q5:.0f}%"
        if isinstance(q7, (int, float)):
            quota += f" 7d:{q7:.0f}%"
        parts.append(quota)

    parts.append(f"D:{fmt_tok(int(day['total_tokens']))} {fmt_nrg(float(day['energy_mwh']))}")
    parts.append(
        f"W:{fmt_tok(int(week['total_tokens']))} {fmt_nrg(float(week['energy_mwh']))}"
    )
    parts.append(
        f"M:{fmt_tok(int(month['total_tokens']))} {fmt_nrg(float(month['energy_mwh']))}"
    )
    return " | ".join(parts)


def print_watch(line: str) -> None:
    if sys.stdout.isatty():
        width = max(len(line), 120)
        print(f"\r{line:<{width}}", end="", flush=True)
        return
    print(line, flush=True)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser()
    explicit_file = args.file.expanduser() if args.file else None

    if args.watch:
        try:
            while True:
                payload = build_payload(root, explicit_file)
                line = json.dumps(payload) if args.json else render_status(payload)
                print_watch(line)
                time.sleep(max(args.interval, 0.2))
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            return 0

    payload = build_payload(root, explicit_file)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_status(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
