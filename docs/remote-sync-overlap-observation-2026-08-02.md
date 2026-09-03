# Observation: overlapping `remote_sync.sh` runs

**Observed:** 2026-08-02 on the laptop, during a process inspection.
**Status:** observation only; no processes were terminated and no behavior was changed.

## What was observed

The scheduled `remote_sync.sh` jobs had accumulated instead of completing:

- approximately 110 live process entries belonged to overlapping `remote_sync.sh` invocations;
- approximately 74 SSH/rsync worker processes were present;
- the oldest observed invocation had been running for more than 24 hours;
- representative workers were sleeping in outbound SSH/rsync transfers.

This creates avoidable process and resource pressure. No data corruption was established by this observation.

## Likely contributing conditions

`remote_sync.sh` launches `rsync -az` directly for each remote and does not currently provide:

- a single-instance lock to prevent cron overlap; or
- a bounded SSH/connectivity or rsync I/O timeout.

A remote outage or stalled transfer can therefore keep one scheduled run alive until later runs accumulate behind it.

## Suggested follow-up

Create an implementation ticket covering:

1. single-instance locking with safe cleanup and stale-lock recovery;
2. bounded connection and transfer timeouts;
3. clear skip/timeout diagnostics;
4. behavior that avoids one unavailable remote preventing useful work for other remotes, where practical; and
5. regression coverage for overlap and timeout behavior.

The implementation should preserve the existing output filenames and missing-file handling. This note intentionally contains no credentials or private host locators.
