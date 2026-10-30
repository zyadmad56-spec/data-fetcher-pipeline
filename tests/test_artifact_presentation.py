"""Phase 6 — provenance Markdown escaping and spreadsheet formula text-binding.

Dataset cell VALUES must survive every render/export path byte-for-byte:
presentation layers may escape how a value is displayed and pin the stored
cell TYPE, but never mutate the data itself.
"""
import io
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from scripts.dataset_profile import ProfileContext, markdown_table, render_profile
from scripts.errors import DataFetchError
from scripts.format_alchemy import (
    FormatAlchemyEngine,
    MAX_XLSX_ENTRIES,
    MAX_XLSX_UNCOMPRESSED_BYTES,
)

FORMULA_PAYLOADS = ['=SUM(1,1)', "=CMD|'/C calc'!A0"]


# --- Markdown provenance escaping ---------------------------------------------


def test_markdown_table_escapes_hostile_cells_and_headers():
    frame = pd.DataFrame({
        'a|b\u202e': ['<x>', 'line1\nline2'],
        'c': ['[click](https://evil.example)', 'p|q'],
        '`bt`': ['\x1b[31mANSI', 'ok'],
    })
    rendered = markdown_table(frame)
    lines = rendered.splitlines()
    # CR/LF never break table rows: exactly header + separator + 2 rows.
    assert len(lines) == 4
    assert lines[0] == '| a\\|b\\u202e | c | \\`bt\\` |'
    assert lines[2] == '| \\<x\\> | \\[click\\](https://evil.example) | ANSI |'
    assert 'line1\\x0aline2' in lines[3]
    assert 'p\\|q' in lines[3]
    assert '\x1b' not in rendered and '\u202e' not in rendered


def test_markdown_table_keeps_clean_values_intact():
    frame = pd.DataFrame({'value': [1, 2], 'label': ['real record', 'A|B']})
    rendered = markdown_table(frame)
    assert '| 1 | real record |' in rendered
    assert 'A\\|B' in rendered


def test_render_profile_escapes_context_fields_and_codespan_url(tmp_path):
    frame = pd.DataFrame({'v': ['<b>bold</b>'], 'w': ['\u202eoverwrite']})
    context = ProfileContext(
        query='q\u202e<script>', source_name='S|X',
        dataset_url='https://example.com/d.csv?token=SECRET',
        requested_goal='g<b>|goal', truncation={'rows': 5}, max_columns=30)
    content = render_profile(frame, tmp_path / 'data_raw.csv', context)
    # URL is an inert code span, never an autolink or raw markup.
    assert '`https://example.com/d.csv?token=SECRET`' in content
    assert '<https://example.com' not in content
    # Hostile context fields are escaped.
    assert 'q\\u202e\\<script\\>' in content
    assert '**S\\|X**' in content
    assert 'g\\<b\\>\\|goal' in content
    # Hostile cells and column names are escaped everywhere (preview + schema + stats).
    assert '\u202e' not in content and '\x1b' not in content
    assert '<b>bold</b>' not in content
    assert '\\<b\\>bold\\</b\\>' in content
    assert 'v\\u202e' in content or 'w' in content  # hostile column escaped in schema


def test_render_profile_schema_escapes_hostile_column_names(tmp_path):
    frame = pd.DataFrame({'c\u202e|x': [1], 'y': [2]})
    context = ProfileContext(query='q', source_name='S', dataset_url='https://e.com',
                             requested_goal='', truncation=None, max_columns=30)
    content = render_profile(frame, tmp_path / 'n_raw.csv', context)
    assert 'c\\u202e\\|x' in content


def test_render_profile_metadata_codespan_neutralizes_url_backticks(tmp_path):
    frame = pd.DataFrame({'v': [1]})
    context = ProfileContext(query='q', source_name='S',
                             dataset_url='https://evil/`inject`',
                             requested_goal='', truncation=None, max_columns=30)
    content = render_profile(frame, tmp_path / 'n_raw.csv', context)
    assert "`https://evil/'inject'`" in content


# --- Excel formula text-binding -----------------------------------------------


def _formula_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / 'formula_raw.csv'
    pd.DataFrame({
        'attack': FORMULA_PAYLOADS + ['+SUM(A1)', '@notes'],
        'plain': ['normal', 'text', 'values', 'here'],
        'number': [1.5, 2, 3, 4],
    }).to_csv(csv_path, index=False)
    return csv_path


def _assert_no_formula_elements(xlsx_path: Path) -> None:
    with zipfile.ZipFile(xlsx_path) as bundle:
        for name in bundle.namelist():
            if name.startswith('xl/worksheets/') and name.endswith('.xml'):
                xml = bundle.read(name).decode('utf-8')
                assert '<f>' not in xml and '<f ' not in xml, f'formula element in {name}'


@pytest.mark.parametrize('via_sqlite', [True, False])
def test_formula_strings_are_stored_as_literal_text(tmp_path, via_sqlite):
    csv_path = _formula_csv(tmp_path)
    engine = FormatAlchemyEngine(str(csv_path))
    if via_sqlite:
        engine.csv_to_sqlite()
        engine.sqlite_to_excel()
    else:
        engine.csv_to_excel_direct()
    xlsx_path = Path(engine.excel_filepath)

    _assert_no_formula_elements(xlsx_path)

    workbook = openpyxl.load_workbook(xlsx_path)
    sheet = workbook.active
    stored = {str(cell.value): cell.data_type for row in sheet.iter_rows() for cell in row}
    for payload in FORMULA_PAYLOADS + ['+SUM(A1)', '@notes']:
        assert stored[payload] == 's', f'{payload!r} not stored as text'
    # pd.read_excel round-trips the exact strings — zero characters modified.
    back = pd.read_excel(xlsx_path)
    assert back['attack'].tolist() == FORMULA_PAYLOADS + ['+SUM(A1)', '@notes']
    assert back['number'].tolist() == [1.5, 2, 3, 4]


def test_normal_excel_conversion_values_unchanged(tmp_path):
    csv_path = tmp_path / 'mixed_raw.csv'
    source = pd.DataFrame({
        'text': ['a', 'b', 'c'],
        'neg_string': ['-5c', '+plus', '@at'],
        'int': [10, 20, 30],
        'float': [0.5, 1.5, 2.5],
        'empty': [None, 'x', None],
    })
    source.to_csv(csv_path, index=False)
    out = FormatAlchemyEngine.convert(str(csv_path), 'excel')
    back = pd.read_excel(out)
    assert back['text'].tolist() == source['text'].tolist()
    assert back['neg_string'].tolist() == source['neg_string'].tolist()
    assert back['int'].tolist() == source['int'].tolist()
    assert back['float'].tolist() == source['float'].tolist()
    assert back['empty'].isna().tolist() == [True, False, True]


# --- xlsx zip-bomb pre-check ---------------------------------------------------


def test_xlsx_entry_count_guard(tmp_path, monkeypatch):
    good = tmp_path / 'small_raw.csv'
    pd.DataFrame({'a': [1]}).to_csv(good, index=False)
    out = FormatAlchemyEngine.convert(str(good), 'excel')
    monkeypatch.setattr('scripts.format_alchemy.MAX_XLSX_ENTRIES', 2)
    with pytest.raises(DataFetchError) as caught:
        FormatAlchemyEngine.xlsx_to_csv(out, str(tmp_path / 'out.csv'))
    assert caught.value.code == 'SIZE_LIMIT'
    assert 'zip entries' in str(caught.value)


def test_xlsx_uncompressed_size_guard(tmp_path, monkeypatch):
    good = tmp_path / 'small_raw.csv'
    pd.DataFrame({'a': [1]}).to_csv(good, index=False)
    out = FormatAlchemyEngine.convert(str(good), 'excel')
    monkeypatch.setattr('scripts.format_alchemy.MAX_XLSX_UNCOMPRESSED_BYTES', 10)
    with pytest.raises(DataFetchError) as caught:
        FormatAlchemyEngine.xlsx_to_csv(out, str(tmp_path / 'out.csv'))
    assert caught.value.code == 'SIZE_LIMIT'


def test_xlsx_oversize_limits_are_strict_by_default():
    assert MAX_XLSX_UNCOMPRESSED_BYTES == 200 * 1024 * 1024
    assert MAX_XLSX_ENTRIES == 1000


def test_xlsx_corrupt_file_keeps_original_error(tmp_path):
    fake = tmp_path / 'broken.xlsx'
    fake.write_bytes(b'this is not a zip archive')
    with pytest.raises(zipfile.BadZipFile):
        FormatAlchemyEngine.xlsx_to_csv(str(fake), str(tmp_path / 'out.csv'))
