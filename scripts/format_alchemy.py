import sqlite3
import re
import json
import os
from pathlib import Path


import pandas as pd
from openpyxl.utils import get_column_letter

from scripts.errors import DataFetchError

MAX_SQLITE_COLUMNS = 2000  # hard SQLite limit (SQLITE_MAX_COLUMN default)
MAX_EXCEL_ROWS = 1048575   # data rows per sheet (1,048,576 total incl. header)

class FormatAlchemyEngine:
    """Engine to convert CSV files into SQLite databases and Excel spreadsheets, and support bidirectional conversions."""
    
    def __init__(self, csv_filepath: str) -> None:
        path = Path(csv_filepath)
        if not path.is_file():
            raise FileNotFoundError(f"Source CSV file not found: {csv_filepath}")
        self.csv_filepath = str(path)
        self.directory = str(path.parent)
        filename = path.name
        self.dataset_name = path.stem.replace("_raw", "")
        self.db_filepath = str(path.parent / f"{self._safe_table_name()}.db")
        self.excel_filepath = str(path.parent / f"{self.dataset_name}_export.xlsx")

    def _safe_table_name(self) -> str:
        """Sanitize table name to prevent SQL injection and SQLite syntax errors."""
        return re.sub(r'[^a-zA-Z0-9_]', '_', self.dataset_name)

    def csv_to_sqlite(self) -> None:
        """Read CSV in chunks and ingest into SQLite database."""
        # Pre-flight guard: SQLite hard-caps tables at 2000 columns (SQLITE_MAX_COLUMN).
        # Without this, ingestion dies mid-chunks with a cryptic "too many columns"
        # OperationalError; fail fast with the Parquet/CSV alternative instead.
        header = pd.read_csv(self.csv_filepath, nrows=0)
        if len(header.columns) > MAX_SQLITE_COLUMNS:
            raise DataFetchError(
                f"Dataset has {len(header.columns)} columns; SQLite supports at most "
                f"{MAX_SQLITE_COLUMNS}. Export to Parquet or CSV instead.",
                code="SIZE_LIMIT",
            )
        try:
            with sqlite3.connect(self.db_filepath) as conn:
                chunksize = 100000
                first_chunk = True
                safe_table = self._safe_table_name()
                for chunk in pd.read_csv(self.csv_filepath, chunksize=chunksize, low_memory=False):
                    chunk.to_sql(safe_table, conn, if_exists="replace" if first_chunk else "append", index=False)
                    first_chunk = False
        except pd.errors.EmptyDataError as e:
            raise ValueError(f"CSV file is empty or corrupted: {e}") from e
        except sqlite3.Error as e:
            raise RuntimeError(f"Database ingestion failed: {e}") from e
            
        print(f"[FormatAlchemy] Ingested CSV into SQLite database at {self.db_filepath}")

    MAX_EXCEL_COLUMNS = 16384  # hard format limit (xlsx)

    def sqlite_to_excel(self) -> None:
        """Stream the SQL table into Excel using openpyxl write-only mode.

        Standard-mode writers materialize ~0.4KB/cell of workbook objects and
        die around ~30M cells; write_only streams rows from the sqlite cursor
        with flat memory (~100M+ cells viable). Column widths are estimated
        from a bounded first-page sample. A torn workbook from a mid-write
        crash is impossible: the .xlsx is written to tmp and promoted once
        complete (atomic replace).
        """
        from openpyxl import Workbook
        try:
            with sqlite3.connect(self.db_filepath) as conn:
                cursor = conn.cursor()
                safe_table = self._safe_table_name()
                cursor.execute(f'SELECT COUNT(*) FROM "{safe_table}"')
                row_count = cursor.fetchone()[0]

                if row_count > MAX_EXCEL_ROWS:
                    print(f"[Warning] Dataset has {row_count} rows, which exceeds Excel's limit. Exporting only the first {MAX_EXCEL_ROWS:,} rows.")

                cursor.execute(f'SELECT * FROM "{safe_table}"')
                headers = [d[0] for d in cursor.description]
                if len(headers) > self.MAX_EXCEL_COLUMNS:
                    raise ValueError(
                        f"Dataset has {len(headers)} columns; Excel supports at most "
                        f"{self.MAX_EXCEL_COLUMNS}. Export to CSV/Parquet instead."
                    )

                wb = Workbook(write_only=True)
                ws = wb.create_sheet(self.dataset_name[:31])
                ws.append(headers)

                # Column widths from a bounded sample of the first rows (never
                # exceeding the sheet cap itself)
                sample_rows = []
                while len(sample_rows) < min(200, MAX_EXCEL_ROWS):
                    row = cursor.fetchone()
                    if row is None:
                        break
                    sample_rows.append(row)
                widths = {}
                for row in sample_rows:
                    for idx, val in enumerate(row):
                        l = min(max(len(str(val)) if val is not None else 0, 0), 60)
                        widths[idx] = max(widths.get(idx, 0), l)
                for idx, header in enumerate(headers):
                    ws.column_dimensions[get_column_letter(idx + 1)].width = min(max(widths.get(idx, 0), len(str(header))) + 2, 100)

                for row in sample_rows:
                    ws.append(row)
                streamed = len(sample_rows)
                for row in cursor:
                    if streamed >= MAX_EXCEL_ROWS:
                        break  # cap enforced here — an out-of-spec workbook must never be produced
                    ws.append(row)
                    streamed += 1
                    if streamed % 100000 == 0:
                        print(f"[FormatAlchemy] ... {streamed:,}/{min(row_count, MAX_EXCEL_ROWS):,} rows written")

                wb.save(self.excel_filepath + ".tmp")
            os.replace(self.excel_filepath + ".tmp", self.excel_filepath)
        except sqlite3.Error as e:
            Path(self.excel_filepath + ".tmp").unlink(missing_ok=True)
            raise RuntimeError(f"Database query failed: {e}") from e
        except OSError as e:
            Path(self.excel_filepath + ".tmp").unlink(missing_ok=True)
            raise RuntimeError(f"Excel generation failed due to OS error: {e}") from e

        print(f"[FormatAlchemy] Exported SQL table to Excel at {self.excel_filepath}")

    def csv_to_excel_direct(self) -> None:
        """Stream CSV straight into Excel (write-only) WITHOUT the SQLite hop.

        Keeps the 16,384-column Excel ceiling reachable (SQLite's 2,000-column
        limit would otherwise choke wide exports) and enforces the row cap.
        """
        from openpyxl import Workbook
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(self.dataset_name[:31])
        header = pd.read_csv(self.csv_filepath, nrows=0)
        if len(header.columns) > self.MAX_EXCEL_COLUMNS:
            raise DataFetchError(
                f"Dataset has {len(header.columns)} columns; Excel supports at most "
                f"{self.MAX_EXCEL_COLUMNS}. Export to Parquet or CSV instead.",
                code="SIZE_LIMIT",
            )
        ws.append([str(c) for c in header.columns])
        for idx, col in enumerate(header.columns):
            ws.column_dimensions[get_column_letter(idx + 1)].width = min(max(len(str(col)) + 2, 12), 100)

        written = 0
        total_source_rows = 0
        try:
            for chunk in pd.read_csv(self.csv_filepath, chunksize=10000, low_memory=False):
                total_source_rows += len(chunk)
                values = chunk.astype(object).where(pd.notna(chunk), None)
                for row in values.itertuples(index=False, name=None):
                    if written >= MAX_EXCEL_ROWS:
                        break
                    ws.append(row)
                    written += 1
                if written >= MAX_EXCEL_ROWS:
                    break
            wb.save(self.excel_filepath + ".tmp")
            os.replace(self.excel_filepath + ".tmp", self.excel_filepath)
        except OSError as e:
            Path(self.excel_filepath + ".tmp").unlink(missing_ok=True)
            raise RuntimeError(f"Excel generation failed due to OS error: {e}") from e
        except BaseException:
            Path(self.excel_filepath + ".tmp").unlink(missing_ok=True)
            raise
        if written < total_source_rows:
            print(f"[Warning] Excel export capped at {MAX_EXCEL_ROWS:,} data rows (source had at least {written:,} rows read before the cap).")
        print(f"[FormatAlchemy] Exported CSV directly to Excel at {self.excel_filepath}")

    def execute_pipeline(self) -> None:
        print("[FormatAlchemy] Initializing transformation pipeline...")
        self.csv_to_sqlite()
        self.sqlite_to_excel()
        print("[FormatAlchemy] Transformation complete.")

    @classmethod
    def xlsx_to_csv(cls, source_path: str, target_path: str) -> str:
        """Convert Excel sheet to CSV format."""
        try:
            df = pd.read_excel(source_path, engine="openpyxl")
            df.to_csv(target_path, index=False)
            return target_path
        except ImportError as exc:
            raise ImportError("Missing openpyxl library for Excel import. Please install it.") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read/write files during Excel conversion: {exc}") from exc

    @classmethod
    def json_to_csv(cls, source_path: str, target_path: str) -> str:
        """Convert JSON (array or lines format) to CSV, normalizing nested fields."""
        try:
            with open(source_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            df = pd.json_normalize(data)
        except json.JSONDecodeError:
            try:
                records = []
                with open(source_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            records.append(json.loads(line))
                df = pd.json_normalize(records)
            except (json.JSONDecodeError, OSError) as exc:
                raise ValueError(f"JSON file is not a valid JSON array or JSON lines document: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"JSON file reading failed: {exc}") from exc

        try:
            df.to_csv(target_path, index=False)
            return target_path
        except OSError as exc:
            raise RuntimeError(f"Failed to write CSV output: {exc}") from exc

    @classmethod
    def parquet_to_csv(cls, source_path: str, target_path: str) -> str:
        """Convert Parquet file to CSV using pyarrow or fastparquet."""
        try:
            df = pd.read_parquet(source_path, engine="pyarrow")
        except ImportError:
            try:
                df = pd.read_parquet(source_path, engine="fastparquet")
            except ImportError as exc:
                raise ImportError("Neither pyarrow nor fastparquet is installed. Please install them to handle Parquet.") from exc
            except OSError as exc:
                raise RuntimeError(f"Failed to read Parquet via fastparquet: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read Parquet via pyarrow: {exc}") from exc

        try:
            df.to_csv(target_path, index=False)
            return target_path
        except OSError as exc:
            raise RuntimeError(f"Failed to write CSV: {exc}") from exc

    @classmethod
    def csv_to_parquet(cls, source_path: str, target_path: str) -> str:
        """Convert CSV dataset to Parquet columnar format."""
        try:
            df = pd.read_csv(source_path)
        except OSError as exc:
            raise RuntimeError(f"Failed to read source CSV: {exc}") from exc

        try:
            df.to_parquet(target_path, engine="pyarrow")
            return target_path
        except ImportError:
            try:
                df.to_parquet(target_path, engine="fastparquet")
                return target_path
            except ImportError as exc:
                raise ImportError("Neither pyarrow nor fastparquet is installed. Please install them to write Parquet.") from exc
            except OSError as exc:
                raise RuntimeError(f"Failed to write Parquet via fastparquet: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to write Parquet via pyarrow: {exc}") from exc

    @classmethod
    def sqlite_to_csv(cls, source_path: str, target_path: str) -> str:
        """Convert SQLite database to CSV by reading the first (or largest) table."""
        try:
            with sqlite3.connect(source_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = [row[0] for row in cursor.fetchall()]
                if not tables:
                    raise ValueError(f"No tables found in SQLite database: {source_path}")
                target_table = tables[0]
                if len(tables) > 1:
                    # Pick the table with the most rows
                    max_rows = 0
                    for table in tables:
                        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                        count = cursor.fetchone()[0]
                        if count > max_rows:
                            max_rows = count
                            target_table = table
                df = pd.read_sql_query(f'SELECT * FROM "{target_table}"', conn)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Failed to read SQLite database: {exc}") from exc
        try:
            df.to_csv(target_path, index=False)
            return target_path
        except OSError as exc:
            raise RuntimeError(f"Failed to write CSV output: {exc}") from exc

    @classmethod
    def convert(cls, source_path: str, target_format: str) -> str:
        """Convert source file to target format. Returns output filepath."""
        path = Path(source_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")
            
        target_format = target_format.lower().strip()
        source_ext = path.suffix.lower().strip(".")
        base_path = str(path.parent / path.stem)
        
        if target_format == "csv":
            out_path = f"{base_path}.csv"
            if source_ext in ["xlsx", "xls"]:
                return cls.xlsx_to_csv(source_path, out_path)
            elif source_ext == "json":
                return cls.json_to_csv(source_path, out_path)
            elif source_ext == "parquet":
                return cls.parquet_to_csv(source_path, out_path)
            elif source_ext in ["db", "sqlite", "sqlite3"]:
                return cls.sqlite_to_csv(source_path, out_path)
            elif source_ext == "csv":
                return source_path
            else:
                raise ValueError(f"Conversion from {source_ext} to CSV is not supported.")
                
        elif target_format in ["excel", "xlsx"]:
            out_path = f"{base_path}.xlsx"
            if source_ext == "csv":
                # Direct CSV→Excel stream: no SQLite hop (keeps the 16,384-col
                # ceiling reachable and doubles conversion speed)
                engine = cls(source_path)
                engine.csv_to_excel_direct()
                if engine.excel_filepath != out_path:
                    os.replace(engine.excel_filepath, out_path)
                return out_path
            elif source_ext in ["parquet", "json", "db", "sqlite", "sqlite3"]:
                temp_csv = f"{base_path}_temp.csv"
                try:
                    to_csv = {
                        "parquet": cls.parquet_to_csv,
                        "json": cls.json_to_csv,
                    }.get(source_ext, cls.sqlite_to_csv)
                    to_csv(source_path, temp_csv)
                    engine = cls(temp_csv)
                    engine.csv_to_excel_direct()
                    os.replace(engine.excel_filepath, out_path)  # canonical name, no _temp leak
                    return out_path
                finally:
                    Path(temp_csv).unlink(missing_ok=True)
            else:
                raise ValueError(f"Conversion from {source_ext} to Excel requires a CSV, Parquet, JSON, or SQLite source. Convert to CSV first.")
                
        elif target_format in ["sqlite", "sql", "db"]:
            if source_ext == "csv":
                engine = cls(source_path)
                engine.csv_to_sqlite()
                return engine.db_filepath
            else:
                raise ValueError(f"Conversion from {source_ext} to SQLite requires a CSV source.")
            
        elif target_format == "parquet":
            out_path = f"{base_path}.parquet"
            if source_ext == "csv":
                return cls.csv_to_parquet(source_path, out_path)
            elif source_ext in ["json", "db", "sqlite", "sqlite3"]:
                temp_csv = f"{base_path}_temp.csv"
                try:
                    to_csv = cls.json_to_csv if source_ext == "json" else cls.sqlite_to_csv
                    to_csv(source_path, temp_csv)
                    return cls.csv_to_parquet(temp_csv, out_path)
                finally:
                    Path(temp_csv).unlink(missing_ok=True)
            else:
                raise ValueError(f"Conversion from {source_ext} to Parquet requires a CSV, JSON, or SQLite source.")
                
        elif target_format == "json":
            out_path = f"{base_path}.json"
            if source_ext == "csv":
                try:
                    df = pd.read_csv(source_path)
                    df.to_json(out_path, orient="records", indent=4)
                    return out_path
                except OSError as exc:
                    raise RuntimeError(f"Conversion to JSON failed: {exc}") from exc
            elif source_ext in ["db", "sqlite", "sqlite3"]:
                temp_csv = f"{base_path}_temp.csv"
                try:
                    cls.sqlite_to_csv(source_path, temp_csv)
                    df = pd.read_csv(temp_csv)
                    df.to_json(out_path, orient="records", indent=4)
                    return out_path
                except OSError as exc:
                    raise RuntimeError(f"Conversion to JSON failed: {exc}") from exc
                finally:
                    Path(temp_csv).unlink(missing_ok=True)
            else:
                raise ValueError(f"Conversion from {source_ext} to JSON requires a CSV or SQLite source.")
                
        else:
            raise ValueError(f"Target format '{target_format}' is not supported.")

def run_alchemy(csv_filepath: str) -> None:
    engine = FormatAlchemyEngine(csv_filepath)
    engine.execute_pipeline()
