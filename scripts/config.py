import os
import sys
import json
from getpass import getpass
from typing import Dict, Tuple, Optional
from pathlib import Path

from scripts.errors import DataFetchError

def _write_config(config: Dict[str, str], config_file: str) -> None:
    """Persist config JSON with owner-only file permissions where the OS supports it."""
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
    try:
        os.chmod(config_file, 0o600)
    except OSError:
        pass  # POSIX-style permissions are not applicable on every filesystem

def get_config_paths() -> Tuple[str, str]:
    config_dir = str(Path("~/.config/data-fetcher-pipeline").expanduser())
    config_file = str(Path(config_dir) / "config.json")
    return config_dir, config_file

def setup_wizard(non_interactive: bool = False) -> Dict[str, str]:
    config_dir, config_file = get_config_paths()
    
    
    if Path(config_file).exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            if isinstance(e, json.JSONDecodeError):
                # A corrupt config must never silently masquerade as "no keys configured"
                print(
                    f"[Config] WARNING: config file at {config_file} is corrupt ({e}). "
                    "Proceeding without it — fix the JSON syntax or delete the file.",
                    file=sys.stderr,
                )
            if non_interactive:
                return {}
            print(f"[Error] Failed to load config file: {e}")
            
    if non_interactive:
        return {}
        
    print("\nWelcome to the Data Fetcher Pipeline! Before we can fetch data, we need to configure your API keys (e.g., Kaggle, FRED).")
    print(f"Configuration is stored globally at: {config_file}\n")

    print("How would you like to set up your API keys?")
    print("  1. Auto-Setup (Recommended): You paste your keys here in the terminal (input is masked for secrets), and I will create the directory, generate the config.json file, and save them for you.")
    print("  2. Manual Setup: I will give you the exact folder path and JSON structure, and you can create and edit the file yourself.")

    choice = input("\nEnter your choice (1 or 2): ").strip()
    config: Dict[str, str] = {}

    if choice == '1':
        print("\n[Auto-Setup] Enter your keys below (press Enter to skip any key).")
        k_user = input("1. Kaggle Username: ").strip()
        if k_user:
            config["KAGGLE_USERNAME"] = k_user

        k_key = getpass("2. Kaggle API Key: ").strip()
        if k_key:
            config["KAGGLE_KEY"] = k_key

        s_key = getpass("3. SEC EDGAR API Key: ").strip()
        if s_key:
            config["SEC_API_KEY"] = s_key

        f_key = getpass("4. FRED API Key: ").strip()
        if f_key:
            config["FRED_API_KEY"] = f_key

        try:
            Path(config_dir).mkdir(parents=True, exist_ok=True)
            _write_config(config, config_file)
            print(f"\n[Success] Keys saved to {config_file} (owner-only permissions where the OS supports them).")
        except OSError as e:
            print(f"\n[Error] Failed to save keys: {e}")
        
    elif choice == '2':
        print("\n[Manual Setup]")
        print("Please run the following commands in another terminal:")
        print(f"  mkdir -p {config_dir}")
        print(f"  nano {config_file}")
        print("\nPaste the following JSON structure and fill in your keys:")
        print("{\n    \"KAGGLE_USERNAME\": \"\",\n    \"KAGGLE_KEY\": \"\",\n    \"SEC_API_KEY\": \"\",\n    \"FRED_API_KEY\": \"\"\n}")
        
        while True:
            done = input("\nType 'Done' once you have created and saved the file (or type 'Skip' to abort): ").strip().lower()
            if done == 'done':
                if Path(config_file).exists():
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                        print("[Success] Manual configuration detected and successfully parsed.")
                        break
                    except json.JSONDecodeError as e:
                        print(f"[Error] The file {config_file} contains invalid JSON syntax: {e}")
                        print("Please fix the JSON formatting and type 'Done' again.")
                    except OSError as e:
                        print(f"[Error] Failed to read file: {e}")
                else:
                    print(f"[Error] The file {config_file} was not found. Please create it or type 'Skip'.")
            elif done == 'skip':
                print("[Wizard] Proceeding without initial keys.")
                break
    else:
        print("\n[Wizard] Invalid choice. Proceeding with Lazy Loading.")
        
    return config

def get_api_key(key_name: str, config: Dict[str, str], prompt_msg: str, non_interactive: Optional[bool] = None) -> str:
    if non_interactive is None:
        raw_val = config.get("non_interactive", False)
        non_interactive = raw_val is True or (isinstance(raw_val, str) and raw_val.lower() == "true")
        
    if key_name in config and config[key_name]:
        return config[key_name]
    
    if non_interactive:
        env_val = os.environ.get(key_name)
        if env_val:
            config[key_name] = env_val
            return env_val
        _, config_file = get_config_paths()
        raise DataFetchError(
            f"Required API key '{key_name}' was not found in config or environment variables.\n"
            f"  Recovery options:\n"
            f"   1. Run 'python scripts/cli.py' interactively to launch setup wizard.\n"
            f"   2. Export environment variable: export {key_name}=<your_key>\n"
            f"   3. Add key to global config file: {config_file}\n"
            f"   4. If the config file is corrupt (invalid JSON), fix or remove it and retry.",
            code="AUTH_MISSING",
        )
        
    print(f"\n[Lazy Load] Missing required key: {key_name}")
    print(prompt_msg)
    val = getpass("").strip()
    if not val:
        raise ValueError(f"Required API key '{key_name}' was not provided.")

    config[key_name] = val
    config_dir, config_file = get_config_paths()
    try:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        _write_config(config, config_file)
    except OSError as e:
        print(f"[Warning] Failed to persist loaded API key: {e}")

    return val
