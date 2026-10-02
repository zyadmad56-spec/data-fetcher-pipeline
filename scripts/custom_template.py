"""Body embedded into generated standalone scripts (no pipeline dependency)."""

EXTRACTION_BODY = r'''
MAX_BYTES = 50 * 1024 * 1024

def _response_format(mime, url):
    # Current response metadata takes precedence over a stale URL extension.
    for marker, payload_format in (
        ('html', 'html'), ('json', 'json'), ('spreadsheetml', 'xlsx'),
        ('parquet', 'parquet'), ('tab-separated', 'tsv'), ('csv', 'csv'),
    ):
        if marker in mime:
            return payload_format
    return os.path.splitext(urlparse(url).path)[1].lower().lstrip('.')

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
        if length.isdigit() and int(length) > MAX_BYTES:
            raise ValueError('SIZE_LIMIT: custom payload exceeds 50 MB')
        chunks, total = [], 0
        for chunk in response.iter_content(16384):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError('SIZE_LIMIT: custom payload exceeds 50 MB')
            chunks.append(chunk)
        body = b''.join(chunks)
        mime = response.headers.get('Content-Type', '').lower()
        final_url = response.url if isinstance(response.url, str) else target
    finally:
        response.close()
    payload_format = _response_format(mime, final_url)
    if not payload_format and DIFFICULTY == 'medium':
        payload_format = 'html'
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
        df = pd.read_excel(io.BytesIO(body))
    elif payload_format == 'parquet':
        df = pd.read_parquet(io.BytesIO(body))
    elif payload_format in ('csv', 'tsv'):
        df = pd.read_csv(io.BytesIO(body), sep='\t' if payload_format == 'tsv' else ',',
                         nrows=row_limit or None)
    else:
        raise ValueError('Unsupported payload format: choose a CSV/TSV/JSON/XLSX/Parquet or HTML table URL')
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
