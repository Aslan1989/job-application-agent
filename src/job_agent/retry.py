"""Bounded retry with exponential backoff and full jitter.

Used for third-party HTTP reads only. Never used around a Google Sheets write:
the values API has no compare-and-swap, so a blind write retry could duplicate
a row. Write failures are surfaced instead.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")

# Transient by nature: rate limiting, upstream outages, gateway hiccups.
RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)


class RetryBudgetExceeded(RuntimeError):
    """All attempts failed. Carries the final cause, never the request body."""


def _status_of(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def is_retryable(error: BaseException, *, retry_on: Sequence[type[BaseException]]) -> bool:
    if not isinstance(error, tuple(retry_on)):
        return False
    status = _status_of(error)
    # A plain connection/timeout error has no status and is worth one more try;
    # a 401/403/404 is a configuration problem that retrying cannot fix.
    return status is None or status in RETRYABLE_STATUS


def retry_call(operation: Callable[[], T], *, attempts: int = 3,
               base_delay: float = 1.0, max_delay: float = 20.0,
               retry_on: Sequence[type[BaseException]] = (Exception,),
               sleep: Callable[[float], None] = time.sleep,
               jitter: Callable[[float], float] | None = None,
               label: str = "request") -> T:
    """Call `operation`, retrying transient failures with exponential backoff.

    `sleep` and `jitter` are injectable so tests exercise the schedule without
    real delays. The raised error message intentionally contains only the
    exception type and the caller-supplied label, never response contents.
    """
    if not 1 <= attempts <= 8:
        raise ValueError("attempts must be between 1 and 8")
    if base_delay < 0 or max_delay < base_delay:
        raise ValueError("invalid backoff bounds")
    spread = jitter if jitter is not None else (lambda ceiling: random.uniform(0, ceiling))
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as error:  # noqa: BLE001 - re-raised below
            if not is_retryable(error, retry_on=retry_on) or attempt == attempts:
                if isinstance(error, tuple(retry_on)) and attempt == attempts and attempts > 1:
                    raise RetryBudgetExceeded(
                        f"{label} failed after {attempts} attempts: {type(error).__name__}"
                    ) from error
                raise
            last = error
            sleep(min(max_delay, spread(base_delay * (2 ** (attempt - 1)))))
    raise RetryBudgetExceeded(f"{label} exhausted retries") from last
