"""Public CLI regressions for resource and metadata failures and format reuse."""
import contextlib
import io
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import asdict

import pandas as pd
import pytest

from scripts import cli
from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.web_analyzer import AnalysisReport, WebAnalyzer


class RecordsFetcher(BaseFetcher):
    def scout(self):
        return {'url': 'https://example.com/records.csv'}

    def extract(self):
        return pd.DataFrame({'value': [1, 2, 3]})


def invoke_cli(fetcher, tmp_path):
    stdout, stderr = io.StringIO(), io.StringIO()
    arguments = ['data-fetcher', '--source', 'worldbank', '--query', 'records',
                 '--outdir', str(tmp_path), '--yes', '--json-output']
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
            patch.object(sys, 'argv', arguments), \
            patch.object(cli, 'setup_wizard', return_value={}), \
            patch.object(cli, 'get_fetcher', return_value=fetcher):
        expected_stdout = sys.stdout
        try:
            cli.main()
            exit_code = 0
        except SystemExit as exc:
            exit_code = exc.code
        finally:
            restored = sys.stdout is expected_stdout
            sys.stdout = expected_stdout
    assert restored, 'CLI must restore stdout even when cleanup fails'
    return json.loads(stdout.getvalue()), exit_code


@pytest.mark.parametrize('extraction_fails', [False, True])
def test_cleanup_failure_is_visible_without_masking_extraction(tmp_path, extraction_fails):
    fetcher = RecordsFetcher('records', str(tmp_path), {})
    fetcher.cleanup = MagicMock(side_effect=PermissionError('temporary extractor is locked'))
    if extraction_fails:
        fetcher.extract = MagicMock(side_effect=DataFetchError('dataset absent', code='NOT_FOUND'))
    envelope, exit_code = invoke_cli(fetcher, tmp_path)
    fetcher.cleanup.assert_called_once()
    assert any('locked' in warning for warning in envelope['warnings'])
    if extraction_fails:
        assert exit_code == 1 and envelope['code'] == 'NOT_FOUND'
    else:
        assert exit_code == 0 and envelope['status'] == 'success'
        assert envelope['complete'] is True
        assert pd.read_csv(envelope['output_path']).value.tolist() == [1, 2, 3]


@pytest.mark.parametrize('failed_artifacts', [
    ['description'], ['manifest'], ['description', 'manifest'],
])
def test_metadata_write_failure_reports_actual_artifacts(tmp_path, failed_artifacts):
    fetcher = RecordsFetcher('records', str(tmp_path), {})
    real_open = open

    def locked_metadata(path, *args, **kwargs):
        filename = str(path)
        if ('description' in failed_artifacts and filename.endswith('_description.md.tmp')
                or 'manifest' in failed_artifacts and filename.endswith('manifest.jsonl.tmp')):
            raise PermissionError('metadata file locked')
        return real_open(path, *args, **kwargs)

    with patch('builtins.open', side_effect=locked_metadata):
        envelope, exit_code = invoke_cli(fetcher, tmp_path)
    assert exit_code == 0 and envelope['status'] == 'success'
    assert envelope['complete'] is True  # data completeness is independent of metadata
    assert pd.read_csv(envelope['output_path']).value.tolist() == [1, 2, 3]
    assert len(envelope['warnings']) == len(failed_artifacts)
    for artifact in ('description', 'manifest'):
        path = envelope[artifact + '_path']
        assert not path if artifact in failed_artifacts else Path(path).is_file()


@pytest.mark.parametrize('difficulty,target,mime,final_url,body', [
    ('medium', 'https://example.com/data.csv', 'text/csv', None, b'model\nA320\n'),
    ('easy', 'https://example.com/table', 'text/html', None,
     b'<table><tr><th>model</th></tr><tr><td>A320</td></tr></table>'),
    ('medium', 'https://example.com/download', 'application/octet-stream',
     'https://example.com/data.csv', b'model\nA320\n'),
    ('medium', 'https://example.com/stale.html', 'text/csv', None, b'model\nA320\n'),
    ('easy', 'https://example.com/stale.csv', 'application/json', None, b'[{"model":"A320"}]'),
])
def test_reused_extractor_uses_current_response_format(tmp_path, difficulty, target, mime, final_url, body):
    analyzer = WebAnalyzer('https://example.com/original', str(tmp_path))
    script = analyzer.generate_script(AnalysisReport(url=analyzer.url, difficulty=difficulty))
    extract = runpy.run_path(script, run_name='regression_extractor')['extract_frame']
    response = MagicMock(status_code=200, headers={'Content-Type': mime}, url=final_url or target)
    response.iter_content.return_value = iter([body])
    extract.__globals__['_fetch_with_retry'] = lambda *args: response
    assert extract(url=target, columns=['model']).model.tolist() == ['A320']
    response.close.assert_called_once()


def test_human_analysis_preserves_report_json_with_warnings(tmp_path, capsys):
    report = AnalysisReport(url='https://example.com/data.csv', difficulty='easy',
                            warnings=['Check provider license before using the records.'])
    arguments = ['data-fetcher', '--source', 'custom', '--query', report.url,
                 '--outdir', str(tmp_path), '--analyze-only']
    with patch.object(sys, 'argv', arguments), patch.object(cli, 'setup_wizard', return_value={}), \
            patch.object(WebAnalyzer, 'analyze', return_value=report):
        cli.main()
    assert json.loads(capsys.readouterr().out) == asdict(report)
