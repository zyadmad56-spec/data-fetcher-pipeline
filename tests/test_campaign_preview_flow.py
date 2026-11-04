"""Dataset request, preview, and approval workflow."""

from pathlib import Path
import importlib
import json
import os
import pandas as pd
import pytest
import subprocess
import sys
import time
from scripts.errors import DataFetchError


def preview_module():
    return importlib.import_module('scripts.adapters.preview_store')


def make_identity(**changes):
    fields = dict(source='kaggle', query='uciml/iris', columns=(),
                  row_limit=0, output_format='json')
    fields.update(changes)
    return preview_module().PreviewIdentity(**fields)


def test_chat_preview_cells_are_literal_and_do_not_echo_data(tmp_path, capsys):
    from scripts.adapters import sanitization

    payload = '<img src="https://attacker.example/pixel" onerror="steal()">'
    url = 'https://attacker.example/approve'
    frame = pd.DataFrame({'header': [payload, url, '=1+2', 'left|right']})
    original = frame.copy(deep=True)
    path = sanitization.write_human_preview(
        frame, 'https://user:secret@source.example/SECRET_PATH?token=SECRET_QUERY',
        'Synthetic preview', tmp_path, 'complete transfer', [])
    markdown = path.read_text(encoding='utf-8')
    assert path.suffix == '.md'
    for value in (payload, url, '=1+2', r'left\|right'):
        assert f'| `{value}` |' in markdown
    assert 'Source URL: `https://source.example`' in markdown
    assert all(secret not in markdown for secret in ('SECRET_PATH', 'SECRET_QUERY', 'user:secret'))
    captured = capsys.readouterr()
    assert payload not in captured.out + captured.err
    pd.testing.assert_frame_equal(frame, original)


def test_wide_chat_preview_is_bounded_without_changing_stored_frame(tmp_path):
    from scripts.adapters import sanitization

    frame = pd.DataFrame([[str(i) for i in range(4096)]],
                         columns=[f'FIELD_{i}' for i in range(4096)])
    path = sanitization.write_human_preview(
        frame, 'https://source.example/data.csv', 'Wide data', tmp_path,
        'complete transfer', [])
    markdown = path.read_text(encoding='utf-8')
    assert len(markdown.encode('utf-8')) < 65536
    assert 'FIELD_29' in markdown and 'FIELD_30' not in markdown
    assert '4096' in markdown and 'first 30' in markdown.lower()
    assert frame.shape == (1, 4096)


def test_complete_preview_survives_reopening_store_with_exact_values(tmp_path):
    module = preview_module()
    original = pd.DataFrame({'id': ['001', 'NA', ''],
                             'text': ['left\x00right', 'line\n2', 'normal']})
    first = module.PreviewStore(tmp_path).create(make_identity(), original)
    loaded = module.PreviewStore(tmp_path).load(first.preview_id)
    assert loaded.complete is True and loaded.identity == make_identity()
    pd.testing.assert_frame_equal(loaded.frame, original)
    assert Path(loaded.preview_path).is_file()


def test_preview_is_readable_from_a_separate_real_interpreter(tmp_path):
    module = preview_module()
    record = module.PreviewStore(tmp_path).create(
        make_identity(), pd.DataFrame({'a': ['001', 'NA']}))
    script = (
        'import json,sys\n'
        'from pathlib import Path\n'
        'from scripts.adapters.preview_store import PreviewStore\n'
        'r=PreviewStore(Path(sys.argv[1])).load(sys.argv[2])\n'
        'print(json.dumps({"rows":len(r.frame),"complete":r.complete}))\n'
    )
    result = subprocess.run([sys.executable, '-B', '-c', script,
                             str(tmp_path), record.preview_id],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'rows': 2, 'complete': True}


@pytest.mark.parametrize('change', [
    {'source': 'openml'}, {'query': 'other/dataset'}, {'columns': ('id',)},
    {'row_limit': 1}, {'output_format': 'excel'},
])
def test_changed_request_cannot_claim_existing_preview(change, tmp_path):
    store = preview_module().PreviewStore(tmp_path)
    record = store.create(make_identity(), pd.DataFrame({'id': ['001']}))
    with pytest.raises(DataFetchError) as caught:
        store.claim(record.preview_id, make_identity(**change))
    assert caught.value.code == 'APPROVAL_MISMATCH'


def test_second_claim_cannot_repeat_an_approved_transfer(tmp_path):
    store = preview_module().PreviewStore(tmp_path)
    identity = make_identity()
    record = store.create(identity, pd.DataFrame({'id': ['001']}))
    store.claim(record.preview_id, identity)
    with pytest.raises(DataFetchError) as caught:
        store.claim(record.preview_id, identity)
    assert caught.value.code in {'PREVIEW_CONSUMED', 'PREVIEW_BUSY'}


def test_prefix_cache_is_not_labeled_as_complete_dataset(tmp_path):
    store = preview_module().PreviewStore(tmp_path)
    record = store.create(make_identity(), pd.DataFrame({'id': ['001']}), complete=False)
    assert store.load(record.preview_id).complete is False


def test_expired_preview_cannot_be_claimed(tmp_path):
    store = preview_module().PreviewStore(tmp_path, ttl_seconds=0.01)
    record = store.create(make_identity(), pd.DataFrame({'id': ['001']}))
    time.sleep(0.03)
    with pytest.raises(DataFetchError) as caught:
        store.claim(record.preview_id, make_identity())
    assert caught.value.code == 'PREVIEW_EXPIRED'


def test_discard_is_idempotent_and_preserves_unrelated_files(tmp_path):
    store = preview_module().PreviewStore(tmp_path / '.previews')
    unrelated = tmp_path / 'user-data.csv'
    unrelated.write_bytes(b'user-owned\n001\n')
    record = store.create(make_identity(), pd.DataFrame({'id': ['001']}))
    assert store.discard(record.preview_id) is True
    assert store.discard(record.preview_id) is False
    assert unrelated.read_bytes() == b'user-owned\n001\n'
    assert not Path(record.preview_path).exists()


@pytest.mark.parametrize('forged', ['../user-data', '/etc/passwd', 'not-a-preview', 'a'*31])
def test_forged_preview_reference_is_rejected_without_opening(forged, tmp_path):
    store = preview_module().PreviewStore(tmp_path)
    with pytest.raises(DataFetchError):
        store.presentation_path(forged)


def test_changed_presentation_file_is_rejected_before_native_open(tmp_path):
    store = preview_module().PreviewStore(tmp_path)
    record = store.create(make_identity(), pd.DataFrame({'id': ['001']}))
    Path(record.preview_path).write_text('unrelated replacement', encoding='utf-8')
    with pytest.raises(DataFetchError) as caught:
        store.presentation_path(record.preview_id)
    assert caught.value.code in {'PREVIEW_TAMPERED', 'PREVIEW_INVALID'}


# Adaptive request interview


def context(**fields):
    return importlib.import_module('scripts.workflow').InterviewContext(**fields)


def questions(request):
    return importlib.import_module('scripts.workflow').questions_for(request)


def complete_context(**changes):
    fields = dict(purpose='dashboard', source='worldbank', selector='SP.POP.TOTL',
                  country='Egypt', date_range='2020-2025', required_fields=(),
                  row_limit=10, output_format='json')
    fields.update(changes)
    return context(**fields)


def test_vague_request_gets_one_short_round_of_material_questions():
    pending = questions(context())
    assert 3 <= len(pending) <= 5
    assert len({question.key for question in pending}) == len(pending)
    assert all(question.prompt.strip() for question in pending)
    assert 'purpose' in {question.key for question in pending}


def test_complete_request_does_not_repeat_known_requirements():
    assert not questions(complete_context())


@pytest.mark.parametrize(('purpose', 'missing'), [
    ('training', 'target'), ('forecasting', 'frequency'), ('join', 'join_key'),
])
def test_purpose_reveals_only_its_consequential_followup(purpose, missing):
    request = complete_context(purpose=purpose)
    keys = {question.key for question in questions(request)}
    assert missing in keys
    assert not keys.intersection({'purpose', 'country', 'date_range',
                                  'required_fields', 'row_limit', 'output_format'})


@pytest.mark.parametrize(('purpose', 'field', 'answer'), [
    ('training', 'target', 'sale_price'),
    ('forecasting', 'frequency', 'monthly'),
    ('join', 'join_key', 'country_code'),
])
def test_resolved_purpose_followup_does_not_create_an_endless_interview(
    purpose, field, answer
):
    request = complete_context(purpose=purpose, **{field: answer})
    assert not questions(request)


def test_answer_revision_preserves_already_resolved_source_and_format():
    workflow = importlib.import_module('scripts.workflow')
    before = complete_context()
    updated = workflow.apply_answers(before, {'country': 'Morocco', 'row_limit': 25})
    assert updated.country == 'Morocco' and updated.row_limit == 25
    assert updated.source == 'worldbank' and updated.selector == 'SP.POP.TOTL'
    assert updated.output_format == 'json'
    assert before.country == 'Egypt' and before.row_limit == 10


@pytest.mark.parametrize('invalid', [-1, True, 'negative'])
def test_invalid_row_answers_fail_instead_of_becoming_an_unbounded_fetch(invalid):
    workflow = importlib.import_module('scripts.workflow')
    with pytest.raises(DataFetchError) as caught:
        workflow.apply_answers(complete_context(), {'row_limit': invalid})
    assert caught.value.code in {'ARG_INVALID', 'ARG_MISSING'}


def test_explicit_all_fields_and_rows_are_answers_not_missing_information():
    assert not questions(complete_context(required_fields=(), row_limit=0))
