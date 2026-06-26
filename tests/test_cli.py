import sys
from unittest.mock import ANY, MagicMock, patch

import pytest

from data_fetcher.cli import main


def test_help_flag_does_not_trigger_wizard() -> None:
    with patch.object(sys, "argv", ["cmd", "--help"]), \
         patch("data_fetcher.cli.setup_wizard") as mock_setup_wizard:
        
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 0
        mock_setup_wizard.assert_not_called()


def test_unknown_source_raises_immediately() -> None:
    with patch.object(sys, "argv", ["cmd", "--source=github", "--query=test"]), \
         patch("data_fetcher.cli.setup_wizard") as mock_setup_wizard, \
         patch("data_fetcher.cli.time.sleep"):
        
        mock_setup_wizard.return_value = {}
        
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 1


def test_valid_source_instantiates_correct_fetcher() -> None:
    mock_instance = MagicMock()
    mock_instance.run.return_value = "dummy_path.csv"

    with patch.object(sys, "argv", ["cmd", "--source=fred", "--query=GDP"]), \
         patch("data_fetcher.cli.setup_wizard", return_value={"api_key": "dummy"}), \
         patch("data_fetcher.cli.get_fetcher", return_value=mock_instance) as mock_get_fetcher, \
         patch("data_fetcher.cli.time.sleep"), \
         patch("builtins.input", return_value="n"):

        main()

        mock_get_fetcher.assert_called_once_with("fred", "GDP", ANY, {"api_key": "dummy"})
        mock_instance.run.assert_called_once()
