#!/usr/bin/env bash
# Master entry script for data-fetcher-pipeline
# Handles interactive UX for environment setup and delegates complex tasks to Python.

set -e

ENV_FILE=".env"

echo "=========================================="
echo "🚀 Starting Data Fetcher Pipeline..."
echo "=========================================="

# 1. Check if the .env file exists; if not, create an empty one
if [ ! -f "$ENV_FILE" ]; then
    echo "⚠️  No .env file found. Let's set up your API keys interactively."
    touch "$ENV_FILE"
else
    echo "✅ Found existing .env file."
fi

# 2. Load existing environment variables safely
set -a
source "$ENV_FILE" 2>/dev/null || true
set +a

# 3. Helper function to check if a key is empty and prompt the user if needed
prompt_for_key() {
    local key_name=$1
    local prompt_msg=$2
    local current_value="${!key_name}"
    
    if [ -z "$current_value" ]; then
        read -p "$prompt_msg" user_input
        if [ -n "$user_input" ]; then
            # Automatically populate the .env file with correct syntax
            echo "$key_name=$user_input" >> "$ENV_FILE"
            export $key_name="$user_input"
        fi
    fi
}

# 4. Explicitly prompt the user for any missing or empty keys
prompt_for_key "KAGGLE_USERNAME" "No Kaggle Username found. Please enter your Kaggle Username (or press Enter to skip): "
prompt_for_key "KAGGLE_KEY" "No Kaggle API Key found. Please enter your Kaggle API Key (or press Enter to skip): "
prompt_for_key "SEC_API_KEY" "No SEC API Key found. Please enter your SEC EDGAR API Key (or press Enter to skip): "
prompt_for_key "FRED_API_KEY" "No FRED API Key found. Please enter your FRED API Key (or press Enter to skip): "

echo "✅ Environment configured securely."

# 5. Hand off to the Python Engine
# The Bash script gracefully passes these exported environment variables
# to our Python script for handling complex API fetching and data processing.

echo ""
read -p "Would you like to search OpenML for a dataset? (y/n) " use_openml
if [[ "$use_openml" =~ ^[Yy]$ ]]; then
    read -p "Enter your search query: " search_query
    if [ -n "$search_query" ]; then
        echo "🔄 Passing variables to Python Fetcher Engine for OpenML..."
        python scripts/fetcher_engine.py --source openml --query "$search_query"
        exit 0
    fi
fi

echo "🔄 Passing variables to Python Fetcher Engine..."
python scripts/fetcher_engine.py "$@"
