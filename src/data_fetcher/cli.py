import os
import sys
import argparse
import time
import random

from .config import setup_wizard
from .wizard import interactive_flow
from .factory import get_fetcher

def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Data Fetcher Background Engine")
    parser.add_argument("--source", required=False, help="Target data platform (e.g., yfinance, fred, airbnb)")
    parser.add_argument("--query", required=False, help="Topic, ticker symbol, or series ID")
    parser.add_argument("--outdir", default=os.path.join(os.getcwd(), "data_raw"), help="Destination directory")
    args = parser.parse_args()

    config = setup_wizard()
    
    try:
        # Zero-args mode triggers the interactive onboarding flow
        if not args.source or not args.query:
            sources, query, outdir = interactive_flow()
        else:
            sources = [s.strip().lower() for s in args.source.split(',')]
            query, outdir = args.query, args.outdir

        # Enforce Layer 2: Humanized Randomized Delays (simulating evasion tactics)
        delay = random.uniform(2, 5)
        print(f"[Engine] Imposing humanized delay of {delay:.2f} seconds to simulate human traffic...")
        time.sleep(delay)
        
        csv_paths = []
        for s in sources:
            if s == "all":
                sources_to_run = ["kaggle", "openml"]
            else:
                sources_to_run = [s]
                
            for target_source in sources_to_run:
                try:
                    fetcher = get_fetcher(target_source, query, outdir, config)
                    path = fetcher.run()
                    csv_paths.append(path)
                except NotImplementedError as e:
                    print(f"\n[Engine] Extraction for '{target_source}' failed: {e}\n")
                except Exception as e:
                    print(f"\n[Engine] Source '{target_source}' failed or was bypassed: {e}\n")
                    
        if not csv_paths:
            print("\n[Engine] No datasets extracted successfully. Pipeline aborting.")
            sys.exit(1)
            
        print(f"\n[Engine] Extraction Complete. {len(csv_paths)} datasets secured.")
        print(f"\n[Prompt] {len(csv_paths)} dataset(s) fetched successfully. Would you like to initialize the Format Alchemy engine to convert them to SQL and Excel? (y/n)")
        alchemy_choice = input().strip().lower()
        if alchemy_choice == 'y':
            from data_fetcher.format_alchemy import run_alchemy
            for path in csv_paths:
                print(f"\n[Engine] Starting Format Alchemy for: {os.path.basename(path)}")
                run_alchemy(path)
        
    except (ValueError, RuntimeError, ImportError) as e:
        print(f"\n[Error] {e}")
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n[Engine] Execution aborted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
