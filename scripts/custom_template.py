"""Body embedded into generated standalone scripts (no pipeline dependency).

The network-policy preamble (constants, IP validation, pinned transport) is
emitted by scripts/web_analyzer.py generate_script(); this module contributes
the extraction logic that runs after it.  MAX_BYTES is defined by the preamble.
"""

EXTRACTION_BODY = r'''
def _response_format(mime, url):
    # Current response metadata takes precedence over a stale URL extension.
    for marker, payload_format in (
        ('html', 'html'), ('json', 'json'), ('spreadsheetml', 'xlsx'),
        ('parquet', 'parquet'), ('tab-separated', 'tsv'), ('csv', 'csv'),
    ):
        if marker in mime:
            return payload_format
    return os.path.splitext(urlparse(url).path)[1].lower().lstrip('.')


def _guard_excel_size(body):
    """Reject zip bombs before pandas touches the workbook."""
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) > XLSX_MAX_ENTRIES:
                raise ValueError('SIZE_LIMIT: workbook contains more than ' + str(XLSX_MAX_ENTRIES) + ' entries')
            if sum(entry.file_size for entry in entries) > XLSX_MAX_UNCOMPRESSED_BYTES:
                raise ValueError('SIZE_LIMIT: workbook expands beyond the 200 MB uncompressed limit')
    except zipfile.BadZipFile:
        pass  # not a zip at all: pd.read_excel below surfaces the real parse error


def _scan_complete_records(text, scanned, in_quotes, segment_start, records, needed):
    """Advance a quote-aware record scan over text[scanned:].

    Returns (scanned, in_quotes, segment_start, records, boundary, done) where
    boundary is the index of the newline completing the `needed`-th complete
    non-empty record (only cut points the scanner believes end a record are
    returned — the scan is conservative and may stop later, never mid-record).
    A trailing quote is left unconsumed so an escaped-quote pair split across
    chunks is re-read with the next batch.
    """
    i = scanned
    n = len(text)
    boundary = -1
    limit = n - 1 if n > i and text[n - 1] == '"' else n
    while i < limit:
        character = text[i]
        if in_quotes:
            if character == '"':
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                in_quotes = False
        elif character == '"':
            in_quotes = True
        elif character == '\n':
            if text[segment_start:i].strip('\r') != '':
                records += 1
                boundary = i
                if records >= needed:
                    return i + 1, in_quotes, i + 1, records, boundary, True
            segment_start = i + 1
        i += 1
    return i, in_quotes, segment_start, records, boundary, False


def _parse_csv_bounded(response, payload_format, row_limit):
    """Stream a CSV/TSV and stop once `row_limit` COMPLETE records are buffered.

    Complete records are found with a quote-aware scanner (quoted multiline
    fields never fool the boundary detection); pandas then parses only the
    buffered prefix.  Returns None when the source holds fewer complete
    records than requested — the caller falls back to the full 50 MB read.
    """
    import codecs
    import io
    separator = '\t' if payload_format == 'tsv' else ','
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    text = ''
    raw_chunks = []
    scanned = 0
    in_quotes = False
    segment_start = 0
    records = 0
    needed = row_limit + 1  # header + the requested rows
    boundary = -1
    total = 0
    # Bounded row requests (previews and explicit --rows) carry the smaller
    # preview budget; only unbounded full fetches may use the full ceiling.
    cap = MAX_PREVIEW_BYTES if row_limit else MAX_BYTES
    for chunk in response.iter_content(16384):
        total += len(chunk)
        if total > cap:
            raise ValueError('SIZE_LIMIT: custom payload exceeds the %d MB %s budget' % (
                cap // (1024 * 1024), 'preview' if row_limit else 'transfer'))
        raw_chunks.append(chunk)
        text += decoder.decode(chunk)
        scanned, in_quotes, segment_start, records, boundary, done = _scan_complete_records(
            text, scanned, in_quotes, segment_start, records, needed)
        if done:
            break
    if records < needed:
        # The final held-back characters may still complete the last record.
        n = len(text)
        i = scanned
        while i < n:
            character = text[i]
            if in_quotes:
                if character == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        i += 2
                        continue
                    in_quotes = False
            elif character == '"':
                in_quotes = True
            elif character == '\n':
                if text[segment_start:i].strip('\r') != '':
                    records += 1
                    boundary = i
                    if records >= needed:
                        break
                segment_start = i + 1
            i += 1
    if records >= needed:
        return pd.read_csv(io.StringIO(text[:boundary + 1]), sep=separator,
                           nrows=row_limit, low_memory=False)
    # Fewer complete records than requested within the cap: parse everything.
    return pd.read_csv(io.BytesIO(b''.join(raw_chunks)), sep=separator,
                       nrows=row_limit or None, low_memory=False)


def extract_frame(url=None, columns=None, table_index=None, row_limit=0):
    import io
    import json
    target = url or URL
    if DIFFICULTY not in ('easy', 'medium'):
        raise ValueError('Unsupported or blocked website: use an authorized API/export or a reviewed browser extractor.')
    response = _fetch_with_retry(target, HEADERS)
    try:
        if response.status_code != 200:
            raise ValueError('HTTP ' + str(response.status_code) + ' fetching dataset')
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > (MAX_PREVIEW_BYTES if row_limit else MAX_BYTES):
            raise ValueError('SIZE_LIMIT: custom payload exceeds the %d MB %s budget' % (
                (MAX_PREVIEW_BYTES if row_limit else MAX_BYTES) // (1024 * 1024),
                'preview' if row_limit else 'transfer'))
        mime = response.headers.get('Content-Type', '').lower()
        final_url = response.url if isinstance(response.url, str) else target
        payload_format = _response_format(mime, final_url)
        if not payload_format and DIFFICULTY == 'medium':
            payload_format = 'html'
        if payload_format in ('csv', 'tsv') and row_limit:
            # Bounded preview: stop streaming once enough complete records exist.
            df = _parse_csv_bounded(response, payload_format, row_limit)
        else:
            chunks, total = [], 0
            full_read_cap = MAX_PREVIEW_BYTES if row_limit else MAX_BYTES
            for chunk in response.iter_content(16384):
                total += len(chunk)
                if total > full_read_cap:
                    raise ValueError('SIZE_LIMIT: custom payload exceeds the %d MB %s budget' % (
                        full_read_cap // (1024 * 1024), 'preview' if row_limit else 'transfer'))
                chunks.append(chunk)
            body = b''.join(chunks)
            if payload_format in ('html', 'htm'):
                # BeautifulSoup is already a package dependency; no implicit lxml requirement.
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(body, 'html.parser')
                tables = []
                for table in soup.find_all('table'):
                    if table.find(attrs={'rowspan': True}) or table.find(attrs={'colspan': True}):
                        tables.append(None)
                        continue
                    rows = [[cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'])]
                            for row in table.find_all('tr')]
                    rows = [row for row in rows if row]
                    if len(rows) < 2 or len(set(rows[0])) != len(rows[0]):
                        tables.append(None)
                        continue
                    width = len(rows[0])
                    if any(len(row) != width for row in rows):
                        tables.append(None)
                        continue  # colspan/rowspan needs a purpose-built extractor
                    tables.append(pd.DataFrame(rows[1:], columns=rows[0]))
                if table_index is not None:
                    if not 0 <= table_index < len(tables) or tables[table_index] is None:
                        raise ValueError('NOT_FOUND: selected table index is missing or needs a tailored extractor')
                    df = tables[table_index]
                else:
                    matches = [t for t in tables if t is not None and (not columns or all(c in t.columns for c in columns))]
                    if len(matches) != 1:
                        raise ValueError('NOT_FOUND: no unique table matches; supply required columns or --table INDEX')
                    df = matches[0]
            elif payload_format == 'json':
                data = json.loads(body)
                if isinstance(data, dict):
                    candidates = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
                    if len(candidates) == 1:
                        data = candidates[0]
                    elif len(candidates) > 1:
                        raise ValueError('NOT_FOUND: multiple record arrays; a tailored JSON selector is required')
                df = pd.json_normalize(data)
            elif payload_format == 'xlsx':
                _guard_excel_size(body)
                df = pd.read_excel(io.BytesIO(body))
            elif payload_format == 'parquet':
                df = pd.read_parquet(io.BytesIO(body))
            elif payload_format in ('csv', 'tsv'):
                df = pd.read_csv(io.BytesIO(body), sep='\t' if payload_format == 'tsv' else ',',
                                 nrows=row_limit or None)
            else:
                raise ValueError('Unsupported payload format: choose a CSV/TSV/JSON/XLSX/Parquet or HTML table URL')
    finally:
        response.close()
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError('NOT_FOUND: missing required columns: ' + ', '.join(missing))
        df = df.loc[:, columns]
    if df.empty:
        raise ValueError('NOT_FOUND: dataset contains no records')
    return df.head(row_limit) if row_limit else df


def run_extraction():
    import argparse
    parser = argparse.ArgumentParser(description='Reviewed public-source extractor')
    parser.add_argument('--output', required=True, help='Explicit destination CSV path')
    parser.add_argument('--rows', type=int, default=0)
    parser.add_argument('--columns', default='')
    parser.add_argument('--table', type=int)
    args = parser.parse_args()
    try:
        if args.rows < 0:
            raise ValueError('Row count must be non-negative')
        frame = extract_frame(columns=[c.strip() for c in args.columns.split(',') if c.strip()],
                              table_index=args.table, row_limit=args.rows)
        frame.to_csv(args.output, index=False)
        print('[Success] Saved ' + str(len(frame)) + ' rows to ' + os.path.abspath(args.output))
    except Exception as exc:
        print('[Error] ' + str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    run_extraction()
'''
