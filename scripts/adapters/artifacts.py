"""Atomic dataset and provenance persistence, independent of downloads and prompts."""
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.core.models import FetchState
from scripts.core.names import sanitize_name
from scripts.core.errors import DataFetchError


class FileArtifactStore:
    def __init__(self, state: FetchState):
        self.state = state

    def generate_markdown_profile(self, df: pd.DataFrame, csv_filepath: str) -> None:
        from scripts.dataset_profile import ProfileContext, render_profile
        csv_path = Path(csv_filepath)
        dataset_name = csv_path.stem.replace('_raw', '')
        md_filepath = csv_path.parent / f'{dataset_name}_description.md'
        self.state.last_description_path = ''
        context = ProfileContext(
            query=self.state.query, source_name=self.state.provider_name.replace('Fetcher', ''),
            dataset_url=self.state.dataset_url, requested_goal=self.state.requested_goal,
            truncation=self.state.truncation, max_columns=self.state.MAX_PROFILE_COLUMNS,
        )
        content = render_profile(df, csv_path, context)
        self._write_markdown_profile(md_filepath, content)

    def _write_markdown_profile(self, md_filepath: Path, content: str) -> None:
        try:
            md_tmp = md_filepath.with_name(md_filepath.name + '.tmp')
            with open(md_tmp, 'w', encoding='utf-8') as stream:
                stream.write(content)
            os.replace(md_tmp, md_filepath)
            self.state.last_description_path = str(md_filepath)
            print(f'[Success] Automated Dataset Markdown Profile generated at {md_filepath}')
        except OSError as exc:
            warning = f'Failed to generate markdown profile: {exc}'
            self.state.artifact_warnings.append(warning)
            print(f'[Warning] {warning}')

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _retain_original_payload(self) -> None:
        """Keep downloaded bytes so parsing cannot erase their original form."""
        payload = self.state._temp_payload
        if payload is None or not payload.is_file():
            return
        digest = self._sha256_file(payload)
        suffix = '.csv.gz' if self.state.payload_compression == 'gzip' else '.tsv' if self.state.payload_sep == '\t' else '.csv'
        target = Path(self.state.outdir) / f'{sanitize_name(self.state.query)}_source_{digest}{suffix}'
        if target.exists():
            if self._sha256_file(target) != digest:
                raise DataFetchError(f'Existing source payload has an invalid hash: {target}', code='INVALID_OUTPUT')
            payload.unlink()
        else:
            os.replace(payload, target)
        self.state.last_original_path = str(target)
        self.state.last_original_sha256 = digest
        self.state.last_original_bytes = target.stat().st_size

    def _append_manifest(self, csv_path: Path) -> None:
        """Atomic replacement prevents torn final lines; repair legacy torn entries."""
        entry = self._manifest_entry()
        manifest = csv_path.parent / 'manifest.jsonl'
        self.state.last_manifest_path = ''
        try:
            lines = self._existing_manifest_lines(manifest)
            lines.append(json.dumps(entry))
            tmp_manifest = manifest.with_name(manifest.name + '.tmp')
            with open(tmp_manifest, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_manifest, manifest)
            self.state.last_manifest_path = str(manifest)
        except OSError as e:
            warning = f'Failed to update manifest: {e}'
            self.state.artifact_warnings.append(warning)
            print(f'[Warning] {warning}', file=sys.stderr)

    def _reconcile_incomplete_commit(self, filepath: Path, tmp_path: Path, prev_path: Path) -> None:
        """Restore stranded files before beginning another transaction."""
        if filepath.exists():
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            return
        if tmp_path.exists():
            os.replace(tmp_path, filepath)
            print(f'[Recovery] Canonical file was missing — promoted stranded temp from a previous crash.', file=sys.stderr)
        elif prev_path.exists():
            shutil.copyfile(prev_path, filepath)
            print(f'[Recovery] Canonical file was missing — restored from .prev.', file=sys.stderr)

    def save_csv(self, df: pd.DataFrame, filename: str) -> str:
        self.state.rows_count = len(df)
        self.state.cols_count = len(df.columns)
        filepath = Path(self.state.outdir) / filename
        tmp_path = filepath.with_name(filepath.name + '.tmp')
        prev_path = filepath.with_name(filepath.name + '.prev')
        self._reconcile_incomplete_commit(filepath, tmp_path, prev_path)
        self._retain_original_payload()
        self._write_csv(df, tmp_path)
        self.state.last_sha256 = self._sha256_file(tmp_path)
        self._retain_previous(filepath, prev_path)
        self._promote_csv(tmp_path, filepath)
        self.state.last_bytes = filepath.stat().st_size
        print(f'[Success] Raw dataset saved atomically to {filepath}')
        self.generate_markdown_profile(df, str(filepath))
        self._append_manifest(filepath)
        return str(filepath)

    def _write_csv(self, frame: pd.DataFrame, tmp_path: Path) -> None:
        try:
            frame.to_csv(tmp_path, index=False)
            # Windows fsync requires a writable descriptor.
            with open(tmp_path, 'ab') as stream:
                os.fsync(stream.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _retain_previous(self, filepath: Path, prev_path: Path) -> None:
        if not filepath.exists():
            return
        prev_tmp = prev_path.with_name(prev_path.name + '.tmp')
        shutil.copyfile(filepath, prev_tmp)
        os.replace(prev_tmp, prev_path)
        print(f'[Retained] Prior version kept as {prev_path.name}', file=sys.stderr)

    def _promote_csv(self, tmp_path: Path, filepath: Path) -> None:
        try:
            os.replace(tmp_path, filepath)
        except PermissionError:
            self._retry_locked_commit(tmp_path, filepath)

    def _retry_locked_commit(self, tmp_path: Path, filepath: Path) -> None:
        for delay in (0.5, 1.0, 2.0):
            time.sleep(delay)
            try:
                os.replace(tmp_path, filepath)
                return
            except PermissionError:
                continue
        raise DataFetchError(
            f'Output file is locked by another process: {filepath}. '
            f'The complete new dataset is preserved at {tmp_path} — close the '
            'file and retry, or move it manually.', code='OUTPUT_LOCKED')

    def _manifest_entry(self) -> dict:
        return {
            'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'source': self.state.source_key,
            'query': self.state.query,
            'url': self.state.dataset_url,
            'rows': self.state.rows_count,
            'cols': self.state.cols_count,
            'bytes': self.state.last_bytes,
            'sha256': self.state.last_sha256,
            'complete': self.state.complete,
            'warnings': list(self.state.completeness_warnings),
            'truncation': self.state.truncation,
            'original_path': self.state.last_original_path or None,
            'original_sha256': self.state.last_original_sha256 or None,
            'original_bytes': self.state.last_original_bytes or None,
        }

    def _existing_manifest_lines(self, manifest: Path) -> list[str]:
        lines = []
        if not manifest.exists():
            return lines
        for line in manifest.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                print(f'[Recovery] Dropped torn manifest line: {line[:50]}...', file=sys.stderr)
                continue
            lines.append(line)
        return lines
