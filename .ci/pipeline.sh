#!/usr/bin/env bash
# CI entry point, independent of the CI vendor. Every stage that exists so far.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== control-plane: lint, types, tests"
(cd control-plane && uv sync --locked && uv run ruff check . && uv run mypy && uv run pytest -q)
