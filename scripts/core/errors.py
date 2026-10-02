from typing import Optional

class DataFetchError(ValueError):
    """Domain error carrying a machine-readable code for the JSON envelope.

    Subclasses ValueError so existing `except ValueError` handlers keep working.
    `code` is a stable string an agent can branch on (AUTH_MISSING, RATE_LIMITED,
    NOT_FOUND, PROVIDER_UNAVAILABLE, ...); `exit_code` distinguishes process
    outcomes (1 = normal error, 3 = provider failed to load / no engine start).
    """

    def __init__(self, message: str, code: str = "INTERNAL", exit_code: int = 1,
                 hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.hint = hint
