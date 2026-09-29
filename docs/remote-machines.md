# Other machines (for example a Raspberry Pi)

Every machine keeps its own history database. `meta.machine` holds a random id (`m-...`) and each
observation records the machine that first observed it. Nothing is shared live: you move a snapshot.

## Install on the Pi

Python 3.10 or newer, then install the wheel (stdlib only, no dependencies):

    pip install claude_code_energy_monitor-<version>-py3-none-any.whl

Refresh on a schedule, one line per harness in use (cron, `crontab -e`):

    */30 * * * * energy-monitor refresh --harness claude
    */30 * * * * energy-monitor refresh --harness pi
    */30 * * * * energy-monitor refresh --harness codex

Cron has a short `PATH`; with `pip install --user` use `~/.local/bin/energy-monitor` or set `PATH`.
`remote_sync.sh` runs `energy-monitor` through non-interactive ssh, so it must be on that `PATH` too.

## Bring it home

`remote_sync.sh` does this per host after the JSONL pulls, when `command -v energy-monitor` succeeds
remotely: `energy-monitor snapshot` on the host, `scp` to `~/.local/state/agentmon/remote/<tag>.sqlite3`
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
