import csv
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import requests

from scripts.factory import get_fetcher, source_info
from scripts.errors import DataFetchError


BASE = 'https://vincentarelbundock.github.io/Rdatasets'
IRIS = b'rownames,Sepal.Length,Species\n1,5.1,setosa\n2,4.9,setosa\n3,4.7,setosa\n'


def response(content, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = content
    result.close = lambda: None
    result.iter_content = lambda chunk_size: iter([content])
    return result


def catalog(*pairs):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=['Package', 'Item', 'Title', 'Rows', 'Cols', 'CSV', 'Doc'])
    writer.writeheader()
    for package, item, title in pairs:
        writer.writerow({'Package': package, 'Item': item, 'Title': title, 'Rows': 3, 'Cols': 2,
                         'CSV': f'{BASE}/csv/{package}/{item}.csv',
                         'Doc': f'{BASE}/doc/{package}/{item}.html'})
    return stream.getvalue().encode()


INDEX = catalog(('datasets', 'iris', 'Iris flower measurements'),
                ('other', 'iris', 'Another Iris dataset'),
                ('datasets', 'cars', 'Speed and stopping distances of cars'))


def test_rdatasets_source_is_available():
    entry = source_info().get('rdatasets')
    assert entry is not None, 'Rdatasets must be available in source selection'
    assert entry['auth'] == 'none'
    assert entry['example'] == 'datasets/iris'


def test_exact_dataset_uses_public_catalog_and_no_auth(tmp_path):
    fetcher = get_fetcher('rdatasets', 'datasets/iris', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)) as request:
        metadata = fetcher.scout()
    assert metadata['url'] == f'{BASE}/csv/datasets/iris.csv'
    assert metadata['documentation_url'] == f'{BASE}/doc/datasets/iris.html'
    assert 'Iris flower' in fetcher.resolved_title
    assert fetcher.source_key == 'rdatasets'
    assert request.call_args.kwargs.get('headers', {}).keys() <= {'User-Agent', 'Accept'}


def test_title_search_resolves_unique_dataset(tmp_path):
    fetcher = get_fetcher('rdatasets', 'stopping distances', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)):
        assert fetcher.scout()['url'].endswith('/datasets/cars.csv')


def test_ambiguous_name_shows_qualified_choices(tmp_path):
    fetcher = get_fetcher('rdatasets', 'iris', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)):
        with pytest.raises(DataFetchError, match='datasets/iris.*other/iris') as error:
            fetcher.scout()
    assert error.value.code == 'AMBIGUOUS_DATASET'


def test_unknown_dataset_is_not_found(tmp_path):
    fetcher = get_fetcher('rdatasets', 'datasets/does_not_exist', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)):
        with pytest.raises(DataFetchError) as error:
            fetcher.scout()
    assert error.value.code == 'NOT_FOUND'


@pytest.mark.parametrize('query', ['', '../iris', 'datasets/../iris', 'https://example.com/iris.csv'])
def test_invalid_query_does_not_make_requests(tmp_path, query):
    fetcher = get_fetcher('rdatasets', query, str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry') as request:
        with pytest.raises(DataFetchError):
            fetcher.scout()
    request.assert_not_called()


@pytest.mark.parametrize('content,status', [(b'unavailable', 503), (b'<html>Maintenance</html>', 200)])
def test_bad_catalog_fails_clearly(tmp_path, content, status):
    fetcher = get_fetcher('rdatasets', 'datasets/iris', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(content, status)):
        with pytest.raises(DataFetchError) as error:
            fetcher.scout()
    assert error.value.code == 'PROVIDER_ERROR'


def test_catalog_cannot_redirect_dataset_to_another_host(tmp_path):
    malicious = INDEX.replace(f'{BASE}/csv/datasets/iris.csv'.encode(), b'https://example.com/iris.csv')
    fetcher = get_fetcher('rdatasets', 'datasets/iris', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(malicious)):
        with pytest.raises(DataFetchError, match='CSV URL'):
            fetcher.scout()


def test_preview_then_save_keeps_original_bytes_and_profile(tmp_path):
    fetcher = get_fetcher('rdatasets', 'datasets/iris', str(tmp_path), {})
    with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)):
        fetcher.scout()
    with patch('scripts.base.request_with_retry', return_value=response(IRIS)) as payload_request:
        fetcher.preview()
        assert list(fetcher.preview_frame.columns) == ['rownames', 'Sepal.Length', 'Species']
        assert len(fetcher.preview_frame) == 3
        assert not list(tmp_path.rglob('*_raw.csv'))
        fetcher.auto_approve = True
        fetcher.row_limit = 2
        with patch('scripts.fetchers.rdatasets.request_with_retry', return_value=response(INDEX)):
            csv_path = Path(fetcher.run())
    assert payload_request.call_count == 1  # a complete preview payload is reused
    saved = pd.read_csv(csv_path)
    assert saved['rownames'].tolist() == [1, 2]
    assert saved['Sepal.Length'].tolist() == [5.1, 4.9]
    assert Path(fetcher.last_description_path).is_file()
    original = Path(fetcher.last_original_path)
    assert original.read_bytes() == IRIS
    assert fetcher.last_original_sha256 == hashlib.sha256(IRIS).hexdigest()
    manifest = json.loads(Path(fetcher.last_manifest_path).read_text().splitlines()[-1])
    assert manifest['url'] == f'{BASE}/csv/datasets/iris.csv'
