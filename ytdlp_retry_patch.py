"""
Entry point like `python -m yt_dlp`, but sleep 10 minutes between retries only on
HTTP 429 / "Too Many Requests"; other errors use a 5 second pause (yt-dlp default
CLI cannot express error-dependent sleep).
"""
from __future__ import annotations

from typing import Any, Callable


def _error_indicates_429(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    inner = getattr(exc, "__cause__", None) or getattr(exc, "reason", None)
    if inner is not None and inner is not exc and _error_indicates_429(inner):
        return True
    text = str(exc).lower()
    return "http error 429" in text or " 429:" in text or "too many requests" in text


def _apply_retry_sleep_patch() -> None:
    from yt_dlp.utils._utils import RetryManager

    if getattr(RetryManager, "_jav_429_sleep_patch", False):
        return

    _orig: Callable[..., Any] = RetryManager.report_retry

    def _patched_report_retry(
        e: BaseException,
        count: int,
        retries: int,
        *,
        sleep_func: Any,
        info: Any,
        warn: Any,
        error: Any = None,
        suffix: Any = None,
    ):
        def sleep_for_attempt(n: int) -> float:
            if _error_indicates_429(e):
                return 600.0
            return 5.0

        return _orig(
            e,
            count,
            retries,
            sleep_func=sleep_for_attempt,
            info=info,
            warn=warn,
            error=error,
            suffix=suffix,
        )

    RetryManager.report_retry = staticmethod(_patched_report_retry)
    RetryManager._jav_429_sleep_patch = True


def main(argv: list[str] | None = None) -> None:
    _apply_retry_sleep_patch()
    import yt_dlp

    yt_dlp.main(argv)


if __name__ == "__main__":
    main()
