#!/bin/bash
# Restore the django-blog fixture to its clean initial state.
# 
# Usage:
#   ./scripts/restore_initial.sh
#
# This:
#   1. Stashes any uncommitted changes
#   2. Resets to the v0.1-initial tag
#   3. Clears all untracked files (except scripts, docker-compose)
#   4. Re-runs migrations and seeds data
#   5. Verifies initial state

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Restoring to v0.1-initial ==="

# Stash + reset to initial state
git stash --include-untracked 2>/dev/null || true
git reset --hard v0.1-initial
git clean -fd -e scripts/ -e docker-compose.yml -e .gitignore 2>/dev/null || true

echo "=== Running migrations ==="
python -m django migrate --settings=config.settings

echo "=== Seeding data ==="
python scripts/seed_data.py

echo "=== Verifying ==="
python scripts/verify_initial.py

echo "=== Done ==="
echo "Fixture is ready for performance optimization tasks."
echo "Run: agent_go run ... --tag v0.1-initial"
