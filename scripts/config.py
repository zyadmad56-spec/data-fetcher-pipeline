import os
import sys
import json
from typing import Dict

DEFAULT_REQUEST_TIMEOUT_SECONDS: int = 30

def get_config_paths() -> tuple[str, str]:
    config_dir = os.path.expanduser("~/.config/data-fetcher-pipeline")
    config_file = os.path.join(config_dir, "config.json")
    return config_dir, config_file

def setup_wizard() -> Dict[str, str]:
    config_dir, config_file = get_config_paths()
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"\n[Warning] The existing configuration file ({config_file}) is corrupt or contains invalid JSON.")
            print(f"Error Details: {e}")
            ans = input("Would you like to overwrite it and reconfigure? (y/n): ").strip().lower()
            if ans not in ['y', 'yes']:
                print("[Wizard] Exiting to prevent configuration conflicts. Please fix the file manually.")
                sys.exit(1)
            print("[Wizard] Proceeding to reconfigure...\n")
            
    print("\nWelcome to the Data Fetcher Pipeline! Before we can fetch data, we need to configure your API keys (e.g., Kaggle, FRED).")
    print(f"To ensure security and avoid exposing keys in your project folder, configurations will be saved globally at: {config_file}\n")
    
    print("How would you like to set up your API keys?")
    print("  1. Auto-Setup (Recommended): You paste your keys here in the terminal, and I will securely create the directory, generate the config.json file, and save them for you.")
    print("  2. Manual Setup: I will give you the exact folder path and JSON structure, and you can create and edit the file yourself.")
    
    choice = input("\nEnter your choice (1 or 2): ").strip()
    
    config: Dict[str, str] = {}
    
    if choice == '1':
        print("\n[Auto-Setup] Enter your keys below (press Enter to skip any key).")
        print("1. Kaggle Username: ", end="")
        k_user = input().strip()
        if k_user: config["KAGGLE_USERNAME"] = k_user
        
        print("2. Kaggle API Key: ", end="")
        k_key = input().strip()
        if k_key: config["KAGGLE_KEY"] = k_key
        
        print("3. SEC EDGAR API Key: ", end="")
        s_key = input().strip()
        if s_key: config["SEC_API_KEY"] = s_key
        
        print("4. FRED API Key: ", end="")
        f_key = input().strip()
        if f_key: config["FRED_API_KEY"] = f_key
        
        os.makedirs(config_dir, exist_ok=True)
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        if os.name != 'nt':
            os.chmod(config_file, 0o600)
        print("\n[Success] Keys securely saved globally!")
        
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
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                        print("[Success] Manual configuration detected and successfully parsed.")
                        break
                    except json.JSONDecodeError as e:
                        print(f"[Error] The file {config_file} contains invalid JSON syntax: {e}")
                        print("Please fix the JSON formatting and type 'Done' again.")
                else:
                    print(f"[Error] The file {config_file} was not found. Please create it or type 'Skip'.")
            elif done == 'skip':
                print("[Wizard] Proceeding without initial keys (Lazy Loading active).")
                break
    else:
        print("\n[Wizard] Invalid choice. Proceeding with Lazy Loading.")
        
    return config

def get_api_key(key_name: str, config: Dict[str, str], prompt_msg: str) -> str:
    if key_name in config and config[key_name]:
        return config[key_name]
    
    print(f"\n[Lazy Load] Missing required key: {key_name}")
    print(prompt_msg)
    val = input().strip()
    if not val:
        raise ValueError(f"Required API key '{key_name}' was not provided.")
        
    config[key_name] = val
    config_dir, config_file = get_config_paths()
    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
    if os.name != 'nt':
        os.chmod(config_file, 0o600)
        
    return val

