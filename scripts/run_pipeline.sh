#!/usr/bin/env bash
# Master entry script for data-fetcher-pipeline

set -e

echo "=========================================="
echo "Starting Data Fetcher Pipeline..."
echo "=========================================="

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

echo ""
read -p "Would you like to search OpenML for a dataset? (y/n) " use_openml
if [[ "$use_openml" =~ ^[Yy]$ ]]; then
    read -p "Enter your search query: " search_query
    if [ -n "$search_query" ]; then
        echo "Passing variables to Python Fetcher Engine for OpenML..."
        python "$DIR/fetcher_engine.py" --source openml --query "$search_query"
        exit 0
    fi
fi

echo "Passing variables to Python Fetcher Engine..."
python "$DIR/fetcher_engine.py" "$@"
