"""Call-level retry with exponential backoff + jitter (tenacity).
Run-level retries (minutes apart, survive restarts) live in the worker queue — see worker.py."""
from tenacity import (Retrying, retry_if_exception, stop_after_attempt,
                      wait_exponential_jitter)

CALL_ATTEMPTS = 4  # 1 try + 3 retries: ~1s, ~2s, ~4s (+ jitter)


def with_backoff(fn, *, is_transient, label: str, log=None, attempts: int = CALL_ATTEMPTS):
    def _before_sleep(rs):
        if log:
            log(f"{label}: attempt {rs.attempt_number} failed "
                f"({type(rs.outcome.exception()).__name__}: {rs.outcome.exception()}); backing off")

    for attempt in Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=20, jitter=1),
        retry=retry_if_exception(is_transient),
        before_sleep=_before_sleep,
        reraise=True,
    ):
        with attempt:
            return fn()


def any_exception(_exc: BaseException) -> bool:
    return True
