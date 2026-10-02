"""Current terminal wording and explicit prompt-free behavior."""
import logging

from scripts.core.errors import DataFetchError

logger = logging.getLogger('scripts.base')


class TerminalInteraction:
    def report(self, message: str) -> None:
        print(message)

    def display_preview(self, frame) -> None:
        print('\n DATASET PREVIEW (first 5 rows; first 30 columns displayed)')
        print(frame.iloc[:, :30].to_string(index=False))

    def consent_to_transfer(self) -> None:
        answer = input('Preview needs a full transfer before final save approval. Allow this transfer? (y/n): ').strip().lower()
        if answer not in ('y', 'yes'):
            raise DataFetchError('Preview transfer declined; nothing saved.', code='ABORTED')

    def approve_sample(self, metadata: dict, row_limit: int) -> int:
        print('\n' + '=' * 50)
        print(' SAMPLE REVIEW AND FINAL SAVE APPROVAL')
        print('=' * 50)
        print(f"Target URL : {metadata.get('url', 'Unknown')}")
        print(f"Data Size  : {metadata.get('size_info', 'Unknown (Determined at runtime)')}")
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

    def approve_sample(self, metadata: dict, row_limit: int) -> int:
        return 0
