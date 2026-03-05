#!/usr/bin/env python3
"""Run Codex, then print a Codex stepcount summary when it exits."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


SKIP_SUBCOMMANDS = {
    "help",
    "completion",
    "features",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "debug",
}


def should_print_summary(args: list[str]) -> bool:
    if any(arg in {"-h", "--help", "-V", "--version"} for arg in args):
        return False
    if args and not args[0].startswith("-") and args[0] in SKIP_SUBCOMMANDS:
        return False
    return True


def main() -> int:
    codex_exe = os.environ.get("CODEX_BIN", "codex")
    summary_script = Path(__file__).with_name("codex_stepcount.py")
    summary_args = shlex.split(os.environ.get("CODEX_SUMMARY_ARGS", ""))
    args = sys.argv[1:]

    rc = subprocess.run([codex_exe, *args]).returncode

    if should_print_summary(args):
        print()
        subprocess.run([sys.executable, str(summary_script), *summary_args], check=False)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
