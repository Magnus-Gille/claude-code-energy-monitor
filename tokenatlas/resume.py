"""How to get back into the conversation behind a ranked turn: a shell command per harness and, for Codex, a desktop deep link.

Built from stored (private) turn data only; both open the whole conversation, never a specific turn."""
from __future__ import annotations
import os
import re
import shlex
import subprocess

# An id goes into a command line, so it must not look like an option or carry shell-relevant bytes (quoting is a second line of defence).
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
# Each tool's own resume invocation (from its --help); Claude Code sessions are stored per project, so it needs the directory.
COMMANDS = {'claude': ['claude', '--resume'], 'codex': ['codex', 'resume'], 'pi': ['pi', '--session'], 'opencode': ['opencode', '--session']}


def _windows():
    """The quoting platform when the caller does not say (patched by tests that compare POSIX command text on any OS)."""
    return os.name == 'nt'


def shell_command(cmd, windows=None):
    """cmd quoted for the user's shell; on Windows None unless every argument is plain, since cmd.exe has no quoting that is safe for every
    character (&, |, ^, %, !, ...), so only the path is shown there."""
    if not (_windows() if windows is None else windows):
        return shlex.join(cmd)
    return None if any(re.search(r'[^\w\-.:\\/ ]', a) for a in cmd) else subprocess.list2cmdline(cmd)


def codex_link(session):
    """codex://threads/<id> for a Codex rollout's session_meta id; None unless it is UUID-shaped."""
    return f'codex://threads/{session}' if isinstance(session, str) and UUID.fullmatch(session) else None


def resume_command(harness, session, cwd=None, windows=None):
    """`cd <cwd> && <tool> <resume> <id>` for the harness, or None when unknown, unsafe, or (Claude Code) the directory is missing."""
    base = COMMANDS.get(harness)
    if base is None or not isinstance(session, str) or not SAFE_ID.fullmatch(session):
        return None
    has_cwd = isinstance(cwd, str) and bool(cwd.strip())
    if has_cwd and CONTROL.search(cwd):
        return None
    if not has_cwd and harness == 'claude':
        return None
    win = _windows() if windows is None else windows
    parts = [shell_command([*base, session], windows=win)]
    if has_cwd:
        parts.insert(0, shell_command(['cd', '/d', cwd] if win else ['cd', cwd], windows=win))
    return None if None in parts else ' && '.join(parts)


def resume_info(harness, session, cwd=None, windows=None):
    """{'command': str|None, 'codex_link': str|None}, or None when neither is known."""
    info = {'command': resume_command(harness, session, cwd, windows), 'codex_link': codex_link(session) if harness == 'codex' else None}
    return info if info['command'] or info['codex_link'] else None
