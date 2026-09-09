#!/usr/bin/env bash
# Run the pack's test suite. Requires pytest.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 -m pytest tests/ "$@"
