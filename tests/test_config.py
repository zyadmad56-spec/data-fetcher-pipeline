import json
import pathlib
from unittest.mock import MagicMock, mock_open, patch

import pytest

from data_fetcher.config import get_api_key


def test_get_api_key_returns_cached_value() -> None:
    config = {"FRED_API_KEY": "cached_key"}
    result = get_api_key("FRED_API_KEY", config, "Prompt: ")
    assert result == "cached_key"


@patch("data_fetcher.config.os.makedirs")
@patch("builtins.open", mock_open())
@patch("data_fetcher.config.json.dump")
@patch("builtins.input", return_value="new_key_value")
def test_get_api_key_prompts_and_persists(
    mock_input: MagicMock,
    mock_dump: MagicMock,
    mock_makedirs: MagicMock,
) -> None:
    config: dict[str, str] = {}

    result = get_api_key("FRED_API_KEY", config, "Enter key: ")

    assert result == "new_key_value"
    assert config["FRED_API_KEY"] == "new_key_value"
    mock_dump.assert_called_once()
    mock_makedirs.assert_called_once()


@patch("builtins.input", return_value="")
def test_get_api_key_raises_on_empty_input(mock_input: MagicMock) -> None:
    config: dict[str, str] = {}

    with pytest.raises(ValueError, match="was not provided"):
        get_api_key("FRED_API_KEY", config, "Enter key: ")


def test_get_api_key_does_not_reprompt_for_existing_key() -> None:
    config = {"SEC_API_KEY": "existing"}

    with patch("builtins.input") as mock_input:
        result = get_api_key("SEC_API_KEY", config, "Prompt: ")

    mock_input.assert_not_called()
    assert result == "existing"
