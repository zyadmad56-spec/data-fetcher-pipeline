import os
import sqlite3

class FormatAlchemyEngine:
    def __init__(self, csv_filepath: str) -> None:
        if not os.path.isfile(csv_filepath):
            raise FileNotFoundError(f"Source CSV file not found: {csv_filepath}")
        self.csv_filepath = csv_filepath
        self.directory = os.path.dirname(csv_filepath)
        filename = os.path.basename(csv_filepath)
        self.dataset_name = os.path.splitext(filename)[0].replace("_raw", "")
        self.db_filepath = os.path.join(self.directory, f"{self.dataset_name}.db")
        self.excel_filepath = os.path.join(self.directory, f"{self.dataset_name}_export.xlsx")

    def csv_to_sqlite(self) -> None:
        import pandas as pd
        try:
            with sqlite3.connect(self.db_filepath) as conn:
                chunksize = 100000
                first_chunk = True
                for chunk in pd.read_csv(self.csv_filepath, chunksize=chunksize, low_memory=False):
                    chunk.to_sql(self.dataset_name, conn, if_exists="replace" if first_chunk else "append", index=False)
                    first_chunk = False
        except pd.errors.EmptyDataError as e:
            raise ValueError(f"CSV file is empty or corrupted: {e}") from e
        except sqlite3.Error as e:
            raise RuntimeError(f"Database ingestion failed: {e}") from e
            
        print(f"[FormatAlchemy] Ingested CSV into SQLite database at {self.db_filepath}")

    def sqlite_to_excel(self) -> None:
        import pandas as pd
        try:
            with sqlite3.connect(self.db_filepath) as conn:
                cursor = conn.cursor()
                cursor.execute(f'SELECT COUNT(*) FROM "{self.dataset_name}"')
                row_count = cursor.fetchone()[0]
                
                if row_count > 1048575:
                    print(f"[Warning] Dataset has {row_count} rows, which exceeds Excel's limit. Exporting only the first 1,048,575 rows.")
                    df = pd.read_sql_query(f'SELECT * FROM "{self.dataset_name}" LIMIT 1048575', conn)
                else:
                    df = pd.read_sql_query(f'SELECT * FROM "{self.dataset_name}"', conn)
        except sqlite3.Error as e:
            raise RuntimeError(f"Database query failed: {e}") from e
            
        try:
            with pd.ExcelWriter(self.excel_filepath, engine="openpyxl") as writer:
                sheet_name = self.dataset_name[:31]
                df.to_excel(writer, index=False, sheet_name=sheet_name)
                worksheet = writer.sheets[sheet_name]
                from openpyxl.utils import get_column_letter
                for idx, col in enumerate(df.columns):
                    col_max_len = df[col].astype(str).map(len).max()
                    if pd.isna(col_max_len):
                        col_max_len = 0
                    max_len = int(max(col_max_len, len(col))) + 2
                    col_letter = get_column_letter(idx + 1)
                    worksheet.column_dimensions[col_letter].width = min(max_len, 100) # Cap width for sanity
        except ImportError as e:
            raise ImportError("Missing openpyxl library for Excel export.") from e
        except OSError as e:
             raise RuntimeError(f"Excel generation failed due to OS error: {e}") from e
             
        print(f"[FormatAlchemy] Exported SQL table to Excel at {self.excel_filepath}")

    def execute_pipeline(self) -> None:
        print("[FormatAlchemy] Initializing transformation pipeline...")
        self.csv_to_sqlite()
        self.sqlite_to_excel()
        print("[FormatAlchemy] Transformation complete.")

def run_alchemy(csv_filepath: str) -> None:
    engine = FormatAlchemyEngine(csv_filepath)
    engine.execute_pipeline()
