"""Independent regressions for runtime gaps found by the synthetic CLI review."""
import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from scripts import cli, workflow
from scripts.adapters.preview_store import PreviewIdentity, PreviewStore
from scripts.core.models import FetchState


REPO = Path(__file__).resolve().parents[1]
CANARY = 'CANARY_ARBITRARY_FILESYSTEM_EXCEPTION_9B31'
DATA_URL = ('https://vincentarelbundock.github.io/Rdatasets/'
            'csv/datasets/iris.csv')
PAYLOAD = b'id,value\n' + b''.join(
    f'{number},{number * 10}\n'.encode() for number in range(1, 13))

# Each invocation has a new interpreter and real CLI/provider/store wiring.
# Only HTTP, DNS and a specifically requested external filesystem fault are
# replaced. No requests can escape to real dataset sites.
CLI_PROCESS = r'''
import builtins
import base64
import io
import json
import os
import pathlib
import socket
import sys
from unittest.mock import patch
import requests
from scripts import cli
from scripts.adapters.http_transport import HTTPRequest
from scripts.errors import DataFetchError

config = json.loads(sys.stdin.read())
requests_seen = []
real_open = builtins.open
real_replace = os.replace

def synthetic_response(url, *, prefix_bytes=None, max_body_bytes=None):
    requests_seen.append(url)
    if config['network'] == 'forbidden':
        raise AssertionError('A cached continuation attempted another transfer.')
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.encoding = 'utf-8'
    if url.endswith('/datasets.csv'):
        body = (b'Package,Item,Title,Rows,Cols,CSV,Doc\n'
                b'datasets,iris,Synthetic,12,2,'
                b'https://vincentarelbundock.github.io/Rdatasets/csv/datasets/iris.csv,'
                b'https://example.com/doc\n')
    elif url == config['data_url']:
        body = base64.b64decode(config['payload_base64'])
    else:
        raise AssertionError('Unexpected HTTP endpoint in synthetic-only test.')
    complete = prefix_bytes is None or len(body) < prefix_bytes
    if prefix_bytes is None and max_body_bytes is not None and len(body) > max_body_bytes:
        raise DataFetchError('Synthetic response exceeds the requested body limit.', code='SIZE_LIMIT')
    delivered = body if prefix_bytes is None else body[:prefix_bytes]
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Length'] = str(len(body))
    response.raw = io.BytesIO(delivered)
    response.body_complete = complete
    response.body_bytes = len(delivered)
    return response

class SyntheticHTTPTransport:
    """Explicit network-port fixture; the real supervisor has its own process tests."""
    def fetch(self, request):
        if not isinstance(request, HTTPRequest):
            raise TypeError('The HTTP boundary requires a real HTTPRequest.')
        return synthetic_response(request.url, prefix_bytes=request.prefix_bytes,
                                  max_body_bytes=request.max_body_bytes)

def fake_http(self, url, **kwargs):
    # Legacy catalog/header scouts still use the in-process requests port.
    return synthetic_response(url)

def forbid_real_request(*args, **kwargs):
    raise AssertionError('Unexpected real HTTP request escaped the synthetic boundary.')

def filesystem_open(path, *args, **kwargs):
    if config.get('profile_failure') and str(path).endswith('_description.md.tmp'):
        raise OSError(config['profile_failure'])
    return real_open(path, *args, **kwargs)

def filesystem_replace(source, destination, *args, **kwargs):
    if config.get('conversion_failure') and str(source).endswith('.json.tmp') and str(destination).endswith('.json'):
        raise OSError(config['conversion_failure'])
    return real_replace(source, destination, *args, **kwargs)

sys.argv = ['data-fetcher', *config['args']]
try:
    with patch('requests.sessions.Session.get', fake_http), \
            patch('requests.sessions.Session.request', forbid_real_request), \
            patch('scripts.adapters.http_transport.SupervisedHTTPTransport', SyntheticHTTPTransport), \
            patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]), \
            patch('builtins.open', filesystem_open), \
            patch('os.replace', filesystem_replace):
        cli.main()
finally:
    pathlib.Path(config['request_log']).write_text(json.dumps(requests_seen), encoding='utf-8')
'''


def run_cli(task, args, *, network='synthetic', profile_failure=None,
            payload=PAYLOAD, data_url=DATA_URL, conversion_failure=None):
    task.mkdir(parents=True, exist_ok=True)
    request_log = task / 'synthetic-http-log.json'
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1', USERPROFILE=str(task / 'home'),
               HOME=str(task / 'home'), TMP=str(task), TEMP=str(task), TMPDIR=str(task))
    config = dict(args=[*args, '--outdir', str(task), '--json-output'],
                  network=network, payload_base64=base64.b64encode(payload).decode(), data_url=data_url,
                  request_log=str(request_log), profile_failure=profile_failure,
                  conversion_failure=conversion_failure)
    result = subprocess.run(
        [sys.executable, '-B', '-c', CLI_PROCESS], input=json.dumps(config),
        text=True, capture_output=True, cwd=REPO, env=env, timeout=45)
    assert result.stdout.strip(), (result.returncode, result.stderr)
    envelope = json.loads(result.stdout)
    return result, envelope, json.loads(request_log.read_text(encoding='utf-8'))


def expected_frame(rows):
    return pd.DataFrame({'value': [number * 10 for number in range(1, rows + 1)]})


def assert_json_result(envelope, rows):
    assert envelope['status'] == 'success', envelope
    assert envelope['rows'] == rows
    path = Path(envelope['converted_file'])
    assert path.suffix == '.json'
    assert json.loads(path.read_text(encoding='utf-8')) == [
        {'value': number * 10} for number in range(1, rows + 1)]
    pd.testing.assert_frame_equal(pd.read_csv(envelope['output_path']), expected_frame(rows))


def test_builtin_preview_persists_identity_and_fresh_process_resumes_without_transfer(tmp_path):
    """Review incident: actual --preview-only produced HTML but no cache/id."""
    result, preview, transfers = run_cli(tmp_path, [
        '--source', 'rdatasets', '--query', 'datasets/iris', '--preview-only',
        '--yes', '--non-interactive', '--rows', '8', '--columns', 'value',
        '--output-format', 'json'])
    assert result.returncode == 0, preview
    assert preview['status'] == 'success' and preview['mode'] == 'preview'
    assert transfers.count(DATA_URL) == 1
    preview_id = preview.get('preview_id')
    assert preview_id, 'The real built-in preview must return a resumable cache identifier.'
    store = PreviewStore(tmp_path / '.previews')
    record = store.load(preview_id)
    assert record.identity == PreviewIdentity(
        'rdatasets', 'datasets/iris', ('value',), 8, 'json')
    assert record.complete is True
    assert Path(preview['preview_path']).is_file()
    assert preview['output_path'] is None
    resumed, saved, transfers = run_cli(
        tmp_path, ['--resume-preview', preview_id, '--yes', '--non-interactive'],
        network='forbidden')
    assert resumed.returncode == 0, saved
    assert transfers == [], 'Complete cached data must survive a fresh process without another transfer.'
    assert_json_result(saved, 8)


def test_complete_cached_resume_preserves_approved_rows_and_omitted_format(tmp_path):
    """A store-only setup exposes continuation bugs independently of creation."""
    record = PreviewStore(tmp_path / '.previews').create(
        PreviewIdentity('rdatasets', 'datasets/iris', ('value',), 3, 'json'),
        expected_frame(12), complete=True)
    result, saved, transfers = run_cli(
        tmp_path, ['--resume-preview', record.preview_id, '--yes', '--non-interactive'],
        network='forbidden')
    assert result.returncode == 0, saved
    assert transfers == []
    assert_json_result(saved, 3)


def test_bounded_cached_approval_fetches_full_request_without_manual_redo(tmp_path):
    identity = PreviewIdentity('rdatasets', 'datasets/iris', ('value',), 8, 'json')
    store = PreviewStore(tmp_path / '.previews')
    record = store.create(identity, expected_frame(3), complete=False)
    assert store.load(record.preview_id).identity == identity
    result, saved, transfers = run_cli(
        tmp_path, ['--resume-preview', record.preview_id, '--yes', '--non-interactive'])
    assert result.returncode == 0, saved
    assert DATA_URL in transfers, 'A bounded prefix cannot stand in for approved full data.'
    assert_json_result(saved, 8)


def test_explicit_resume_format_drift_is_rejected_without_consuming_approval(tmp_path):
    store = PreviewStore(tmp_path / '.previews')
    record = store.create(
        PreviewIdentity('rdatasets', 'datasets/iris', ('value',), 3, 'json'),
        expected_frame(3), complete=True)
    result, rejected, transfers = run_cli(tmp_path, [
        '--resume-preview', record.preview_id, '--output-format', 'parquet',
        '--yes', '--non-interactive'], network='forbidden')
    assert result.returncode != 0, rejected
    assert rejected['status'] == 'error'
    assert rejected['code'] in {'APPROVAL_MISMATCH', 'ARG_INVALID'}
    assert transfers == []
    assert not list(tmp_path.rglob('*_raw.*')), 'Rejected drift must not save a dataset.'
    corrected, saved, _ = run_cli(
        tmp_path, ['--resume-preview', record.preview_id, '--yes', '--non-interactive'],
        network='forbidden')
    assert corrected.returncode == 0, saved
    assert_json_result(saved, 3)


def test_artifact_write_error_cannot_promote_arbitrary_prose_to_automatic_channels(tmp_path):
    result, saved, _ = run_cli(tmp_path, [
        '--source', 'rdatasets', '--query', 'datasets/iris', '--yes', '--non-interactive'],
        profile_failure=CANARY)
    assert result.returncode == 0, saved
    assert saved['status'] == 'success'
    assert Path(saved['output_path']).is_file()
    assert saved['warnings'], 'Artifact failure must remain visible as a controlled advisory.'
    assert CANARY not in result.stdout + result.stderr


def test_automatic_source_reference_withholds_token_bearing_path(tmp_path):
    marker = 'CANARY_SECRET_PATH_TOKEN_43C2'
    state = FetchState(query='synthetic', outdir=str(tmp_path))
    state.dataset_url = f'https://user:password@example.com/download/{marker}?token=QUERY_SECRET#FRAGMENT_SECRET'
    envelope = cli._fetch_envelope_meta(state)
    source = envelope['source_url']
    assert source in {'https://example.com', 'https://example.com/'}
    assert all(secret not in json.dumps(envelope) for secret in (
        marker, 'password', 'QUERY_SECRET', 'FRAGMENT_SECRET'))


def test_terminal_training_interview_collects_target_without_repeating_answers(tmp_path, monkeypatch):
    """Review incident: adaptive questions existed but terminal never asked them."""
    answered = []

    def terminal_answer(prompt):
        text = prompt.lower()
        if 'choose 1 or 2' in text or ('ready' in text and 'external' in text):
            key, value = 'route', '1'
        elif 'source' in text and ('number' in text or 'name' in text):
            key, value = 'source', 'rdatasets'
        elif 'target' in text or 'label' in text:
            key, value = 'target', 'species'
        elif 'what data' in text and 'need' in text:
            key, value = 'goal', 'Prepare iris data for classifier training.'
        elif 'purpose' in text or 'data for' in text or 'training' in text:
            key, value = 'purpose', 'training'
        elif 'selector' in text or 'dataset id' in text:
            key, value = 'selector', 'datasets/iris'
        elif 'scope' in text or 'country' in text or 'region' in text or 'date range' in text:
            key, value = 'scope', 'all'
        elif 'column' in text or 'field' in text:
            key, value = 'required_fields', ''
        elif 'rows' in text:
            key, value = 'row_limit', '100'
        elif 'format' in text:
            key, value = 'output_format', 'json'
        else:
            pytest.fail(f'Unrecognized material input question: {prompt!r}')
        answered.append(key)
        assert len(answered) <= 15, 'The terminal interview did not converge.'
        return value

    monkeypatch.setattr('builtins.input', terminal_answer)
    request = workflow.interview(str(tmp_path))
    assert request.source == 'rdatasets' and request.query == 'datasets/iris'
    assert request.rows == 100 and request.output_format == 'json'
    assert 'target' in answered, 'Training requests must ask for the prediction target.'
    assert all(count == 1 for count in Counter(answered).values()), answered
    context = getattr(request, 'context', None)
    assert isinstance(context, workflow.InterviewContext), 'Collected adaptive requirements must survive the interview.'
    assert context.purpose == 'training' and context.target == 'species'
    assert context.required_fields == () and context.row_limit == 100
    assert workflow.questions_for(context) == [], 'Completed requirements must not trigger another round.'


@pytest.mark.parametrize('source,query,data_url,payload', [
    pytest.param(
        'airbnb',
        'https://data.insideairbnb.com/united-states/ny/new-york-city/2026-09-01/data/listings.csv.gz',
        'https://data.insideairbnb.com/united-states/ny/new-york-city/2026-09-01/data/listings.csv.gz',
        gzip.compress(PAYLOAD, mtime=0), id='official-airbnb-gzip'),
    pytest.param(
        'eurostat', 'synthetic_series',
        'https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/synthetic_series?format=TSV&compressed=false',
        PAYLOAD.replace(b',', b'\t'), id='eurostat-tsv'),
])
def test_complete_provider_cache_resumes_original_encoding_and_provenance(
        tmp_path, source, query, data_url, payload):
    """Review regression: source bytes were always restored as plain comma CSV."""
    result, preview, transfers = run_cli(tmp_path, [
        '--source', source, '--query', query, '--preview-only', '--yes',
        '--non-interactive', '--rows', '8', '--columns', 'value', '--output-format', 'json'],
        payload=payload, data_url=data_url)
    assert result.returncode == 0, preview
    assert data_url in transfers
    record = PreviewStore(tmp_path / '.previews').load(preview['preview_id'])
    assert record.complete is True, 'The synthetic response was received to EOF, beyond its 5-row sample.'
    resumed, saved, transfers = run_cli(
        tmp_path, ['--resume-preview', record.preview_id, '--yes', '--non-interactive'],
        network='forbidden')
    assert resumed.returncode == 0, saved
    assert transfers == [], 'Already complete compressed/TSV data must not be downloaded again.'
    assert_json_result(saved, 8)
    assert saved['source_url'] == data_url.split('/', 3)[0] + '//' + data_url.split('/')[2]
    original = Path(saved['original_path'])
    assert original.read_bytes() == payload
    assert saved['original_sha256'] == hashlib.sha256(payload).hexdigest()
    assert saved['original_bytes'] == len(payload)
    entries = [json.loads(line) for line in Path(saved['manifest_path']).read_text(encoding='utf-8').splitlines()]
    assert entries[-1]['url'] == data_url


def test_conversion_filesystem_failure_preserves_approval_for_corrected_retry(tmp_path):
    store = PreviewStore(tmp_path / '.previews')
    record = store.create(
        PreviewIdentity('rdatasets', 'datasets/iris', ('value',), 3, 'json'),
        expected_frame(3), complete=True)
    result, failed, transfers = run_cli(tmp_path, [
        '--resume-preview', record.preview_id, '--yes', '--non-interactive'],
        network='forbidden', conversion_failure=CANARY)
    assert result.returncode != 0 and failed['status'] == 'error', failed
    assert CANARY not in result.stdout + result.stderr
    assert transfers == []
    pd.testing.assert_frame_equal(store.load(record.preview_id).frame, expected_frame(3))
    corrected, saved, transfers = run_cli(tmp_path, [
        '--resume-preview', record.preview_id, '--yes', '--non-interactive'], network='forbidden')
    assert corrected.returncode == 0, 'A local conversion fault must not consume human approval: ' + str(saved)
    assert transfers == []
    assert_json_result(saved, 3)


def test_changed_human_preview_cannot_be_approved_through_cli(tmp_path):
    record = PreviewStore(tmp_path / '.previews').create(
        PreviewIdentity('rdatasets', 'datasets/iris', ('value',), 3, 'json'),
        expected_frame(3), complete=True)
    Path(record.preview_path).write_text('CANARY_CHANGED_HUMAN_APPROVAL_ARTIFACT', encoding='utf-8')
    result, rejected, transfers = run_cli(tmp_path, [
        '--resume-preview', record.preview_id, '--yes', '--non-interactive'], network='forbidden')
    assert result.returncode != 0 and rejected['status'] == 'error', rejected
    assert rejected['code'] == 'PREVIEW_TAMPERED'
    assert transfers == []
    assert not list(tmp_path.rglob('*_raw.*')), 'Changed review content must be rejected before dataset persistence.'


def test_actual_bounded_prefix_cache_is_incomplete_until_approved_full_fetch(tmp_path):
    payload = b'id,value,padding\n' + b''.join(
        f'{number},{number * 10},'.encode() + b'x' * 30000 + b'\n'
        for number in range(1, 13))
    result, preview, transfers = run_cli(tmp_path, [
        '--source', 'rdatasets', '--query', 'datasets/iris', '--preview-only',
        '--yes', '--non-interactive', '--rows', '8', '--columns', 'value', '--output-format', 'json'],
        payload=payload)
    assert result.returncode == 0, preview
    assert DATA_URL in transfers
    record = PreviewStore(tmp_path / '.previews').load(preview['preview_id'])
    assert record.complete is False, 'A 256 KB prefix of a longer response is not the complete dataset.'
    assert record.identity.row_limit == 8 and record.identity.columns == ('value',)
    resumed, saved, transfers = run_cli(tmp_path, [
        '--resume-preview', record.preview_id, '--yes', '--non-interactive'], payload=payload)
    assert resumed.returncode == 0, saved
    assert DATA_URL in transfers, 'Approval must fetch the rest of the requested data.'
    assert_json_result(saved, 8)
