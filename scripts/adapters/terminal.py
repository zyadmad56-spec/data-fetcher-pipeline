"""Current terminal wording and explicit prompt-free behavior.

Presentation safety: every print that can carry externally fetched text
(query strings, URLs, transfer scopes, preview tables) is sanitized against
ANSI/CSI/OSC and BiDi injection before it reaches the terminal. The
non-interactive adapter never displays the preview table at all: in
``--json-output`` mode stdout is aliased to stderr (cli.py), so anything
printed here would leak raw dataset cells into the agent's error channel.
The human reads the sample from the terminal (interactive) or, once the CLI
wave wires it, from the private ``_human_preview.md`` artifact only.
"""
import logging

from scripts.adapters.sanitization import sanitize_display_text
from scripts.core.errors import DataFetchError

logger = logging.getLogger('scripts.base')

_MESSAGE_LIMIT = 2000    # chars per one-line status message
_PROMPT_FIELD_LIMIT = 300
_PREVIEW_LINE_LIMIT = 500  # chars per rendered table line


class TerminalInteraction:
    def report(self, message: str) -> None:
        print(sanitize_display_text(message, max_len=_MESSAGE_LIMIT))

    def display_preview(self, frame) -> None:
        print('\n DATASET PREVIEW (first 5 rows; first 30 columns displayed)')
        table = frame.iloc[:, :30].to_string(index=False)
        for line in table.splitlines():
            print(sanitize_display_text(line, max_len=_PREVIEW_LINE_LIMIT))

    def consent_to_transfer(self) -> None:
        answer = input('Preview needs a full transfer before final save approval. Allow this transfer? (y/n): ').strip().lower()
        if answer not in ('y', 'yes'):
            raise DataFetchError('Preview transfer declined; nothing saved.', code='ABORTED')

    def approve_sample(self, metadata: dict, row_limit: int) -> int:
        print('\n' + '=' * 50)
        print(' SAMPLE REVIEW AND FINAL SAVE APPROVAL')
        print('=' * 50)
        print(f"Target URL : {sanitize_display_text(metadata.get('url', 'Unknown'), max_len=_PROMPT_FIELD_LIMIT)}")
        print(f"Data Size  : {sanitize_display_text(metadata.get('size_info', 'Unknown (Determined at runtime)'), max_len=_PROMPT_FIELD_LIMIT)}")
        print('-' * 50)
        return self._approval_answer(row_limit)

    def _approval_answer(self, row_limit: int) -> int:
        while True:
            answer = input(f"Does the sample match your request? Save it (y/n), or enter a row count/all (current: {row_limit or 'all'}): ").strip().lower()
            if answer in ('y', 'yes'):
                return 0
            if answer in ('n', 'no'):
                raise DataFetchError('Extraction aborted by user during Pre-Flight Authorization.', code='ABORTED')
            if answer in ('all', '0'):
                return -1
            if answer.isdigit():
                return int(answer)
            print("[Error] Invalid input. Please type 'y', 'n', 'all', or a positive number.")

    def show_validation(self, null_ratio: float) -> None:
        if null_ratio > 0.8:
            logger.warning('High null density detected (%.2f%%). Dataset may be sparse by design.', null_ratio * 100)
            print(f'[Validator] Warning: Null density is {null_ratio:.2%}. Proceeding — dataset may be sparse by design (e.g. SEC XBRL, Eurostat pivot).')
        else:
            print('[Validator] Payload passed Null-Density Check.')

    def retention_name(self, default_name: str | None) -> str | None:
        answer = input('Data saved. Add this platform and extractor to ready sources? (y/n): ').strip().lower()
        if answer in ('y', 'yes'):
            return input('Platform name: ').strip() or default_name
        return None


class NonInteractiveInteraction(TerminalInteraction):
    def consent_to_transfer(self) -> None:
        # Explicit CLI flags choose this adapter; JSON output alone does not.
        return None

    def display_preview(self, frame) -> None:
        # No-op: in --json-output mode stdout is aliased to stderr, so the
        # inherited table print would leak raw sample cells and column names
        # into the agent-facing error channel. Non-interactive previews are
        # delivered to the human through the private preview artifact instead.
        return None

    def approve_sample(self, metadata: dict, row_limit: int) -> int:
        return 0
