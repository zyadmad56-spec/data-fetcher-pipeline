import os
import json
import pytest
from unittest.mock import patch, mock_open

from scripts.fetcher_engine import (
    setup_wizard,
    get_fetcher,
    FREDFetcher,
    OpenMLFetcher,
    GenericFetcher
)

def test_setup_wizard_existing_valid_config():
    """Verify that an existing valid config.json bypasses the wizard and loads state."""
    mock_config = '{"FRED_API_KEY": "test_key"}'
    with patch('os.path.exists', return_value=True):
        with patch('builtins.open', mock_open(read_data=mock_config)):
            config = setup_wizard()
            assert config.get("FRED_API_KEY") == "test_key"

def test_setup_wizard_manual_invalid_json():
    """Verify JSONDecodeError resilience during manual wizard configuration."""
    with patch('os.path.exists', side_effect=[False, True, True]):
        # Mock inputs: select manual (2), enter 'done' (json fails), enter 'skip' to exit loop
        with patch('builtins.input', side_effect=['2', 'done', 'skip']):
            with patch('builtins.open', mock_open(read_data='{invalid_json:')):
                with patch('json.load', side_effect=json.JSONDecodeError("Expecting value", "", 0)):
                    config = setup_wizard()
                    # After skipping, config should be an empty dictionary
                    assert isinstance(config, dict)
                    assert len(config) == 0

def test_get_fetcher_strategy_routing():
    """Verify polymorphic Strategy pattern extraction routing."""
    config = {}
    
    fetcher_fred = get_fetcher("fred", "GDP", "/tmp/out", config)
    assert isinstance(fetcher_fred, FREDFetcher)
    
    fetcher_openml = get_fetcher("openml", "finance", "/tmp/out", config)
    assert isinstance(fetcher_openml, OpenMLFetcher)
    
    # Unimplemented or unknown source should fallback to GenericFetcher
    fetcher_generic = get_fetcher("unknown_source", "data", "/tmp/out", config)
    assert isinstance(fetcher_generic, GenericFetcher)
