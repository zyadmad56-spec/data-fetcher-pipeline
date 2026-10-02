import io
import json
import runpy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.base import BaseFetcher
from scripts.cli import main
from scripts.errors import DataFetchError
from scripts.factory import get_fetcher, list_sources, source_info
from scripts.source_registry import save_source, load_sources, verified_script, finish_source_choice
from scripts.web_analyzer import WebAnalyzer, AnalysisReport
from scripts.workflow import interview, relevance_warning


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path):
    with patch('scripts.source_registry.registry_root', return_value=tmp_path / 'config'):
        yield


def response(body, mime='text/csv'):
    r = MagicMock(status_code=200, headers={'Content-Type': mime, 'Content-Length': str(len(body))})
    r.iter_content.side_effect = lambda *a, **k: iter([body[i:i+16384] for i in range(0, len(body), 16384)])
    return r


class FrameFetcher(BaseFetcher):
    def scout(self):
        return {'url': 'https://example.com/data.csv'}

    def extract(self):
        return pd.DataFrame({'value': range(20), 'label': ['real record'] * 20})


def test_source_first_number_and_five_data_questions():
    number = str(list_sources().index('kaggle') + 1)
    with patch('builtins.input', side_effect=['1', number, 'aircraft training data', 'owner/aircraft', 'value,label', '100', 'excel']) as ask:
        req = interview('chosen-output')
    assert req.source == 'kaggle' and req.query == 'owner/aircraft'
    assert req.rows == 100 and req.columns == ['value', 'label'] and req.output_format == 'excel'
    assert req.outdir == 'chosen-output'
    assert ask.call_args_list[0].args[0].startswith('1. Ready')
    assert ask.call_count == 7  # two routing questions plus five request questions


def test_external_platform_name_requires_url():
    with patch('builtins.input', side_effect=['2', 'Example Portal', 'https://example.com/data.csv', 'aircraft', 'value', '50', 'json']):
        req = interview()
    assert req.source == 'custom' and req.query == 'https://example.com/data.csv'
    assert req.rows == 50 and req.output_format == 'json'


def test_mismatch_can_switch_source():
    with patch('builtins.input', side_effect=['1', 'airbnb', 'طيارات', 'switch', 'openml', 'aircraft', '', '', '']):
        req = interview()
    assert req.source == 'openml'
    assert relevance_warning('sec', 'aircraft company revenue') == ''
    assert relevance_warning('sec', 'افلام')


def test_preset_rows_still_require_final_approval(tmp_path, capsys):
    f = FrameFetcher('records', str(tmp_path), {})
    f.row_limit = 2
    answers = iter(['y', 'n'])
    def answer(prompt):
        if 'Does the sample' in prompt:
            assert 'real record' in capsys.readouterr().out
        return next(answers)
    with patch('builtins.input', side_effect=answer), pytest.raises(DataFetchError, match='aborted') as err:
        f.run()
    assert err.value.code == 'ABORTED'
    assert not list(Path(f.outdir).glob('*'))


def test_full_preview_cached_but_final_rows_can_increase(tmp_path):
    f = FrameFetcher('records', str(tmp_path), {})
    f.row_limit = 2
    with patch.object(f, 'extract', wraps=f.extract) as extract, patch('builtins.input', side_effect=['y', '10']):
        output = f.run()
    assert len(pd.read_csv(output)) == 10
    assert extract.call_count == 1


def test_transfer_decline_does_not_extract(tmp_path):
    f = FrameFetcher('records', str(tmp_path), {})
    with patch.object(f, 'extract') as extract, patch('builtins.input', return_value='n'), pytest.raises(DataFetchError):
        f.run()
    extract.assert_not_called()


def test_final_approval_can_change_preset_count_to_all(tmp_path):
    f = FrameFetcher('records', str(tmp_path), {})
    f.row_limit = 2
    with patch('builtins.input', side_effect=['y', 'all']):
        output = f.run()
    assert len(pd.read_csv(output)) == 20


def test_missing_fields_stop_before_save(tmp_path):
    f = FrameFetcher('records', str(tmp_path), {}, auto_approve=True)
    f.required_columns = ['aircraft_model']
    with pytest.raises(DataFetchError) as err:
        f.run()
    assert err.value.code == 'NOT_FOUND'
    assert not list(Path(f.outdir).glob('*'))


def test_csv_prefix_is_bounded_and_closed(tmp_path):
    f = FrameFetcher('records', str(tmp_path), {}, auto_approve=True)
    f.download_url = 'https://example.com/data.csv'
    f.preview_only = True
    payload = b'value,label\n' + b'1,real\n' * 100000
    r = response(payload)
    consumed = []
    def chunks(*a):
        for i in range(0, len(payload), 16384):
            consumed.append(i)
            yield payload[i:i+16384]
    r.iter_content.side_effect = chunks
    with patch('scripts.base.request_with_retry', return_value=r):
        assert f.run() == ''
    assert len(consumed) == 16
    assert len(f.preview_frame) == 5
    r.close.assert_called_once()
    assert not list(Path(f.outdir).glob('*'))


def generated(tmp_path, difficulty='easy', url='https://example.com/data.csv'):
    analyzer = WebAnalyzer(url, str(tmp_path))
    path = analyzer.generate_script(AnalysisReport(url=url, difficulty=difficulty))
    module = runpy.run_path(path, run_name='tested_extractor')
    return Path(path), module['extract_frame']


@pytest.mark.parametrize('url,mime,payload,expected', [
    ('https://example.com/a.csv', 'text/csv', b'a,b\n1,2\n3,4\n', [1,3]),
    ('https://example.com/a.tsv', 'text/tab-separated-values', b'a\tb\n1\t2\n', [1]),
    ('https://example.com/a.json', 'application/json', b'{"data":[{"a":1,"b":2}]}', [1]),
])
def test_generated_extractors_actually_parse(tmp_path, url, mime, payload, expected):
    _, extract = generated(tmp_path, url=url)
    r = response(payload, mime)
    extract.__globals__['_fetch_with_retry'] = lambda *a: r
    frame = extract(columns=['a'])
    assert frame.columns.tolist() == ['a'] and frame.a.tolist() == expected
    r.close.assert_called_once()


def test_generated_xlsx_and_parquet(tmp_path):
    frame = pd.DataFrame({'a':[1,2]})
    for suffix in ('xlsx', 'parquet'):
        stream = io.BytesIO()
        (frame.to_excel if suffix == 'xlsx' else frame.to_parquet)(stream, index=False)
        _, extract = generated(tmp_path, url='https://example.com/a.' + suffix)
        extract.__globals__['_fetch_with_retry'] = lambda *a: response(stream.getvalue(), 'application/octet-stream')
        pd.testing.assert_frame_equal(extract(), frame)


def test_html_table_selection_uses_requested_fields(tmp_path):
    _, extract = generated(tmp_path, 'medium', 'https://example.com/table')
    html = b'<table><tr><th>x</th></tr><tr><td>bad</td></tr></table><table><tr><th>model</th></tr><tr><td>A320</td></tr></table>'
    extract.__globals__['_fetch_with_retry'] = lambda *a: response(html, 'text/html')
    assert extract(columns=['model']).model.tolist() == ['A320']
    with pytest.raises(ValueError, match='unique table'):
        extract()
    assert extract(table_index=0).x.tolist() == ['bad']


@pytest.mark.parametrize('failure', ['empty', 'http', 'size', 'missing'])
def test_generated_failure_propagates(tmp_path, failure):
    _, extract = generated(tmp_path)
    r = response(b'a\n' if failure == 'empty' else b'a\n1\n')
    if failure == 'http':
        r.status_code = 403
    if failure == 'size':
        r.headers['Content-Length'] = str(51 * 1024 * 1024)
    extract.__globals__['_fetch_with_retry'] = lambda *a: r
    with pytest.raises(ValueError):
        extract(columns=['wrong'] if failure == 'missing' else [])
    r.close.assert_called_once()


def test_save_discover_reuse_and_checksum(tmp_path):
    script, _ = generated(tmp_path)
    with patch('scripts.web_analyzer.validate_public_url'):
        key = save_source('Example Portal', 'https://example.com/data.csv', script)
    assert key in list_sources() and source_info()[key]['platform'] == 'Example Portal'
    descriptor = load_sources()[key]
    f = get_fetcher(key, 'https://example.com/data.csv', str(tmp_path), {})
    assert f.descriptor == descriptor
    with pytest.raises(DataFetchError):
        get_fetcher(key, 'https://another.com/data.csv', str(tmp_path), {})
    retained = verified_script(descriptor)
    retained.write_text('tampered', encoding='utf-8')
    with pytest.raises(DataFetchError, match='changed'):
        verified_script(descriptor)


def test_registry_collision_does_not_replace(tmp_path):
    script, _ = generated(tmp_path)
    with patch('scripts.web_analyzer.validate_public_url'):
        save_source('Portal', 'https://example.com/data.csv', script)
        before = load_sources()
        with pytest.raises(DataFetchError) as err:
            save_source('Portal', 'https://example.com/other.csv', script)
    assert err.value.code == 'SOURCE_EXISTS' and load_sources() == before


def test_distinct_arabic_platform_names_can_both_be_saved(tmp_path):
    script, _ = generated(tmp_path)
    with patch('scripts.web_analyzer.validate_public_url'):
        first = save_source('منصة الطيران', 'https://example.com/data.csv', script)
        second = save_source('منصة الإحصاء', 'https://example.com/data.csv', script)
    assert first != second
    assert source_info()[first]['platform'] == 'منصة الطيران'
    assert source_info()[second]['platform'] == 'منصة الإحصاء'


def test_concurrent_registration_fails_without_overwriting(tmp_path):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'sources.lock').write_text('another writer')
    script, _ = generated(tmp_path)
    with pytest.raises(DataFetchError) as err:
        save_source('Portal', 'https://example.com/data.csv', script)
    assert err.value.code == 'REGISTRY_BUSY'
    assert (root / 'sources.lock').read_text() == 'another writer'
    assert not (root / 'sources.json').exists()


def test_registry_path_traversal_rejected(tmp_path):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'sources.json').write_text(json.dumps({'version':1,'sources':{'custom_bad':{'script':'../bad.py'}}}))
    with pytest.raises(DataFetchError) as err:
        load_sources()
    assert err.value.code == 'REGISTRY_INVALID'


def run_custom_cli(tmp_path, argv, inputs=None):
    report = AnalysisReport(url='https://example.com/data.csv', difficulty='easy')
    with patch('sys.argv', ['data-fetcher', '--source','custom','--query',report.url,'--outdir',str(tmp_path / 'data'), *argv]), \
         patch('scripts.cli.setup_wizard', return_value={}), \
         patch('scripts.web_analyzer.WebAnalyzer.analyze', return_value=report), \
         patch('socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',0))]), \
         patch('requests.get', side_effect=lambda url, **k: MagicMock(status_code=404, headers={}) if url.endswith('/robots.txt') else response(b'value,label\n1,real\n2,real\n3,real\n')), \
         patch('builtins.input', side_effect=inputs or AssertionError('Machine mode prompted')):
        main()


def test_custom_json_fetch_saves_data_and_source(tmp_path, capsys):
    run_custom_cli(tmp_path, ['--yes','--non-interactive','--json-output','--save-source','Portal','--output-format','excel','--rows','2'])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope['status'] == 'success' and envelope['rows'] == 2
    assert Path(envelope['output_path']).is_file() and Path(envelope['converted_file']).is_file()
    assert envelope['registered_source'] in list_sources()
    assert not list((tmp_path / 'data').rglob('scrape_*.py'))


def test_custom_preview_json_contains_actual_rows_no_dataset(tmp_path, capsys):
    run_custom_cli(tmp_path, ['--yes','--non-interactive','--json-output','--preview-only'])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope['mode'] == 'preview' and envelope['sample'][0]['label'] == 'real'
    assert envelope['output_path'] is None
    assert not list((tmp_path / 'data').rglob('*raw.csv')) and not load_sources()


def test_declining_registration_preserves_data_removes_only_script(tmp_path):
    run_custom_cli(tmp_path, [], ['y', 'y', 'n'])
    assert len(list((tmp_path / 'data').rglob('*raw.csv'))) == 1
    assert not load_sources()
    assert not list((tmp_path / 'data').rglob('*.py'))


@pytest.mark.parametrize('keep', [True, False])
def test_agent_can_resolve_retention_after_cli_exits_without_refetch(tmp_path, capsys, keep):
    run_custom_cli(tmp_path, ['--yes','--non-interactive','--json-output','--defer-source-choice'])
    envelope = json.loads(capsys.readouterr().out)
    token = envelope['pending_source_id']
    pending = tmp_path / 'config' / 'pending_sources'
    assert (pending / (token + '.py')).is_file() and not load_sources()
    argv = ['data-fetcher', '--json-output', '--keep-source' if keep else '--discard-source', token]
    if keep:
        argv += ['--save-source', 'Portal']
    with patch('sys.argv', argv), patch('scripts.web_analyzer.validate_public_url'), patch('requests.get', side_effect=AssertionError('Refetched during retention')):
        main()
    result = json.loads(capsys.readouterr().out)
    assert result['source_choice'] == ('kept' if keep else 'discarded')
    assert bool(load_sources()) == keep and not list(pending.iterdir())
    assert Path(envelope['output_path']).is_file()


def test_saved_source_actually_extracts_again(tmp_path):
    script, _ = generated(tmp_path)
    with patch('scripts.web_analyzer.validate_public_url'):
        key = save_source('Portal', 'https://example.com/data.csv', script)
    f = get_fetcher(key, 'https://example.com/data.csv', str(tmp_path / 'again'), {})
    f.auto_approve = True
    report = AnalysisReport(url=f.query, difficulty='easy')
    with patch('scripts.web_analyzer.WebAnalyzer.analyze', return_value=report), \
         patch('socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',0))]), \
         patch('requests.get', side_effect=lambda url, **k: MagicMock(status_code=404,headers={}) if url.endswith('/robots.txt') else response(b'a\n42\n')):
        output = f.run()
    assert pd.read_csv(output).a.tolist() == [42]
    assert f._workspace is None  # reusable script not deleted by cleanup
    f.cleanup()
    assert verified_script(load_sources()[key]).is_file()


def test_corrupt_registry_discovery_has_single_error_envelope(tmp_path, capsys):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'sources.json').write_text('{bad')
    with patch('sys.argv', ['data-fetcher','--list-sources','--json-output']), pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'REGISTRY_INVALID'


def test_pending_choice_rejects_arbitrary_paths(tmp_path):
    data = tmp_path / 'important.csv'
    data.write_text('keep me')
    with pytest.raises(DataFetchError):
        finish_source_choice(str(data))
    assert data.read_text() == 'keep me'


@pytest.mark.parametrize('payload', [[], {'version':1,'sources':{'custom_bad':{'name':'Bad','script':'custom_bad.py','sha256':'a'*64,'url':42}}}])
def test_invalid_registry_structure_returns_coded_error(tmp_path, payload):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'sources.json').write_text(json.dumps(payload))
    with pytest.raises(DataFetchError) as err:
        load_sources()
    assert err.value.code == 'REGISTRY_INVALID'


def test_table_index_does_not_shift_when_first_table_is_unsupported(tmp_path):
    _, extract = generated(tmp_path, 'medium', 'https://example.com/table')
    html = b'<table><tr><th colspan="2">bad</th></tr><tr><td>1</td><td>2</td></tr></table><table><tr><th>model</th></tr><tr><td>A320</td></tr></table>'
    extract.__globals__['_fetch_with_retry'] = lambda *a: response(html,'text/html')
    assert extract(table_index=1).model.tolist() == ['A320']
    with pytest.raises(ValueError, match='tailored extractor'):
        extract(table_index=0)


def test_custom_failure_returns_error_and_cleans_script(tmp_path, capsys):
    with patch('scripts.custom_source.CustomFetcher.extract', side_effect=DataFetchError('No aircraft records', code='NOT_FOUND')):
        with pytest.raises(SystemExit) as exit:
            run_custom_cli(tmp_path, ['--yes','--non-interactive','--json-output'])
    result = json.loads(capsys.readouterr().out)
    assert exit.value.code == 1 and result['status'] == 'error' and result['code'] == 'NOT_FOUND'
    assert not list((tmp_path / 'data').rglob('*.py'))
    assert not list((tmp_path / 'data').rglob('*raw.csv'))


def test_direct_file_403_never_classified_easy(tmp_path):
    analyzer = WebAnalyzer('https://example.com/data.csv', str(tmp_path))
    r = response(b'forbidden')
    r.status_code = 403
    with patch('scripts.web_analyzer.validate_public_url'), patch.object(analyzer, '_check_robots_txt', return_value=True), patch('scripts.web_analyzer.request_with_retry', return_value=r):
        report = analyzer.analyze()
    assert report.difficulty == 'blocked'


def test_redirect_robots_denial_stops_second_target_request(tmp_path):
    analyzer = WebAnalyzer('https://example.com/data.csv', str(tmp_path))
    r = MagicMock(status_code=302, headers={'Location':'https://second.com/data.csv'})
    with patch('scripts.web_analyzer.validate_public_url'), \
         patch('scripts.web_analyzer.WebAnalyzer._check_robots_txt', side_effect=[True,False]), \
         patch('scripts.web_analyzer.request_with_retry', return_value=r) as request:
        report = analyzer.analyze()
    assert report.difficulty == 'blocked' and not report.robots_allowed
    assert request.call_count == 1
