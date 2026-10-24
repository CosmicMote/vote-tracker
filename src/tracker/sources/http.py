"""Shared HTTP client: descriptive User-Agent, per-host throttling, retry with backoff."""

from __future__ import annotations

import logging
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"([?&])api_key=[^&#]*&?")


def redact(url: str) -> str:
    """Remove the api_key query parameter so a URL is safe to log or store."""
    out = _KEY_RE.sub(lambda m: m.group(1), str(url))
    return out.rstrip("?&")


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class RetryableHttpError(HttpError):
    def __init__(self, message: str, status: int | None, url: str, retry_after: float | None = None):
        super().__init__(message, status, url)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class HttpClient:
    """One client for all sources. Throttles each host to one request per min_interval seconds."""

    def __init__(
        self,
        user_agent: str,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_backoff_seconds: float = 120.0,
    ):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self.min_interval = min_interval_seconds
        self.max_attempts = max_attempts
        self.max_backoff = max_backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableHttpError) and exc.retry_after is not None:
            return min(exc.retry_after, self.max_backoff)
        return min(2.0 ** state.attempt_number, self.max_backoff)

    def _once(self, method: str, url: str, params: dict[str, Any] | None, **kw: Any) -> httpx.Response:
        self._throttle(urlsplit(url).netloc)
        try:
            resp = self._client.request(method, url, params=params, **kw)
        except httpx.TransportError as e:
            raise RetryableHttpError(f"{type(e).__name__}: {e}", None, redact(url)) from None
        safe = redact(str(resp.request.url))
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHttpError(
                f"HTTP {resp.status_code} from {safe}",
                resp.status_code,
                safe,
                _parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status_code >= 400:
            raise HttpError(f"HTTP {resp.status_code} from {safe}", resp.status_code, safe)
        return resp

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, **kw: Any) -> httpx.Response:
        retrying = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=self._wait,
            retry=retry_if_exception(lambda e: isinstance(e, RetryableHttpError)),
            sleep=self._sleep,
            reraise=True,
            before_sleep=lambda s: log.warning(
                "retrying after %s (attempt %d)", s.outcome.exception(), s.attempt_number
            ),
        )
        return retrying(self._once, method, url, params, **kw)

    def get(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> httpx.Response:
        return self.request("GET", url, params, **kw)
