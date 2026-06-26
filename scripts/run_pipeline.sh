#!/usr/bin/env bash
# Master entry script for data-fetcher-pipeline

set -e

echo "=========================================="
echo "Starting Data Fetcher Pipeline..."
echo "=========================================="

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Delegate all interactive routing to the Python Engine Wizard 2.0
python "$DIR/fetcher_engine.py" "$@"
