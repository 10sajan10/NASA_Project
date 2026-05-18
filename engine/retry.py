"""Retry policy for producer execution.

A `RetryPolicy` tells the runner how to handle transient failures: how
many times to try, how long to wait between attempts, and which
exceptions are worth retrying. Permanent failures (configuration errors,
contract violations) shouldn't be retried — they'll just waste time and
mask the real bug.

Default policy: 1 attempt, no retry. Backwards-compatible.

Typical knobs:
  * Network-bound drivers: ``RetryPolicy(max_attempts=3, backoff_seconds=2.0)``
  * Iterative numerics: keep max_attempts=1 (a numerical bug doesn't fix
    itself by retrying)
  * Whitelist: ``retryable_exceptions=(IOError, TimeoutError, ConnectionError)``
    to avoid retrying programming errors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class RetryPolicy:
    """How aggressively to retry a producer / tile.

    max_attempts=1 means "no retry" (one shot). max_attempts=3 means try
    up to three times before giving up. backoff_seconds is the BASE delay;
    actual sleep is ``backoff_seconds * attempt`` (linear) so retries
    grow further apart.
    """
    max_attempts: int = 1
    backoff_seconds: float = 0.0
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")

    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retryable_exceptions)


def attempt_with_retry(fn: Callable[..., Any],
                        *args: Any,
                        policy: Optional[RetryPolicy] = None,
                        **kwargs: Any
                        ) -> tuple[Any, Optional[BaseException], int]:
    """Run fn(*args, **kwargs). On retryable exception, sleep linear
    backoff and retry up to ``policy.max_attempts`` times.

    Returns ``(result, exception, attempts)``:
      * result is the function's return value when exception is None
      * exception is the LAST observed exception when retries are
        exhausted; result is None in that case
      * attempts is how many tries were made (1..max_attempts)

    A non-retryable exception fails fast (does NOT consume remaining
    attempts) so programming errors surface immediately.
    """
    pol = policy or RetryPolicy()
    last_exc: Optional[BaseException] = None
    attempts = 0
    for attempt in range(1, pol.max_attempts + 1):
        attempts = attempt
        try:
            return fn(*args, **kwargs), None, attempts
        except BaseException as e:
            last_exc = e
            if not pol.is_retryable(e):
                break
            if attempt >= pol.max_attempts:
                break
            if pol.backoff_seconds > 0:
                time.sleep(pol.backoff_seconds * attempt)
    return None, last_exc, attempts
