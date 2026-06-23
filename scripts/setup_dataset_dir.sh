#!/usr/bin/env bash
# Usage: ./setup_dataset_dir.sh <source_url_or_name> <file_format> <dataset_topic>

set -e

SOURCE="$1"
FORMAT="$2"
TOPIC="$3"

if [ -z "$TOPIC" ]; then
    echo "Error: Missing arguments." >&2
    echo "Usage: $0 <source_url_or_name> <file_format> <dataset_topic>" >&2
    exit 1
fi

# 1. Locate the Desktop directory
DESKTOP_DIR="${HOME}/Desktop"

# 2. Sanitize the Source URL
CLEAN_SOURCE=$(echo "$SOURCE" | sed -E 's|^https?://||ig; s|^www\.||ig')
CLEAN_SOURCE=$(echo "$CLEAN_SOURCE" | cut -d'/' -f1)
CLEAN_SOURCE=$(echo "$CLEAN_SOURCE" | sed -E 's/[<>:"/\|?* ]+/_/g')
CLEAN_SOURCE=$(echo "$CLEAN_SOURCE" | tr '[:upper:]' '[:lower:]')

# 3. Sanitize Format and Topic
CLEAN_FORMAT=$(echo "$FORMAT" | tr '[:lower:]' '[:upper:]')
CLEAN_TOPIC=$(echo "$TOPIC" | sed -E 's/[^a-zA-Z0-9_-]+/_/g' | tr '[:upper:]' '[:lower:]')

# 4. Construct the full path
TARGET_DIR="${DESKTOP_DIR}/datasets_of_data-fetcher-pipeline/${CLEAN_SOURCE}/${CLEAN_FORMAT}/${CLEAN_TOPIC}"

# 5. Create the structure safely
mkdir -p "$TARGET_DIR"

# 6. Echo the final path
echo "$TARGET_DIR"
