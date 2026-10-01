#!/usr/bin/env bash
# Checkout shim: the canonical script ships inside the package as tokenatlas/remote_sync.sh.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tokenatlas/remote_sync.sh" "$@"
