"""Exercise application contracts without concrete providers, prompts or disk effects."""
import ast
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from scripts.application.contracts import FetchPorts
from scripts.application.fetch import FetchDataset
from scripts.application.complete import complete_dataset
from scripts.application.retention import RetentionRequest, retain_source
from scripts.core.models import FetchState
from scripts.core.errors import DataFetchError


class MemoryProvider:
    def __init__(self, state, events, population=None):
        self.state, self.events, self.known_population = state, events, population

    def scout(self):
        self.events.append('scout')
        return {'url': 'memory://records'}

    def preview_transfer_scope(self):
        return 'memory sample'

    def needs_full_transfer(self):
        return True

    def preview(self):
        self.events.append('preview')
        self.state._cached_df = self.extract()
        self.state.preview_frame = self.state._cached_df.head(5)

    def extract(self):
        self.events.append('extract')
        return pd.DataFrame({'value': range(10), 'label': ['actual record'] * 10})

    def population(self):
        return self.known_population

    def release_payload(self):
        self.events.append('release')


class MemoryInteraction:
    def __init__(self, state, events, answer=0):
        self.state, self.events, self.answer = state, events, answer

    def report(self, message):
        pass

    def consent_to_transfer(self):
        self.events.append('transfer consent')

    def approve_sample(self, metadata, row_limit):
        assert self.state.preview_frame.label.iloc[0] == 'actual record'
        self.events.append('final approval')
        return self.answer

    def show_validation(self, null_ratio):
        self.events.append('validate')


class MemoryArtifacts:
    def __init__(self, state, events):
        self.state, self.events, self.frames = state, events, []

    def save(self, frame, filename):
        self.events.append('save')
        self.frames.append(frame.copy())
        return self.state.saved_artifacts('memory://' + filename)


@pytest.mark.parametrize('answer,expected', [(0, 2), (7, 7), (-1, 10), (1, 1)])
def test_ordered_use_case_in_memory_with_changed_final_count(answer, expected):
    state = FetchState(query='records', provider_name='Memory', row_limit=2)
    events = []
    provider = MemoryProvider(state, events)
    artifacts = MemoryArtifacts(state, events)
    use_case = FetchDataset(state, FetchPorts(provider, MemoryInteraction(state, events, answer), artifacts))
    with patch('builtins.input', side_effect=AssertionError('terminal dependency')), \
            patch('builtins.open', side_effect=AssertionError('filesystem dependency')):
        saved = use_case.run()
    assert saved.csv_path == 'memory://records_raw.csv'
    assert len(artifacts.frames[0]) == expected
    assert events == ['scout', 'transfer consent', 'preview', 'extract', 'final approval', 'validate', 'save', 'release']


def test_preview_only_never_approves_or_saves():
    state = FetchState(query='records', preview_only=True, auto_approve=True)
    events = []
    artifacts = MemoryArtifacts(state, events)
    assert FetchDataset(state, FetchPorts(MemoryProvider(state, events), MemoryInteraction(state, events), artifacts)).run() is None
    assert events == ['scout', 'preview', 'extract', 'release']
    assert artifacts.frames == []


def test_missing_actual_sample_releases_resources_without_approval():
    state = FetchState(query='records')
    events = []
    provider = MemoryProvider(state, events)
    provider.preview = lambda: events.append('empty preview')
    with pytest.raises(DataFetchError) as caught:
        FetchDataset(state, FetchPorts(provider, MemoryInteraction(state, events), MemoryArtifacts(state, events))).run()
    assert caught.value.code == 'NOT_FOUND'
    assert events == ['scout', 'transfer consent', 'empty preview', 'release']


@pytest.mark.parametrize('population,truncation', [(10, None), (20, {'rows':10, 'population':20, 'by':'user'}), (None, {'rows':10, 'population':None, 'by':'user'})])
def test_exhausted_population_is_distinct_from_parser_cut(population, truncation):
    state = FetchState(query='records', row_limit=10, auto_approve=True)
    events = []
    FetchDataset(state, FetchPorts(MemoryProvider(state, events, population), MemoryInteraction(state, events), MemoryArtifacts(state, events))).run()
    assert state.truncation == truncation


@pytest.mark.parametrize('frame', [pd.DataFrame(), pd.DataFrame({'other':[1]})])
def test_invalid_payload_never_reaches_storage(frame):
    state = FetchState(query='records', required_columns=['value'], auto_approve=True)
    events = []
    provider = MemoryProvider(state, events)
    provider.extract = lambda: frame
    with pytest.raises(ValueError):
        FetchDataset(state, FetchPorts(provider, MemoryInteraction(state, events), MemoryArtifacts(state, events))).run()
    assert 'save' not in events and events[-1] == 'release'


def test_artifact_store_runs_without_fetcher_or_provider(tmp_path):
    from scripts.adapters.artifacts import FileArtifactStore
    state = FetchState(query='records', provider_name='MemoryFetcher', source_key='memory', outdir=str(tmp_path))
    payload = tmp_path / 'payload.tmp'
    original = b'value,label\r\n1,first\r\n2,second\r\n'
    payload.write_bytes(original)
    state._temp_payload = payload
    writer = FileArtifactStore(state)
    first = pd.DataFrame({'value':[1], 'label':['first']})
    csv_path = writer.save_csv(first, 'records_raw.csv')
    saved = state.saved_artifacts(csv_path)
    assert Path(saved.original_path).read_bytes() == original
    assert saved.original_sha256 == hashlib.sha256(original).hexdigest()
    assert saved.sha256 == hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()
    writer.save_csv(pd.DataFrame({'value':[2], 'label':['second']}), 'records_raw.csv')
    assert pd.read_csv(csv_path + '.prev').value.tolist() == [1]
    assert pd.read_csv(csv_path).value.tolist() == [2]
    assert len(Path(saved.manifest_path).read_text().splitlines()) == 2


def test_conversion_failure_prevents_retention():
    calls = []
    def failing_conversion(*args):
        calls.append('convert')
        raise OSError('locked converted output')
    with pytest.raises(OSError):
        complete_dataset('memory://csv', 'excel', failing_conversion, lambda: calls.append('retain'))
    assert calls == ['convert']


@pytest.mark.parametrize('name,deferred,expected', [('Portal', True, ('saved', None)), (None, True, (None, 'pending')), (None, False, (None, None))])
def test_retention_policy_has_no_script_or_registry_effects(name, deferred, expected):
    class MemoryRegistry:
        def save(self, name, url, script):
            return 'saved'
        def defer(self, url, script):
            return 'pending'
    with patch('builtins.open', side_effect=AssertionError('filesystem dependency')):
        assert retain_source(RetentionRequest(name, 'memory://source', object(), deferred), MemoryRegistry()) == expected


def test_core_and_application_import_direction_and_effects():
    root = Path(__file__).resolve().parents[1] / 'scripts'
    allowed = {'dataclasses', 'typing', 're', 'pandas'}
    for package in ('core', 'application'):
        for path in (root / package).glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 0, path
                    imported = [node.module]
                else:
                    imported = []
                for module in imported:
                    assert module in allowed or module.startswith(('scripts.core.', 'scripts.application.')), (path, module)
                    if package == 'core':
                        assert module != 'pandas' and not module.startswith('scripts.application.'), (path, module)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in {'input', 'print', 'open', 'Path'}, (path, node.func.id)
