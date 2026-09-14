import sys
from pathlib import Path

# Bootstrap path when executed directly as a script
if __name__ == "__main__":
    parent_dir = str(Path(__file__).resolve().parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

import json
import argparse
import time
import random
from typing import Dict, Optional, Tuple

# Guaranteed startup envelope: if the engine itself fails to import (broken
# dependency, corrupt install), the CLI must still answer in JSON with exit 3
# rather than dying with a raw traceback and an empty stdout.
_startup_error: Optional[Exception] = None
try:
    from scripts.config import setup_wizard
    from scripts.errors import DataFetchError
    from scripts.factory import get_fetcher, list_sources
except Exception as _exc:  # pragma: no cover - exercised via import-failure tests
    _startup_error = _exc

def _fallback_code(exc: Exception) -> str:
    """Derive a stable error code for exceptions raised without one."""
    if isinstance(exc, ImportError):
        return "DEPENDENCY_MISSING"
    msg = str(exc).lower()
    if isinstance(exc, OSError) and ("file exists" in msg or "cannot create" in msg):
        return "INVALID_OUTPUT"
    if msg.startswith("blocked"):
        return "BLOCKED_URL"
    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return "RATE_LIMITED"
    if "api key" in msg or "credentials" in msg:
        return "AUTH_MISSING"
    if "unsupported data source" in msg:
        return "UNSUPPORTED_SOURCE"
    if "requires a" in msg and "source" in msg:
        # "Conversion from X to Y requires a ... source." — an unsupported pair
        return "UNSUPPORTED_CONVERSION"
    if "not supported" in msg and ("format" in msg or "conversion" in msg):
        return "UNSUPPORTED_CONVERSION"
    if "not found" in msg or "does not exist" in msg or "no cryptocurrency" in msg or "invalid indicator" in msg:
        return "NOT_FOUND"
    if "connection" in msg or "timeout" in msg or "network" in msg:
        return "NETWORK"
    return "PROVIDER_ERROR"

def _gc_orphan_dir(fetcher) -> None:
    """Remove the per-fetch directory when a failed run left it empty (no debris)."""
    try:
        from scripts.base import provision_data_directory  # noqa: F401 (platforms list lives in base)
        outdir = getattr(fetcher, "outdir", None)
        if not outdir:
            return
        d = Path(outdir)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            parent = d.parent
            if parent.is_dir() and parent.name != Path(outdir).drive and not any(parent.iterdir()) and parent != Path(outdir).anchor:
                try:
                    parent.rmdir()
                except OSError:
                    pass
    except (OSError, TypeError):
        pass

def _fetch_envelope_meta(fetcher) -> dict:
    """Build envelope metadata from fetcher state, defensively (mock-safe)."""
    def typed(attr, default):
        val = getattr(fetcher, attr, default)
        return val if isinstance(val, type(default)) else default

    warnings_raw = getattr(fetcher, "completeness_warnings", [])
    warnings_list = [str(w) for w in warnings_raw] if isinstance(warnings_raw, list) else []
    complete_raw = getattr(fetcher, "complete", True)
    complete = complete_raw if isinstance(complete_raw, bool) else True

    trunc_raw = getattr(fetcher, "truncation", None)

    return {
        "source_url": typed("dataset_url", ""),
        "resolved_title": typed("resolved_title", "") or None,
        "sha256": typed("last_sha256", ""),
        "bytes": typed("last_bytes", 0),
        "description_path": typed("last_description_path", ""),
        "complete": complete,
        "warnings": warnings_list,
        "truncation": trunc_raw if isinstance(trunc_raw, dict) else None,
    }

def interactive_flow() -> Tuple[str, str, str]:
    """Interactive wizard to guide parameters when run in zero-args mode."""
    print("==========================================")
    print("Welcome to the Data Fetcher Pipeline")
    print("==========================================\n")

    topic = input("1. What specific topic or domain do you need datasets for? ").strip()
    while not topic:
        print("[Error] Topic cannot be empty — enter a topic (e.g. 'covid', 'housing').")
        topic = input("Topic: ").strip()

    print("\n2. Please choose a fetching mode:")
    print("  1. Standard Mode: Choose a specific source from our supported list.")
    print("  2. Meta-Search (Coming Soon): Search ALL supported sources.")
    print("  3. Advanced Mode (Coming Soon): Provide an external/custom website.")

    source_choice = input("Enter your choice (1-3): ").strip()

    valid_sources = list_sources()
    source = "openml"

    if source_choice == '1':
        print(f"\nSupported Sources: {valid_sources}")
        source = input("Enter the specific source: ").strip().lower()
        while source not in valid_sources:
            print(f"[Error] '{source}' is not a supported source.")
            source = input(f"Please choose from {valid_sources}: ").strip().lower()

        topic_lower = topic.lower()
        if source == "sec" and any(word in topic_lower for word in ["movie", "game", "sports", "anime"]):
            ans = input(f"\nWarning: SEC is for corporate financial filings, which is logically unrelated to '{topic}'. Proceed anyway, or switch to Kaggle/OpenML? (proceed/switch): ").strip().lower()
            if ans == "switch":
                source = input("Enter new source (e.g. kaggle): ").strip().lower()
                while source not in valid_sources:
                    print(f"[Error] '{source}' is not a supported source.")
                    source = input(f"Please choose from {valid_sources}: ").strip().lower()
    else:
        print(f"\n[Notice] Advanced routing (Choices {source_choice}) is currently in development.")
        print("Falling back to standard source selection.")
        print(f"\nSupported Sources: {valid_sources}")
        source = input("Enter the specific source: ").strip().lower()
        while source not in valid_sources:
            print(f"[Error] '{source}' is not a supported source.")
            source = input(f"Please choose from {valid_sources}: ").strip().lower()

    print("\n[Wizard] All parameters collected successfully. Initializing Fetcher Engine...\n")

    return source, topic, str(Path.cwd() / "data_raw")

def run_conversion(target_format: str, filepath: str) -> str:
    """Execute standalone format conversion; returns the output artifact path."""
    from scripts.format_alchemy import FormatAlchemyEngine
    output = FormatAlchemyEngine.convert(filepath, target_format)
    print(f"[FormatAlchemy] Converted to: {output}")
    return output

def main() -> None:
    if _startup_error is not None:
        print(json.dumps({
            "status": "error",
            "code": "STARTUP_FAILURE",
            "message": f"Engine failed to initialize: {_startup_error}. "
                       "Reinstall dependencies from pyproject.toml.",
        }))
        sys.exit(3)

    parser = argparse.ArgumentParser(description="Data Fetcher Background Engine")
    parser.add_argument("--source", required=False, help="Target data platform (e.g., yfinance, fred, airbnb)")
    parser.add_argument("--query", required=False, help="Topic, ticker symbol, or series ID")
    parser.add_argument("--outdir", default=str(Path.cwd() / "data_raw"), help="Destination directory")

    parser.add_argument("--non-interactive", action="store_true", help="Run without user input prompt blocks")
    parser.add_argument("--yes", action="store_true", help="Auto-approve pre-flight data downloads")
    parser.add_argument("--rows", type=int, default=0, help="Fetch only the first N rows (provider-pushed where supported)")
    parser.add_argument("--list-sources", action="store_true", help="Print supported data sources and exit")
    parser.add_argument("--json-output", action="store_true", help="Provide machine-readable JSON output")
    parser.add_argument("--convert", nargs=2, metavar=("FORMAT", "FILE"), help="Convert FILE to FORMAT")

    args = parser.parse_args()

    # Machine mode: stdout is reserved for the JSON envelope; every progress or
    # diagnostic print from any module is redirected to stderr
    real_stdout = sys.stdout
    if args.json_output:
        sys.stdout = sys.stderr
    try:
        if args.list_sources:
            sources = list_sources()
            if args.json_output:
                from scripts.factory import SOURCE_INFO
                print(json.dumps({
                    "status": "success",
                    "sources": [
                        {
                            "key": s,
                            "platform": SOURCE_INFO.get(s, {}).get("platform", s.title()),
                            "query_format": SOURCE_INFO.get(s, {}).get("query_format", "free text"),
                            "example": SOURCE_INFO.get(s, {}).get("example", ""),
                            "auth": SOURCE_INFO.get(s, {}).get("auth", "none"),
                        }
                        for s in sources
                    ],
                }), file=real_stdout)
            else:
                titles = {
                    "airbnb": "Inside Airbnb",
                    "datagov": "Data.gov CKAN API",
                    "eurostat": "Eurostat Bulk TSV Data",
                    "fred": "Federal Reserve Economic Data (FRED)",
                    "github": "GitHub Code Search API",
                    "kaggle": "Kaggle Datasets API",
                    "openml": "OpenML Machine Learning Repository",
                    "sec": "SEC EDGAR XBRL Facts",
                    "worldbank": "World Bank Indicators API",
                    "coingecko": "CoinGecko Crypto Markets",
                    "yahoo": "Yahoo Finance (Alias)",
                    "yfinance": "Yahoo Finance",
                }
                print("\nSupported Data Sources:")
                print("-" * 55)
                print(f"{'CLI Key':<15} | {'Platform / Title':<35}")
                print("-" * 55)
                for s in sources:
                    title = titles.get(s, s.title())
                    print(f"{s:<15} | {title:<35}")
                print("-" * 55)
            sys.exit(0)

        if args.convert:
            target_format, file_path = args.convert[0], args.convert[1]
            try:
                output_artifact = run_conversion(target_format, file_path)
                if args.json_output:
                    print(json.dumps({
                        "status": "success",
                        "converted_file": output_artifact,
                        "source_file": file_path,
                    }), file=real_stdout)
                sys.exit(0)
            except (ValueError, RuntimeError, ImportError, FileNotFoundError, OSError) as e:
                if args.json_output:
                    code = e.code if isinstance(e, DataFetchError) else _fallback_code(e)
                    print(json.dumps({"status": "error", "code": code, "message": str(e)}), file=real_stdout)
                else:
                    print(f"[Error] Conversion failed: {e}")
                sys.exit(1)
            except Exception as e:
                # Third-party converters raise anything (zipfile.BadZipFile,
                # pyarrow errors) — the JSON envelope is guaranteed here too
                if args.json_output:
                    print(json.dumps({"status": "error", "code": "INTERNAL", "message": f"{type(e).__name__}: {e}"}), file=real_stdout)
                else:
                    print(f"[Error] Conversion failed unexpectedly ({type(e).__name__}): {e}")
                sys.exit(1)

        non_interactive = args.non_interactive
        fetcher = None  # for orphan-dir GC on failure paths

        try:
            # Guarded zone: from wizard config onward every failure lands in the
            # except chain below — the JSON envelope is guaranteed unconditionally
            # JSON mode is non-interactive by definition: the wizard must never
            # prompt (getpass would hang a piped/stdin-less process)
            config = setup_wizard(non_interactive=non_interactive or args.json_output)
            # Note: 'non_interactive' is a runtime flag injected for backward compatibility, it is not persisted.
            config["non_interactive"] = non_interactive or args.json_output

            # JSON mode must never reach an interactive prompt: pre-flight input would
            # EOF and double-envelope the stream. Demand an explicit non-interactive flag.
            if args.json_output and not (args.yes or non_interactive):
                raise DataFetchError(
                    "--json-output requires --yes or --non-interactive: interactive "
                    "pre-flight prompts cannot be served in JSON mode.",
                    code="INTERACTIVE_REQUIRED",
                )

            if not args.source or not args.query:
                # JSON mode never reaches the interactive wizard — a piped-stdin
                # agent must not hang on prompts or fire live requests silently
                if non_interactive or args.json_output:
                    raise DataFetchError(
                        "Source and Query arguments are required in non-interactive mode.",
                        code="ARG_MISSING",
                    )
                source, query, outdir = interactive_flow()
            else:
                source, query, outdir = args.source, args.query, args.outdir

            if args.rows < 0:
                raise DataFetchError(
                    f"--rows must be a non-negative integer (got {args.rows}).",
                    code="ARG_MISSING",
                )

            # Provision only what this run touches — discovery/conversion stay side-effect free
            from scripts.base import provision_data_directory
            provision_data_directory(outdir, source_key={"yfinance": "yahoo"}.get(source.lower(), source.lower()))

            if not args.json_output and not args.yes and not args.non_interactive:
                delay = random.uniform(2, 5)
                print(f"[Engine] Imposing humanized delay of {delay:.2f} seconds to simulate human traffic...")
                time.sleep(delay)

            if source.lower() == "custom":
                from scripts.web_analyzer import WebAnalyzer
                # Keep the layout convention: everything a source produces lives under <outdir>/<source>/
                analyzer = WebAnalyzer(query, str(Path(outdir) / "custom"))
                report = analyzer.analyze()
                script_path = analyzer.generate_script(report)

                if args.json_output:
                    print(json.dumps({
                        "status": "success",
                        "url": report.url,
                        "difficulty": report.difficulty,
                        "status_code": report.status_code,
                        "content_type": report.content_type,
                        "content_length": report.content_length,
                        "robots_allowed": report.robots_allowed,
                        "cloudflare_detected": report.cloudflare_detected,
                        "captcha_detected": report.captcha_detected,
                        "table_count": report.table_count,
                        "warnings": report.warnings,
                        "generated_script": script_path
                    }), file=real_stdout)
                    sys.exit(0)

                print("\n" + "="*50)
                print(" WEB SCRAPING ASSESSMENT REPORT")
                print("="*50)
                print(f"Target URL: {report.url}")
                print(f"Status Code: {report.status_code}")
                print(f"Content Type: {report.content_type}")
                print(f"Estimated Size: {report.content_length} bytes")
                print(f"Robots.txt Allowed: {report.robots_allowed}")
                print(f"Cloudflare Detected: {report.cloudflare_detected}")
                print(f"CAPTCHA Detected: {report.captcha_detected}")
                print(f"HTML Table Count: {report.table_count}")
                print(f"Difficulty Level: {report.difficulty.upper()}")
                print("-" * 50)
                if report.warnings:
                    print("Warnings:")
                    for warn in report.warnings:
                        print(f"  - {warn}")
                print(f"\nGenerated scraping script saved to: {script_path}")
                print("="*50 + "\n")

                if report.difficulty in ["easy", "medium"] and not non_interactive and not args.json_output:
                    ans = input("Would you like to execute the generated scraper script right now? (y/n): ").strip().lower()
                    if ans == 'y':
                        print("[Engine] Executing generated scraper script...")
                        import subprocess
                        subprocess.run([sys.executable, script_path], check=False)
                sys.exit(0)

            # JSON-mode demand moved above (wizard must never prompt); fetcher section follows
            fetcher = get_fetcher(source, query, outdir, config)
            if args.yes or non_interactive:
                fetcher.auto_approve = True
            if args.rows > 0:
                fetcher.row_limit = args.rows

            from scripts.http_utils import reset_metrics, get_metrics
            reset_metrics()
            csv_path = fetcher.run()
            metrics = get_metrics()

            # Retrieve rows and columns metadata directly from the fetcher to avoid redundant I/O
            rows = getattr(fetcher, "rows_count", 0)
            rows = rows if isinstance(rows, int) else 0
            cols = getattr(fetcher, "cols_count", 0)
            cols = cols if isinstance(cols, int) else 0

            if args.json_output:
                from datetime import datetime, timezone
                envelope = {
                    "status": "success",
                    "source": source,
                    "query": query,
                    "output_path": str(Path(csv_path).resolve()),
                    "rows": rows,
                    "columns": cols,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "provider_requests": metrics["requests"],
                    "retries": metrics["retries"],
                    "bytes_transferred": metrics["bytes"],
                }
                envelope.update(_fetch_envelope_meta(fetcher))
                envelope["description_path"] = str(Path(envelope["description_path"]).resolve()) if envelope["description_path"] else ""
                print(json.dumps(envelope), file=real_stdout)
            else:
                print("[Engine] Extraction Complete. Pipeline exiting successfully.")

            # Only a human in an interactive session gets the optional FormatAlchemy prompt;
            # --yes auto-approves downloads but must never block on input()
            if not non_interactive and not args.yes and not args.json_output:
                print("\n[Prompt] Data fetched successfully. Would you like to initialize the Format Alchemy engine to convert this dataset to SQL and Excel? (y/n)")
                alchemy_choice = input().strip().lower()
                if alchemy_choice == 'y':
                    from scripts.format_alchemy import run_alchemy
                    run_alchemy(csv_path)

        except DataFetchError as e:
            _gc_orphan_dir(fetcher)
            if args.json_output:
                print(json.dumps({"status": "error", "code": e.code, "message": str(e)}), file=real_stdout)
            else:
                print(f"\n[Error] {e}")
            sys.exit(e.exit_code)
        except (ValueError, RuntimeError, ImportError) as e:
            _gc_orphan_dir(fetcher)
            if args.json_output:
                print(json.dumps({"status": "error", "code": _fallback_code(e), "message": str(e)}), file=real_stdout)
            else:
                print(f"\n[Error] {e}")
            sys.exit(1)
        except OSError as e:
            # Filesystem failures (e.g. --outdir is an existing file) get precise
            # codes too instead of collapsing into INTERNAL
            _gc_orphan_dir(fetcher)
            if args.json_output:
                print(json.dumps({"status": "error", "code": _fallback_code(e), "message": str(e)}), file=real_stdout)
            else:
                print(f"\n[Error] {e}")
            sys.exit(1)
        except (KeyboardInterrupt, EOFError):
            _gc_orphan_dir(fetcher)
            if args.json_output:
                code = "ABORTED" if isinstance(sys.exc_info()[1], KeyboardInterrupt) else "INTERACTIVE_REQUIRED"
                print(json.dumps({"status": "error", "code": code, "message": "Interactive input required but unavailable. Add --yes flag for non-interactive mode."}), file=real_stdout)
            else:
                print("\n[Engine] Execution aborted by user.")
            sys.exit(130 if isinstance(sys.exc_info()[1], KeyboardInterrupt) else 1)
        except Exception as e:
            # Third-party libraries can raise anything (e.g. requests.ConnectionError
            # from the openml client) — never surface a raw traceback to an agent
            _gc_orphan_dir(fetcher)
            if args.json_output:
                print(json.dumps({"status": "error", "code": "INTERNAL", "message": f"{type(e).__name__}: {e}"}), file=real_stdout)
            else:
                print(f"\n[Error] Unexpected failure ({type(e).__name__}): {e}")
            sys.exit(1)
    finally:
        sys.stdout = real_stdout

if __name__ == "__main__":
    main()
