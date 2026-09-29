# Other machines (for example a Raspberry Pi)

Every machine keeps its own history database. `meta.machine` holds a random id (`m-...`) and each
observation records the machine that first observed it. Nothing is shared live: you move a snapshot.

## Install on the Pi

Python 3.10 or newer, stdlib only, no dependencies. Debian 12/13 and Raspberry Pi OS mark the system
Python as externally managed (PEP 668), so `pip install --user` fails. Use a venv, pinned to a commit
or tag:

    python3 -m venv ~/.local/share/energy-monitor/venv
    ~/.local/share/energy-monitor/venv/bin/pip install "git+https://github.com/Magnus-Gille/claude-code-energy-monitor@<commit>"
    ln -sf ~/.local/share/energy-monitor/venv/bin/energy-monitor ~/.local/bin/energy-monitor

`pipx install "git+https://github.com/Magnus-Gille/claude-code-energy-monitor@<commit>"` works as well.

Refresh on a schedule, one line per harness in use (cron, `crontab -e`). Cron has a short `PATH`, so
use the full path:

    */30 * * * * ~/.local/bin/energy-monitor refresh --harness claude
    */30 * * * * ~/.local/bin/energy-monitor refresh --harness pi
    */30 * * * * ~/.local/bin/energy-monitor refresh --harness codex

`remote_sync.sh` puts `~/.local/bin` on `PATH` locally and for every command it runs over
non-interactive ssh, so a user install is found on both ends. To try the sync without touching your
default database, set `ENERGY_MONITOR_DB=/path/to/test.sqlite3`; the local import then uses
`energy-monitor --db <path> import ...`.

## Bring it home

`remote_sync.sh` does this per host after the JSONL pulls, when `command -v energy-monitor` succeeds
remotely (with `~/.local/bin` prepended to `PATH`): `energy-monitor snapshot` on the host, `scp` to `~/.local/state/agentmon/remote/<tag>.sqlite3`
(directory mode 0700), then `energy-monitor import ... --label <tag>` locally. A host without it prints
`history: not installed on <tag>` and the loop continues.

By hand:

    ssh pi energy-monitor snapshot '~/.local/state/agentmon/snapshot.sqlite3'
    scp pi:.local/state/agentmon/snapshot.sqlite3 ./pi.sqlite3
    energy-monitor import ./pi.sqlite3 --label pi

`snapshot` writes a consistent 0600 copy with the SQLite backup API, atomically, and refuses the
database itself as target. `import` works on a temporary copy (an older schema is migrated on the copy,
never in your snapshot file) and refuses unknown schema versions.

## Identity and reports

- Observation keys are `(provider, harness, machine-if-synthetic, id)`. Provider request ids merge across
  machines (counters take the maximum, never a sum), so a request seen on two machines counts once.
  Ids that had to be synthesized stay per machine.
- Imported rows keep their original `machine`. When the same request exists on both machines the merged
  row carries the machine of the newer copy.
- Import is idempotent; importing a snapshot of this machine is a no-op with a warning.
- Source files are stored as `<machine>:<path>`, so they never collide with local paths, and `doctor`
  counts missing source files for local paths only.
- Each import is an `imports` row with harness `import` and root `<label>`; `meta.machine_labels`
  maps the source machine id to that label. Reports over the merged database cover all machines; use
  `report --records` to see per-observation `machine` and sources.
