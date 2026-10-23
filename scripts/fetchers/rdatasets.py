"""Public Rdatasets catalog discovery and CSV downloads."""
import csv
import io
import re
from urllib.parse import quote

from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.http_utils import add_metrics_bytes, request_with_retry


class RdatasetsFetcher(BaseFetcher):
    BASE = 'https://vincentarelbundock.github.io/Rdatasets'
    MAX_CATALOG_BYTES = 4 * 1024 * 1024

    def _catalog(self) -> list[dict]:
        response = request_with_retry(self.BASE + '/datasets.csv',
                                      headers=self._payload_headers(), stream=True)
        try:
            if response.status_code != 200:
                raise DataFetchError(f'Rdatasets catalog returned HTTP {response.status_code}.',
                                     code='PROVIDER_ERROR')
            content = bytearray()
            for chunk in response.iter_content(16384):
                add_metrics_bytes(len(chunk))
                content.extend(chunk)
                if len(content) > self.MAX_CATALOG_BYTES:
                    raise DataFetchError('Rdatasets catalog exceeds the 4 MB limit.',
                                         code='PROVIDER_ERROR')
        finally:
            response.close()
        try:
            reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
            required = {'Package', 'Item', 'Title', 'Rows', 'Cols', 'CSV', 'Doc'}
            if not required.issubset(reader.fieldnames or []):
                raise DataFetchError('Rdatasets catalog is missing expected CSV fields.',
                                     code='PROVIDER_ERROR')
            return list(reader)
        except (UnicodeError, csv.Error) as exc:
            raise DataFetchError('Rdatasets catalog is not valid UTF-8 CSV.',
                                 code='PROVIDER_ERROR') from exc

    def _select(self, rows: list[dict], query: str) -> dict:
        if '/' in query:
            matches = [row for row in rows if f"{row['Package']}/{row['Item']}".casefold() == query.casefold()]
        else:
            matches = [row for row in rows if (row.get('Item') or '').casefold() == query.casefold()]
            if not matches:
                terms = query.casefold().split()
                matches = [row for row in rows if all(
                    term in f"{row['Package']} {row['Item']} {row['Title']}".casefold() for term in terms)]
        if not matches:
            raise DataFetchError(f'No Rdatasets dataset matches {query!r}. Use package/dataset, such as datasets/iris.',
                                 code='NOT_FOUND')
        if len(matches) > 1:
            choices = sorted(f"{row['Package']}/{row['Item']}" for row in matches)
            raise DataFetchError('Multiple Rdatasets matches. Choose package/dataset: ' + ', '.join(choices[:10]),
                                 code='AMBIGUOUS_DATASET')
        return matches[0]

    def scout(self) -> dict:
        query = self.query.strip()
        if not query or ('/' in query and (
                query.count('/') != 1 or any(part in ('.', '..') or not re.fullmatch(r'[\w.-]+', part)
                                             for part in query.split('/')))):
            raise DataFetchError('Use an Rdatasets package/dataset identifier or search words, not a URL.',
                                 code='ARG_INVALID')
        row = self._select(self._catalog(), query)
        package, item = row['Package'], row['Item']
        expected = f'{self.BASE}/csv/{quote(package, safe="")}/{quote(item, safe="")}.csv'
        if row['CSV'] != expected:
            raise DataFetchError('Unexpected CSV URL in the Rdatasets catalog.', code='PROVIDER_ERROR')
        self.download_url = expected
        self.resolved_title = f"{package}/{item}: {row['Title']}"
        print(f'[Scout] Rdatasets selected {package}/{item} (public CSV, no API key).')
        return {'url': self.download_url, 'documentation_url': row['Doc'],
                'size_info': f"Catalog reports {row['Rows']} rows and {row['Cols']} data columns; CSV may also include row names"}

    def extract(self):
        if not getattr(self, 'download_url', ''):
            raise DataFetchError('Run scout() before extracting Rdatasets.', code='ARG_INVALID')
        return self._consume_payload_csv()
