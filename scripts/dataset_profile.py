"""Pure Markdown profile rendering; file persistence stays with the fetcher.

Presentation safety: every externally sourced string interpolated into the
profile (query, source name, goal, URLs, file names, column names, cells,
truncation metadata) is Markdown-escaped — pipes, angle brackets, code span
delimiters, link brackets, CR/LF, ANSI sequences and Unicode BiDi overrides
cannot break out of table rows or forge links/markup. Source URLs are
rendered inside literal code spans so they can never become active links.
The dataset VALUES themselves are never modified — only how they are
displayed in the provenance artifact.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.adapters.sanitization import escape_code_span, escape_markdown_text


@dataclass(frozen=True)
class ProfileContext:
    query: str
    source_name: str
    dataset_url: str
    requested_goal: str
    truncation: dict | None
    max_columns: int


def markdown_table(frame: pd.DataFrame) -> str:
    def escaped(cell):
        return escape_markdown_text(cell)

    headers = ' | '.join(escaped(column) for column in frame.columns)
    separator = ' | '.join('---' for _ in frame.columns)
    rows = [' | '.join(escaped(cell) for cell in row) for _, row in frame.iterrows()]
    return f'| {headers} |\n| {separator} |\n' + '\n'.join(f'| {row} |' for row in rows)


def _overview(context: ProfileContext) -> str:
    return (
        f'This dataset contains raw data related to **{escape_markdown_text(context.query)}**, '
        f'sourced dynamically from **{escape_markdown_text(context.source_name)}**.\n\n'
        'This is a normalized extract; requested row limits and column selection may apply. '
        'This profile was automatically compiled by the Data Fetcher Pipeline agent interface to assist in exploratory data analysis and database ingestion workflows.'
    )


def _metadata(frame: pd.DataFrame, csv_path: Path, context: ProfileContext) -> str:
    null_density = frame.isnull().sum().sum() / frame.size if frame.size else 0
    safe_url = escape_code_span(context.dataset_url, max_len=500)
    goal = (f'* **Requested goal (not an automatic content filter):** {escape_markdown_text(context.requested_goal)}\n'
            if context.requested_goal else '')
    return (
        f'* **Source URL:** `{safe_url}`\n' + goal
        + f'* **Original Filename:** {escape_markdown_text(csv_path.name)}\n'
        f'* **Generated At (UTC):** {datetime.now(timezone.utc).isoformat(timespec="seconds")}\n'
        f'* **Total Rows:** {len(frame)}\n'
        f'* **Total Columns:** {len(frame.columns)}\n'
        f'* **Overall Missing Value Density:** {null_density:.2%}\n'
        + (f'* **Truncation:** {escape_markdown_text(json.dumps(context.truncation))}\n' if context.truncation else '')
    )


def _preview(frame: pd.DataFrame) -> str:
    try:
        preview = frame.head(5).copy()
        for column in preview.columns:
            preview[column] = preview[column].astype(str)
    except (TypeError, ValueError):
        preview = frame.head(5).fillna('N/A').astype(str)
    return markdown_table(preview)


def _schema(frame: pd.DataFrame, sample: pd.DataFrame, max_columns: int) -> str:
    rows = []
    for column in frame.columns[:max_columns]:
        nulls = frame[column].isnull().sum()
        null_percentage = nulls / len(frame) if len(frame) else 0
        rows.append(f'| {escape_markdown_text(column)} | {escape_markdown_text(frame[column].dtype)} '
                    f'| {sample[column].nunique(dropna=True)} | {nulls} ({null_percentage:.2%}) |')
    truncated_note = (f'\n\n*Note: showing the first {max_columns} of {len(frame.columns)} columns in schema/statistics tables.*'
                      if len(frame.columns) > max_columns else '')
    return (
        '| Column Name | Data Type (Dtype) | Unique Values Count | Null Density (Count & Percentage) |\n'
        '| --- | --- | --- | --- |\n' + '\n'.join(rows) + truncated_note
    )


def _statistics(sample: pd.DataFrame, max_columns: int) -> str:
    if sample.empty or len(sample.columns) == 0:
        return '*No data available for statistics.*'
    summary = sample.iloc[:, :max_columns].describe(include='all').reset_index().fillna('N/A').astype(str)
    return markdown_table(summary)


def render_profile(frame: pd.DataFrame, csv_path: Path, context: ProfileContext) -> str:
    # Sampling bounds expensive uniqueness/statistics scans on large datasets.
    sample = frame.sample(n=100_000, random_state=42) if len(frame) > 100_000 else frame
    dataset_name = csv_path.stem.replace('_raw', '')
    return (
        f'# Dataset Profile: {escape_markdown_text(dataset_name)}\n\n'
        f'## Overview\n{_overview(context)}\n\n'
        f'## Metadata Summary\n{_metadata(frame, csv_path, context)}\n'
        f'## Data Preview (First 5 Rows)\n{_preview(frame)}\n\n'
        f'## Column Schema & Health\n{_schema(frame, sample, context.max_columns)}\n\n'
        f'## Summary Statistics\n{_statistics(sample, context.max_columns)}\n'
    )
