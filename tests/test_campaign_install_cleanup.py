"""Project discovery and preview cleanup remain safe outside the checkout."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd


REPO = Path(__file__).resolve().parents[1]


def cli_process(arguments, home, cwd):
    env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home),
           'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-B', '-S', str(REPO/'scripts'/'cli.py'),
                            *arguments], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=20)


def test_source_discovery_works_without_data_stack_or_output_directories(tmp_path):
    result = cli_process(['--list-sources', '--json-output'], tmp_path/'isolated-home', tmp_path)
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    keys = [source['key'] for source in envelope['sources']]
    assert len(keys) == len(set(keys)) and 'airbnb' in keys
    assert {'yahoo', 'yfinance', 'kaggle', 'rdatasets'}.issubset(keys)
    assert not list(tmp_path.iterdir())


def test_discard_cli_needs_no_source_credentials_or_data_stack(tmp_path):
    module = importlib.import_module('scripts.adapters.preview_store')
    root = tmp_path/'task'
    store = module.PreviewStore(root/'.previews')
    identity = module.PreviewIdentity(source='kaggle', query='uciml/iris')
    record = store.create(identity, pd.DataFrame({'id': ['001']}))
    sibling = tmp_path/'user.csv'
    sibling.write_bytes(b'user-data\n')
    result = cli_process(['--discard-preview', record.preview_id, '--outdir', str(root),
                           '--json-output'], tmp_path/'unused-home', tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'success'
    assert sibling.read_bytes() == b'user-data\n'
    assert not Path(record.preview_path).exists()


def test_discard_cli_rejects_mixed_fetch_without_deleting_preview(tmp_path):
    module = importlib.import_module('scripts.adapters.preview_store')
    root = tmp_path/'task'
    store = module.PreviewStore(root/'.previews')
    identity = module.PreviewIdentity(source='kaggle', query='uciml/iris')
    record = store.create(identity, pd.DataFrame({'id': ['001']}))
    result = cli_process(['--discard-preview', record.preview_id, '--source', 'kaggle',
                           '--query', 'uciml/iris', '--outdir', str(root), '--json-output'],
                          tmp_path/'unused-home', tmp_path)
    assert result.returncode != 0
    assert Path(record.preview_path).exists()
