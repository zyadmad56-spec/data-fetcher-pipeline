"""Small terminal interview; agents follow the same contract in SKILL.md.

The adaptive core lives in :class:`InterviewContext`, :func:`questions_for` and
:func:`apply_answers` (frozen contract in execution/CONTRACTS.md): a vague
request gets one round of 3-5 short material questions, an already-complete
request gets zero redundant questions, and each purpose reveals only its own
consequential follow-up (training -> target, forecasting -> frequency,
join -> join_key).  The terminal interview drives these functions; it does
not duplicate the logic.
"""
from dataclasses import dataclass, field, fields, replace
from typing import Optional

from scripts.core.models import FetchRequest
from pathlib import Path

from scripts.errors import DataFetchError

_SUPPORTED_OUTPUT_FORMATS = ('csv', 'json', 'excel', 'xlsx', 'parquet', 'sqlite', 'db')


@dataclass
class InterviewContext:
    """Typed requirements state; ``None`` means the field is unanswered.

    ``required_fields=()`` means explicitly all fields and ``row_limit=0``
    means explicitly all rows — both are answers, not missing information.
    """
    purpose: str = ''
    source: str = ''
    selector: str = ''
    country: Optional[str] = None
    date_range: Optional[str] = None
    required_fields: Optional[tuple[str, ...]] = None
    row_limit: Optional[int] = None
    output_format: Optional[str] = None
    target: Optional[str] = None
    frequency: Optional[str] = None
    join_key: Optional[str] = None


@dataclass
class InterviewQuestion:
    """One material question; ``key`` names the unresolved context field."""
    key: str
    prompt: str
    choices: tuple[str, ...] = ()
    recommendation: str = ''


def _purpose_followup_key(context: InterviewContext) -> Optional[str]:
    """The one consequential field the declared purpose still needs."""
    if context.purpose == 'training' and context.target is None:
        return 'target'
    if context.purpose == 'forecasting' and context.frequency is None:
        return 'frequency'
    if context.purpose == 'join' and context.join_key is None:
        return 'join_key'
    return None


def questions_for(context: InterviewContext) -> list[InterviewQuestion]:
    """Only the currently answerable material questions, in one short round.

    Purpose-specific follow-ups stay withheld while the purpose itself is
    unknown, so a vague request never exceeds five questions in its first
    round and a complete request gets none.
    """
    questions: list[InterviewQuestion] = []
    if not context.purpose:
        questions.append(InterviewQuestion(
            'purpose',
            'What is the data for? (training / forecasting / join / dashboard / other)',
            choices=('training', 'forecasting', 'join', 'dashboard', 'other'),
        ))
    if context.country is None or context.date_range is None:
        questions.append(InterviewQuestion(
            'scope',
            "Scope: country/region and date range (e.g. 'Egypt 2020-2025', or 'all')?",
        ))
    if context.required_fields is None:
        questions.append(InterviewQuestion(
            'required_fields',
            'Required columns, comma separated (Enter for all fields)?',
        ))
    if context.row_limit is None:
        questions.append(InterviewQuestion(
            'row_limit',
            'How many rows? (a number, or all)',
        ))
    if context.output_format is None:
        questions.append(InterviewQuestion(
            'output_format',
            'Output format? (csv / json / excel / parquet / sqlite)',
            choices=('csv', 'json', 'excel', 'parquet', 'sqlite'),
            recommendation='csv',
        ))
    followup = _purpose_followup_key(context)
    if followup == 'target':
        questions.append(InterviewQuestion(
            'target', 'Which column is the prediction target (label)?'))
    elif followup == 'frequency':
        questions.append(InterviewQuestion(
            'frequency', 'What observation frequency should the series have?'))
    elif followup == 'join_key':
        questions.append(InterviewQuestion(
            'join_key', 'Which shared column should the datasets join on?'))
    return questions


def _validated_row_limit(value) -> int:
    if isinstance(value, bool):
        raise DataFetchError('Row limit must be a non-negative integer, not a boolean.',
                             code='ARG_INVALID')
    if isinstance(value, int):
        rows = value
    elif isinstance(value, str):
        text = value.strip().lower()
        if text == 'all':
            return 0
        if not text.isdigit():
            raise DataFetchError(f'Row limit {value!r} is not a row count.', code='ARG_INVALID')
        rows = int(text)
    else:
        raise DataFetchError(f'Row limit {value!r} is not a row count.', code='ARG_INVALID')
    if rows < 0:
        raise DataFetchError('Row limit must be zero (all rows) or a positive count.',
                             code='ARG_INVALID')
    return rows


def _parse_scope(text: str) -> dict:
    """Deterministic CLI convenience split of one scope line into country/date.

    Agents combine scope naturally in conversation; the terminal needs one
    deterministic parse: a year or year-range becomes ``date_range`` and the
    remaining words become ``country``.  'all' (or empty) is an explicit
    whole-scope answer stored as empty strings, not as unanswered None.
    """
    import re
    value = text.strip()
    if not value or value.lower() in ('all', 'any', '*'):
        return {'country': '', 'date_range': ''}
    match = re.search(r'\d{4}(?:\s*[-–/]\s*\d{4})?', value)
    if match:
        return {'country': (value[:match.start()] + ' ' + value[match.end():]).strip(),
                'date_range': match.group(0).replace(' ', '')}
    return {'country': value, 'date_range': ''}


def apply_answers(context: InterviewContext, answers: dict) -> InterviewContext:
    """Return a validated updated context; the input context is not mutated.

    Invalid row limits and unsupported output formats fail with coded
    ``DataFetchError``; unknown keys are ignored for forward compatibility.
    """
    updates: dict = {}
    for key, value in dict(answers or {}).items():
        if key not in {f.name for f in fields(InterviewContext)} and key != 'scope':
            continue
        if key == 'row_limit':
            updates[key] = _validated_row_limit(value)
        elif key == 'output_format':
            fmt = str(value or '').strip().lower()
            if fmt not in _SUPPORTED_OUTPUT_FORMATS:
                raise DataFetchError(
                    f'Unsupported output format {value!r}; choose one of '
                    'csv/json/excel/parquet/sqlite.', code='ARG_INVALID')
            updates[key] = fmt
        elif key == 'required_fields':
            if value is None:
                continue
            if isinstance(value, str):
                value = [part.strip() for part in value.split(',') if part.strip()]
            updates[key] = tuple(value)
        elif key == 'scope':
            if isinstance(value, str):
                updates.update(_parse_scope(value))
        elif key in ('purpose', 'source', 'selector', 'country', 'date_range',
                     'target', 'frequency', 'join_key'):
            if value is None:
                continue
            updates[key] = str(value)
    return replace(context, **updates)


def _ask_question(question: InterviewQuestion, label: str = '') -> str:
    prefix = f"{label}. " if label else '- '
    suffix = f" [{'/'.join(question.choices)}]" if question.choices else ''
    hint = f" (suggested: {question.recommendation})" if question.recommendation else ''
    return input(f"{prefix}{question.prompt}{suffix}{hint}: ").strip()


def _required(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a value.")


def choose_source() -> str:
    from scripts.factory import list_sources, source_info
    sources, info = list_sources(), source_info()
    for i, key in enumerate(sources, 1):
        print(f"  {i}. {info[key]['platform']} ({key})")
    while True:
        choice = input("Choose a source number or name: ").strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(sources):
            return sources[int(choice) - 1]
        aliases = {v['platform'].lower(): k for k, v in info.items()}
        aliases['kaggel'] = 'kaggle'
        choice = aliases.get(choice, choice)
        if choice in sources:
            return choice
        print("Choose one of the listed sources.")


def relevance_warning(source: str, goal: str) -> str:
    """Advisory only: a domain can overlap another (e.g. aircraft company finances)."""
    domains = {
        'airbnb': 'short-term rental listings and reviews, not general aviation or electronics',
        'sec': 'public company financial filings, not general topic datasets',
        'yahoo': 'market prices and company tickers', 'yfinance': 'market prices and company tickers',
        'coingecko': 'cryptocurrency market prices', 'fred': 'economic time series',
        'worldbank': 'country development indicators', 'eurostat': 'European statistics',
    }
    unrelated = ('aircraft', 'airplane', 'aviation', 'movie', 'anime', 'electronics',
                 'طيارات', 'طائرات', 'افلام', 'أفلام', 'الكترونيات', 'إلكترونيات')
    financial = ('financial', 'finance', 'stock', 'revenue', 'econom', 'مالي', 'أسهم', 'ايراد', 'إيراد')
    text = goal.lower()
    if source in domains and any(w in text for w in unrelated) and not any(w in text for w in financial):
        return f"This source covers {domains[source]}. We must inspect the sample for your goal before saving."
    return ''


def interview(outdir: str | None = None) -> FetchRequest:
    print("Data Fetcher Pipeline — choose where to get your data.")
    while True:
        route = input("1. Ready/saved sources  2. External platform — choose 1 or 2: ").strip().lower()
        if route in ('1', '2', 'ready', 'external'):
            break
        print("Choose 1 or 2.")
    source = 'custom' if route in ('2', 'external') else choose_source()
    url = ''
    if source == 'custom':
        # A platform name alone needs human/agent discovery; the CLI does not invent a URL.
        url = _required("Platform name or full data/page URL: ")
        if not url.lower().startswith(('https://', 'http://')):
            print(f"Platform: {url}. Enter its public dataset or table page URL.")
            url = _required("Full URL: ")
    goal = _required("What data do you need? Include any country/date range and purpose: ")
    while True:
        warning = relevance_warning(source, goal)
        if not warning:
            break
        print("[Source scope] " + warning)
        choice = input("Inspect a sample here, choose another source, or cancel? (inspect/switch/cancel): ").strip().lower()
        if choice == 'inspect':
            break
        if choice == 'cancel':
            raise DataFetchError("Source selection cancelled.", code='ABORTED')
        if choice == 'switch':
            source = choose_source()
    if source == 'custom':
        query = url
        print("Using the supplied URL; HTML tables are selected by required columns.")
    else:
        from scripts.factory import source_info
        info = source_info()[source]
        query = input(f"Dataset selector ({info['query_format']}; e.g. {info['example']}): ").strip()
        if not query and info['query_format'] in ('free text', 'name fragment or numeric dataset ID'):
            query = goal
        elif not query and info.get('saved'):
            query = info['example']
        while not query:
            query = _required("Enter the selector required by this platform (a topic is not an ID): ")
    # The adaptive engine drives the terminal: one short round of the
    # currently unresolved material questions, then purpose-specific
    # follow-ups in the next round. Already-answered facts (the selector, the
    # declared source) are never re-asked; the goal text stays context.
    context = InterviewContext(source=source, selector=query)
    for _round in range(3):
        pending = questions_for(context)
        if not pending:
            break
        total = len(pending)
        for index, question in enumerate(pending, 1):
            while True:
                answer = _ask_question(question, label=f"{index}/{total}")
                try:
                    context = apply_answers(context, {question.key: answer})
                    break
                except DataFetchError as exc:
                    print(str(exc))
    print("We will show actual rows before final approval. The sample may require a full transfer; you will be told first.")
    return FetchRequest(source, query, outdir or str(Path.cwd() / 'data_raw'), goal,
                        list(context.required_fields or ()), context.row_limit or 0,
                        context.output_format or 'csv', context=context)
