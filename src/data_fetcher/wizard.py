import os

def interactive_flow() -> tuple[list[str], str, str]:
    """Interactive wizard to guide the user when run in zero-args mode."""
    print("==========================================")
    print("Welcome to the Data Fetcher Pipeline")
    print("==========================================\n")
    
    topic = input("1. What specific topic or domain do you need datasets for? ").strip()
    
    print("\n2. Where should we fetch this data from?")
    valid_sources = ["kaggle", "openml", "sec", "fred", "airbnb", "yfinance"]
    print(f"   Supported data banks: {valid_sources}")
    print("   - Type the exact name of a specific site (or comma-separated list like 'kaggle, sec').")
    print("   - Type 'all' to run a Meta-Search across all supported sites.")
    print("   - Or simply press Enter to proceed.")
    
    source_input = input("Enter source: ").strip().lower()
    
    sources = []
    
    while True:
        if not source_input:
            ans = input("\n[Wizard] You did not specify a website. I will default to fetching from Kaggle. Is this acceptable, or would you prefer I pull from another site? (Type 'y' for Kaggle, 'all' to pull from all registered sites, or type specific sites like 'site1, site2'): ").strip().lower()
            if ans in ['y', 'yes']:
                sources = ["kaggle"]
                break
            elif ans == 'all':
                sources = ["all"]
                break
            elif ans:
                source_input = ans
                continue
            else:
                continue
        elif source_input == 'all':
            sources = ["all"]
            break
        else:
            raw_sources = [s.strip() for s in source_input.split(',')]
            valid_selections = []
            
            for s in raw_sources:
                if s in valid_sources:
                    valid_selections.append(s)
                else:
                    print(f"\n[Notice] The source '{s}' is not currently registered in the core pipeline.")
                    ans = input(f"[Wizard] Would you like me to initiate the New Source Discovery Protocol to check the terms and conditions (API rules, robots.txt) for '{s}' and guide you through the structural steps to add it? (y/n): ").strip().lower()
                    if ans == 'y':
                        print(f"\n{'='*50}\n NEW SOURCE DISCOVERY PROTOCOL: {s.upper()}\n{'='*50}")
                        print(f"Phase 1: Compliance & Terms of Service")
                        print(f"  - Check `https://{s}.com/robots.txt` for endpoint crawling permissions.")
                        print(f"  - Review the official API documentation for {s.upper()} for rate limits.")
                        print(f"  - Verify Authentication requirements (OAuth, Bearer Token, API Key).")
                        print("\nPhase 2: Structural Implementation Template")
                        print(f"  To integrate this source, create `data_fetcher/fetchers/{s}.py` and register the class in `data_fetcher/factory.py`:\n")
                        print(f"class {s.capitalize()}Fetcher(BaseFetcher):")
                        print(f"    def scout(self) -> dict:")
                        print(f"        print(f\"[Scout] Validating parameters for {s.upper()}...\")")
                        print(f"        return {{")
                        print(f"            \"url\": f\"https://{s}.com/api/\",")
                        print(f"            \"size_info\": \"Unknown (Dependent on runtime response)\"")
                        print(f"        }}")
                        print(f"\n    def extract(self) -> pd.DataFrame:")
                        print(f"        print(f\"[Extract] Pulling raw payloads from {s.upper()}...\")")
                        print(f"        import pandas as pd")
                        print(f"        df = pd.DataFrame()")
                        print(f"        return df")
                        print(f"{'='*50}")
            
            if valid_selections:
                sources = valid_selections
                break
            else:
                source_input = input("\n[Wizard] No valid sources configured. Please enter a valid source (or press Enter for Kaggle): ").strip().lower()
                
    topic_lower = topic.lower()
    if "sec" in sources and any(word in topic_lower for word in ["movie", "game", "sports", "anime"]):
        ans = input(f"\nWarning: SEC is for corporate financial filings, which is logically unrelated to '{topic}'. Proceed anyway, or drop 'sec'? (proceed/drop): ").strip().lower()
        if ans == "drop":
            sources.remove("sec")
            if not sources:
                sources = ["kaggle"]
                print("\n[Wizard] List empty after dropping sec. Switched source to Kaggle.")

    print("\n[Wizard] All parameters collected successfully. Initializing Fetcher Engine...\n")
    
    if not topic:
        topic = "finance"
    
    return sources, topic, os.path.join(os.getcwd(), "data_raw")


