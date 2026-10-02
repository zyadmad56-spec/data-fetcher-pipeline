"""Custom sources participate in the same preview/validate/save lifecycle."""
import runpy
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from scripts.base import BaseFetcher
from scripts.errors import DataFetchError
from scripts.source_registry import verified_script
from scripts.web_analyzer import WebAnalyzer


class CustomFetcher(BaseFetcher):
    def __init__(self, query, outdir, config, source='custom', descriptor=None):
        if descriptor:
            query = query or descriptor['url']
            if urlparse(query).hostname != urlparse(descriptor['url']).hostname:
                raise DataFetchError('Saved extractor can only be reused on its registered hostname; use custom for another platform.', code='BLOCKED_URL')
        super().__init__(query, outdir, config, source=source)
        self.descriptor = descriptor
        self.script_path = None
        self._workspace = None
        self.report = None
        self.transfer_scope = ('reviewed saved extractor; transfer cost depends on its implementation; result cached'
                               if descriptor else 'full custom payload, capped at 50 MB; result cached for final save')

    def scout(self):
        analyzer = WebAnalyzer(self.query, self.outdir)
        self.report = analyzer.analyze()
        print(f'[Custom assessment] {self.report.difficulty}; robots allowed={self.report.robots_allowed}; tables={self.report.table_count}')
        for warning in self.report.warnings:
            print('[Custom assessment] ' + warning)
        if self.report.status_code == 0:
            raise DataFetchError('Custom assessment could not reach the provider. See assessment warnings.', code='NETWORK')
        if self.report.status_code == 404:
            raise DataFetchError('Custom dataset URL was not found (HTTP 404).', code='NOT_FOUND')
        if self.report.status_code == 429:
            raise DataFetchError('Custom provider rate limit reached (HTTP 429).', code='RATE_LIMITED')
        if self.report.status_code >= 400:
            raise DataFetchError(f'Custom provider refused the request (HTTP {self.report.status_code}).', code='BLOCKED_URL' if self.report.status_code in (401,403) else 'PROVIDER_ERROR')
        if self.report.difficulty == 'blocked':
            raise DataFetchError('Target blocked by access policy or anti-bot protection. Use an authorized API/export.', code='BLOCKED_URL')
        if self.report.difficulty == 'hard' and not self.descriptor:
            raise DataFetchError('No supported export/table found; a reviewed platform-specific browser/API extractor is required.', code='UNSUPPORTED_SOURCE')
        if self.descriptor:
            self.script_path = verified_script(self.descriptor)
        else:
            self._workspace = tempfile.TemporaryDirectory(prefix='dfp-custom-')
            generator = WebAnalyzer(self.query, self._workspace.name)
            self.script_path = Path(generator.generate_script(self.report))
        self.resolved_title = self.descriptor['name'] if self.descriptor else urlparse(self.query).hostname
        return {'url': self.query, 'size_info': f'{self.report.content_length or "Unknown"} bytes; {self.transfer_scope}'}

    def extract(self):
        if not self.script_path:
            raise DataFetchError('Custom extractor was not prepared.', code='INTERNAL')
        module = runpy.run_path(str(self.script_path), run_name='dfp_generated_extractor')
        try:
            return module['extract_frame'](self.query, columns=self.required_columns,
                                           table_index=getattr(self, 'table_index', None), row_limit=self.row_limit)
        except ValueError as exc:
            text = str(exc)
            code = 'SIZE_LIMIT' if 'SIZE_LIMIT' in text else 'BLOCKED_URL' if text.lower().startswith('blocked') else 'NOT_FOUND' if 'NOT_FOUND' in text else 'PROVIDER_ERROR'
            raise DataFetchError(text, code=code) from exc

    def cleanup(self):
        if self._workspace:
            self._workspace.cleanup()
            self._workspace = None
