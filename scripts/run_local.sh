#!/usr/bin/env bash
# Run the pipeline locally. Loads .env from the repo root if present.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

python -m src.main "$@"
