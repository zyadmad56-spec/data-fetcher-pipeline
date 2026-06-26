import sys
from unittest.mock import ANY, MagicMock, patch

import pytest

from scripts.cli import main


def test_help_flag_does_not_trigger_wizard() -> None:
    with patch.object(sys, "argv", ["cmd", "--help"]), \
         patch("scripts.cli.setup_wizard") as mock_setup_wizard:
        
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 0
        mock_setup_wizard.assert_not_called()


def test_unknown_source_raises_immediately() -> None:
    with patch.object(sys, "argv", ["cmd", "--source=github", "--query=test"]), \
         patch("scripts.cli.setup_wizard") as mock_setup_wizard, \
         patch("scripts.cli.time.sleep"):
        
        mock_setup_wizard.return_value = {}
        
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 1


def test_valid_source_instantiates_correct_fetcher() -> None:
    with patch.object(sys, "argv", ["cmd", "--source=fred", "--query=GDP"]), \
         patch("scripts.cli.setup_wizard") as mock_setup_wizard, \
         patch("scripts.factory.FREDFetcher") as mock_fred_class, \
         patch("scripts.cli.time.sleep"), \
         patch("builtins.input") as mock_input:
        
        mock_setup_wizard.return_value = {"api_key": "dummy"}
        mock_input.return_value = "n"
        
        mock_instance = MagicMock()
        mock_instance.run.return_value = "dummy_path.csv"
        mock_fred_class.return_value = mock_instance
        
        main()
        
        mock_fred_class.assert_called_once_with("GDP", ANY, {"api_key": "dummy"})
        mock_instance.run.assert_called_once()
