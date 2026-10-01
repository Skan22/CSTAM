#!/usr/bin/env bash
# CI entry point, independent of the CI vendor. Every stage that exists so far.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== control-plane: lint, types, tests"
(cd control-plane && uv sync --locked && uv run ruff check . && uv run mypy && uv run pytest -q)

echo "== system tests: lint, types, spec cross-checks"
(cd tests && uv sync --locked && uv run ruff check . && uv run mypy && uv run pytest -q)

echo "== isolation lab: every path against security-groups.yaml, both layers and each alone"
(cd tests && uv run python -m isolation.run -q)

if [[ "${IPO_CI_CHAOS:-}" == 1 ]]; then
  echo "== chaos lab: real Traefik, keepalived and agent (about four minutes)"
  (cd tests && uv run python -m chaos.run -q)
fi
