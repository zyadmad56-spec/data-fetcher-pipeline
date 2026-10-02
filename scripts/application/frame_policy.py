"""Selection and truncation rules have no network or persistence dependencies."""
import pandas as pd

from scripts.core.errors import DataFetchError
from scripts.core.models import FetchState


def select_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if not columns:
        return frame
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise DataFetchError('Missing required columns: ' + ', '.join(missing)
                             + '. Choose another dataset/source.', code='NOT_FOUND')
    return frame.loc[:, columns]


def null_density(frame: pd.DataFrame) -> float:
    if frame.empty:
        raise ValueError('Graceful Fallback Triggered: Dataframe is completely empty.')
    return frame.isnull().sum().sum() / frame.size


def limit_rows(frame: pd.DataFrame, state: FetchState, population: int | None) -> pd.DataFrame:
    if state.row_limit > 0 and len(frame) > state.row_limit:
        state.truncation = {'rows': state.row_limit, 'population': len(frame), 'by': 'user'}
        return frame.head(state.row_limit)
    if state.row_limit > 0 and state.truncation is None and len(frame) == state.row_limit:
        if population is None or population > len(frame):
            state.truncation = {'rows': len(frame), 'population': population, 'by': 'user'}
    return frame
